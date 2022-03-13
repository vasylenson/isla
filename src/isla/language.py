import copy
import html
import itertools
import logging
import operator
import pickle
import re
from abc import ABC
from functools import reduce, lru_cache
from typing import Union, List, Optional, Dict, Tuple, Callable, cast, Generator, Set, Iterable, Sequence, Protocol, \
    TypeVar, MutableSet

import antlr4
import z3
from antlr4 import InputStream, RuleContext
from antlr4.Token import CommonToken
from fuzzingbook.GrammarFuzzer import tree_to_string, GrammarFuzzer
from fuzzingbook.Parser import EarleyParser
from grammar_graph import gg
from graphviz import Digraph
from orderedset import OrderedSet
from z3 import Z3Exception

import isla.mexpr_parser.MexprParserListener as MexprParserListener
from isla.helpers import RE_NONTERMINAL, is_nonterminal, traverse, TRAVERSE_POSTORDER, assertions_activated, \
    remove_subtrees_for_prefix
from isla.helpers import replace_line_breaks, delete_unreachable, pop, powerset, grammar_to_immutable, \
    immutable_to_grammar, \
    nested_list_to_tuple
from isla.isla_language import IslaLanguageListener
from isla.isla_language.IslaLanguageLexer import IslaLanguageLexer
from isla.isla_language.IslaLanguageParser import IslaLanguageParser
from isla.mexpr_lexer.MexprLexer import MexprLexer
from isla.mexpr_parser.MexprParser import MexprParser
from isla.type_defs import ParseTree, Path, Grammar, ImmutableGrammar
from isla.z3_helpers import is_valid, z3_push_in_negations, z3_subst, get_symbols, smt_expr_to_str

SolutionState = List[Tuple['Constant', 'Formula', 'DerivationTree']]
Assignment = Tuple['Constant', 'Formula', 'DerivationTree']

language_core_logger = logging.getLogger("isla-language-core")

# The below is an important optimization: In Z3's ExprRef, __eq__ is overloaded
# to return a *formula* (an equation). This is quite costly, and automatically
# called when putting ExprRefs into hash tables. To resort to a structural
# comparison, we replace the __eq__ method by AstRef's __eq__, which does perform
# a structural comparison.
z3.ExprRef.__eq__ = z3.AstRef.__eq__


class Variable:
    NUMERIC_NTYPE = "NUM"

    def __init__(self, name: str, n_type: str):
        self.name = name
        self.n_type = n_type

    def to_smt(self):
        return z3.String(self.name)

    def is_numeric(self):
        return self.n_type == Constant.NUMERIC_NTYPE

    def __eq__(self, other):
        return type(self) is type(other) and (self.name, self.n_type) == (other.name, other.n_type)

    # Comparisons (<, <=, >, >=) implemented s.t. variables can be used, e.g., in priority lists
    def __lt__(self, other):
        return self.name < other.name

    def __le__(self, other):
        return self.name <= other.name

    def __gt__(self, other):
        assert issubclass(type(other), Variable)
        return self.name > other.name

    def __ge__(self, other):
        return self.name >= other.name

    def __hash__(self):
        return hash((type(self).__name__, self.name, self.n_type))

    def __repr__(self):
        return f'{type(self).__name__}("{self.name}", "{self.n_type}")'

    def __str__(self):
        return self.name


class Constant(Variable):
    def __init__(self, name: str, n_type: str):
        """
        A constant is a "free variable" in a formula.

        :param name: The name of the constant.
        :param n_type: The nonterminal type of the constant, e.g., "<var>".
        """
        super().__init__(name, n_type)


class BoundVariable(Variable):
    def __init__(self, name: str, n_type: str):
        """
        A variable bound by a quantifier.

        :param name: The name of the variable.
        :param n_type: The nonterminal type of the variable, e.g., "<var>".
        """
        super().__init__(name, n_type)

    def __add__(self, other: Union[str, 'BoundVariable']) -> 'BindExpression':
        assert isinstance(other, str) or isinstance(other, BoundVariable)
        return BindExpression(self, other)


class DummyVariable(BoundVariable):
    """A variable of which only its nonterminal is of interest (primarily for BindExpressions)."""

    cnt = 0

    def __init__(self, n_type: str):
        super().__init__(f"DUMMY_{DummyVariable.cnt}", n_type)
        DummyVariable.cnt += 1

    def __str__(self):
        return self.n_type


class DerivationTree:
    """Derivation trees are immutable!"""
    next_id: int = 0

    TRAVERSE_PREORDER = 0
    TRAVERSE_POSTORDER = 1

    def __init__(self, value: str,
                 children: Optional[Sequence['DerivationTree']] = None,
                 id: Optional[int] = None,
                 k_paths: Optional[Dict[int, Set[Tuple[gg.Node, ...]]]] = None,
                 hash: Optional[int] = None,
                 structural_hash: Optional[int] = None,
                 is_open: Optional[bool] = None):
        self.__value = value
        self.__children = None if children is None else tuple(children)

        if id:
            self._id = id
        else:
            self._id = DerivationTree.next_id
            DerivationTree.next_id += 1

        self.__len = 1 if not children else None
        self.__hash = hash
        self.__structural_hash = structural_hash
        self.__k_paths: Dict[int, Set[Tuple[gg.Node, ...]]] = k_paths or {}

        self.__is_open = is_open
        if children is None:
            self.__is_open = True

    def __setstate__(self, state):
        # To ensure that when resuming from a checkpoint during debugging,
        # ID uniqueness constraints are maintained.
        self.__dict__.update(state)
        if self.id >= DerivationTree.next_id:
            DerivationTree.next_id = self.id + 1

    @property
    def children(self) -> Tuple['DerivationTree']:
        return self.__children

    @property
    def value(self) -> str:
        return self.__value

    @property
    def id(self) -> int:
        return self._id

    @id.setter
    def id(self, value: int):
        raise NotImplementedError()

    def has_unique_ids(self) -> bool:
        return all(
            not any(
                subt_1 is not subt_2 and subt_1.id == subt_2.id
                for _, subt_2 in self.paths())
            for _, subt_1 in self.paths())

    def k_coverage(self, graph: gg.GrammarGraph, k: int) -> float:
        return len(self.k_paths(graph, k)) / len(graph.k_paths(k))

    def k_paths(self, graph: gg.GrammarGraph, k: int) -> Set[Tuple[gg.Node, ...]]:
        if k not in self.__k_paths:
            self.recompute_k_paths(graph, k)
            assert k in self.__k_paths

        return self.__k_paths[k]

    def recompute_k_paths(self, graph: gg.GrammarGraph, k: int) -> Set[Tuple[gg.Node, ...]]:
        self.__k_paths[k] = set(iter(graph.k_paths_in_tree(self.to_parse_tree(), k)))
        return self.__k_paths[k]

    def root_nonterminal(self) -> str:
        if isinstance(self.value, Variable):
            return self.value.n_type

        assert is_nonterminal(self.value)
        return self.value

    def num_children(self) -> int:
        return 0 if self.children is None else len(self.children)

    def is_open(self):
        if self.__is_open is None:
            self.__is_open = self.__compute_is_open()

        return self.__is_open

    def __compute_is_open(self):
        if self.children is None:
            return True

        result = False

        def action(_, node: DerivationTree) -> bool:
            nonlocal result
            if node.children is None:
                result = True
                return True

            return False

        self.traverse(lambda p, n: None, action)

        return result

    def is_complete(self):
        return not self.is_open()

    @lru_cache(maxsize=20)
    def get_subtree(self, path: Path) -> 'DerivationTree':
        """Access a subtree based on `path` (a list of children numbers)"""
        curr_node = self
        while path:
            curr_node = curr_node.children[path[0]]
            path = path[1:]

        return curr_node

    def is_valid_path(self, path: Path) -> bool:
        curr_node = self
        while path:
            if not curr_node.children or len(curr_node.children) <= path[0]:
                return False

            curr_node = curr_node.children[path[0]]
            path = path[1:]

        return True

    @lru_cache
    def paths(self) -> List[Tuple[Path, 'DerivationTree']]:
        def action(path, node):
            result.append((path, node))

        result: List[Tuple[Path, 'DerivationTree']] = []
        self.traverse(action, kind=DerivationTree.TRAVERSE_PREORDER)
        return result

    def filter(self, f: Callable[['DerivationTree'], bool],
               enforce_unique: bool = False) -> List[Tuple[Path, 'DerivationTree']]:
        result: List[Tuple[Path, 'DerivationTree']] = []

        for path, subtree in self.paths():
            if f(subtree):
                result.append((path, subtree))

                if enforce_unique and len(result) > 1:
                    raise RuntimeError(f"Found searched-for element more than once in {self}")

        return result

    def find_node(self, node_or_id: Union['DerivationTree', int]) -> Optional[Path]:
        """Finds a node by its (assumed unique) ID. Returns the path relative to this node."""
        if isinstance(node_or_id, DerivationTree):
            node_or_id = node_or_id.id

        try:
            return next(path for path, subtree in self.paths() if subtree.id == node_or_id)
        except StopIteration:
            return None

        stack: List[Tuple[Path, DerivationTree]] = [((), self)]
        while stack:
            path, tree = stack.pop()
            if tree.id == node_or_id:
                return path

            if tree.children:
                stack.extend([(path + (idx,), child) for idx, child in enumerate(tree.children)])

        return None

    def traverse(
            self,
            action: Callable[[Path, 'DerivationTree'], None],
            abort_condition: Callable[[Path, 'DerivationTree'], bool] = lambda p, n: False,
            kind: int = TRAVERSE_PREORDER,
            reverse: bool = False) -> None:
        stack_1: List[Tuple[Path, DerivationTree]] = [((), self)]
        stack_2: List[Tuple[Path, DerivationTree]] = []

        if kind == DerivationTree.TRAVERSE_PREORDER:
            reverse = not reverse

        while stack_1:
            path, node = stack_1.pop()

            if abort_condition(path, node):
                return

            if kind == DerivationTree.TRAVERSE_POSTORDER:
                stack_2.append((path, node))

            if kind == DerivationTree.TRAVERSE_PREORDER:
                action(path, node)

            if node.children:
                iterator = reversed(node.children) if reverse else iter(node.children)

                for idx, child in enumerate(iterator):
                    new_path = path + ((len(node.children) - idx - 1) if reverse else idx,)
                    stack_1.append((new_path, child))

        if kind == DerivationTree.TRAVERSE_POSTORDER:
            while stack_2:
                action(*stack_2.pop())

    def bfs(self,
            action: Callable[[Path, 'DerivationTree'], None],
            abort_condition: Callable[[Path, 'DerivationTree'], bool] = lambda p, n: False):
        queue: List[Tuple[Path, DerivationTree]] = [((), self)]  # FIFO queue
        explored: Set[Path] = {()}

        while queue:
            p, v = queue.pop(0)
            action(p, v)
            if abort_condition(p, v):
                return

            for child_idx, child in enumerate(v.children or []):
                child_path = p + (child_idx,)
                if child_path in explored:
                    continue

                explored.add(child_path)
                queue.append((child_path, child))

    def nonterminals(self) -> Set[str]:
        result: Set[str] = set()

        def add_if_nonterminal(_: Path, tree: DerivationTree):
            if is_nonterminal(tree.value):
                result.add(tree.value)

        self.traverse(action=add_if_nonterminal)

        return result

    def terminals(self) -> List[str]:
        result: List[str] = []

        def add_if_terminal(_: Path, tree: DerivationTree):
            if not is_nonterminal(tree.value):
                result.append(tree.value)

        self.traverse(action=add_if_terminal)

        return result

    def reachable_symbols(self, grammar: Grammar, is_reachable: Callable[[str, str], bool]) -> Set[str]:
        return self.nonterminals() | {
            nonterminal for nonterminal in grammar
            if any(is_reachable(leaf[1].value, nonterminal)
                   for leaf in self.leaves()
                   if is_nonterminal(leaf[1].value))
        }

    def next_path(self, path: Path, skip_children=False) -> Optional[Path]:
        """
        Returns the next path in the tree. Repeated calls result in an iterator over the paths in the tree.
        """

        def num_children(path: Path) -> int:
            _, children = self.get_subtree(path)
            if children is None:
                return 0
            return len(children)

        # Descent towards left-most child leaf
        if not skip_children and num_children(path) > 0:
            return path + (0,)

        # Find next sibling
        for i in range(1, len(path)):
            if path[-i] + 1 < num_children(path[:-i]):
                return path[:-i] + (path[-i] + 1,)

        # Proceed to next root child
        if path and path[0] + 1 < num_children(tuple()):
            return path[0] + 1,

        # path already is the last path.
        assert skip_children or list(self.paths())[-1][0] == path
        return None

    def replace_path(
            self,
            path: Path,
            replacement_tree: 'DerivationTree',
            retain_id=False) -> 'DerivationTree':
        """Returns tree where replacement_tree has been inserted at `path` instead of the original subtree"""
        stack: List[DerivationTree] = [self]
        for idx in path:
            stack.append(stack[-1].children[idx])

        if retain_id:
            replacement_tree = DerivationTree(
                replacement_tree.value,
                replacement_tree.children,
                id=stack[-1].id,
                is_open=replacement_tree.is_open(),
            )

        stack[-1] = replacement_tree

        for idx in reversed(path):
            assert len(stack) > 1
            replacement = stack.pop()
            parent = stack.pop()

            children = parent.children
            new_children = (
                    children[:idx] +
                    (replacement,) +
                    children[idx + 1:])

            if replacement.__is_open is True:
                is_open = True
            elif replacement.__is_open is False and parent.__is_open is False:
                is_open = False
            else:
                is_open = None

            stack.append(DerivationTree(
                parent.value,
                new_children,
                id=parent.id,
                is_open=is_open))

        assert len(stack) == 1
        return stack[0]

    def leaves(self) -> Generator[Tuple[Path, 'DerivationTree'], None, None]:
        return ((path, sub_tree)
                for path, sub_tree in self.paths()
                if not sub_tree.children)

    def open_leaves(self) -> Generator[Tuple[Path, 'DerivationTree'], None, None]:
        return ((path, sub_tree)
                for path, sub_tree in self.paths()
                if sub_tree.children is None)

    def depth(self) -> int:
        if not self.children:
            return 1
        return 1 + max(child.depth() for child in self.children)

    def new_ids(self) -> 'DerivationTree':
        return DerivationTree(
            self.value,
            None if self.children is None
            else [child.new_ids() for child in self.children])

    def __len__(self):
        if self.__len is None:
            self.__len = sum([len(child) for child in (self.children or [])]) + 1

        return self.__len

    def __lt__(self, other):
        return len(self) < len(other)

    def __le__(self, other):
        return len(self) <= len(other)

    def __gt__(self, other):
        return len(self) > len(other)

    def __ge__(self, other):
        return len(self) >= len(other)

    def substitute(self, subst_map: Dict[Union[Variable, 'DerivationTree'], 'DerivationTree']) -> 'DerivationTree':
        # We perform an iterative reverse post-order depth-first traversal and use a stack
        # to store intermediate results from lower levels.
        assert self.has_unique_ids()

        # Looking up IDs performs much better for big trees, since we do not necessarily
        # have to compute hashes for all nodes (made necessary by tar case study).
        # We remove "nested" replacements since removing elements in replacements is not intended.

        id_subst_map = {
            tree.id: repl for tree, repl in subst_map.items()
            if (isinstance(tree, DerivationTree) and
                all(repl.id == tree.id or repl.find_node(tree.id) is None
                    for otree, repl in subst_map.items()
                    if isinstance(otree, DerivationTree)))
        }

        result = self
        for tree_id in id_subst_map:
            path = result.find_node(tree_id)
            if path is not None:
                result = result.replace_path(path, id_subst_map[tree_id])

        return result

    def is_prefix(self, other: 'DerivationTree') -> bool:
        if len(self) > len(other):
            return False

        if self.value != other.value:
            return False

        if not self.children:
            return self.children is None or (not other.children and other.children is not None)

        if not other.children:
            return False

        assert self.children
        assert other.children

        if len(self.children) != len(other.children):
            return False

        return all(self.children[idx].is_prefix(other.children[idx])
                   for idx, _ in enumerate(self.children))

    def is_potential_prefix(self, other: 'DerivationTree') -> bool:
        """Returns `True` iff this the `other` tree can be extended such that this tree
        is a prefix of the `other` tree."""
        if self.value != other.value:
            return False

        if not self.children:
            return self.children is None or (not other.children and other.children is not None)

        if not other.children:
            return other.children is None

        assert self.children
        assert other.children

        if len(self.children) != len(other.children):
            return False

        return all(self.children[idx].is_potential_prefix(other.children[idx])
                   for idx, _ in enumerate(self.children))

    @staticmethod
    def from_parse_tree(tree: ParseTree):
        result_stack: List[DerivationTree] = []

        def action(_, tree: ParseTree) -> None:
            node, children = tree
            if not children:
                result_stack.append(DerivationTree(node, children))
                return

            children_results: List[DerivationTree] = []
            for _ in range(len(children)):
                children_results.append(result_stack.pop())

            result_stack.append(DerivationTree(node, children_results))

        traverse(tree, action, kind=TRAVERSE_POSTORDER, reverse=True)

        assert len(result_stack) == 1
        result = result_stack[0]

        return result

    def to_parse_tree(self) -> ParseTree:
        return self.value, None if self.children is None else [child.to_parse_tree() for child in self.children]

    def __iter__(self):
        # Allows tuple unpacking: node, children = tree
        yield self.value
        yield self.children

    def compute_hash_iteratively(self, structural=False):
        # We perform an iterative reverse post-order depth-first traversal and use a stack
        # to store intermediate results from lower levels.

        stack: List[int] = []

        def action(_, node: DerivationTree) -> None:
            if structural and node.__structural_hash is not None:
                for _ in range(len(node.children or [])):
                    stack.pop()
                stack.append(node.__structural_hash)
                return
            if not structural and node.__hash is not None:
                for _ in range(len(node.children or [])):
                    stack.pop()
                stack.append(node.__hash)
                return

            if node.children is None:
                node_hash = hash(node.value) if structural else hash((node.value, node.id))
            else:
                children_values = []
                for _ in range(len(node.children)):
                    children_values.append(stack.pop())
                node_hash = hash(((node.value,) if structural else (node.value, node.id)) + tuple(children_values))

            stack.append(node_hash)
            if structural:
                node.__structural_hash = node_hash
            else:
                node.__hash = node_hash

        self.traverse(action, kind=DerivationTree.TRAVERSE_POSTORDER, reverse=True)

        assert len(stack) == 1
        return stack.pop()

    def __hash__(self):
        # return self.id  # Should be unique!
        if self.__hash is not None:
            return self.__hash

        self.__hash = self.compute_hash_iteratively(structural=False)
        return self.__hash

    def structural_hash(self):
        if self.__structural_hash is not None:
            return self.__structural_hash

        self.__structural_hash = self.compute_hash_iteratively(structural=True)
        return self.__structural_hash

    def structurally_equal(self, other: 'DerivationTree'):
        if not isinstance(other, DerivationTree):
            return False

        if (self.value != other.value
                or (self.children is None and other.children is not None)
                or (other.children is None and self.children is not None)):
            return False

        if self.children is None:
            return True

        if len(self.children) != len(other.children):
            return False

        return all(self.children[idx].structurally_equal(other.children[idx])
                   for idx in range(len(self.children)))

    def __eq__(self, other):
        """
        Equality takes the randomly assigned ID into account!
        So trees with the same structure might not be equal.
        """
        stack: List[Tuple[DerivationTree, DerivationTree]] = [(self, other)]

        while stack:
            t1, t2 = stack.pop()
            if (not isinstance(t2, DerivationTree)
                    or t1.value != t2.value
                    or t1.id != t2.id
                    or (t1.children is None and t2.children is not None)
                    or (t2.children is None and t1.children is not None)
                    or len(t1.children or []) != len(t2.children or [])):
                return False

            if t1.children:
                assert t2.children
                stack.extend(list(zip(t1.children, t2.children)))

        return True

    def __repr__(self):
        return f"DerivationTree({repr(self.value)}, {repr(self.children)}, id={self.id})"

    @lru_cache(maxsize=100)
    def to_string(self, show_open_leaves: bool = False) -> str:
        result = []
        stack = [self]

        while stack:
            node = stack.pop(0)
            symbol = node.value
            children = node.children

            if not children:
                if children is not None:
                    result.append("" if is_nonterminal(symbol) else symbol)
                else:
                    result.append(symbol if show_open_leaves else "")

                continue

            stack = list(children) + stack

        return ''.join(result)

    def __str__(self) -> str:
        return self.to_string(show_open_leaves=True)

    def to_dot(self) -> str:
        dot = Digraph(comment="Derivation Tree")
        dot.attr('node', shape='plain')

        def action(_, t: DerivationTree):
            dot.node(repr(t.id), "<" + html.escape(t.value) + f' <FONT COLOR="gray">({t.id})</FONT>>')
            for child in t.children or []:
                dot.edge(repr(t.id), repr(child.id))

        self.traverse(action)

        return dot.source


