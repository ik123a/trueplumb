"""CLI tests.

The exit code is the CLI's real interface. A verifier wired into CI is only as trustworthy
as the guarantee that a violation fails the build, so the codes are asserted here rather
than left to manual testing:

    0  policy held
    1  policy violated
    2  bad input

Everything runs through Typer's CliRunner against the real command functions, so these
cover argument parsing and exit codes together -- testing them separately would let a
rename break the contract without any test noticing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trueplumb.cli import app

runner = CliRunner()

POLICY = "G(call_refund_api -> F(human_approved))"

COMPLIANT = """\
{"index": 0, "atoms": ["read_order"]}
{"index": 1, "atoms": ["call_refund_api", "human_approved"]}
{"index": 2, "atoms": ["send_receipt"]}
"""

VIOLATING = """\
{"index": 0, "atoms": ["read_order"]}
{"index": 1, "atoms": ["call_refund_api"]}
{"index": 2, "atoms": ["send_receipt"]}
"""

STALLED = """\
{"index": 0, "atoms": ["work"]}
{"index": 1, "atoms": ["work"]}
"""


@pytest.fixture()
def compliant(tmp_path: Path) -> Path:
    path = tmp_path / "compliant.jsonl"
    path.write_text(COMPLIANT, encoding="utf-8")
    return path


@pytest.fixture()
def violating(tmp_path: Path) -> Path:
    path = tmp_path / "violating.jsonl"
    path.write_text(VIOLATING, encoding="utf-8")
    return path


class TestExitCodes:
    def test_compliant_exits_zero(self, compliant: Path) -> None:
        result = runner.invoke(app, ["verify", POLICY, str(compliant)])
        assert result.exit_code == 0, result.output

    def test_violation_exits_one(self, violating: Path) -> None:
        result = runner.invoke(app, ["verify", POLICY, str(violating)])
        assert result.exit_code == 1, result.output

    def test_stalled_run_exits_one(self, tmp_path: Path) -> None:
        # The guarantee that matters most: an agent run that never finished must not be
        # able to report success. If this ever returns 0, the product thesis is broken.
        path = tmp_path / "stalled.jsonl"
        path.write_text(STALLED, encoding="utf-8")
        result = runner.invoke(app, ["verify", "F(agent_done)", str(path)])
        assert result.exit_code == 1, result.output

    def test_bad_policy_exits_two(self, compliant: Path) -> None:
        result = runner.invoke(app, ["verify", "G(((", str(compliant)])
        assert result.exit_code == 2, result.output

    def test_missing_trace_exits_two(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["verify", "G(a)", str(tmp_path / "nope.jsonl")])
        assert result.exit_code == 2, result.output

    def test_usage_error_is_distinguishable_from_a_violation(self, violating: Path) -> None:
        # A wrapper script that cannot tell these apart will eventually treat a malformed
        # file as a policy failure, or worse, a policy failure as a benign no-op.
        bad_input = runner.invoke(app, ["verify", "G(((", str(violating)])
        real_violation = runner.invoke(app, ["verify", POLICY, str(violating)])
        assert bad_input.exit_code != real_violation.exit_code


class TestJsonOutput:
    def test_json_output_is_parseable_and_complete(self, violating: Path) -> None:
        result = runner.invoke(app, ["verify", POLICY, str(violating), "--json"])
        assert result.exit_code == 1

        # Rich wraps output to the terminal width, so strip before decoding rather than
        # assuming a clean single-line payload.
        payload = json.loads(result.output)
        assert payload["compliant"] is False
        assert payload["violation_index"] == 1
        assert payload["policy"]
        assert isinstance(payload["counterexample"], list)
        assert payload["counterexample"], "a violation must ship its evidence"

    def test_json_output_for_a_compliant_trace(self, compliant: Path) -> None:
        result = runner.invoke(app, ["verify", POLICY, str(compliant), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["compliant"] is True
        assert payload["violation_index"] is None
        assert payload["counterexample"] == []


class TestAtomsCommand:
    def test_lists_atoms_with_their_steps(self, compliant: Path) -> None:
        result = runner.invoke(app, ["atoms", str(compliant)])
        assert result.exit_code == 0, result.output
        assert "call_refund_api" in result.output
        assert "human_approved" in result.output
        assert "read_order" in result.output

    def test_empty_trace_reports_no_atoms(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text('{"index": 0, "atoms": []}\n', encoding="utf-8")
        result = runner.invoke(app, ["atoms", str(path)])
        assert result.exit_code == 0, result.output
        assert "no atoms" in result.output


class TestExplainCommand:
    def test_reports_parsed_form_and_state_count(self) -> None:
        result = runner.invoke(app, ["explain", POLICY])
        assert result.exit_code == 0, result.output
        assert "call_refund_api" in result.output
        assert "states" in result.output

    def test_rejects_an_unparseable_policy(self) -> None:
        result = runner.invoke(app, ["explain", "G((("])
        assert result.exit_code == 2


class TestTraceFormats:
    def test_reads_a_plain_json_list(self, tmp_path: Path) -> None:
        path = tmp_path / "trace.json"
        path.write_text(
            json.dumps(
                [
                    {"index": 0, "atoms": ["call_refund_api", "human_approved"]},
                    {"index": 1, "atoms": ["send_receipt"]},
                ]
            ),
            encoding="utf-8",
        )
        result = runner.invoke(app, ["verify", POLICY, str(path)])
        assert result.exit_code == 0, result.output

    def test_rejects_a_malformed_atoms_field(self, tmp_path: Path) -> None:
        # A mistyped atom silently produces a policy that can never be satisfied and a
        # verdict that still looks meaningful, so this must be a loud error, not a coercion.
        path = tmp_path / "bad.json"
        path.write_text(json.dumps([{"index": 0, "atoms": "call_refund_api"}]), encoding="utf-8")
        result = runner.invoke(app, ["verify", POLICY, str(path)])
        assert result.exit_code == 2
