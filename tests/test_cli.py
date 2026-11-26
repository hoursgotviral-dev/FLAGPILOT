"""Tests for the FlagPilot CLI."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from flagpilot.bootstrap import BootstrapError, BootstrapResult
from flagpilot.cli import SUBCOMMANDS, main
from flagpilot.planner import build_plan, save_plan
from flagpilot.report import save_run_result
from flagpilot.types import Decision, RolloutChange, RolloutRunResult, StageMetrics, StageOutcome


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def apply_stage(self, plan, percentage: int) -> RolloutChange:
        self.calls.append(("apply", percentage))
        return RolloutChange(
            feature_name=plan.feature_name,
            environment=plan.environment,
            segment_name=plan.segment_name or "seg",
            percentage=percentage,
            enabled=percentage > 0,
        )

    def rollback(self, plan) -> RolloutChange:
        self.calls.append(("rollback", plan.rollback_percentage))
        return RolloutChange(
            feature_name=plan.feature_name,
            environment=plan.environment,
            segment_name=plan.segment_name or "seg",
            percentage=plan.rollback_percentage,
            enabled=False,
        )


def test_cli_help(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI --help exits cleanly and lists all subcommands."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    for command in SUBCOMMANDS:
        assert command in captured.out


def test_cli_has_no_auto_approve_flag(tmp_path: Path) -> None:
    """CLI refuses any --auto-approve or --yes bypass flag."""
    plan_path = tmp_path / "plan.json"
    save_plan(build_plan("checkout_v2", "prod"), plan_path)

    for forbidden in ("--auto-approve", "--yes", "-y"):
        with pytest.raises(SystemExit) as exc_info:
            main(["apply", str(plan_path), "--percentage", "1", forbidden])
        assert exc_info.value.code != 0


def test_cli_plan_plain_english_writes_json_and_md_without_flagsmith(tmp_path: Path) -> None:
    """plan subcommand parses plain English and writes plan.json + plan.md offline."""
    exit_code = main(
        [
            "plan",
            "roll out checkout_v2 to 10% in Development, pause if errors exceed 2%",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert exit_code == 0
    plan_json = tmp_path / "plan.json"
    plan_md = tmp_path / "plan.md"
    assert plan_json.exists()
    assert plan_md.exists()

    data = json.loads(plan_json.read_text(encoding="utf-8"))
    assert data["feature_name"] == "checkout_v2"
    assert data["environment"] == "Development"
    assert [s["percentage"] for s in data["steps"]] == [10]
    assert "Exact API Calls That Would Run" in plan_md.read_text(encoding="utf-8")


def test_cli_plan_and_report_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """plan and report subcommands work locally without calling Flagsmith."""
    plan_path = tmp_path / "plan.json"
    exit_code = main(
        [
            "plan",
            "--feature",
            "new_checkout",
            "--env",
            "staging",
            "--out",
            str(plan_path),
        ]
    )
    assert exit_code == 0
    assert plan_path.exists()
    assert plan_path.with_suffix(".md").exists()

    plan = build_plan("new_checkout", "staging")
    step = plan.steps[0]
    run_result = RolloutRunResult(
        plan=plan,
        final_status="completed",
        outcomes=(
            StageOutcome(
                step=step,
                metrics=StageMetrics(percentage=1, sample_count=20, error_count=0, enabled_count=1),
                decision=Decision(
                    action="complete",
                    reason="passed",
                    current_percentage=1,
                    next_percentage=1,
                ),
                applied=True,
            ),
        ),
        is_synthetic=True,
    )
    run_path = tmp_path / "run.json"
    report_path = tmp_path / "report.md"
    save_run_result(run_result, run_path)

    exit_code = main(["report", "--input", str(run_path), "--out", str(report_path)])
    assert exit_code == 0
    content = report_path.read_text(encoding="utf-8")
    assert "new_checkout" in content
    assert "SYNTHETIC DATA" in content
    capsys.readouterr()


def test_cli_apply_non_interactive_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """apply refuses writes in non-interactive runs without calling Flagsmith."""
    plan_path = tmp_path / "plan.json"
    save_plan(build_plan("new_checkout", "prod"), plan_path)

    monkeypatch.setattr("sys.stdin", io.StringIO("APPROVE\n"))

    fake_backend = _FakeBackend()
    with patch("flagpilot.cli.FlagsmithManagementClient", return_value=fake_backend):
        exit_code = main(
            [
                "apply",
                str(plan_path),
                "--percentage",
                "1",
                "--audit-log",
                str(tmp_path / "audit.log"),
            ]
        )

    assert exit_code == 1
    assert fake_backend.calls == []
    assert "non-interactive" in capsys.readouterr().err.lower()


def test_cli_apply_requires_exact_approve_and_cancels_otherwise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing is written unless exact APPROVE is typed in an interactive TTY."""
    plan_path = tmp_path / "plan.json"
    save_plan(build_plan("checkout_v2", "Development", stages=(10,)), plan_path)
    audit_log = tmp_path / "audit.log"

    monkeypatch.setenv("FLAGSMITH_API_URL", "http://localhost:8000/api/v1")
    monkeypatch.setenv("FLAGSMITH_ADMIN_KEY", "dummy-admin-key")

    for bad in ("approve\n", "yes\n", "APPROVE \n", "\n"):
        fake_backend = _FakeBackend()
        bad_stdin = io.StringIO(bad)
        monkeypatch.setattr(bad_stdin, "isatty", lambda: True)
        monkeypatch.setattr("sys.stdin", bad_stdin)
        with patch("flagpilot.cli.FlagsmithManagementClient", return_value=fake_backend):
            code = main(["apply", str(plan_path), "--audit-log", str(audit_log)])
        assert code == 1
        assert fake_backend.calls == []
        assert not audit_log.exists()

    good_backend = _FakeBackend()
    good_stdin = io.StringIO("APPROVE\n")
    monkeypatch.setattr(good_stdin, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", good_stdin)
    with patch("flagpilot.cli.FlagsmithManagementClient", return_value=good_backend):
        code = main(["apply", str(plan_path), "--audit-log", str(audit_log)])

    assert code == 0
    assert good_backend.calls == [("apply", 10)]
    assert audit_log.exists()
    out = capsys.readouterr().out
    assert "# FlagPilot Rollout Plan: `checkout_v2`" in out
    assert "Applied 10% to checkout_v2 (Development)." in out


def test_cli_bootstrap_subcommand(tmp_path: Path) -> None:
    """bootstrap subcommand invokes bootstrap_flagsmith with parsed arguments."""
    env_file = tmp_path / ".env"
    fake_result = BootstrapResult(
        api_url="http://localhost:8000",
        has_users_before=False,
        org_id=1,
        project_id=2,
        environment_id=3,
        feature_id=4,
        segment_id=5,
        preserved_master_key=False,
        preserved_server_key=False,
        env_path=env_file,
    )
    with patch("flagpilot.cli.bootstrap_flagsmith", return_value=fake_result) as mock_boot:
        code = main(["bootstrap", "--base-url", "http://localhost:8000", "--env-file", str(env_file)])

    assert code == 0
    mock_boot.assert_called_once()
    passed_cfg = mock_boot.call_args.args[0]
    assert passed_cfg.base_url == "http://localhost:8000"
    assert passed_cfg.env_path == env_file

    with patch("flagpilot.cli.bootstrap_flagsmith", side_effect=BootstrapError("boom")):
        err_code = main(["bootstrap", "--env-file", str(env_file)])
    assert err_code == 1
