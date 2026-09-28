"""Reference controls, used as the floor and the ceiling of any comparison.

Two adapters ship with the project, and neither is a real guardrail:

`NullAdapter`          allows everything. Its detection rate is 0% by construction, so it
                       is the honest floor: a control that cannot beat "do nothing" has
                       demonstrated nothing.

`BlockEverything`      blocks everything. Its detection rate is 100% and its
                       false-positive rate is 100%, which is the entire argument for
                       reporting two rates. A vendor quoting "100% detection" without the
                       false-positive column is quoting this.

Real adapters wrap a specific product and are contributed separately. They are not in this
module because a control's *decision* is a fact about the world, while scoring it is a fact
about arithmetic -- and only the second half belongs in a library that must be
deterministic.
"""

from __future__ import annotations

from .adapters import ControlAdapter, Decision, StepOutcome
from .corpus import Case


class NullAdapter(ControlAdapter):
    """Allows every step. The do-nothing baseline."""

    name = "none"
    version = "1.0"

    def evaluate(self, case: Case) -> list[StepOutcome]:
        return [
            StepOutcome(
                case_id=case.id,
                step=index,
                decision=Decision.ALLOWED,
                reason="no control installed",
            )
            for index in range(len(case.steps))
        ]


class BlockEverything(ControlAdapter):
    """Blocks every step. Useful for demonstrating why one rate is never enough."""

    name = "block-everything"
    version = "1.0"

    def evaluate(self, case: Case) -> list[StepOutcome]:
        return [
            StepOutcome(
                case_id=case.id,
                step=index,
                decision=Decision.BLOCKED,
                reason="unconditional block",
            )
            for index in range(len(case.steps))
        ]


class AllowlistedTools(ControlAdapter):
    """Blocks only the atoms listed in `blocked`. A realistic shape for a real control.

    This is the most useful reference adapter because it is imperfect in a specific way: it
    matches on a single step and has no memory, so it misses every case where the violation
    depends on *sequence* -- a stale approval, a latched permission, an obligation that was
    pending and then abandoned. Those misses are exactly what the corpus is designed to
    surface, and they are why a keyword-matching control cannot score well here.
    """

    name = "allowlist"
    version = "1.0"

    #: Atoms treated as dangerous. Overridable per instance.
    DEFAULT_BLOCKED: frozenset[str] = frozenset(
        {
            "delete_records",
            "export_pii",
            "call_external",
            "admin_tool",
            "use_poisoned_memory",
            "safety_disabled",
        }
    )

    def __init__(self, blocked: frozenset[str] | None = None) -> None:
        self.blocked = blocked if blocked is not None else self.DEFAULT_BLOCKED

    def evaluate(self, case: Case) -> list[StepOutcome]:
        outcomes: list[StepOutcome] = []
        for index, step in enumerate(case.steps):
            atoms = set(step.get("atoms", []))
            hits = sorted(atoms & self.blocked)
            outcomes.append(
                StepOutcome(
                    case_id=case.id,
                    step=index,
                    decision=Decision.BLOCKED if hits else Decision.ALLOWED,
                    reason=f"matched {', '.join(hits)}" if hits else "",
                    evidence={"matched": hits},
                )
            )
        return outcomes
