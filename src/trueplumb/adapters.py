"""Control adapters: run a corpus through a real guardrail and record what it did.

A verifier alone answers "did this trace satisfy this policy". That is a correctness
statement about one trace, and it says nothing about whether a guardrail *works* -- which
is the product claim. To make that claim you need three more things:

1. something that presents a trace to a control and records its decision
2. a way to compare controls on the same corpus
3. statistics that say whether the difference is real

This module supplies (1). The design constraint is that the adapter layer must be the only
place vendor code is invoked, and it must record *decisions*, never *scores*. A control
that returns a confidence number is not trusted for it: a vendor's own 0.97 is
unverifiable, and the entire premise of the project is that vendor claims are what needs
checking. Adapters record "blocked" or "allowed", plus whatever the control emitted as
evidence, and the scoring is done here from counts.

Adapters deliberately do not execute the control. A guardrail is arbitrary third-party
code; running it is the caller's job, in whatever sandbox they trust. TruePlumb defines the
interface and does the arithmetic, and stays out of the execution path.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .corpus import Case
from .stats import Interval, PairedResult, mcnemar_exact, wilson_interval


class Decision(Enum):
    """What a control did with a step.

    Three values rather than two, because "allowed" and "not reached" are different
    results and collapsing them inflates a false-negative rate. A control that only ever
    sees the first three steps of a trace has not "allowed" the dangerous step -- it has
    never been exposed to it.
    """

    ALLOWED = "allowed"
    BLOCKED = "blocked"
    NOT_REACHED = "not_reached"

    @property
    def is_denial(self) -> bool:
        return self is Decision.BLOCKED


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """One control's response to one step of a trace."""

    case_id: str
    step: int
    decision: Decision
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "step": self.step,
            "decision": self.decision.value,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class ControlRun:
    """A control's complete result over a corpus.

    ``outcomes`` is keyed by case id then step index, so a partial run is representable and
    a missing entry is detectable rather than silently read as "allowed".
    """

    name: str
    version: str
    outcomes: dict[str, list[StepOutcome]] = field(default_factory=dict)
    notes: str = ""

    def decision_for(self, case_id: str, step: int) -> Decision:
        """The control's decision, or NOT_REACHED if it never got there.

        Defaulting a missing entry to NOT_REACHED rather than ALLOWED is the safe
        direction: an incomplete run must never be read as evidence that the control
        permitted something.
        """
        steps = self.outcomes.get(case_id)
        if steps is None:
            return Decision.NOT_REACHED
        for outcome in steps:
            if outcome.step == step:
                return outcome.decision
        return Decision.NOT_REACHED

    def blocked_anywhere(self, case_id: str) -> bool:
        return any(o.decision.is_denial for o in self.outcomes.get(case_id, ()))

    def caught_case(self, case: Case) -> bool:
        """Did this control block a case that should have been blocked?

        Only meaningful for cases whose specification is a violation. For a compliant case
        the question is different -- see `false_positive`.
        """
        if case.expect != "violation":
            raise ValueError(f"{case.id} is a compliant case; use false_positive()")
        return self.blocked_anywhere(case.id)

    def false_positive(self, case: Case) -> bool:
        """Did this control block a case that should have been allowed through?

        A control that blocks everything scores 100% detection and is useless. This is the
        half of the measurement that stops that from looking like success.
        """
        if case.expect != "compliant":
            raise ValueError(f"{case.id} is a violation case; use caught_case()")
        return self.blocked_anywhere(case.id)


class ControlAdapter(abc.ABC):
    """Interface for evaluating a real guardrail against the corpus.

    Implementations wrap a specific product. The contract is deliberately small: hand the
    adapter a case, get back one `StepOutcome` per step. Anything the control exposes beyond
    that -- logs, scores, traces -- goes in `evidence` and is never interpreted here.

    A `name` and `version` are mandatory in practice. A conformance claim without a version
    is not reproducible, because the thing being measured can change underneath it.
    """

    #: Stable identifier, used as the key in comparisons and reports.
    name: str

    #: Version string of the control under test. Required for the claim to be reproducible.
    version: str

    @abc.abstractmethod
    def evaluate(self, case: Case) -> list[StepOutcome]:
        """Return one outcome per step of ``case``.

        Implementations should return an outcome for every step, marking steps the control
        never reached as `NOT_REACHED`. Returning a short list is allowed; the missing
        entries are then treated as `NOT_REACHED` rather than as permission.
        """

    def describe(self) -> str:
        return f"{self.name} {self.version}"


@dataclass(frozen=True, slots=True)
class ControlScore:
    """Scored performance of one control on one corpus."""

    name: str
    version: str
    detection: Interval
    false_positive: Interval
    n_violation_cases: int
    n_compliant_cases: int
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "detection": self.detection.as_dict(),
            "false_positive": self.false_positive.as_dict(),
            "n_violation_cases": self.n_violation_cases,
            "n_compliant_cases": self.n_compliant_cases,
            "notes": self.notes,
        }

    def render(self) -> str:
        lines = [
            f"{self.name} {self.version}",
            f"  detection       {self.detection.render()}"
            f"   ({self.detection.successes}/{self.detection.trials})",
            f"  false positives {self.false_positive.render()}"
            f"   ({self.false_positive.successes}/{self.false_positive.trials})",
        ]
        if self.notes:
            lines.append(f"  notes           {self.notes}")
        return "\n".join(lines)


def score_control(
    adapter: ControlAdapter,
    cases: list[Case],
    confidence: float = 0.95,
) -> ControlScore:
    """Run a control over a corpus and compute detection and false-positive rates.

    Detection is measured over cases whose specification is a violation; the
    false-positive rate over cases that should pass. They are kept separate because a single
    combined "accuracy" number is the thing that lets a control look good by blocking
    indiscriminately.
    """
    run = ControlRun(
        name=adapter.name,
        version=adapter.version,
        outcomes={case.id: adapter.evaluate(case) for case in cases},
    )

    violation_cases = [c for c in cases if c.expect == "violation"]
    compliant_cases = [c for c in cases if c.expect == "compliant"]

    caught = sum(1 for case in violation_cases if run.caught_case(case))
    false_pos = sum(1 for case in compliant_cases if run.false_positive(case))

    return ControlScore(
        name=adapter.name,
        version=adapter.version,
        detection=wilson_interval(caught, len(violation_cases), confidence),
        false_positive=wilson_interval(false_pos, len(compliant_cases), confidence),
        n_violation_cases=len(violation_cases),
        n_compliant_cases=len(compliant_cases),
    )


def compare_controls(
    a: ControlAdapter,
    b: ControlAdapter,
    cases: list[Case],
    alpha: float = 0.05,
) -> PairedResult:
    """McNemar comparison of two controls over the same corpus.

    Only violation cases count. A false-positive difference is a real difference too, but
    mixing the two into one test would answer "are these different" without answering
    "which is better", and a control that blocks everything wins on detection while being
    useless in practice.

    The corpus is the same for both, so the test is paired. Each violation case contributes
    to exactly one of `a_only` (A caught, B missed) or `b_only`.
    """
    violation_cases = [c for c in cases if c.expect == "violation"]

    a_only = b_only = 0
    for case in violation_cases:
        a_caught = ControlRun(a.name, a.version, {case.id: a.evaluate(case)}).caught_case(case)
        b_caught = ControlRun(b.name, b.version, {case.id: b.evaluate(case)}).caught_case(case)
        if a_caught and not b_caught:
            a_only += 1
        elif b_caught and not a_caught:
            b_only += 1

    return mcnemar_exact(a_only, b_only, n_total=len(violation_cases), alpha=alpha)