class BindExpression:
    def __init__(self, *bound_elements: Union[str, BoundVariable, List[str]]):
        self.bound_elements: List[BoundVariable | List[BoundVariable]] = []
        for bound_elem in bound_elements:
            if isinstance(bound_elem, BoundVariable):
                self.bound_elements.append(bound_elem)
                continue

            if isinstance(bound_elem, list):
                if all(isinstance(list_elem, Variable) for list_elem in bound_elem):
                    self.bound_elements.append(bound_elem)
                    continue

                assert all(isinstance(list_elem, str) for list_elem in bound_elem)

                self.bound_elements.append([
                    DummyVariable(token)
                    for token in re.split(RE_NONTERMINAL, "".join(bound_elem))
                    if token
                ])
                continue

            self.bound_elements.extend([
                DummyVariable(token)
                for token in re.split(RE_NONTERMINAL, bound_elem)
                if token
            ])

        self.prefixes: Dict[str, List[Tuple[DerivationTree, Dict[BoundVariable, Path]]]] = {}
        self.__flattened_elements: Dict[str, Tuple[Tuple[BoundVariable, ...], ...]] = {}

    def __add__(self, other: Union[str, 'BoundVariable']) -> 'BindExpression':
        assert type(other) == str or type(other) == BoundVariable
        result = BindExpression(*self.bound_elements)
        result.bound_elements.append(other)
        return result

    def substitute_variables(self, subst_map: Dict[Variable, Variable]):
        return BindExpression(*[elem if isinstance(elem, list)
                                else subst_map.get(elem, elem)
                                for elem in self.bound_elements])

    def bound_variables(self) -> OrderedSet[BoundVariable]:
        # Not isinstance(var, BoundVariable) since we want to exclude dummy variables
        return OrderedSet([var for var in self.bound_elements if type(var) is BoundVariable])

    def all_bound_variables(self, grammar: Grammar) -> OrderedSet[BoundVariable]:
        # Includes dummy variables
        return OrderedSet([
            var
            for alternative in flatten_bound_elements(
                nested_list_to_tuple(self.bound_elements),
                grammar_to_immutable(grammar),
                in_nonterminal=None)
            for var in alternative
            if isinstance(var, BoundVariable)])

    def to_tree_prefix(self, in_nonterminal: str, grammar: Grammar) -> \
            List[Tuple[DerivationTree, Dict[BoundVariable, Path]]]:
        if in_nonterminal in self.prefixes:
            cached = self.prefixes[in_nonterminal]
            return [(opt[0].new_ids(), opt[1]) for opt in cached]

        result: List[Tuple[DerivationTree, Dict[BoundVariable, Path]]] = []

        for bound_elements in flatten_bound_elements(
                nested_list_to_tuple(self.bound_elements),
                grammar_to_immutable(grammar),
                in_nonterminal=in_nonterminal):
            tree = bound_elements_to_tree(bound_elements, grammar_to_immutable(grammar), in_nonterminal)

            match_result = self.match_with_backtracking(tree.paths(), list(bound_elements))
            if match_result:
                positions: Dict[BoundVariable, Path] = {}
                for bound_element in bound_elements:
                    if bound_element not in match_result:
                        assert isinstance(bound_element, DummyVariable) and not is_nonterminal(bound_element.n_type)
                        continue

                    elem_match_result = match_result[bound_element]
                    tree = tree.replace_path(
                        elem_match_result[0],
                        DerivationTree(
                            elem_match_result[1].value,
                            None if is_nonterminal(bound_element.n_type) else ()))

                    positions[bound_element] = elem_match_result[0]

                result.append((tree, positions))

        self.prefixes[in_nonterminal] = result
        return result

    def match(
            self,
            tree: DerivationTree,
            grammar: Grammar,
            paths: Optional[Dict[Path, DerivationTree]] = None) -> \
            Optional[Dict[BoundVariable, Tuple[Path, DerivationTree]]]:
        if not paths:
            paths = dict(tree.paths())

        leaves = [(path, subtree) for path, subtree in paths.items() if not subtree.children]

        for combination in reversed(flatten_bound_elements(
                nested_list_to_tuple(self.bound_elements),
                grammar_to_immutable(grammar),
                in_nonterminal=tree.value)):

            maybe_result = self.match_with_backtracking(
                cast(List[Tuple[Path, DerivationTree]], list(paths.items())),
                list(combination))
            if maybe_result is not None:
                maybe_result = dict(maybe_result)

                # NOTE: We also have to check if the match was complete; the current
                #       combination could not have been adequate (containing nonexisting
                #       input elements), such that they got matched to wrong elements, but
                #       there remain tree elements that were not matched.
                if all(any(match_path == leaf_path[:len(match_path)]
                           for match_path, _ in maybe_result.values())
                       for leaf_path, _ in leaves):
                    return maybe_result

        return None

    @staticmethod
    def match_with_backtracking(
            subtrees: List[Tuple[Path, DerivationTree]],
            bound_variables: List[BoundVariable],
            excluded_matches: Tuple[Tuple[Path, BoundVariable], ...] = ()
    ) -> Optional[Dict[BoundVariable, Tuple[Path, DerivationTree]]]:
        if not bound_variables:
            return None

        result: Dict[BoundVariable, Tuple[Path, DerivationTree]] = {}

        if BindExpression.match_without_backtracking(
                list(subtrees), list(bound_variables), result, excluded_matches):
            return result

        # In the case of nested structures with recursive nonterminals, we might have matched
        # to greedily in the first place; e.g., an XML <attribute> might contain other XML
        # <attribute>s that we should rather match. Thus, we might have to backtrack.
        if not excluded_matches:
            for excluded_matches in itertools.chain.from_iterable(
                    itertools.combinations({p: v for v, (p, t) in result.items()}.items(), k)
                    for k in range(1, len(result) + 1)):
                curr_elem: BoundVariable
                backtrack_result = BindExpression.match_with_backtracking(
                    subtrees,
                    bound_variables,
                    excluded_matches
                )

                if backtrack_result is not None:
                    return backtrack_result

        return None

    @staticmethod
    def match_without_backtracking(
            subtrees: List[Tuple[Path, DerivationTree]],
            bound_variables: List[BoundVariable],
            result: Dict[BoundVariable, Tuple[Path, DerivationTree]],
            excluded_matches: Tuple[Tuple[Path, BoundVariable], ...] = ()) -> bool:
        orig_bound_variables = list(bound_variables)
        split_variables: Dict[BoundVariable, List[BoundVariable]] = {}

        curr_elem = bound_variables.pop(0)

        while subtrees and curr_elem:
            path, subtree = pop(subtrees)

            subtree_str = str(subtree)
            if (isinstance(curr_elem, DummyVariable) and
                    not is_nonterminal(curr_elem.n_type) and
                    len(subtree_str) < len(curr_elem.n_type) and
                    curr_elem.n_type.startswith(subtree_str)):
                # Divide terminal dummy variables if they only can be unified with *different*
                # subtrees; e.g., we have a dummy variable with n_type "xmlns:" for an XML grammar,
                # and have to unify an ID prefix with "xmlns", leaving the ":" for the next subtree.
                # NOTE: If we split here, this cannot be recovered, because we only need to split
                # in the first place since the grammar does not allow for a graceful division of
                # the considered terminal symbols. In the XML example, ":" is in a sibling tree; there
                # is no common super tree to match *only* "xmlns:".
                remainder_var = DummyVariable(curr_elem.n_type[len(subtree_str):])
                next_var = DummyVariable(subtree_str)

                split_variables[curr_elem] = [next_var, remainder_var]
                bound_variables.insert(0, remainder_var)

                curr_elem = next_var

            if ((path, curr_elem) not in excluded_matches and
                    (subtree.value == curr_elem.n_type or
                     isinstance(curr_elem, DummyVariable) and
                     not is_nonterminal(curr_elem.n_type) and
                     subtree_str == curr_elem.n_type)):
                result[curr_elem] = (path, subtree)
                curr_elem = pop(bound_variables, default=None)

                # TODO: Slow!
                # subtrees = [(p, s) for p, s in subtrees
                #             if not p[:len(path)] == path]
                subtrees = remove_subtrees_for_prefix(subtrees, path)

        # We did only split dummy variables
        assert subtrees or curr_elem or \
               {v for v in orig_bound_variables if type(v) is BoundVariable} == \
               {v for v in result.keys() if type(v) is BoundVariable}

        return not subtrees and not curr_elem

    def __repr__(self):
        return f'BindExpression({", ".join(map(repr, self.bound_elements))})'

    def __str__(self):
        return ''.join(map(
            lambda e: f'{str(e)}'
            if isinstance(e, str)
            else ("[" + "".join(map(str, e)) + "]") if isinstance(e, list)
            else (f"{{{e.n_type} {str(e)}}}" if not isinstance(e, DummyVariable)
                  else str(e)), self.bound_elements))

    @staticmethod
    def __dummy_vars_to_str(
            elem: BoundVariable | List[BoundVariable]) -> str | BoundVariable | List[str | BoundVariable]:
        if isinstance(elem, DummyVariable):
            return elem.n_type

        if isinstance(elem, list):
            return list(map(BindExpression.__dummy_vars_to_str, elem))

        return elem

    def __hash__(self):
        return hash(tuple([
            tuple(e) if isinstance(e, list) else e
            for e in map(BindExpression.__dummy_vars_to_str, self.bound_elements)]))

    def __eq__(self, other):
        return (isinstance(other, BindExpression) and
                list(map(BindExpression.__dummy_vars_to_str, self.bound_elements)) ==
                list(map(BindExpression.__dummy_vars_to_str, other.bound_elements)))


