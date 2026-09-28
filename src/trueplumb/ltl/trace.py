"""Trace checking: run a monitor over a recorded agent execution, extract a counterexample.

A counterexample is the minimal suffix of the trace that proves the policy failed, plus the
exact index where the obligation was discharged. That is the difference between "your
agent violated policy" and "your agent violated policy *here*, by calling ``refund`` on
step 14 without the human approval at step 13."
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ast import Formula
from .automata import DFA, to_dfa
from .automata import accepts as _accepts
from .parser import parse


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One recorded step of an agent execution."""

    index: int
    atoms: frozenset[str]
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], index: int) -> TraceEvent:
        atoms = payload.get("atoms")
        if atoms is None:
            derived = payload.get("events") or payload.get("labels") or []
            atoms = derived
        return cls(index=index, atoms=frozenset(str(a) for a in atoms), raw=payload)

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "atoms": sorted(self.atoms), "raw": self.raw}


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Verdict for one trace under one policy."""

    compliant: bool
    policy: str
    violation_index: int | None
    counterexample: tuple[TraceEvent, ...]
    states_visited: int
    total_steps: int
    monitor_stats: dict[str, int] = field(default_factory=dict)

    def explain(self) -> str:
        if self.compliant:
            return f"COMPLIANT: {self.policy} held for all {self.total_steps} steps."
        step = self.violation_index
        # One step per line rather than one long arrow chain: a 40-step counterexample
        # printed inline is unreadable in a terminal, and this string is what a developer
        # pastes into an issue.
        rendered = "\n".join(
            f"  step {e.index}: {', '.join(sorted(e.atoms)) or '(no atoms)'}"
            for e in self.counterexample
        )
        return (
            f"VIOLATION: {self.policy} first fails at step {step}.\n"
            f"Counterexample ({len(self.counterexample)} step(s)):\n"
            f"{rendered}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "compliant": self.compliant,
            "policy": self.policy,
            "violation_index": self.violation_index,
            "counterexample": [e.to_dict() for e in self.counterexample],
            "states_visited": self.states_visited,
            "total_steps": self.total_steps,
            "monitor_stats": self.monitor_stats,
        }


def load_trace(path: str | Path) -> list[TraceEvent]:
    """Read a JSONL trace file, one event per line."""
    events: list[TraceEvent] = []
    with open(path, encoding="utf-8") as handle:
        for i, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            events.append(TraceEvent.from_dict(payload, i))
    return events


def check(
    formula: Formula | str,
    trace: Sequence[TraceEvent] | Iterable[TraceEvent],
    monitor: DFA | None = None,
) -> CheckResult:
    """Check ``trace`` against ``formula``, returning a verdict and counterexample.

    ``monitor`` lets a caller reuse a compiled policy across many traces -- the intended
    CI pattern, since construction dominates cost.
    """
    if isinstance(formula, str):
        formula = parse(formula)
        monitor = None

    if monitor is None:
        monitor = to_dfa(formula, formula_text=str(formula))

    events = list(trace)
    state = monitor.start
    visited: list[int] = [state]

    for event in events:
        state = monitor.step(state, set(event.atoms))
        visited.append(state)

    # The verdict is the residual judged over the empty suffix that follows the trace.
    # `accepting` stays on the monitor as a static over-approximation of the safe states
    # (useful for pruning and for inspecting the constructed automaton), but it is not the
    # verdict: acceptance can turn on vacuity, which no construction-time boolean captures.
    residual = monitor.residual_for(state)
    compliant = _accepts(residual)

    violation_index: int | None = None
    counterexample: tuple[TraceEvent, ...] = ()

    if not compliant:
        # Find the first position at which the policy stopped holding. Everything from
        # there to the end is the counterexample, and that slice is itself a violating
        # trace, so a developer can replay exactly the part that broke the policy.
        for i in range(len(events)):
            prefix_state = monitor.start
            for e in events[: i + 1]:
                prefix_state = monitor.step(prefix_state, set(e.atoms))
            if not _accepts(monitor.residual_for(prefix_state)):
                counterexample = tuple(events[i:])
                violation_index = events[i].index
                break
        else:
            # The full trace fails but no proper prefix does, so the obligation could not
            # be discharged at all -- the whole trace is the evidence.
            counterexample = tuple(events)
            violation_index = events[0].index if events else 0

    return CheckResult(
        compliant=compliant,
        policy=monitor.formula_text,
        violation_index=violation_index,
        counterexample=counterexample,
        states_visited=len(set(visited)),
        total_steps=len(events),
        monitor_stats=dict(monitor.build_stats),
    )
