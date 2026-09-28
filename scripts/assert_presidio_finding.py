"""Assert the one real-product finding this repository makes is still true.

`presidio-payload` scores high detection *and* higher false positives: it blocks more
legitimate traffic than it catches attacks. That is the whole argument for reporting two
rates, demonstrated against a commercial product rather than against a deliberately useless
baseline. If it ever stops holding, either the product changed or the measurement did, and
in both cases the documentation is now wrong.

This is a CI assertion, not a measurement. It does not re-run Presidio -- that needs a
sandbox -- so it re-derives the claim from the committed log and the corpus. If the product
is upgraded, re-record the log and update the assertions here in the same commit, which is
the moment a human is forced to look at the new numbers.

Run it directly:

    python scripts/assert_presidio_finding.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trueplumb.adapters import score_control  # noqa: E402
from trueplumb.corpus import load_corpus  # noqa: E402
from trueplumb.presidio import PresidioAdapter  # noqa: E402

CORPUS_PATH = "corpus/agent_safety_baseline.json"
PAYLOAD_LOG = "measurements/presidio-log-payload.json"
ATOM_LOG = "measurements/presidio-log.json"


def main() -> int:
    cases = list(load_corpus(CORPUS_PATH).cases)
    payload = score_control(PresidioAdapter(PAYLOAD_LOG, rendering="payload"), cases)
    atom = score_control(PresidioAdapter(ATOM_LOG), cases)

    # The two rates must never be merged. A report carrying one "accuracy" field is exactly
    # the regression this project was built to make impossible.
    report = payload.as_dict()
    for banned in ("accuracy", "score"):
        assert banned not in report, f"the report grew a {banned!r} field: {sorted(report)}"
    assert "detection" in report
    assert "false_positive" in report

    # The headline claim.
    assert payload.false_positive.point > payload.detection.point, (
        "presidio-payload no longer blocks more legitimate traffic than it catches attacks; "
        f"detection={payload.detection.point:.3f} "
        f"false_positive={payload.false_positive.point:.3f}"
    )
    assert not payload.false_positive.contains(0.0), (
        "the false-positive interval now includes zero, so the control is no longer "
        "demonstrably unusable and the docs need rewriting"
    )

    # The control is beatable and not merely a block-everything, which is what CONTRIBUTING
    # requires of every adapter. Both bounds matter: zero detection means the adapter is
    # broken, saturation means the corpus cannot tell it from the useless baseline.
    assert 0.0 < payload.detection.point < 1.0, payload.detection.as_dict()
    assert payload.false_positive.point < 1.0, payload.false_positive.as_dict()

    # The corpus-format finding: the same product, handed atom names instead of payloads,
    # detects nothing at all. If this equalises, the two renderings have converged and the
    # argument in ARCHITECTURE.md section 8 no longer holds.
    assert atom.detection.successes == 0, (
        f"the atom rendering now detects {atom.detection.successes}; the trace-format "
        "finding in the docs is stale"
    )
    assert payload.detection.successes > atom.detection.successes

    print(
        f"presidio-payload  detection {payload.detection.point:.3f} "
        f"({payload.detection.successes}/{payload.detection.trials})   "
        f"false positives {payload.false_positive.point:.3f} "
        f"({payload.false_positive.successes}/{payload.false_positive.trials})"
    )
    print(
        f"presidio          detection {atom.detection.point:.3f}   "
        f"false positives {atom.false_positive.point:.3f}"
    )
    print("PASS: the false-positive half is still visible, and no single rate replaced it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
