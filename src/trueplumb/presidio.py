"""Control adapter for Microsoft Presidio, Microsoft's PII detection library.

What this is
------------

The first adapter in the project that wraps a real commercial product rather than a
reference control, and the shape is worth reading before writing another one.

Three rules from `CONTRIBUTING.md` are load-bearing here rather than decorative:

1. **It executes nothing.** There is no ``import presidio_analyzer`` in this file, and there
   must never be one. Presidio drags in spaCy, a statistical model and a table of regexes;
   a conformance tool that imports the thing it is measuring has no independent verdict
   left to give. `scripts/record_presidio.py` runs the product in a sandbox the caller
   chooses and writes what it found to a JSON log; this module only reads that log.

2. **It records decisions, never scores.** Presidio returns a confidence per detected
   entity -- 0.4 for a bare phone-shaped string, 1.0 for a clean email match. That number
   is a vendor's opinion about its own output, and trusting it is precisely what this
   project exists to stop people doing. So the *decision* uses only the fact that Presidio
   reported an entity of an accepted type, and the score travels to
   `StepOutcome.evidence["vendor_scores"]` where no arithmetic ever reads it. There is a
   test that rewrites every score in the log and asserts the verdicts do not move.

3. **Unseen steps are `NOT_REACHED`, never `ALLOWED`.** Presidio is invoked per step. In an
   enforcing deployment the first block aborts the run, so later steps are never presented
   to it. A step the control never saw has not been permitted, and reporting it as allowed
   would inflate the false-negative rate silently.

The decision rule, stated plainly
--------------------------------

    a step is BLOCKED  iff Presidio reported at least one entity of an accepted type
    a step is ALLOWED  iff Presidio was run on it and reported nothing
    a step is NOT_REACHED  iff the run halted before it, or the log has no record of it

A detection also halts the run. That is the *testbed's* enforcement policy, not Presidio's
behaviour -- Presidio is a detector and halts nothing. It is modelled here because a
guardrail in front of tool execution really does stop the agent, and because modelling the
halt is what makes `NOT_REACHED` observable rather than vacuous.

On the two renderings
---------------------

`measurements/presidio-log.json` and `measurements/presidio-log-payload.json` differ only in
how a corpus step becomes text, and the difference between their scores is the most useful
thing in this repository right now. A trace records *that* the agent exported customer
records; Presidio reads *text*; the `atom` rendering gives it the first and the `payload`
rendering gives it the second. Nothing about Presidio changed between those two runs. See
`docs/ARCHITECTURE.md` for what that says about the corpus, and `METHODOLOGY.md` §6 on why
"the payload was not in the trace" is a statement about the trace rather than about the
control.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .adapters import ControlAdapter, Decision, StepOutcome
from .corpus import Case

#: Log schema this adapter understands. Bumping it means the harness changed shape.
LOG_SCHEMA_VERSION = "1.0"

#: Registry names for the two supported renderings. The name encodes the rendering because
#: a score without its rendering is not a result, it is a rumour.
RENDERING_NAMES = {"atom": "presidio", "payload": "presidio-payload"}


class PresidioLogError(ValueError):
    """The vendor log is missing, malformed, or not the one this adapter was asked to read."""


def _load_log(path: Path, expected_rendering: str) -> dict[str, Any]:
    """Read and validate a Presidio decision log.

    Validation is strict on purpose. A log that is subtly the wrong one -- a stale
    `payload` run read as if it were the neutral baseline, a log from a different control --
    produces confident, well-formatted, wrong numbers. There is no way for a reader to tell
    that from the output, so it is rejected at the door instead.
    """
    if not path.is_file():
        raise PresidioLogError(
            f"no Presidio log at {path}. The adapter never runs the product itself; produce "
            "the log first with:\n"
            "  python scripts/record_presidio.py --corpus corpus/ "
            f"--out {path} --rendering {expected_rendering}\n"
            "run in a sandbox with presidio-analyzer installed."
        )

    try:
        log = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PresidioLogError(f"{path}: invalid JSON: {exc}") from exc

    if not isinstance(log, dict):
        raise PresidioLogError(f"{path}: top level must be an object")
    if log.get("schema_version") != LOG_SCHEMA_VERSION:
        raise PresidioLogError(
            f"{path}: log schema_version must be {LOG_SCHEMA_VERSION!r}, "
            f"got {log.get('schema_version')!r}"
        )

    rendering = str(log.get("rendering", ""))
    if rendering != expected_rendering:
        raise PresidioLogError(
            f"{path}: this is a {rendering!r} log but the {expected_rendering!r} adapter was "
            f"asked to read it. Scores from the two renderings are not comparable, so this "
            "is refused rather than silently mislabelled."
        )

    if "cases" not in log or not isinstance(log["cases"], dict):
        raise PresidioLogError(f"{path}: log has no 'cases' object")
    if not str(log.get("product_version", "")).strip():
        raise PresidioLogError(
            f"{path}: log has no product_version. A conformance claim without a version is "
            "not reproducible, and the adapter refuses to report one."
        )
    return log


class PresidioAdapter(ControlAdapter):
    """Translate recorded Presidio findings into TruePlumb decisions.

    ``entity_types`` narrows which findings count as a block, and defaults to `None` --
    every entity Presidio reported counts. The widest setting is the default on purpose:
    a testbed that scores a vendor should give it its best reasonable shot, and a default
    tuned to flatter the measured product would make every number here untrustworthy. Every
    score reported in the docs uses this default.
    """

    def __init__(
        self,
        log_path: str | Path,
        rendering: str = "atom",
        entity_types: frozenset[str] | None = None,
    ) -> None:
        if rendering not in RENDERING_NAMES:
            raise PresidioLogError(
                f"unknown rendering {rendering!r}; expected one of {sorted(RENDERING_NAMES)}"
            )

        self.log_path = Path(log_path)
        self.rendering = rendering
        self.entity_types = entity_types
        self.log = _load_log(self.log_path, rendering)

        # Instance attributes, not class attributes: the product version comes from the log
        # rather than being asserted here, so a report cannot claim a version nobody ran.
        self.name = RENDERING_NAMES[rendering]
        self.version = str(self.log["product_version"])

    @property
    def spacy_model(self) -> str:
        return str(self.log.get("spacy_model", "unknown"))

    def describe(self) -> str:
        """Identify the product, the model, and the rendering, since all three matter."""
        return f"{self.name} {self.version} (spacy {self.spacy_model}, rendering={self.rendering})"

    def _counts_towards_block(self, entity: dict[str, Any]) -> bool:
        if self.entity_types is None:
            return True
        return str(entity.get("entity_type", "")) in self.entity_types

    def evaluate(self, case: Case) -> list[StepOutcome]:
        recorded: dict[str, Any] = self.log["cases"].get(case.id, {})
        outcomes: list[StepOutcome] = []
        halted_at: int | None = None

        for index in range(len(case.steps)):
            if halted_at is not None:
                outcomes.append(
                    StepOutcome(
                        case_id=case.id,
                        step=index,
                        decision=Decision.NOT_REACHED,
                        reason=(
                            f"run halted at step {halted_at}; this step was never presented "
                            "to the control"
                        ),
                    )
                )
                continue

            record = recorded.get(str(index))
            if record is None:
                # A step with no record was never shown to Presidio. The run's state from
                # here on is unknown, so nothing after it can be read as a decision.
                halted_at = index
                outcomes.append(
                    StepOutcome(
                        case_id=case.id,
                        step=index,
                        decision=Decision.NOT_REACHED,
                        reason=(
                            "no record in the vendor log: the control was never shown this "
                            "step, so it neither permitted nor blocked anything"
                        ),
                    )
                )
                continue

            found = [e for e in record.get("entities", []) if self._counts_towards_block(e)]

            if not found:
                outcomes.append(
                    StepOutcome(
                        case_id=case.id,
                        step=index,
                        decision=Decision.ALLOWED,
                        reason="presidio reported no entity of an accepted type",
                        evidence={
                            "analyzed_text": record.get("text", ""),
                            "ignored_entities": [
                                str(e.get("entity_type", "")) for e in record.get("entities", [])
                            ],
                        },
                    )
                )
                continue

            halted_at = index
            outcomes.append(
                StepOutcome(
                    case_id=case.id,
                    step=index,
                    decision=Decision.BLOCKED,
                    reason=(
                        f"presidio reported {len(found)} accepted entit"
                        f"{'y' if len(found) == 1 else 'ies'}"
                    ),
                    evidence={
                        "analyzed_text": record.get("text", ""),
                        "entity_types": [str(e.get("entity_type", "")) for e in found],
                        "recognizers": [str(e.get("recognizer", "")) for e in found],
                        # Recorded verbatim and read by nothing. Presidio's confidence in
                        # its own output is exactly the kind of claim this project measures
                        # rather than trusts.
                        "vendor_scores": [float(e.get("score", 0.0)) for e in found],
                    },
                )
            )

        return outcomes
