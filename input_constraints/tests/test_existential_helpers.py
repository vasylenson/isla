import unittest

from fuzzingbook.Grammars import JSON_GRAMMAR
from fuzzingbook.Parser import canonical
from grammar_graph.gg import GrammarGraph

from input_constraints.existential_helpers import insert_tree, wrap_in_tree_starting_in
from input_constraints.isla import DerivationTree
from input_constraints.tests.subject_languages import tinyc
from input_constraints.tests.test_data import *
from input_constraints.tests.test_helpers import parse


class TestExistentialHelpers(unittest.TestCase):
    def test_insert_lang(self):
        canonical_grammar = canonical(LANG_GRAMMAR)

        inp = "x := 1 ; y := z"
        tree = DerivationTree.from_parse_tree(parse(inp, LANG_GRAMMAR))

        to_insert = DerivationTree.from_parse_tree(parse("y := 0", LANG_GRAMMAR, "<assgn>"))
        results = insert_tree(canonical_grammar, to_insert, tree)
        self.assertIn("x := 1 ; y := 0 ; y := z", map(str, results))

        results = insert_tree(canonical_grammar, to_insert, tree)
        self.assertIn("y := 0 ; x := 1 ; y := z", map(str, results))

        inp = "x := 1 ; y := 2 ; y := z"
        tree = DerivationTree.from_parse_tree(parse(inp, LANG_GRAMMAR))
        results = insert_tree(canonical_grammar, to_insert, tree)
        self.assertIn("x := 1 ; y := 2 ; y := 0 ; y := z", map(str, results))

    def test_insert_json_1(self):
        inp = ' { "T" : { "I" : true , "" : [ false , "salami" ] , "" : true , "" : null , "" : false } } '
        tree = DerivationTree.from_parse_tree(parse(inp, JSON_GRAMMAR))
        to_insert = DerivationTree.from_parse_tree(parse(' "key" : { "key" : null } ', JSON_GRAMMAR, "<member>"))

        results = insert_tree(canonical(JSON_GRAMMAR), to_insert, tree, max_num_solutions=None)

        self.assertIn(
            ' { "T" : { "I" : true , '
            '"key" : { "key" : null } , '
            '"" : [ false , "salami" ] , "" : true , "" : null , "" : false } } ',
            [result.to_string() for result in results])

    def test_insert_json_2(self):
        inp = ' { "T" : { "I" : true , "" : [ false , "salami" ] , "" : true , "" : null , "" : false } } '
        tree = DerivationTree.from_parse_tree(parse(inp, JSON_GRAMMAR))
        to_insert = DerivationTree.from_parse_tree(parse(' "cheese" ', JSON_GRAMMAR, "<element>"))

        results = insert_tree(canonical(JSON_GRAMMAR), to_insert, tree, max_num_solutions=None)
        self.assertIn(
            ' { "T" : { "I" : true , "" : [ false , "cheese" , "salami" ] , "" : true , "" : null , "" : false } } ',
            [result.to_string() for result in results])

    def test_insert_assignment(self):
        assgn = DerivationTree.from_parse_tree(("<assgn>", None))
        tree = ('<start>', [('<stmt>', [('<assgn>', [('<var>', None), (' := ', []), ('<rhs>', [('<var>', None)])])])])
        results = insert_tree(
            canonical(LANG_GRAMMAR),
            assgn,
            DerivationTree.from_parse_tree(tree))

        self.assertEqual(
            ['<var> := <var> ; <assgn>',
             '<var> := <var> ; <assgn> ; <stmt>',
             '<assgn> ; <var> := <var>',
             '<assgn> ; <assgn> ; <var> := <var>',
             ],
            list(map(str, results))
        )

    def test_insert_assignment_2(self):
        tree = ('<start>', [('<stmt>', [('<assgn>', [("<var>", None), (' := ', []), ('<rhs>', [("<var>", None)])])])])

        results = insert_tree(canonical(LANG_GRAMMAR),
                              DerivationTree("<assgn>", None),
                              DerivationTree.from_parse_tree(tree))

        self.assertEqual(
            ['<var> := <var> ; <assgn>',
             '<var> := <var> ; <assgn> ; <stmt>',
             '<assgn> ; <var> := <var>',
             '<assgn> ; <assgn> ; <var> := <var>'],
            list(map(str, results))
        )

    def test_wrap_tinyc_assignment(self):
        tree = DerivationTree.from_parse_tree(
            ('<expr>', [('<id>', None), ('<mwss>', None), ('=', []), ('<mwss>', None), ('<expr>', None)]))
        result = wrap_in_tree_starting_in(
            "<term>", tree, tinyc.TINYC_GRAMMAR, GrammarGraph.from_grammar(tinyc.TINYC_GRAMMAR))
        self.assertTrue(result.find_node(tree))

    # Test deactivated: Should assert that no prefix trees are generated. The implemented
    # check in insert_tree, however, was too expensive for the JSON examples. Stalling for now.
    # def test_insert_var(self):
    #    tree = ('<start>', [('<stmt>', [('<assgn>', None), ('<stmt>', None)])])
    #
    #    results = insert_tree(canonical(LANG_GRAMMAR),
    #                          DerivationTree("<var>", None),
    #                          DerivationTree.from_parse_tree(tree))
    #
    #    print(list(map(str, results)))
    #    self.assertEqual(
    #        ['<var> := <rhs><stmt>',
    #         '<assgn><var> := <rhs>',
    #         '<var> := <rhs> ; <assgn><stmt>',
    #         '<assgn> ; <var> := <rhs> ; <assgn><stmt>',
    #         '<assgn><var> := <rhs> ; <stmt>',
    #         '<assgn><assgn> ; <var> := <rhs> ; <stmt>'],
    #        list(map(str, results))
    #    )


if __name__ == '__main__':
    unittest.main()