class FormulaVisitor:
    def do_continue(self, formula: 'Formula') -> bool:
        """If this returns False, this formula should not call the visit methods for its children."""
        return True

    def visit_predicate_formula(self, formula: 'StructuralPredicateFormula'):
        pass

    def visit_semantic_predicate_formula(self, formula: 'SemanticPredicateFormula'):
        pass

    def visit_negated_formula(self, formula: 'NegatedFormula'):
        pass

    def visit_conjunctive_formula(self, formula: 'ConjunctiveFormula'):
        pass

    def visit_disjunctive_formula(self, formula: 'DisjunctiveFormula'):
        pass

    def visit_smt_formula(self, formula: 'SMTFormula'):
        pass

    def visit_exists_formula(self, formula: 'ExistsFormula'):
        pass

    def visit_forall_formula(self, formula: 'ForallFormula'):
        pass

    def visit_exists_int_formula(self, formula: 'ExistsIntFormula'):
        pass

    def visit_forall_int_formula(self, formula: 'ForallIntFormula'):
        pass


class Formula(ABC):
    # def __getstate__(self):
    #     return {f: pickle.dumps(v) for f, v in self.__dict__.items()} | {"cls": type(self).__name__}
    #
    # def __setstate__(self, state):
    #     pass

    def bound_variables(self) -> OrderedSet[BoundVariable]:
        """Non-recursive: Only non-empty for quantified formulas"""
        raise NotImplementedError()

    def free_variables(self) -> OrderedSet[Variable]:
        """Recursive."""
        raise NotImplementedError()

    def tree_arguments(self) -> OrderedSet[DerivationTree]:
        """Trees that were substituted for variables."""
        raise NotImplementedError()

    def substitute_variables(self, subst_map: Dict[Variable, Variable]) -> 'Formula':
        raise NotImplementedError()

    def substitute_expressions(self, subst_map: Dict[Union[Variable, DerivationTree], DerivationTree]) -> 'Formula':
        raise NotImplementedError()

    def accept(self, visitor: FormulaVisitor):
        raise NotImplementedError()

    def __len__(self):
        raise NotImplementedError()

    def __hash__(self):
        raise NotImplementedError()

    def __eq__(self, other: 'Formula'):
        raise NotImplementedError()

    def __and__(self, other: 'Formula'):
        if self == other:
            return self

        if isinstance(self, SMTFormula) and self.is_false:
            return self

        if isinstance(other, SMTFormula) and other.is_false:
            return other

        if isinstance(self, SMTFormula) and self.is_true:
            return other

        if isinstance(other, SMTFormula) and other.is_true:
            return self

        if isinstance(self, NegatedFormula) and self.args[0] == other:
            return SMTFormula(z3.BoolVal(False))

        if isinstance(other, NegatedFormula) and other.args[0] == self:
            return SMTFormula(z3.BoolVal(False))

        return ConjunctiveFormula(self, other)

    def __or__(self, other: 'Formula'):
        if self == other:
            return self

        if isinstance(self, SMTFormula) and z3.is_true(self.formula):
            return self

        if isinstance(other, SMTFormula) and z3.is_true(other.formula):
            return other

        if isinstance(self, SMTFormula) and z3.is_false(self.formula):
            return other

        if isinstance(other, SMTFormula) and z3.is_false(other.formula):
            return self

        if isinstance(self, NegatedFormula) and self.args[0] == other:
            return SMTFormula(z3.BoolVal(True))

        if isinstance(other, NegatedFormula) and other.args[0] == self:
            return SMTFormula(z3.BoolVal(True))

        return DisjunctiveFormula(self, other)

    def __neg__(self):
        if isinstance(self, SMTFormula):
            # Overloaded in SMTFormula
            assert False
        elif isinstance(self, NegatedFormula):
            return self.args[0]
        elif isinstance(self, ConjunctiveFormula):
            return reduce(lambda a, b: a | b, [-arg for arg in self.args])
        elif isinstance(self, DisjunctiveFormula):
            return reduce(lambda a, b: a & b, [-arg for arg in self.args])
        elif isinstance(self, ForallFormula):
            return ExistsFormula(
                self.bound_variable, self.in_variable, -self.inner_formula, self.bind_expression)
        elif isinstance(self, ExistsFormula):
            return ForallFormula(
                self.bound_variable, self.in_variable, -self.inner_formula, self.bind_expression)
        elif isinstance(self, ForallIntFormula):
            return ExistsIntFormula(self.bound_variable, -self.inner_formula)
        elif isinstance(self, ExistsIntFormula):
            return ForallIntFormula(self.bound_variable, -self.inner_formula)

        return NegatedFormula(self)


def substitute(
        formula: Formula,
        subst_map: Dict[Variable | DerivationTree, str | int | Variable | DerivationTree]) -> Formula:
    assert not any(isinstance(subst, DerivationTree) and isinstance(value, Variable)
                   for subst, value in subst_map.items())

    subst_map = {
        to_subst: (DerivationTree(str(value), [])
                   if isinstance(value, str) or isinstance(value, int)
                   else value)
        for to_subst, value in subst_map.items()
    }

    result = formula.substitute_variables({
        to_subst: value
        for to_subst, value in subst_map.items()
        if isinstance(to_subst, Variable) and isinstance(value, Variable)
    })

    result = result.substitute_expressions({
        to_subst: value
        for to_subst, value in subst_map.items()
        if isinstance(value, DerivationTree)
    })

    return result


class StructuralPredicate:
    def __init__(self, name: str, arity: int, eval_fun: Callable[[DerivationTree, Union[Path, str], ...], bool]):
        self.name = name
        self.arity = arity
        self.eval_fun = eval_fun

    def evaluate(self, context_tree: DerivationTree, *instantiations: Union[Path, str]):
        return self.eval_fun(context_tree, *instantiations)

    def __eq__(self, other):
        return type(other) is StructuralPredicate and (self.name, self.arity) == (other.name, other.arity)

    def __hash__(self):
        return hash((self.name, self.arity))

    def __repr__(self):
        return f"Predicate({self.name}, {self.arity})"

    def __str__(self):
        return self.name


class StructuralPredicateFormula(Formula):
    def __init__(self, predicate: StructuralPredicate, *args: Variable | str | DerivationTree):
        assert len(args) == predicate.arity
        self.predicate = predicate
        self.args: List[Variable | str | DerivationTree] = list(args)

    def evaluate(self, context_tree: DerivationTree) -> bool:
        args_with_paths: List[Union[str, Tuple[Path, DerivationTree]]] = \
            [arg if isinstance(arg, str) else
             (context_tree.find_node(arg), arg)
             for arg in self.args]

        if any(arg[0] is None for arg in args_with_paths if isinstance(arg, tuple)):
            raise RuntimeError(
                "Could not find paths for all predicate arguments in context tree:\n" +
                str([str(tree) for path, tree in args_with_paths if path is None]) +
                f"\nContext tree:\n{context_tree}")

        return self.predicate.eval_fun(
            context_tree, *[arg if isinstance(arg, str) else arg[0] for arg in args_with_paths])

    def substitute_variables(self, subst_map: Dict[Variable, Variable]) -> 'StructuralPredicateFormula':
        return StructuralPredicateFormula(
            self.predicate,
            *[arg if arg not in subst_map else subst_map[arg] for arg in self.args])

    def substitute_expressions(
            self, subst_map: Dict[Union[Variable, DerivationTree], DerivationTree]) -> 'StructuralPredicateFormula':
        new_args = []
        for arg in self.args:
            if isinstance(arg, Variable):
                if arg in subst_map:
                    new_args.append(subst_map[arg])
                else:
                    new_args.append(arg)
                continue

            if isinstance(arg, str):
                new_args.append(arg)
                continue

            assert isinstance(arg, DerivationTree)
            tree: DerivationTree = arg
            if tree in subst_map:
                new_args.append(subst_map[tree])
                continue

            new_args.append(tree.substitute({k: v for k, v in subst_map.items()}))

        return StructuralPredicateFormula(self.predicate, *new_args)

    def bound_variables(self) -> OrderedSet[BoundVariable]:
        return OrderedSet([])

    def free_variables(self) -> OrderedSet[Variable]:
        return OrderedSet([arg for arg in self.args if isinstance(arg, Variable)])

    def tree_arguments(self) -> OrderedSet[DerivationTree]:
        return OrderedSet([arg for arg in self.args if isinstance(arg, DerivationTree)])

    def accept(self, visitor: FormulaVisitor):
        visitor.visit_predicate_formula(self)

    def __len__(self):
        return 1

    def __hash__(self):
        return hash((type(self).__name__, self.predicate, tuple(self.args)))

    def __eq__(self, other):
        return type(self) is type(other) and (self.predicate, self.args) == (other.predicate, other.args)

    def __str__(self):
        arg_strings = [
            f'"{arg}"' if isinstance(arg, str) else str(arg)
            for arg in self.args
        ]

        return f"{self.predicate}({', '.join(arg_strings)})"

    def __repr__(self):
        return f'PredicateFormula({repr(self.predicate), ", ".join(map(repr, self.args))})'


