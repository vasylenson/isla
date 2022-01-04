import string
import subprocess
import tempfile
from subprocess import PIPE
from typing import cast, Union, Optional, IO

import z3
from fuzzingbook.Grammars import srange

from isla import isla
from isla import isla_shortcuts as sc

# Kartik Talwar. Tiny-C Compiler. https://gist.github.com/KartikTalwar/3095780.

TINYC_GRAMMAR = {
    "<start>": ["<statement>"],
    "<statement>": [
        "if<paren_expr> <statement>",
        "if<paren_expr> <statement> else <statement>",
        "while<paren_expr> <statement>",
        "do <statement> while<paren_expr>;",
        "{<statements>}",
        "<expr>;",
        ";"
    ],
    "<statements>": ["", "<statement><statements>"],
    "<paren_expr>": ["(<expr>)"],
    "<expr>": [
        "<test>",
        "<id> = <expr>"
    ],
    "<test>": [
        "<sum>",
        "<sum> < <sum>"
    ],
    "<sum>": [
        "<term>",
        "<sum> + <term>",
        "<sum> - <term>"
    ],
    "<term>": [
        "<id>",
        "<int>",
        "<paren_expr>"
    ],
    "<id>": srange(string.ascii_lowercase),
    "<int>": [
        "<digit>",
        "<digit_nonzero><digits>"
    ],
    "<digits>": [
        "<digit>",
        "<digit><int>"
    ],
    "<digit>": srange(string.digits),
    "<digit_nonzero>": list(set(srange(string.digits)) - {"0"}),
}

# TODO: Scoping information
mgr = isla.VariableManager(TINYC_GRAMMAR)
TINYC_DEF_BEFORE_USE_CONSTRAINT = mgr.create(sc.forall(
    mgr.bv("$test", "<test>"),
    mgr.const("$start", "<start>"),
    sc.forall(
        mgr.bv("$id_1", "<id>"),
        mgr.bv("$test"),
        sc.exists_bind(
            mgr.bv("$id_2", "<id>") + " = <expr>",
            mgr.bv("$expr", "<expr>"),
            mgr.const("$start"),
            sc.before(mgr.bv("$expr"), mgr.bv("$test")) &
            mgr.smt(cast(z3.BoolRef, mgr.bv("$id_1").to_smt() == mgr.bv("$id_2").to_smt()))
        )
    )
))


# TINYC_GRAMMAR = {
#     "<start>": ["<statements>"],
#     "<statements>": [
#         "<statement>\n<statement>",
#         "<statement>\n<statements>"
#     ],
#     "<statement>": [
#         "<id> = <expr>;"
#     ],
#     "<expr>": ["<test>", ],
#     "<test>": ["<sum>", ],
#     "<sum>": ["<term>", ],
#     "<term>": ["<id>", "<int>", ],
#     "<id>": srange(string.ascii_lowercase),
#     "<int>": ["<digit>", ],
#     "<digit>": srange(string.digits),
# }
#
# mgr = isla.VariableManager()
# TINYC_DEF_BEFORE_USE_CONSTRAINT = mgr.create(sc.forall(
#     mgr.bv("$test", "<test>"),
#     mgr.const("$start", "<start>"),
#     sc.forall(
#         mgr.bv("$id_1", "<id>"),
#         mgr.bv("$test"),
#         sc.exists_bind(
#             mgr.bv("$id_2", "<id>") + " = <expr>;",
#             mgr.bv("$expr", "<statement>"),
#             mgr.const("$start"),
#             sc.before(mgr.bv("$expr"), mgr.bv("$test")) &
#             mgr.smt(cast(z3.BoolRef, mgr.bv("$id_1").to_smt() == mgr.bv("$id_2").to_smt()))
#         )
#     )
# ))


def compile_tinyc_clang(tree: isla.DerivationTree, outfile: Optional[IO] = None) -> Union[bool, str]:
    vars = set([str(assignment.children[0])
                for _, assignment in tree.filter(lambda node: node.value == "<expr>" and len(node.children) == 5)])
    contents = "int main() {\n"
    contents += "\n".join([f"    int {v};" for v in vars])
    contents += "\n" + str(tree).replace("\n", "    \t")
    contents += "\n" + "}"

    with tempfile.NamedTemporaryFile(suffix=".c") as tmp, tempfile.NamedTemporaryFile(suffix=".out") as _outfile:
        the_outfile = outfile or _outfile
        tmp.write(contents.encode())
        tmp.flush()
        cmd = ["clang", tmp.name, "-o", the_outfile.name]
        process = subprocess.Popen(cmd, stderr=PIPE)
        (stdout, stderr) = process.communicate(timeout=2)
        exit_code = process.wait()

        return True if exit_code == 0 else stderr.decode("utf-8")