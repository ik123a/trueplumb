"""Check that every committed vendor decision log still matches the corpus it was run over.

A vendor log is a measurement, and a measurement recorded against a corpus that has since
changed is worse than no measurement: it keeps reporting confident numbers for cases that
no longer exist. Nothing in the adapter can detect that on its own -- the adapter reads the
log, not the corpus -- so this script does.

What it checks:

- Every case in the corpus has a record in the log, and the log has no case the corpus
  dropped. Both directions: a missing case means the run predates the corpus, and an extra
  one means the corpus shrank without re-recording.
- Every step of every case has an entry, and the entry carries the fields the adapter
  reads. A log truncated at step 2 of 4 would otherwise read as "Presidio allowed the rest".
- Every recorded entity is internally plausible: its score is a number in [0, 1], its
  offsets fall inside the text it annotates, its span is non-degenerate and covers
  non-whitespace, and it names a recognizer. This set was added after a real gap: a
  fabricated entity injected into a shipped log moved detection from 13/15 to 14/15 and
  this script passed it, because it checked only that the keys were *present*.
- No timestamp-shaped key exists anywhere, so the log stays byte-reproducible.

What it deliberately does *not* check is whether the scores are correct — only that the log
is internally consistent. Verifying the findings means re-running the product, which means a
presidio sandbox, which is deliberately not a CI dependency. The scores are a property of
the pinned product version recorded in the log, and re-recording is a manual step that
refreshes this file's view of the corpus.

Run it directly:

    python scripts/check_vendor_logs.py measurements/
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trueplumb.corpus import CorpusError, load_corpus  # noqa: E402

LOG_SCHEMA_VERSION = "1.0"
REQUIRED_LOG_FIELDS = ("control", "product_version", "spacy_model", "language", "rendering")
REQUIRED_STEP_FIELDS = ("text", "entities")
REQUIRED_ENTITY_FIELDS = ("entity_type", "score", "start", "end", "recognizer")

#: Fields whose presence would make the log non-reproducible. A timestamp means re-running
#: the harness produces a diff, so a diff can no longer be read as "the product changed".
FORBIDDEN_FIELDS = ("recorded", "recorded_at", "timestamp", "generated_at", "date", "run_at")


def keys_of(node: Any) -> set[str]:
    """Every dictionary key anywhere in the document, at any depth."""
    if isinstance(node, dict):
        return {str(k) for k in node} | set().union(*(keys_of(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(keys_of(v) for v in node)) if node else set()
    return set()


def _entity_problems(
    name: str, case_id: str, index: int, entity: dict[str, Any], text: str
) -> list[str]:
    """Validate one recorded entity against the text it claims to annotate.

    The adapter's whole decision comes from these records: an entity is what makes a step
    count as detected, so a fabricated or malformed one silently changes the reported rate.
    Demonstrated concretely -- injecting a single fake entity into the shipped log moved
    detection from 13/15 to 14/15, and the checker passed the result, because it only checked
    that the keys were *present* and never that they were *plausible*.

    This still does not prove a log is truthful. It proves the log is internally consistent,
    which is a strictly weaker and honestly-stated claim.
    """
    where = f"{name}: case {case_id!r} step {index} entity"
    problems: list[str] = []

    score = entity.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        problems.append(f"{where} 'score' is not a number")
    elif not 0.0 <= float(score) <= 1.0:
        problems.append(f"{where} 'score' is {score}, outside [0.0, 1.0]")

    start, end = entity.get("start"), entity.get("end")
    if not isinstance(start, int) or isinstance(start, bool):
        problems.append(f"{where} 'start' is not an integer")
    elif not 0 <= start < max(len(text), 1):
        problems.append(f"{where} 'start' is {start}, outside the text of length {len(text)}")
    if not isinstance(end, int) or isinstance(end, bool):
        problems.append(f"{where} 'end' is not an integer")
    elif not 0 <= end < max(len(text), 1):
        problems.append(f"{where} 'end' is {end}, outside the text of length {len(text)}")
    if isinstance(start, int) and isinstance(end, int) and not isinstance(start, bool):
        if end < start:
            problems.append(f"{where} 'end' ({end}) precedes 'start' ({start})")
        elif end == start:
            # A zero-width span annotates no text at all, yet the adapter counts a step as
            # detected on the strength of the entity list. Such a record is a detection
            # with nothing behind it, so it is rejected rather than silently scoring.
            problems.append(f"{where} has a zero-width span ({start}..{end}); it annotates nothing")
        elif text and not text[start:end].strip():
            # A non-empty span that covers only whitespace is the signature of a placeholder.
            problems.append(f"{where} spans {text[start:end]!r}, which is only whitespace")

    if not str(entity.get("recognizer", "")).strip():
        problems.append(f"{where} has no recognizer; a record with no provenance is not evidence")

    return problems


def check_log(log_path: Path, case_steps: dict[str, int]) -> list[str]:
    """Return a list of problems with one log. Empty means the log is consistent."""
    problems: list[str] = []
    name = log_path.name

    try:
        raw = log_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{name}: unreadable: {exc}"]

    try:
        log = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [f"{name}: invalid JSON: {exc}"]

    if not isinstance(log, dict):
        return [f"{name}: top level must be an object"]
    if log.get("schema_version") != LOG_SCHEMA_VERSION:
        problems.append(f"{name}: schema_version must be {LOG_SCHEMA_VERSION!r}")

    for field in REQUIRED_LOG_FIELDS:
        if not str(log.get(field, "")).strip():
            problems.append(f"{name}: missing or empty {field!r}")

    for field in FORBIDDEN_FIELDS:
        if field in keys_of(log):
            problems.append(
                f"{name}: contains {field!r}; the log must stay byte-reproducible so a diff "
                "means the product's behaviour changed"
            )

    cases = log.get("cases")
    if not isinstance(cases, dict):
        problems.append(f"{name}: 'cases' must be an object")
        return problems

    for case_id, expected_steps in case_steps.items():
        if case_id not in cases:
            problems.append(f"{name}: no record for case {case_id!r}; re-record the log")
            continue
        steps = cases[case_id]
        if not isinstance(steps, dict):
            problems.append(f"{name}: case {case_id!r} is not an object")
            continue
        for index in range(expected_steps):
            key = str(index)
            if key not in steps:
                problems.append(
                    f"{name}: case {case_id!r} has no record for step {index}; a truncated "
                    "log would read as the control allowing the remaining steps"
                )
                continue
            record = steps[key]
            if not isinstance(record, dict):
                problems.append(f"{name}: case {case_id!r} step {index} is not an object")
                continue
            for field in REQUIRED_STEP_FIELDS:
                if field not in record:
                    problems.append(f"{name}: case {case_id!r} step {index} has no {field!r}")
            text = record.get("text", "")
            if not isinstance(text, str):
                problems.append(f"{name}: case {case_id!r} step {index} 'text' is not a string")
                text = ""

            for entity in record.get("entities", []):
                if not isinstance(entity, dict):
                    problems.append(
                        f"{name}: case {case_id!r} step {index} has a non-object entity"
                    )
                    continue
                for field in REQUIRED_ENTITY_FIELDS:
                    if field not in entity:
                        problems.append(
                            f"{name}: case {case_id!r} step {index} entity has no {field!r}"
                        )
                problems.extend(_entity_problems(name, case_id, index, entity, text))

    for case_id in sorted(set(cases) - set(case_steps)):
        problems.append(
            f"{name}: records case {case_id!r}, which the corpus no longer contains; "
            "re-record the log"
        )

    return problems


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in (argv or ["measurements"])]

    case_steps: dict[str, int] = {}
    for corpus_file in sorted(Path("corpus").glob("*.json")):
        try:
            corpus = load_corpus(corpus_file)
        except CorpusError as exc:
            print(f"FAIL  {corpus_file}: {exc}")
            return 1
        for case in corpus.cases:
            case_steps[case.id] = len(case.steps)

    if not case_steps:
        print("no corpus cases found in corpus/")
        return 1

    logs: list[Path] = []
    for target in targets:
        logs.extend(sorted(target.glob("*.json")) if target.is_dir() else [target])
    if not logs:
        print(f"no vendor logs found in {targets}")
        return 1

    all_problems: list[str] = []
    for log_path in logs:
        problems = check_log(log_path, case_steps)
        print(
            f"{'FAIL' if problems else 'ok':4s}  {log_path.name}  "
            f"({len(case_steps)} corpus cases covered)"
        )
        for problem in problems:
            print(f"        - {problem}")
        all_problems.extend(problems)

    print()
    if all_problems:
        print(f"FAILED: {len(all_problems)} problem(s) across {len(logs)} vendor log(s)")
        print(
            "re-record with: python scripts/record_presidio.py --corpus corpus/ "
            "--out measurements/<name>.json --rendering <atom|payload>"
        )
        return 1

    print(f"PASS: {len(logs)} vendor log(s) cover all {len(case_steps)} corpus cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
