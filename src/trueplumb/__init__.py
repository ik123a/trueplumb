"""TruePlumb -- vendor-neutral conformance testbed for AI agent safety controls.

The verification core is deterministic by construction: no LLM calls, no network, no
model weights. A verdict computed here is reproducible on any machine.
"""

from __future__ import annotations

__version__ = "0.1.0.dev0"

from .ltl.ast import Atom, Formula
from .ltl.automata import DFA, to_dfa
from .ltl.parser import ParseError, parse
from .ltl.trace import CheckResult, TraceEvent, check, load_trace

__all__ = [
    "Atom",
    "CheckResult",
    "DFA",
    "Formula",
    "ParseError",
    "TraceEvent",
    "__version__",
    "check",
    "load_trace",
    "parse",
    "to_dfa",
]