class SemPredEvalResult:
    def __init__(self, result: Optional[bool | Dict[Variable | DerivationTree, DerivationTree]]):
        self.result = result

    def is_boolean(self) -> bool:
        return self.true() or self.false()

    def true(self) -> bool:
        return self.result is True

    def false(self) -> bool:
        return self.result is False

    def ready(self) -> bool:
        return self.result is not None

    def __eq__(self, other):
        return isinstance(other, SemPredEvalResult) and self.result == other.result

    def __str__(self):
        if self.ready():
            if self.true() or self.false():
                return str(self.result)
            else:
                return "{" + ", ".join([str(key) + ": " + str(value) for key, value in self.result.items()]) + "}"
        else:
            return "UNKNOWN"


SemPredArg = Union[DerivationTree, Variable, str, int]


def binds_nothing(tree: DerivationTree, args: Tuple[SemPredArg, ...]) -> bool:
    return False


def binds_argument_trees(tree: DerivationTree, args: Tuple[SemPredArg, ...]) -> bool:
    return any(
        tree_arg.find_node(tree) is not None
        for tree_arg in args
        if isinstance(tree_arg, DerivationTree))


class SemanticPredicateEvalFun(Protocol):
    def __call__(
            self,
            graph: Optional[gg.GrammarGraph],
            *args: DerivationTree | Constant | str | int,
            negate=False) -> SemPredEvalResult: ...


class SemanticPredicate:
    def __init__(
            self, name: str, arity: int,
            eval_fun: SemanticPredicateEvalFun,
            binds_tree: Optional[Callable[[DerivationTree, Tuple[SemPredArg, ...]], bool] | bool] = None):
        """
        :param name:
        :param arity:
        :param eval_fun:
        :param binds_tree: Given a derivation tree and the arguments for the predicate, this function tests whether
        the tree is bound by the predicate formula. The effect of this is that bound trees cannot be freely expanded,
        similarly to nonterminals bound by a universal quantifier. A semantic predicate may also not bind any of its
        arguments; in that case, we can freely instantiate the arguments and then ask the predicate for a "fix" if
        the instantiation is non-conformant. Most semantic predicates do not bind their arguments. Pass nothing or
        True for this parameter for predicates binding all trees in all their arguments. Pass False for predicates
        binding no trees at all. Pass a custom function for anything special.
        """
        self.name = name
        self.arity = arity
        self.eval_fun = eval_fun

        if binds_tree is not None and binds_tree is not True:
            if binds_tree is False:
                self.binds_tree = binds_nothing
            else:
                self.binds_tree = binds_tree
        else:
            self.binds_tree = binds_argument_trees

    def evaluate(self, graph: gg.GrammarGraph, *instantiations: SemPredArg, negate: bool = False):
        if negate:
            return self.eval_fun(graph, *instantiations, negate=True)
        else:
            return self.eval_fun(graph, *instantiations)

    def __eq__(self, other):
        return isinstance(other, SemanticPredicate) and (self.name, self.arity) == (other.name, other.arity)

    def __hash__(self):
        return hash((self.name, self.arity))

    def __repr__(self):
        return f"SemanticPredicate({self.name}, {self.arity})"

    def __str__(self):
        return self.name


class SemanticPredicateFormula(Formula):
    def __init__(self, predicate: SemanticPredicate, *args: SemPredArg, order: int = 0):
        assert len(args) == predicate.arity
        self.predicate = predicate
        self.args: Tuple[SemPredArg, ...] = args
        self.order = order

    def evaluate(self, graph: gg.GrammarGraph, negate: bool = False) -> SemPredEvalResult:
        return self.predicate.evaluate(graph, *self.args, negate=negate)

    def binds_tree(self, tree: DerivationTree) -> bool:
        return self.predicate.binds_tree(tree, self.args)

    def substitute_variables(self, subst_map: Dict[Variable, Variable]) -> 'SemanticPredicateFormula':
        return SemanticPredicateFormula(self.predicate,
                                        *[arg if arg not in subst_map
                                          else subst_map[arg] for arg in self.args], order=self.order)

    def substitute_expressions(
            self, subst_map: Dict[Union[Variable, DerivationTree], DerivationTree]) -> 'SemanticPredicateFormula':
        tree_id_subst_map = {
            tree.id: repl
            for tree, repl in subst_map.items()
            if isinstance(tree, DerivationTree)
        }

        new_args = []
        for arg in self.args:
            if isinstance(arg, str) or isinstance(arg, int):
                new_args.append(arg)
                continue

            if isinstance(arg, Variable):
                if arg in subst_map:
                    new_args.append(subst_map[arg])
                else:
                    new_args.append(arg)
                continue

            tree: DerivationTree = arg
            if tree.id in tree_id_subst_map:
                new_args.append(tree_id_subst_map[tree.id])
                continue

            new_args.append(tree.substitute({k: v for k, v in subst_map.items()}))

        return SemanticPredicateFormula(self.predicate, *new_args, order=self.order)

    def bound_variables(self) -> OrderedSet[BoundVariable]:
        return OrderedSet([])

    def free_variables(self) -> OrderedSet[Variable]:
        return OrderedSet([arg for arg in self.args if isinstance(arg, Variable)])

    def tree_arguments(self) -> OrderedSet[DerivationTree]:
        return OrderedSet([arg for arg in self.args if isinstance(arg, DerivationTree)])

    def accept(self, visitor: FormulaVisitor):
        visitor.visit_semantic_predicate_formula(self)

    def __len__(self):
        return 1

    def __hash__(self):
        return hash((type(self).__name__, self.predicate, self.args))

    def __eq__(self, other):
        return (isinstance(other, SemanticPredicateFormula)
                and self.predicate == other.predicate
                and self.args == other.args)

    def __str__(self):
        arg_strings = [
            f'"{arg}"' if isinstance(arg, str) else str(arg)
            for arg in self.args
        ]

        return f"{self.predicate}({', '.join(arg_strings)})"

    def __repr__(self):
        return f'SemanticPredicateFormula({repr(self.predicate), ", ".join(map(repr, self.args))})'


class PropositionalCombinator(Formula, ABC):
    def __init__(self, *args: Formula):
        self.args = args

    def bound_variables(self) -> OrderedSet[BoundVariable]:
        return reduce(operator.or_, [arg.bound_variables() for arg in self.args])

    def free_variables(self) -> OrderedSet[Variable]:
        result: OrderedSet[Variable] = OrderedSet([])
        for arg in self.args:
            result |= arg.free_variables()
        return result

    def tree_arguments(self) -> OrderedSet[DerivationTree]:
        result: OrderedSet[DerivationTree] = OrderedSet([])
        for arg in self.args:
            result |= arg.tree_arguments()
        return result

    def __len__(self):
        return 1 + len(self.args)

    def __repr__(self):
        return f"{type(self).__name__}({', '.join(map(repr, self.args))})"

    def __hash__(self):
        return hash((type(self).__name__, self.args))

    def __eq__(self, other):
        return type(self) == type(other) and self.args == other.args


class NegatedFormula(PropositionalCombinator):
    def __init__(self, arg: Formula):
        super().__init__(arg)

    def accept(self, visitor: FormulaVisitor):
        visitor.visit_negated_formula(self)
        if visitor.do_continue(self):
            for formula in self.args:
                formula.accept(visitor)

    def substitute_variables(self, subst_map: Dict[Variable, Variable]) -> 'NegatedFormula':
        return NegatedFormula(*[arg.substitute_variables(subst_map) for arg in self.args])

    def substitute_expressions(
            self, subst_map: Dict[Union[Variable, DerivationTree], DerivationTree]) -> 'NegatedFormula':
        return NegatedFormula(*[arg.substitute_expressions(subst_map) for arg in self.args])

    def __hash__(self):
        return hash((type(self).__name__, self.args))

    def __str__(self):
        return f"¬({self.args[0]})"


class ConjunctiveFormula(PropositionalCombinator):
    def __init__(self, *args: Formula):
        if len(args) < 2:
            raise RuntimeError(f"Conjunction needs at least two arguments, {len(args)} given.")
        super().__init__(*args)

    def substitute_variables(self, subst_map: Dict[Variable, Variable]):
        return reduce(lambda a, b: a & b, [arg.substitute_variables(subst_map) for arg in self.args])

    def substitute_expressions(self, subst_map: Dict[Union[Variable, DerivationTree], DerivationTree]) -> Formula:
        return reduce(lambda a, b: a & b, [arg.substitute_expressions(subst_map) for arg in self.args])

    def accept(self, visitor: FormulaVisitor):
        visitor.visit_conjunctive_formula(self)
        if visitor.do_continue(self):
            for formula in self.args:
                formula.accept(visitor)

    def __hash__(self):
        return hash((type(self).__name__, self.args))

    def __eq__(self, other):
        return split_conjunction(self) == split_conjunction(other)

    def __str__(self):
        return f"({' ∧ '.join(map(str, self.args))})"


class DisjunctiveFormula(PropositionalCombinator):
    def __init__(self, *args: Formula):
        if len(args) < 2:
            raise RuntimeError(f"Disjunction needs at least two arguments, {len(args)} given.")
        super().__init__(*args)

    def substitute_variables(self, subst_map: Dict[Variable, Variable]):
        return reduce(lambda a, b: a | b, [arg.substitute_variables(subst_map) for arg in self.args])

    def substitute_expressions(self, subst_map: Dict[Union[Variable, DerivationTree], DerivationTree]) -> Formula:
        return reduce(lambda a, b: a | b, [arg.substitute_expressions(subst_map) for arg in self.args])

    def accept(self, visitor: FormulaVisitor):
        visitor.visit_disjunctive_formula(self)
        if visitor.do_continue(self):
            for formula in self.args:
                formula.accept(visitor)

    def __hash__(self):
        return hash((type(self).__name__, self.args))

    def __eq__(self, other):
        return split_disjunction(self) == split_disjunction(other)

    def __str__(self):
        return f"({' ∨ '.join(map(str, self.args))})"


