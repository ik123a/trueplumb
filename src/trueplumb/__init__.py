"""TruePlumb -- vendor-neutral conformance testbed for AI agent safety controls.

The verification core is deterministic by construction: no LLM calls, no network, no
model weights. A verdict computed here is reproducible on any machine.
"""

from __future__ import annotations

__version__ = "0.1.0.dev0"

from .adapters import (
    ControlAdapter,
    ControlRun,
    ControlScore,
    Decision,
    StepOutcome,
    compare_controls,
    score_control,
)
from .corpus import Case, Corpus, CorpusError, load_corpus
from .ltl.ast import Atom, Formula
from .ltl.automata import DFA, to_dfa
from .ltl.parser import ParseError, parse
from .ltl.trace import CheckResult, TraceEvent, check, load_trace
from .stats import Interval, PairedResult, StatsError, mcnemar_exact, wilson_interval

__all__ = [
    "Atom",
    "Case",
    "CheckResult",
    "ControlAdapter",
    "ControlRun",
    "ControlScore",
    "Corpus",
    "CorpusError",
    "DFA",
    "Decision",
    "Formula",
    "Interval",
    "PairedResult",
    "ParseError",
    "StatsError",
    "StepOutcome",
    "TraceEvent",
    "__version__",
    "check",
    "compare_controls",
    "load_corpus",
    "load_trace",
    "mcnemar_exact",
    "parse",
    "score_control",
    "to_dfa",
    "wilson_interval",
]
