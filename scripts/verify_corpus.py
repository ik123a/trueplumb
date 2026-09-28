"""Verify a corpus file against the engine.

Run it directly:

    python scripts/verify_corpus.py corpus/agent_safety_baseline.json
    python scripts/verify_corpus.py corpus/            # every .json in the directory

Exit code is 0 only if every case matches its specification.

Each case states the verdict it requires. The engine is then run and the two are compared,
so a disagreement means either a real semantics bug or a wrong expectation in the corpus.
Both are worth catching, which is why a mismatch is always reported with enough context to
decide which it is -- and why `violation_at` is pinned wherever the exact step matters.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trueplumb.corpus import CorpusError, load_corpus  # noqa: E402


def main(argv: list[str]) -> int:
    targets: list[Path] = []
    for arg in argv or ["corpus"]:
        path = Path(arg)
        targets.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])

    if not targets:
        print("no corpus files found")
        return 1

    total_cases = 0
    all_failures: list[str] = []

    for target in targets:
        try:
            corpus = load_corpus(target)
        except CorpusError as exc:
            print(f"FAIL  {target}: {exc}")
            all_failures.append(f"{target}: {exc}")
            continue

        failures = corpus.verify_all()
        total_cases += len(corpus.cases)

        status = "FAIL" if failures else "ok"
        print(
            f"{status:4s}  {target.name}  "
            f"({len(corpus.cases)} cases, schema {corpus.schema_version})"
        )
        for failure in failures:
            print(f"        - {failure}")

        by_attack = corpus.by_attack()
        if not failures:
            print(f"        categories covered: {len(by_attack)}")
            for category, cases in sorted(by_attack.items()):
                violating = sum(1 for c in cases if c.expect == "violation")
                pinned = sum(1 for c in cases if c.violation_at is not None)
                detail = f"{len(cases)} ({violating} must-fail"
                detail += f", {pinned} step-pinned)" if pinned else ")"
                print(f"          {category}: {detail}")

            # The strict pass only covers cases that pin a step, and it is where a wrong
            # expectation shows up most sharply.
            strict = corpus.strict_failures()
            if strict:
                for failure in strict:
                    print(f"        STRICT FAIL - {failure}")
                all_failures.extend(strict)
            else:
                pinned_total = sum(1 for c in corpus.cases if c.violation_at is not None)
                print(f"        strict index check: {pinned_total} pinned case(s) verified")

        all_failures.extend(failures)

    print()
    if all_failures:
        print(f"FAILED: {len(all_failures)} case(s) across {total_cases} checked")
        return 1

    print(f"PASS: all {total_cases} cases match their expected verdicts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