class SMTFormula(Formula):
    def __init__(self, formula: z3.BoolRef, *free_variables: Variable,
                 instantiated_variables: Optional[OrderedSet[Variable]] = None,
                 substitutions: Optional[Dict[Variable, DerivationTree]] = None,
                 auto_eval: bool = True):
        """
        Encapsulates an SMT formula.
        :param formula: The SMT formula.
        :param free_variables: Free variables in this formula.
        """
        self.formula = formula
        self.is_false = z3.is_false(formula)
        self.is_true = z3.is_true(formula)

        self.free_variables_ = OrderedSet(free_variables)
        self.instantiated_variables = instantiated_variables or OrderedSet([])
        self.substitutions: Dict[Variable, DerivationTree] = substitutions or {}

        if assertions_activated():
            actual_symbols = get_symbols(formula)
            assert len(self.free_variables_) + len(self.instantiated_variables) == len(actual_symbols), \
                f"Supplied number of {len(free_variables)} symbols does not match " + \
                f"actual number of symbols {len(actual_symbols)} in formula '{formula}'"

        # When substituting expressions, the formula is automatically evaluated if this flag
        # is set to True and all substituted expressions are closed trees, i.e., the formula
        # is ground. Deactivate only for special purposes, e.g., vacuity checking.
        self.auto_eval = auto_eval
        # self.auto_eval = False

    def __getstate__(self) -> Dict[str, bytes]:
        result: Dict[str, bytes] = {f: pickle.dumps(v) for f, v in self.__dict__.items() if f != "formula"}
        # result["formula"] = self.formula.sexpr().encode("utf-8")
        result["formula"] = smt_expr_to_str(self.formula).encode("utf-8")
        return result

    def __setstate__(self, state: Dict[str, bytes]) -> None:
        inst = {f: pickle.loads(v) for f, v in state.items() if f != "formula"}
        free_variables: OrderedSet[Variable] = inst["free_variables_"]
        instantiated_variables: OrderedSet[Variable] = inst["instantiated_variables"]

        z3_constr = z3.parse_smt2_string(
            f"(assert {state['formula'].decode('utf-8')})",
            decls={var.name: z3.String(var.name) for var in free_variables | instantiated_variables})[0]

        self.__dict__ = inst
        self.formula = z3_constr

    def substitute_variables(self, subst_map: Dict[Variable, Variable]) -> 'SMTFormula':
        new_smt_formula = z3_subst(self.formula, {v1.to_smt(): v2.to_smt() for v1, v2 in subst_map.items()})

        new_free_variables = [variable if variable not in subst_map
                              else subst_map[variable]
                              for variable in self.free_variables_]

        assert all(inst_var not in subst_map for inst_var in self.instantiated_variables)
        assert all(inst_var not in subst_map for inst_var in self.substitutions.keys())

        return SMTFormula(cast(z3.BoolRef, new_smt_formula),
                          *new_free_variables,
                          instantiated_variables=self.instantiated_variables,
                          substitutions=self.substitutions,
                          auto_eval=self.auto_eval)

    def substitute_expressions(
            self, subst_map: Dict[Union[Variable, DerivationTree], DerivationTree]) -> 'SMTFormula':
        tree_subst_map = {k: v for k, v in subst_map.items()
                          if isinstance(k, DerivationTree)
                          and (k in self.substitutions.values()
                               or any(t.find_node(k) is not None for t in self.substitutions.values()))}
        var_subst_map: Dict[Variable: DerivationTree] = {
            k: v for k, v in subst_map.items() if k in self.free_variables_}

        updated_substitutions: Dict[Variable, DerivationTree] = {
            var: tree.substitute(tree_subst_map)
            for var, tree in self.substitutions.items()
        }

        new_substitutions: Dict[Variable, DerivationTree] = updated_substitutions | var_subst_map

        complete_substitutions = {k: v for k, v in new_substitutions.items() if v.is_complete()}
        new_substitutions = {k: v for k, v in new_substitutions.items() if not v.is_complete()}

        new_instantiated_variables = OrderedSet([
            var for var in self.instantiated_variables | OrderedSet(new_substitutions.keys())
            if var not in complete_substitutions
        ])

        new_smt_formula: z3.BoolRef = cast(z3.BoolRef, z3_subst(self.formula, {
            variable.to_smt(): z3.StringVal(str(tree))
            for variable, tree in complete_substitutions.items()
        }))

        new_free_variables: OrderedSet[Variable] = OrderedSet([
            variable for variable in self.free_variables_
            if variable not in var_subst_map])

        if self.auto_eval and len(new_free_variables) + len(new_instantiated_variables) == 0:
            # Formula is ground, we can evaluate it!
            return SMTFormula(z3.BoolVal(is_valid(new_smt_formula).to_bool()))

        return SMTFormula(cast(z3.BoolRef, new_smt_formula), *new_free_variables,
                          instantiated_variables=new_instantiated_variables,
                          substitutions=new_substitutions,
                          auto_eval=self.auto_eval)

    def tree_arguments(self) -> OrderedSet[DerivationTree]:
        return OrderedSet(self.substitutions.values())

    def bound_variables(self) -> OrderedSet[BoundVariable]:
        return OrderedSet([])

    def free_variables(self) -> OrderedSet[Variable]:
        return self.free_variables_

    def accept(self, visitor: FormulaVisitor):
        visitor.visit_smt_formula(self)

    # NOTE: Combining SMT formulas with and/or is not that easy due to tree substitutions, see
    #       function "eliminate_semantic_formula" in solver.py. Problems: Name collisions, plus
    #       impedes clustering which improves solver efficiency. The conjunction and disjunction
    #       functions contain assertions preventing name collisions.
    def disjunction(self, other: 'SMTFormula') -> 'SMTFormula':
        assert (self.free_variables().isdisjoint(other.free_variables()) or
                not any((var in self.substitutions and var not in other.substitutions) or
                        (var in other.substitutions and var not in self.substitutions) or
                        (var in self.substitutions and var in other.substitutions and
                         self.substitutions[var].id != other.substitutions[var].id)
                        for var in self.free_variables().intersection(other.free_variables())))
        return SMTFormula(
            z3.Or(self.formula, other.formula),
            *(self.free_variables() | other.free_variables()),
            instantiated_variables=self.instantiated_variables | other.instantiated_variables,
            substitutions=self.substitutions | other.substitutions,
            auto_eval=self.auto_eval and other.auto_eval
        )

    def conjunction(self, other: 'SMTFormula') -> 'SMTFormula':
        assert (self.free_variables().isdisjoint(other.free_variables()) or
                not any((var in self.substitutions and var not in other.substitutions) or
                        (var in other.substitutions and var not in self.substitutions) or
                        (var in self.substitutions and var in other.substitutions and
                         self.substitutions[var].id != other.substitutions[var].id)
                        for var in self.free_variables().intersection(other.free_variables())))

        return SMTFormula(
            z3.And(self.formula, other.formula),
            *(self.free_variables() | other.free_variables()),
            instantiated_variables=self.instantiated_variables | other.instantiated_variables,
            substitutions=self.substitutions | other.substitutions,
            auto_eval=self.auto_eval and other.auto_eval
        )

    def __len__(self):
        return 1

    def __neg__(self) -> 'SMTFormula':
        return SMTFormula(
            z3_push_in_negations(self.formula, negate=True),
            *self.free_variables(),
            instantiated_variables=self.instantiated_variables,
            substitutions=self.substitutions,
            auto_eval=self.auto_eval
        )

    def __repr__(self):
        return f"SMTFormula({repr(self.formula)}, {', '.join(map(repr, self.free_variables_))}, " \
               f"instantiated_variables={repr(self.instantiated_variables)}, " \
               f"substitutions={repr(self.substitutions)})"

    def __str__(self):
        if not self.substitutions:
            return str(self.formula)
        else:
            subst_string = str({str(var): str(tree) for var, tree in self.substitutions.items()})
            return f"({self.formula}, {subst_string})"

    def __eq__(self, other):
        return (isinstance(other, SMTFormula)
                and self.formula == other.formula
                and self.substitutions == other.substitutions)

    def __hash__(self):
        return hash((type(self).__name__, self.formula, tuple(self.substitutions.items())))


class NumericQuantifiedFormula(Formula, ABC):
    def __init__(self, bound_variable: BoundVariable, inner_formula: Formula):
        self.bound_variable = bound_variable
        self.inner_formula = inner_formula

    def bound_variables(self) -> OrderedSet[BoundVariable]:
        """Non-recursive: Only non-empty for quantified formulas"""
        return OrderedSet([self.bound_variable])

    def free_variables(self) -> OrderedSet[Variable]:
        """Recursive."""
        return self.inner_formula.free_variables().difference(self.bound_variables())

    def tree_arguments(self) -> OrderedSet[DerivationTree]:
        return self.inner_formula.tree_arguments()

    def __len__(self):
        return 1 + len(self.inner_formula)


class ExistsIntFormula(NumericQuantifiedFormula):
    def __init__(self, bound_variable: BoundVariable, inner_formula: Formula):
        super().__init__(bound_variable, inner_formula)

    def substitute_variables(self, subst_map: Dict[Variable, Variable]) -> 'Formula':
        return ExistsIntFormula(
            subst_map.get(self.bound_variable, self.bound_variable),
            self.inner_formula.substitute_variables(subst_map))

    def substitute_expressions(self, subst_map: Dict[Union[Variable, DerivationTree], DerivationTree]) -> 'Formula':
        assert self.bound_variable not in subst_map
        return ExistsIntFormula(
            self.bound_variable,
            self.inner_formula.substitute_expressions(subst_map))

    def accept(self, visitor: FormulaVisitor):
        visitor.visit_exists_int_formula(self)
        if visitor.do_continue(self):
            self.inner_formula.accept(visitor)

    def __hash__(self):
        return hash((type(self).__name__, self.bound_variable, self.inner_formula))

    def __eq__(self, other):
        return (isinstance(other, ExistsIntFormula)
                and self.bound_variable == other.bound_variable
                and self.inner_formula == other.inner_formula)

    def __str__(self):
        return f"∃ int {self.bound_variable.name}: {str(self.inner_formula)}"

    def __repr__(self):
        return f"ExistsIntFormula({repr(self.bound_variable)}, {repr(self.inner_formula)})"


class ForallIntFormula(NumericQuantifiedFormula):
    def __init__(self, bound_variable: BoundVariable, inner_formula: Formula):
        super().__init__(bound_variable, inner_formula)

    def substitute_variables(self, subst_map: Dict[Variable, Variable]) -> 'Formula':
        return ForallIntFormula(
            subst_map.get(self.bound_variable, self.bound_variable),
            self.inner_formula.substitute_variables(subst_map))

    def substitute_expressions(self, subst_map: Dict[Union[Variable, DerivationTree], DerivationTree]) -> 'Formula':
        assert self.bound_variable not in subst_map
        return ForallIntFormula(
            self.bound_variable,
            self.inner_formula.substitute_expressions(subst_map))

    def accept(self, visitor: FormulaVisitor):
        visitor.visit_forall_int_formula(self)
        if visitor.do_continue(self):
            self.inner_formula.accept(visitor)

    def __hash__(self):
        return hash((type(self).__name__, self.bound_variable, self.inner_formula))

    def __eq__(self, other):
        return (isinstance(other, ForallIntFormula)
                and self.bound_variable == other.bound_variable
                and self.inner_formula == other.inner_formula)

    def __str__(self):
        return f"∀ int {self.bound_variable.name}: {str(self.inner_formula)}"

    def __repr__(self):
        return f"ForallIntFormula({repr(self.bound_variable)}, {repr(self.inner_formula)})"


class QuantifiedFormula(Formula, ABC):
    def __init__(self,
                 bound_variable: Union[BoundVariable, str],
                 in_variable: Union[Variable, DerivationTree],
                 inner_formula: Formula,
                 bind_expression: Optional[Union[BindExpression, BoundVariable]] = None):
        assert inner_formula is not None
        assert isinstance(bound_variable, BoundVariable) or bind_expression is not None

        if isinstance(bound_variable, str):
            assert is_nonterminal(bound_variable)
            self.bound_variable = DummyVariable(bound_variable)
        else:
            self.bound_variable = bound_variable

        self.in_variable = in_variable
        self.inner_formula = inner_formula

        if isinstance(bind_expression, BoundVariable):
            self.bind_expression = BindExpression(bind_expression)
        else:
            self.bind_expression = bind_expression

    def bound_variables(self) -> OrderedSet[BoundVariable]:
        return (OrderedSet([self.bound_variable])
                | (OrderedSet([]) if self.bind_expression is None else self.bind_expression.bound_variables()))

    def free_variables(self) -> OrderedSet[Variable]:
        return ((OrderedSet([self.in_variable] if isinstance(self.in_variable, Variable) else [])
                 | self.inner_formula.free_variables())
                - self.bound_variables())

    def tree_arguments(self) -> OrderedSet[DerivationTree]:
        result = OrderedSet([])
        if isinstance(self.in_variable, DerivationTree):
            result.add(self.in_variable)
        result.update(self.inner_formula.tree_arguments())
        return result

    def is_already_matched(self, tree: DerivationTree) -> bool:
        return False

    def __len__(self):
        result = 1 + len(self.inner_formula)
        if self.bind_expression:
            result += len(self.bind_expression.bound_elements)
        return result

    def __repr__(self):
        return f'{type(self).__name__}({repr(self.bound_variable)}, {repr(self.in_variable)}, ' \
               f'{repr(self.inner_formula)}{"" if self.bind_expression is None else ", " + repr(self.bind_expression)})'

    def __hash__(self):
        return hash((
            type(self).__name__,
            self.bound_variable,
            self.in_variable,
            self.inner_formula,
            self.bind_expression or 0
        ))

    def __eq__(self, other):
        return type(self) == type(other) and \
               (self.bound_variable, self.in_variable, self.inner_formula, self.bind_expression) == \
               (other.bound_variable, other.in_variable, other.inner_formula, other.bind_expression)


class ForallFormula(QuantifiedFormula):
    __next_id = 0

    def __init__(self,
                 bound_variable: Union[BoundVariable, str],
                 in_variable: Union[Variable, DerivationTree],
                 inner_formula: Formula,
                 bind_expression: Optional[BindExpression] = None,
                 already_matched: Optional[Set[int]] = None,
                 id: Optional[int] = None):
        super().__init__(bound_variable, in_variable, inner_formula, bind_expression)
        self.already_matched: Set[int] = set() if not already_matched else set(already_matched)

        # The id field is used by eliminate_quantifiers to avoid counting universal
        # formulas twice when checking for vacuous satisfaction.
        if id is None:
            self.id = ForallFormula.__next_id
            ForallFormula.__next_id += 1
        else:
            self.id = id

    def substitute_variables(self, subst_map: Dict[Variable, Variable]):
        assert not self.already_matched

        return ForallFormula(
            self.bound_variable if self.bound_variable not in subst_map else subst_map[self.bound_variable],
            self.in_variable if self.in_variable not in subst_map else subst_map[self.in_variable],
            self.inner_formula.substitute_variables(subst_map),
            None if not self.bind_expression else self.bind_expression.substitute_variables(subst_map),
            self.already_matched,
            id=self.id
        )

    def substitute_expressions(self, subst_map: Dict[Union[Variable, DerivationTree], DerivationTree]) -> Formula:
        new_in_variable = self.in_variable
        if self.in_variable in subst_map:
            new_in_variable = subst_map[new_in_variable]
        elif isinstance(new_in_variable, DerivationTree):
            new_in_variable = new_in_variable.substitute(subst_map)

        new_inner_formula = self.inner_formula.substitute_expressions(subst_map)

        if self.bound_variable not in new_inner_formula.free_variables() and self.bind_expression is None:
            # NOTE: We cannot remove the quantifier if there is a bind expression, not even if
            #       the variables in the bind expression do not occur in the inner formula,
            #       since there might be multiple expansion alternatives of the bound variable
            #       nonterminal and it makes a difference whether a particular expansion has been
            #       chosen. Consider, e.g., an inner formula "false". Then, this formula evaluates
            #       to false IF, AND ONLY IF, the defined expansion alternative is chosen, and
            #       NOT always.
            return new_inner_formula

        return ForallFormula(
            self.bound_variable,
            new_in_variable,
            new_inner_formula,
            self.bind_expression,
            self.already_matched,
            id=self.id
        )

    def add_already_matched(self, trees: Union[DerivationTree, Iterable[DerivationTree]]) -> 'ForallFormula':
        return ForallFormula(
            self.bound_variable,
            self.in_variable,
            self.inner_formula,
            self.bind_expression,
            self.already_matched | ({trees.id} if isinstance(trees, DerivationTree) else {tree.id for tree in trees}),
            id=self.id
        )

    def is_already_matched(self, tree: DerivationTree) -> bool:
        return tree.id in self.already_matched

    def accept(self, visitor: FormulaVisitor):
        visitor.visit_forall_formula(self)
        if visitor.do_continue(self):
            self.inner_formula.accept(visitor)

    def __str__(self):
        quote = '"'
        return f'∀ {"" if not self.bind_expression else quote + str(self.bind_expression) + quote + " = "}' \
               f'{str(self.bound_variable)} ∈ {replace_line_breaks(str(self.in_variable))}: ({str(self.inner_formula)})'


