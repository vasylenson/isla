import string
from typing import cast

import z3
from fuzzingbook.GrammarCoverageFuzzer import GrammarCoverageFuzzer
from fuzzingbook.Grammars import srange

from input_constraints.isla import VariableManager

from input_constraints import isla_shortcuts as sc
from input_constraints.solver import ISLaSolver

REST_GRAMMAR = {
    "<start>": ["<body-elements>"],
    "<body-elements>": ["", "<body-element>\n<body-elements>"],
    "<body-element>": [
        "<section-title>\n",
    ],
    "<section-title>": ["<nobr-string>\n<underline>"],
    "<nobr-string>": ["<nobr-char>", "<nobr-char><nobr-string>"],
    "<nobr-char>": list(set(srange(string.printable)) - {"\n", "\r"}),
    "<underline>": ["<eqs>", "<dashes>"],
    "<eqs>": ["=", "=<eqs>"],
    "<dashes>": ["-", "-<dashes>"],
}

mgr = VariableManager()
length_underline = mgr.create(
    mgr.smt(z3.StrToInt(mgr.num_const("$length").to_smt()) > z3.IntVal(1)) &
    mgr.smt(z3.StrToInt(mgr.num_const("$length").to_smt()) < z3.IntVal(10)) &
    sc.forall_bind(
        mgr.bv("$titletxt", "<nobr-string>") + "\n" + mgr.bv("$underline", "<underline>"),
        mgr.bv("$title", "<section-title>"),
        mgr.const("$start", "<start>"),
        mgr.smt(cast(z3.BoolRef, z3.Length(mgr.bv("$titletxt").to_smt()) == z3.StrToInt(mgr.num_const("$length").to_smt()))) &
        mgr.smt(cast(z3.BoolRef, z3.Length(mgr.bv("$underline").to_smt()) >= z3.StrToInt(mgr.num_const("$length").to_smt())))
    )
)

rest_constraints = length_underline