class ExistsFormula(QuantifiedFormula):
    def __init__(self,
                 bound_variable: Union[BoundVariable, str],
                 in_variable: Union[Variable, DerivationTree],
                 inner_formula: Formula,
                 bind_expression: Optional[BindExpression] = None):
        super().__init__(bound_variable, in_variable, inner_formula, bind_expression)

    def substitute_variables(self, subst_map: Dict[Variable, Variable]):
        return ExistsFormula(
            self.bound_variable if self.bound_variable not in subst_map else subst_map[self.bound_variable],
            self.in_variable if self.in_variable not in subst_map else subst_map[self.in_variable],
            self.inner_formula.substitute_variables(subst_map),
            None if not self.bind_expression else self.bind_expression.substitute_variables(subst_map))

    def substitute_expressions(self, subst_map: Dict[Union[Variable, DerivationTree], DerivationTree]) -> Formula:
        new_in_variable = self.in_variable
        if self.in_variable in subst_map:
            new_in_variable = subst_map[new_in_variable]
        elif isinstance(new_in_variable, DerivationTree):
            new_in_variable = new_in_variable.substitute(subst_map)

        new_inner_formula = self.inner_formula.substitute_expressions(subst_map)

        if (self.bound_variable not in new_inner_formula.free_variables()
                and (self.bind_expression is None or
                     not any(bv in new_inner_formula.free_variables()
                             for bv in self.bind_expression.bound_variables()))):
            return new_inner_formula

        return ExistsFormula(
            self.bound_variable,
            new_in_variable,
            self.inner_formula.substitute_expressions(subst_map),
            self.bind_expression)

    def accept(self, visitor: FormulaVisitor):
        visitor.visit_exists_formula(self)
        if visitor.do_continue(self):
            self.inner_formula.accept(visitor)

    def __str__(self):
        quote = "'"
        return f'∃ {"" if not self.bind_expression else quote + str(self.bind_expression) + quote + " = "}' \
               f'{str(self.bound_variable)} ∈ {replace_line_breaks(str(self.in_variable))}: ({str(self.inner_formula)})'


class VariablesCollector(FormulaVisitor):
    def __init__(self):
        self.result: OrderedSet[Variable] = OrderedSet()

    @staticmethod
    def collect(formula: Formula) -> OrderedSet[Variable]:
        c = VariablesCollector()
        formula.accept(c)
        return c.result

    def visit_exists_formula(self, formula: ExistsFormula):
        self.visit_quantified_formula(formula)

    def visit_forall_formula(self, formula: ForallFormula):
        self.visit_quantified_formula(formula)

    def visit_quantified_formula(self, formula: QuantifiedFormula):
        if isinstance(formula.in_variable, Variable):
            self.result.add(formula.in_variable)
        self.result.add(formula.bound_variable)
        if formula.bind_expression is not None:
            self.result.update(formula.bind_expression.bound_variables())

    def visit_exists_int_formula(self, formula: ExistsIntFormula):
        self.result.add(formula.bound_variable)

    def visit_forall_int_formula(self, formula: ForallIntFormula):
        self.result.add(formula.bound_variable)

    def visit_predicate_formula(self, formula: StructuralPredicateFormula):
        self.result.update([arg for arg in formula.args if isinstance(arg, Variable)])

    def visit_semantic_predicate_formula(self, formula: SemanticPredicateFormula):
        self.result.update([arg for arg in formula.args if isinstance(arg, Variable)])

    def visit_smt_formula(self, formula: SMTFormula):
        self.result.update(formula.free_variables())


class FilterVisitor(FormulaVisitor):
    def __init__(
            self,
            filter_fun: Callable[[Formula], bool],
            do_continue: Callable[[Formula], bool] = lambda f: True):
        self.filter = filter_fun
        self.result: List[Formula] = []
        self.do_continue = do_continue

    def collect(self, formula: Formula) -> List[Formula]:
        self.result = []
        formula.accept(self)
        return self.result

    def do_continue(self, formula: Formula) -> bool:
        return self.do_continue(formula)

    def visit_forall_formula(self, formula: ForallFormula):
        if self.filter(formula):
            self.result.append(formula)

    def visit_exists_formula(self, formula: ExistsFormula):
        if self.filter(formula):
            self.result.append(formula)

    def visit_exists_int_formula(self, formula: ExistsIntFormula):
        if self.filter(formula):
            self.result.append(formula)

    def visit_forall_int_formula(self, formula: ForallIntFormula):
        if self.filter(formula):
            self.result.append(formula)

    def visit_negated_formula(self, formula: NegatedFormula):
        if self.filter(formula):
            self.result.append(formula)

    def visit_smt_formula(self, formula: SMTFormula):
        if self.filter(formula):
            self.result.append(formula)

    def visit_predicate_formula(self, formula: StructuralPredicateFormula):
        if self.filter(formula):
            self.result.append(formula)

    def visit_semantic_predicate_formula(self, formula: SemanticPredicateFormula):
        if self.filter(formula):
            self.result.append(formula)

    def visit_disjunctive_formula(self, formula: DisjunctiveFormula):
        if self.filter(formula):
            self.result.append(formula)

    def visit_conjunctive_formula(self, formula: ConjunctiveFormula):
        if self.filter(formula):
            self.result.append(formula)


def replace_formula(in_formula: Formula,
                    to_replace: Union[Formula, Callable[[Formula], bool | Formula]],
                    replace_with: Optional[Formula] = None) -> Formula:
    """
    Replaces a formula inside a conjunction or disjunction.
    to_replace is either (1) a formula to replace, or (2) a predicate which holds if the given formula
    should been replaced (if it returns True, replace_with must not be None), or (3) a function returning
    the formula to replace if the subformula should be replaced, or False otherwise. For (3), replace_with
    may be None (it is irrelevant).
    """

    if callable(to_replace):
        result = to_replace(in_formula)
        if isinstance(result, Formula):
            return replace_formula(result, to_replace, replace_with)

        assert isinstance(result, bool)
        if result:
            assert replace_with is not None
            return replace_with
    else:
        if in_formula == to_replace:
            return replace_with

    if isinstance(in_formula, ConjunctiveFormula):
        return reduce(lambda a, b: a & b, [replace_formula(child, to_replace, replace_with)
                                           for child in in_formula.args])
    elif isinstance(in_formula, DisjunctiveFormula):
        return reduce(lambda a, b: a | b, [replace_formula(child, to_replace, replace_with)
                                           for child in in_formula.args])
    elif isinstance(in_formula, NegatedFormula):
        child_result = replace_formula(in_formula.args[0], to_replace, replace_with)

        if child_result == SMTFormula(z3.BoolVal(False)):
            return SMTFormula(z3.BoolVal(True))
        elif child_result == SMTFormula(z3.BoolVal(True)):
            return SMTFormula(z3.BoolVal(False))

        return NegatedFormula(child_result)
    elif isinstance(in_formula, ForallFormula):
        return ForallFormula(
            in_formula.bound_variable,
            in_formula.in_variable,
            replace_formula(in_formula.inner_formula, to_replace, replace_with),
            in_formula.bind_expression,
            in_formula.already_matched,
            id=in_formula.id
        )
    elif isinstance(in_formula, ExistsFormula):
        return ExistsFormula(
            in_formula.bound_variable,
            in_formula.in_variable,
            replace_formula(in_formula.inner_formula, to_replace, replace_with),
            in_formula.bind_expression)
    elif isinstance(in_formula, ExistsIntFormula):
        return ExistsIntFormula(
            in_formula.bound_variable,
            replace_formula(in_formula.inner_formula, to_replace, replace_with))
    elif isinstance(in_formula, ForallIntFormula):
        return ForallIntFormula(
            in_formula.bound_variable,
            replace_formula(in_formula.inner_formula, to_replace, replace_with))

    return in_formula


def convert_to_nnf(formula: Formula, negate=False) -> Formula:
    """Pushes negations inside the formula."""
    if isinstance(formula, NegatedFormula):
        return convert_to_nnf(formula.args[0], not negate)
    elif isinstance(formula, ConjunctiveFormula):
        args = [convert_to_nnf(arg, negate) for arg in formula.args]
        if negate:
            return reduce(lambda a, b: a | b, args)
        else:
            return reduce(lambda a, b: a & b, args)
    elif isinstance(formula, DisjunctiveFormula):
        args = [convert_to_nnf(arg, negate) for arg in formula.args]
        if negate:
            return reduce(lambda a, b: a & b, args)
        else:
            return reduce(lambda a, b: a | b, args)
    elif isinstance(formula, StructuralPredicateFormula) or isinstance(formula, SemanticPredicateFormula):
        if negate:
            return -formula
        else:
            return formula
    elif isinstance(formula, SMTFormula):
        negated_smt_formula = z3_push_in_negations(formula.formula, negate)
        # Automatic simplification can remove free variables from the formula!
        actual_symbols = get_symbols(negated_smt_formula)
        free_variables = [var for var in formula.free_variables() if var.to_smt() in actual_symbols]
        instantiated_variables = OrderedSet([
            var for var in formula.instantiated_variables if var.to_smt() in actual_symbols])
        substitutions = {var: repl for var, repl in formula.substitutions.items() if var.to_smt() in actual_symbols}

        return SMTFormula(
            negated_smt_formula,
            *free_variables,
            instantiated_variables=instantiated_variables,
            substitutions=substitutions,
            auto_eval=formula.auto_eval
        )
    elif isinstance(formula, ExistsIntFormula) or isinstance(formula, ForallIntFormula):
        inner_formula = convert_to_nnf(formula.inner_formula, negate) if negate else formula.inner_formula

        if ((isinstance(formula, ForallIntFormula) and negate)
                or (isinstance(formula, ExistsIntFormula) and not negate)):
            return ExistsIntFormula(formula.bound_variable, inner_formula)
        else:
            return ForallIntFormula(formula.bound_variable, inner_formula)
    elif isinstance(formula, QuantifiedFormula):
        inner_formula = convert_to_nnf(formula.inner_formula, negate) if negate else formula.inner_formula
        already_matched: Set[int] = formula.already_matched if isinstance(formula, ForallFormula) else set()

        if ((isinstance(formula, ForallFormula) and negate)
                or (isinstance(formula, ExistsFormula) and not negate)):
            return ExistsFormula(
                formula.bound_variable, formula.in_variable, inner_formula, formula.bind_expression)
        else:
            return ForallFormula(
                formula.bound_variable, formula.in_variable, inner_formula, formula.bind_expression, already_matched)
    else:
        assert False, f"Unexpected formula type {type(formula).__name__}"


def convert_to_dnf(formula: Formula) -> Formula:
    assert (not isinstance(formula, NegatedFormula)
            or not isinstance(formula.args[0], PropositionalCombinator)), "Convert to NNF before converting to DNF"

    if isinstance(formula, ConjunctiveFormula):
        disjuncts_list = [split_disjunction(convert_to_dnf(arg)) for arg in formula.args]
        return reduce(
            lambda a, b: a | b,
            [reduce(lambda a, b: a & b, OrderedSet(split_conjunction(left & right)), SMTFormula(z3.BoolVal(True)))
             for left, right in itertools.product(*disjuncts_list)],
            SMTFormula(z3.BoolVal(False))
        )
    elif isinstance(formula, DisjunctiveFormula):
        return reduce(
            lambda a, b: a | b,
            [convert_to_dnf(subformula) for subformula in formula.args],
            SMTFormula(z3.BoolVal(False))
        )
    elif isinstance(formula, ForallFormula):
        return ForallFormula(
            formula.bound_variable,
            formula.in_variable,
            convert_to_dnf(formula.inner_formula),
            formula.bind_expression,
            formula.already_matched)
    elif isinstance(formula, ExistsFormula):
        return ExistsFormula(
            formula.bound_variable,
            formula.in_variable,
            convert_to_dnf(formula.inner_formula),
            formula.bind_expression)
    else:
        return formula


def ensure_unique_bound_variables(formula: Formula, used_names: Optional[Set[str]] = None) -> Formula:
    used_names: Set[str] = set() if used_names is None else used_names

    def fresh_vars(orig_vars: OrderedSet[BoundVariable]) -> Dict[BoundVariable, BoundVariable]:
        nonlocal used_names
        result: Dict[BoundVariable, BoundVariable] = {}

        for variable in orig_vars:
            if variable.name not in used_names:
                used_names.add(variable.name)
                result[variable] = variable
                continue

            idx = 0
            while f"{variable.name}_{idx}" in used_names:
                idx += 1

            new_name = f"{variable.name}_{idx}"
            used_names.add(new_name)
            result[variable] = BoundVariable(new_name, variable.n_type)

        return result

    if isinstance(formula, ForallFormula):
        formula = cast(ForallFormula, formula.substitute_variables(fresh_vars(formula.bound_variables())))
        return ForallFormula(
            formula.bound_variable,
            formula.in_variable,
            ensure_unique_bound_variables(formula.inner_formula, used_names),
            formula.bind_expression,
            formula.already_matched
        )
    elif isinstance(formula, ExistsFormula):
        formula = cast(ExistsFormula, formula.substitute_variables(fresh_vars(formula.bound_variables())))
        return ExistsFormula(
            formula.bound_variable,
            formula.in_variable,
            ensure_unique_bound_variables(formula.inner_formula, used_names),
            formula.bind_expression,
        )
    elif isinstance(formula, NegatedFormula):
        return NegatedFormula(ensure_unique_bound_variables(formula.args[0], used_names))
    elif isinstance(formula, ConjunctiveFormula):
        return reduce(lambda a, b: a & b, [ensure_unique_bound_variables(arg, used_names) for arg in formula.args])
    elif isinstance(formula, DisjunctiveFormula):
        return reduce(lambda a, b: a | b, [ensure_unique_bound_variables(arg, used_names) for arg in formula.args])
    else:
        return formula


def split_conjunction(formula: Formula) -> List[Formula]:
    if not type(formula) is ConjunctiveFormula:
        return [formula]
    else:
        formula: ConjunctiveFormula
        return [elem for arg in formula.args for elem in split_conjunction(arg)]


def split_disjunction(formula: Formula) -> List[Formula]:
    if not type(formula) is DisjunctiveFormula:
        return [formula]
    else:
        formula: DisjunctiveFormula
        return [elem for arg in formula.args for elem in split_disjunction(arg)]


class VariableManager:
    def __init__(self, grammar: Optional[Grammar] = None):
        self.placeholders: Dict[str, Variable] = {}
        self.variables: Dict[str, Variable] = {}
        self.grammar = grammar

    def _var(self,
             name: str,
             n_type: Optional[str],
             constr: Optional[Callable[[str, Optional[str]], Variable]] = None) -> Variable:
        if self.grammar is not None and n_type is not None:
            assert n_type == Variable.NUMERIC_NTYPE or n_type in self.grammar, \
                f"Unknown nonterminal type {n_type} for variable {name}"

        matching_variables = [var for var_name, var in self.variables.items() if var_name == name]
        if matching_variables:
            return matching_variables[0]

        if constr is not None and n_type:
            return self.variables.setdefault(name, constr(name, n_type))

        matching_placeholders = [var for var_name, var in self.placeholders.items() if var_name == name]
        if matching_placeholders:
            return matching_placeholders[0]

        assert constr is not None
        return self.placeholders.setdefault(name, constr(name, None))

    def var_declared(self, name: str) -> bool:
        return name in self.variables.keys()

    def all_var_names(self) -> Set[str]:
        return set(self.variables.keys()) | set(self.placeholders.keys())

    def const(self, name: str, n_type: Optional[str] = None) -> Constant:
        return cast(Constant, self._var(name, n_type, Constant))

    def num_var(self, name: str) -> BoundVariable:
        return cast(BoundVariable, self._var(name, BoundVariable.NUMERIC_NTYPE, BoundVariable))

    def bv(self, name: str, n_type: Optional[str] = None) -> BoundVariable:
        return cast(BoundVariable, self._var(name, n_type, BoundVariable))

    def smt(self, formula) -> SMTFormula:
        assert isinstance(formula, z3.BoolRef)
        z3_symbols = get_symbols(formula)
        isla_variables = [self._var(str(z3_symbol), None) for z3_symbol in z3_symbols]
        return SMTFormula(formula, *isla_variables)

    def create(self, formula: Formula) -> Formula:
        undeclared_variables = [
            ph_name for ph_name in self.placeholders
            if all(var_name != ph_name for var_name in self.variables)
        ]

        if undeclared_variables:
            raise RuntimeError("Undeclared variables: " + ", ".join(undeclared_variables))

        return formula.substitute_variables({
            ph_var: next(var for var_name, var in self.variables.items() if var_name == ph_name)
            for ph_name, ph_var in self.placeholders.items()
        })


def antlr_get_text_with_whitespace(ctx) -> str:
    if isinstance(ctx, antlr4.TerminalNode):
        return ctx.getText()

    a = ctx.start.start
    b = ctx.stop.stop
    assert isinstance(a, int)
    assert isinstance(b, int)
    stream = ctx.start.source[1]
    assert isinstance(stream, antlr4.InputStream)
    return stream.getText(start=a, stop=b)


def parse_tree_text(elem: RuleContext | CommonToken) -> str:
    if isinstance(elem, CommonToken):
        return elem.text
    else:
        return elem.getText()


class MExprEmitter(MexprParserListener.MexprParserListener):
    def __init__(self, mgr: VariableManager):
        self.result: List[Union[str, BoundVariable, List[str]]] = []
        self.mgr = mgr

    def exitMatchExprOptional(self, ctx: MexprParser.MatchExprOptionalContext):
        self.result.append([antlr_get_text_with_whitespace(ctx)[1:-1]])

    def exitMatchExprChars(self, ctx: MexprParser.MatchExprCharsContext):
        text = antlr_get_text_with_whitespace(ctx)
        text = text.replace("{{", "{")
        text = text.replace("}}", "}")
        text = text.replace('\\"', '"')
        self.result.append(text)

    def exitMatchExprVar(self, ctx: MexprParser.MatchExprVarContext):
        self.result.append(self.mgr.bv(parse_tree_text(ctx.ID()), parse_tree_text(ctx.varType())))


class ISLaEmitter(IslaLanguageListener.IslaLanguageListener):
    def __init__(
            self,
            grammar: Optional[Grammar] = None,
            structural_predicates: Optional[Set[StructuralPredicate]] = None,
            semantic_predicates: Optional[Set[SemanticPredicate]] = None):
        self.structural_predicates_map = {} if not structural_predicates else {p.name: p for p in structural_predicates}
        self.semantic_predicates_map = {} if not semantic_predicates else {p.name: p for p in semantic_predicates}

        self.result: Optional[Formula] = None
        self.constant: Constant = Constant("start", "<start>")
        self.formulas = {}
        self.predicate_args = {}

        self.mgr = VariableManager(grammar)

    def parse_mexpr(self, inp: str, mgr: VariableManager) -> BindExpression:
        class BailPrintErrorStrategy(antlr4.BailErrorStrategy):
            def recover(self, recognizer: antlr4.Parser, e: antlr4.RecognitionException):
                recognizer._errHandler.reportError(recognizer, e)
                super().recover(recognizer, e)

        lexer = MexprLexer(InputStream(inp))
        parser = MexprParser(antlr4.CommonTokenStream(lexer))
        parser._errHandler = BailPrintErrorStrategy()
        mexpr_emitter = MExprEmitter(mgr)
        antlr4.ParseTreeWalker().walk(mexpr_emitter, parser.matchExpr())
        return BindExpression(*mexpr_emitter.result)

    def exitStart(self, ctx: IslaLanguageParser.StartContext):
        try:
            self.result = self.mgr.create(self.formulas[ctx.formula()])
        except RuntimeError as exc:
            raise SyntaxError(str(exc))

    def get_var(self, var_name: str, var_type: Optional[str] = None) -> BoundVariable | Constant:
        if var_name == self.constant.name:
            return self.constant

        return self.mgr.bv(var_name, var_type)

    def known_var_names(self) -> Set[str]:
        return self.mgr.all_var_names() | {self.constant.name}

    def exitConstDecl(self, ctx: IslaLanguageParser.ConstDeclContext):
        self.constant = Constant(parse_tree_text(ctx.ID()), parse_tree_text(ctx.varType()))

    def exitForall(self, ctx: IslaLanguageParser.ForallContext):
        var_id = parse_tree_text(ctx.varId)
        var_type = parse_tree_text(ctx.varType())

        if self.mgr.var_declared(var_id):
            raise SyntaxError(
                f"Variable {var_id} already declared "
                f"(line {ctx.varId.line}, column {ctx.varId.column})"
            )

        self.formulas[ctx] = ForallFormula(
            self.get_var(var_id, var_type),
            self.get_var(parse_tree_text(ctx.inId)),
            self.formulas[ctx.formula()])

    def exitExists(self, ctx: IslaLanguageParser.ExistsContext):
        var_id = parse_tree_text(ctx.varId)
        var_type = parse_tree_text(ctx.varType())

        if self.mgr.var_declared(var_id):
            raise SyntaxError(
                f"Variable {var_id} already declared "
                f"(line {ctx.varId.line}, column {ctx.varId.column})"
            )

        self.formulas[ctx] = ExistsFormula(
            self.get_var(var_id, var_type),
            self.get_var(parse_tree_text(ctx.inId)),
            self.formulas[ctx.formula()])

    def exitForallMexpr(self, ctx: IslaLanguageParser.ForallMexprContext):
        mexpr = self.parse_mexpr(
            antlr_get_text_with_whitespace(ctx.STRING())[1:-1],
            self.mgr)

        var_id = parse_tree_text(ctx.varId)
        var_type = parse_tree_text(ctx.varType())

        if self.mgr.var_declared(var_id):
            raise SyntaxError(
                f"Variable {var_id} already declared "
                f"(line {ctx.varId.line}, column {ctx.varId.column})"
            )

        self.formulas[ctx] = ForallFormula(
            self.get_var(var_id, var_type),
            self.get_var(parse_tree_text(ctx.inId)),
            self.formulas[ctx.formula()],
            bind_expression=mexpr
        )

    def exitExistsMexpr(self, ctx: IslaLanguageParser.ExistsMexprContext):
        mexpr = self.parse_mexpr(
            antlr_get_text_with_whitespace(ctx.STRING())[1:-1],
            self.mgr)

        var_id = parse_tree_text(ctx.varId)
        var_type = parse_tree_text(ctx.varType())

        if self.mgr.var_declared(var_id):
            raise SyntaxError(
                f"Variable {var_id} already declared "
                f"(line {ctx.varId.line}, column {ctx.varId.column})"
            )

        self.formulas[ctx] = ExistsFormula(
            self.get_var(var_id, var_type),
            self.get_var(parse_tree_text(ctx.inId)),
            self.formulas[ctx.formula()],
            bind_expression=mexpr
        )

    def exitNegation(self, ctx: IslaLanguageParser.NegationContext):
        self.formulas[ctx] = -self.formulas[ctx.formula()]

    def exitConjunction(self, ctx: IslaLanguageParser.ConjunctionContext):
        self.formulas[ctx] = self.formulas[ctx.formula(0)] & self.formulas[ctx.formula(1)]

    def exitDisjunction(self, ctx: IslaLanguageParser.DisjunctionContext):
        self.formulas[ctx] = self.formulas[ctx.formula(0)] | self.formulas[ctx.formula(1)]

    def exitImplication(self, ctx: IslaLanguageParser.ImplicationContext):
        left = self.formulas[ctx.formula(0)]
        right = self.formulas[ctx.formula(1)]
        self.formulas[ctx] = -left | right

    def exitEquivalence(self, ctx: IslaLanguageParser.EquivalenceContext):
        left = self.formulas[ctx.formula(0)]
        right = self.formulas[ctx.formula(1)]
        self.formulas[ctx] = (-left & -right) | (left & right)

    def exitExclusiveOr(self, ctx: IslaLanguageParser.ExclusiveOrContext):
        left = self.formulas[ctx.formula(0)]
        right = self.formulas[ctx.formula(1)]
        self.formulas[ctx] = (left & -right) | (-left & right)

    def exitPredicateAtom(self, ctx: IslaLanguageParser.PredicateAtomContext):
        predicate_name = parse_tree_text(ctx.ID())
        if (predicate_name not in self.structural_predicates_map and
                predicate_name not in self.semantic_predicates_map):
            raise SyntaxError(f"Unknown predicate {predicate_name} in {parse_tree_text(ctx)}")

        args = [self.predicate_args[arg] for arg in ctx.predicateArg()]

        is_structural = predicate_name in self.structural_predicates_map

        predicate = (
            self.structural_predicates_map[predicate_name] if is_structural
            else self.semantic_predicates_map[predicate_name])

        if len(args) != predicate.arity:
            raise SyntaxError(
                f"Unexpected number {len(args)} for predicate {predicate_name} "
                f"({predicate.arity} expected) in {parse_tree_text(ctx)} "
                f"(line {ctx.ID().symbol.line}, column {ctx.ID().symbol.column}"
            )

        if is_structural:
            self.formulas[ctx] = StructuralPredicateFormula(predicate, *args)
        else:
            self.formulas[ctx] = SemanticPredicateFormula(predicate, *args)

    def exitPredicateArg(self, ctx: IslaLanguageParser.PredicateArgContext):
        if ctx.ID():
            self.predicate_args[ctx] = self.get_var(parse_tree_text(ctx.ID()))
        elif ctx.INT():
            self.predicate_args[ctx] = int(parse_tree_text(ctx))
        elif ctx.STRING():
            self.predicate_args[ctx] = parse_tree_text(ctx)[1:-1]

    def exitSMTFormula(self, ctx: IslaLanguageParser.SMTFormulaContext):
        formula_text = "<N/A>"
        try:
            formula_text = antlr_get_text_with_whitespace(ctx)
            z3_constr = z3.parse_smt2_string(
                f"(assert {formula_text})",
                decls={var: z3.String(var) for var in self.known_var_names()})[0]
        except z3.Z3Exception as exp:
            raise SyntaxError(
                f"Error parsing SMT formula '{formula_text}', {exp.value.decode().strip()}")

        free_vars = [self.get_var(str(s)) for s in get_symbols(z3_constr)]
        self.formulas[ctx] = SMTFormula(z3_constr, *free_vars)

    def exitExistsInt(self, ctx: IslaLanguageParser.ExistsIntContext):
        var_id = parse_tree_text(ctx.ID())

        if self.mgr.var_declared(var_id):
            raise SyntaxError(
                f"Variable {var_id} already declared "
                f"(line {ctx.varId.symbol.line}, column {ctx.varId.symbol.column})"
            )

        self.formulas[ctx] = ExistsIntFormula(
            self.get_var(var_id, Variable.NUMERIC_NTYPE),
            self.formulas[ctx.formula()])

    def exitForallInt(self, ctx: IslaLanguageParser.ForallIntContext):
        var_id = parse_tree_text(ctx.ID())

        if self.mgr.var_declared(var_id):
            raise SyntaxError(
                f"Variable {var_id} already declared "
                f"(line {ctx.varId.symbol.line}, column {ctx.varId.symbol.column})"
            )

        self.formulas[ctx] = ForallIntFormula(
            self.get_var(var_id, Variable.NUMERIC_NTYPE),
            self.formulas[ctx.formula()])

    def exitSexprId(self, ctx: IslaLanguageParser.SexprIdContext):
        id_text = parse_tree_text(ctx.ID())
        if not ISLaEmitter.is_protected_smtlib_keyword(id_text):
            self.get_var(id_text)  # Simply register variable

    def exitParFormula(self, ctx: IslaLanguageParser.ParFormulaContext):
        self.formulas[ctx] = self.formulas[ctx.formula()]

    @staticmethod
    def is_protected_smtlib_keyword(symbol: str) -> bool:
        if symbol in {"let", "forall", "exists", "match", "!", "ite"}:
            return True

        try:
            z3.parse_smt2_string(f"(assert ({symbol} 1))")
            return True
        except Z3Exception as exc:
            return "unknown constant" not in str(exc)


def parse_isla(
        inp: str,
        grammar: Optional[Grammar] = None,
        structural_predicates: Optional[Set[StructuralPredicate]] = None,
        semantic_predicates: Optional[Set[SemanticPredicate]] = None) -> Formula:
    class BailPrintErrorStrategy(antlr4.BailErrorStrategy):
        def recover(self, recognizer: antlr4.Parser, e: antlr4.RecognitionException):
            recognizer._errHandler.reportError(recognizer, e)
            super().recover(recognizer, e)

    lexer = IslaLanguageLexer(InputStream(inp))
    parser = IslaLanguageParser(antlr4.CommonTokenStream(lexer))
    parser._errHandler = BailPrintErrorStrategy()
    isla_emitter = ISLaEmitter(grammar, structural_predicates, semantic_predicates)
    antlr4.ParseTreeWalker().walk(isla_emitter, parser.start())
    return isla_emitter.result


class ISLaUnparser:
    def __init__(self, formula: Formula, indent="  "):
        assert isinstance(formula, Formula)
        self.formula = formula
        self.indent = indent

    def unparse(self) -> str:
        result: str = ""

        try:
            constant = next(
                (c for c in VariablesCollector.collect(self.formula)
                 if isinstance(c, Constant) and not c.is_numeric()))

            if constant != Constant("start", "<start>"):
                result += f"const {constant.name}: {constant.n_type};\n\n"
        except StopIteration:
            pass

        result += "\n".join(self._unparse_constraint(self.formula))

        return result

    def _unparse_constraint(self, formula: Formula) -> List[str]:
        if isinstance(formula, QuantifiedFormula):
            return self._unparse_quantified_formula(formula)
        elif isinstance(formula, ExistsIntFormula):
            return self._unparse_exists_int_formula(formula)
        elif isinstance(formula, ForallIntFormula):
            return self._unparse_forall_int_formula(formula)
        elif isinstance(formula, ConjunctiveFormula) or isinstance(formula, DisjunctiveFormula):
            return self._unparse_propositional_combination(formula)
        elif isinstance(formula, NegatedFormula):
            return self._unparse_negated_formula(formula)
        elif isinstance(formula, StructuralPredicateFormula) or isinstance(formula, SemanticPredicateFormula):
            return self._unparse_predicate_formula(formula)
        elif isinstance(formula, SMTFormula):
            return self._unparse_smt_formula(formula)
        else:
            raise NotImplementedError(f"Unparsing of formulas of type {type(formula).__name__} not implemented.")

    def _unparse_match_expr(self, match_expr: BindExpression | None) -> str:
        return "" if match_expr is None else f'="{match_expr}"'

    def _unparse_quantified_formula(self, formula: QuantifiedFormula) -> List[str]:
        qfr = "forall" if isinstance(formula, ForallFormula) else "exists"
        result = [
            f"{qfr} {formula.bound_variable.n_type} {formula.bound_variable.name}"
            f"{self._unparse_match_expr(formula.bind_expression)} in {formula.in_variable}:"]
        child_result = self._unparse_constraint(formula.inner_formula)
        result += [self.indent + line for line in child_result]

        return result

    def _unparse_propositional_combination(self, formula: ConjunctiveFormula | DisjunctiveFormula):
        combinator = "and" if isinstance(formula, ConjunctiveFormula) else "or"
        child_results = [self._unparse_constraint(child) for child in formula.args]

        for idx, child_result in enumerate(child_results[:-1]):
            child_results[idx][-1] = child_results[idx][-1] + f" {combinator}"

        for idx, child_result in enumerate(child_results[1:]):
            child_results[idx] = [" " + line for line in child_results[idx]]

        child_results[0][0] = "(" + child_results[0][0][1:]
        child_results[-1][-1] += ")"

        return [line for child_result in child_results for line in child_result]

    def _unparse_predicate_formula(self, formula: StructuralPredicateFormula | SemanticPredicateFormula):
        return [str(formula)]

    def _unparse_smt_formula(self, formula):
        return [smt_expr_to_str(formula.formula)]

        # result = formula.formula.sexpr()
        # # str.to_int is str.to.int in Z3; use this to preserve idempotency of parsing/unparsing
        # result = result.replace("str.to_int", "str.to.int")
        # return [result]

    def _unparse_negated_formula(self, formula):
        child_results = [self._unparse_constraint(child) for child in formula.args]
        result = [line for child_result in child_results for line in child_result]
        result[0] = "not(" + result[0]
        result[-1] += ")"
        return result

    def _unparse_forall_int_formula(self, formula):
        result = [f"forall int {formula.bound_variable.name}:"]
        child_result = self._unparse_constraint(formula.inner_formula)
        result += [self.indent + line for line in child_result]
        return result

    def _unparse_exists_int_formula(self, formula):
        result = [f"exists int {formula.bound_variable.name}:"]
        child_result = self._unparse_constraint(formula.inner_formula)
        result += [self.indent + line for line in child_result]
        return result


def get_conjuncts(formula: Formula) -> List[Formula]:
    return [conjunct
            for disjunct in split_disjunction(formula)
            for conjunct in split_conjunction(disjunct)]


T = TypeVar("T")


def fresh_variable(
        used: MutableSet[Variable],
        base_name: str,
        n_type: str,
        constructor: Callable[[str, str], T],
        add: bool = True) -> T:
    name = base_name
    idx = 0
    while any(used_var.name == name for used_var in used):
        name = f"{base_name}_{idx}"
        idx += 1

    result = constructor(name, n_type)
    if add:
        used.add(result)

    return result


def fresh_constant(used: MutableSet[Variable], proposal: Constant, add: bool = True) -> Constant:
    return fresh_variable(used, proposal.name, proposal.n_type, Constant, add)


def fresh_bound_variable(used: MutableSet[Variable], proposal: BoundVariable, add: bool = True) -> BoundVariable:
    return fresh_variable(used, proposal.name, proposal.n_type, BoundVariable, add)


def instantiate_top_constant(formula: Formula, tree: DerivationTree) -> Formula:
    top_constant: Constant = next(
        c for c in VariablesCollector.collect(formula)
        if isinstance(c, Constant) and not c.is_numeric())
    return formula.substitute_expressions({top_constant: tree})


@lru_cache(maxsize=None)
def bound_elements_to_tree(
        bound_elements: Tuple[BoundVariable, ...],
        immutable_grammar: ImmutableGrammar,
        in_nonterminal: str) -> DerivationTree:
    grammar = immutable_to_grammar(immutable_grammar)
    fuzzer = GrammarFuzzer(grammar)

    placeholder_map: Dict[Union[str, BoundVariable], str] = {}
    for bound_element in bound_elements:
        if isinstance(bound_element, str):
            placeholder_map[bound_element] = bound_element
        elif not is_nonterminal(bound_element.n_type):
            placeholder_map[bound_element] = bound_element.n_type
        else:
            ph_candidate = fuzzer.expand_tree((bound_element.n_type, None))
            placeholder_map[bound_element] = tree_to_string(ph_candidate)

    inp = "".join(list(map(lambda elem: placeholder_map[elem], bound_elements)))

    subgrammar = copy.deepcopy(grammar)
    subgrammar["<start>"] = [in_nonterminal]
    delete_unreachable(subgrammar)
    parser = EarleyParser(subgrammar)

    try:
        return DerivationTree.from_parse_tree(list(parser.parse(inp))[0][1][0])
    except SyntaxError as serr:
        raise RuntimeError(
            f"Could not transform bound elements {''.join(map(str, bound_elements))} "
            f"to tree starting in {in_nonterminal}: SyntaxError {serr}")


@lru_cache(maxsize=None)
def flatten_bound_elements(
        orig_bound_elements: Tuple[Union[BoundVariable, Tuple[BoundVariable, ...]], ...],
        grammar: ImmutableGrammar,
        in_nonterminal: Optional[str] = None) -> Tuple[Tuple[BoundVariable, ...], ...]:
    """Returns all possible bound elements lists where each contained optional either has
    been chosen or removed. If this BindExpression has no optionals, the returned list is
    a singleton."""
    bound_elements_combinations: Tuple[Tuple[BoundVariable, ...], ...] = ()

    # Consider all possible on/off combinations for optional elements
    optionals = [elem for elem in orig_bound_elements if isinstance(elem, tuple)]

    if not optionals:
        return orig_bound_elements,

    for combination in powerset(optionals):
        # Inline all chosen, remove all not chosen optionals
        raw_bound_elements: List[BoundVariable, ...] = []
        for bound_element in orig_bound_elements:
            if not isinstance(bound_element, tuple):
                raw_bound_elements.append(bound_element)
                continue

            if bound_element in combination:
                raw_bound_elements.extend(bound_element)

        bound_elements: List[BoundVariable] = []
        for bound_element in raw_bound_elements:
            # We have to merge consecutive dummy variables representing terminal symbols
            # ...and to split result elements containing two variables representing nonterminal symbols
            if (isinstance(bound_element, DummyVariable) and
                    not is_nonterminal(bound_element.n_type) and
                    bound_elements and
                    isinstance(bound_elements[-1], DummyVariable) and
                    not is_nonterminal(bound_elements[-1].n_type)):
                bound_elements[-1] = DummyVariable(bound_elements[-1].n_type + bound_element.n_type)
                continue

            bound_elements.append(bound_element)

        if is_valid_combination(tuple(bound_elements), grammar, in_nonterminal):
            bound_elements_combinations += (tuple(bound_elements),)

    return bound_elements_combinations


@lru_cache(maxsize=None)
def is_valid_combination(
        combination: Sequence[BoundVariable],
        immutable_grammar: ImmutableGrammar,
        in_nonterminal: Optional[str]) -> bool:
    grammar = immutable_to_grammar(immutable_grammar)
    fuzzer = GrammarFuzzer(grammar, min_nonterminals=1, max_nonterminals=6)
    in_nonterminals = [in_nonterminal] if in_nonterminal else grammar.keys()

    for nonterminal in in_nonterminals:
        if nonterminal == "<start>":
            specialized_grammar = grammar
        else:
            specialized_grammar = copy.deepcopy(grammar)
            specialized_grammar["<start>"] = [nonterminal]
            delete_unreachable(specialized_grammar)

        parser = EarleyParser(specialized_grammar)

        for _ in range(3):
            inp = "".join([
                tree_to_string(fuzzer.expand_tree((v.n_type, None)))
                if is_nonterminal(v.n_type)
                else v.n_type
                for v in combination])
            try:
                next(iter(parser.parse(inp)))
            except SyntaxError:
                break
        else:
            return True

    return False


def set_smt_auto_eval(formula: Formula, auto_eval: bool = False):
    class AutoEvalVisitor(FormulaVisitor):
        def visit_smt_formula(self, formula: SMTFormula):
            formula.auto_eval = auto_eval

    formula.accept(AutoEvalVisitor())
