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

    with patch("flagpilot.cli.wait_for_liveness") as mock_wait:
        wait_code = main(["bootstrap", "--wait-only", "--base-url", "http://localhost:8000"])
    assert wait_code == 0
    mock_wait.assert_called_once()
    assert mock_wait.call_args.kwargs["timeout_seconds"] == 300.0


def test_cli_missing_file_returns_clean_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing input files return exit code 1 with concise Error output instead of traceback."""
    missing = tmp_path / "does_not_exist.json"
    code = main(["apply", str(missing)])
    assert code == 1
    assert "Error:" in capsys.readouterr().err


def test_cli_run_upfront_approve_and_step_approve_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI run executes spike+0% rollback with 1 upfront APPROVE, or pauses with --step-approve."""
    monkeypatch.setenv("FLAGSMITH_API_URL", "http://localhost:8000/api/v1")
    monkeypatch.setenv("FLAGSMITH_ADMIN_KEY", "dummy-admin")

    # Default (upfront single APPROVE) with --rollback-to-zero
    client1 = _FakeBackend()
    run_out = tmp_path / "last_run.json"
    html_out = tmp_path / "rollout.html"
    stdin1 = io.StringIO("APPROVE\n")
    monkeypatch.setattr(stdin1, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", stdin1)
    with patch("flagpilot.cli.FlagsmithManagementClient", return_value=client1):
        code1 = main(
            [
                "run",
                "--feature",
                "checkout_v2",
                "--env",
                "Development",
                "--offline-evaluator",
                "--spike-rate",
                "0.25",
                "--spike-threshold-pct",
                "25",
                "--rollback-to-zero",
                "--identities-file",
                str(tmp_path / "ids.json"),
                "--audit-log",
                str(tmp_path / "audit1.log"),
                "--run-out",
                str(run_out),
                "--report-out",
                str(html_out),
            ]
        )
    assert code1 == 1
    assert client1.calls == [("apply", 1), ("apply", 10), ("apply", 50), ("rollback", 0)]
    out1 = capsys.readouterr().out
    assert "Post-rollback verification: 0/200 exposed identities after rollback to 0%" in out1

    run_json = json.loads(run_out.read_text(encoding="utf-8"))
    assert run_json["monotonic_enrollment"]["all_monotonic"] is True
    assert run_json["outcomes"][1]["retained_from_previous_stage"]["monotonic"] is True
    assert run_json["post_rollback_verification"]["exposed_count"] == 0
    assert run_json["post_rollback_verification"]["sample_count"] == 200
    assert run_json["post_rollback_verification"]["verified"] is True

    html_doc = html_out.read_text(encoding="utf-8")
    assert "0/200 exposed identities after rollback to 0%" in html_doc
    assert "stayed enrolled" in html_doc
    assert "approve_run" in html_doc

    audit1_entries = [
        json.loads(line) for line in (tmp_path / "audit1.log").read_text(encoding="utf-8").splitlines()
    ]
    assert audit1_entries[0]["action"] == "approve_run"
    assert [e["action"] for e in audit1_entries].count("rollback") == 1

    # Opt-in --step-approve with only 1 APPROVE pauses at stage 2
    client2 = _FakeBackend()
    stdin2 = io.StringIO("APPROVE\n")
    monkeypatch.setattr(stdin2, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", stdin2)
    with patch("flagpilot.cli.FlagsmithManagementClient", return_value=client2):
        code2 = main(
            [
                "run",
                "--feature",
                "checkout_v2",
                "--env",
                "Development",
                "--offline-evaluator",
                "--step-approve",
                "--identities-file",
                str(tmp_path / "ids.json"),
                "--audit-log",
                str(tmp_path / "audit2.log"),
                "--run-out",
                str(tmp_path / "run2.json"),
                "--report-out",
                str(tmp_path / "html2.html"),
            ]
        )
    assert code2 == 1
    assert client2.calls == [("apply", 1)]
    capsys.readouterr()


def test_cli_plan_and_run_rich_and_plain_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI plan and run emit Rich tables/panels without ANSI on pipes and clean --plain output."""
    plan_code = main(
        [
            "plan",
            "roll out checkout_v2 through 1%, 10%, 50%, 100% in Development, rollback if errors exceed 2%",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert plan_code == 0
    plan_out = capsys.readouterr().out
    assert "\x1b[" not in plan_out
    assert "FlagPilot Rollout Plan" in plan_out
    assert "0% → 1% → 10% → 50% → 100%" in plan_out

    plain_plan_code = main(
        [
            "--plain",
            "plan",
            "--feature",
            "checkout_v2",
            "--env",
            "Development",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert plain_plan_code == 0
    plain_plan_out = capsys.readouterr().out
    assert "\x1b[" not in plain_plan_out
    assert "╭" not in plain_plan_out
    assert "Stage Progression: 0% -> 1% -> 10% -> 50% -> 100%" in plain_plan_out

    monkeypatch.setenv("FLAGSMITH_API_URL", "http://localhost:8000/api/v1")
    monkeypatch.setenv("FLAGSMITH_ADMIN_KEY", "dummy-admin")

    # Rich run output (non-TTY capsys -> zero ANSI codes, full table + rollback panel)
    client_rich = _FakeBackend()
    stdin_rich = io.StringIO("APPROVE\n")
    monkeypatch.setattr(stdin_rich, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", stdin_rich)
    with patch("flagpilot.cli.FlagsmithManagementClient", return_value=client_rich):
        run_rich_code = main(
            [
                "run",
                "--feature",
                "checkout_v2",
                "--env",
                "Development",
                "--offline-evaluator",
                "--spike-rate",
                "0.25",
                "--spike-threshold-pct",
                "25",
                "--rollback-to-zero",
                "--identities-file",
                str(tmp_path / "ids.json"),
                "--audit-log",
                str(tmp_path / "audit_rich.log"),
                "--run-out",
                str(tmp_path / "run_rich.json"),
                "--report-out",
                str(tmp_path / "rich.html"),
            ]
        )
    assert run_rich_code == 1
    run_rich_out = capsys.readouterr().out
    assert "\x1b[" not in run_rich_out
    assert "Exposed Identities" in run_rich_out
    assert "Retention Proof" in run_rich_out
    assert "Error Rate vs. Threshold" in run_rich_out
    assert "ROLLBACK EXECUTED → 0%" in run_rich_out
    assert "VERIFIED 0/200 EXPOSED" in run_rich_out

    # --plain run output -> no table borders, no ANSI codes, deterministic verification line
    client_plain = _FakeBackend()
    stdin_plain = io.StringIO("APPROVE\n")
    monkeypatch.setattr(stdin_plain, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", stdin_plain)
    with patch("flagpilot.cli.FlagsmithManagementClient", return_value=client_plain):
        run_plain_code = main(
            [
                "run",
                "--plain",
                "--feature",
                "checkout_v2",
                "--env",
                "Development",
                "--offline-evaluator",
                "--spike-rate",
                "0.25",
                "--spike-threshold-pct",
                "25",
                "--rollback-to-zero",
                "--identities-file",
                str(tmp_path / "ids.json"),
                "--audit-log",
                str(tmp_path / "audit_plain.log"),
                "--run-out",
                str(tmp_path / "run_plain.json"),
                "--report-out",
                str(tmp_path / "plain.html"),
            ]
        )
    assert run_plain_code == 1
    run_plain_out = capsys.readouterr().out
    assert "\x1b[" not in run_plain_out
    assert "┃" not in run_plain_out
    assert "VERIFIED 0/200 EXPOSED" in run_plain_out


def test_cli_plan_use_llm_routes_through_adapter_and_keeps_approve_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default plan never invokes LLM; --use-llm calls plan_with_llm and apply still enforces APPROVE."""
    monkeypatch.delenv("FLAGPILOT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # 1. Default zero-key path does NOT invoke plan_with_llm
    with patch("flagpilot.cli.plan_with_llm") as mock_llm:
        default_code = main(
            [
                "plan",
                "roll out checkout_v2 to 10% in Development, pause if errors exceed 2%",
                "--out-dir",
                str(tmp_path / "default"),
            ]
        )
    assert default_code == 0
    mock_llm.assert_not_called()

    # 2. --use-llm invokes plan_with_llm, writes plan.json/plan.md without Flagsmith calls,
    #    and apply STILL refuses non-interactive writes (mandatory APPROVE gate preserved).
    llm_plan = build_plan("ai_checkout", "Development", stages=(1, 25, 100), max_error_rate=0.01)
    llm_dir = tmp_path / "llm"
    with patch("flagpilot.cli.plan_with_llm", return_value=llm_plan) as mock_llm:
        llm_code = main(
            [
                "plan",
                "Canary ai_checkout to 100% in Development with strict 1% safety",
                "--use-llm",
                "--out-dir",
                str(llm_dir),
            ]
        )
    assert llm_code == 0
    mock_llm.assert_called_once()
    assert (llm_dir / "plan.json").exists()
    assert (llm_dir / "plan.md").exists()
    assert "Exact API Calls That Would Run" in (llm_dir / "plan.md").read_text(encoding="utf-8")
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO("APPROVE\n"))
    fake_backend = _FakeBackend()
    with patch("flagpilot.cli.FlagsmithManagementClient", return_value=fake_backend):
        apply_code = main(["apply", str(llm_dir / "plan.json")])
    assert apply_code == 1
    assert fake_backend.calls == []
    assert "non-interactive" in capsys.readouterr().err.lower()


def test_cli_run_signal_file_and_signal_url_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--signal-file and --signal-url breach at 1% directly, fail closed, and avoid SYNTHETIC error label."""
    import requests

    monkeypatch.setenv("FLAGSMITH_API_URL", "http://localhost:8000/api/v1")
    monkeypatch.setenv("FLAGSMITH_ADMIN_KEY", "dummy-admin")

    # Mutually exclusive flags rejected
    with pytest.raises(SystemExit):
        main(
            [
                "run",
                "--feature",
                "checkout_v2",
                "--env",
                "Development",
                "--signal-url",
                "http://localhost:9090/err",
                "--signal-file",
                str(tmp_path / "sig.json"),
            ]
        )

    # 1. --signal-file with breach (0.05 > 0.02) rolls back at stage 1% even though 1% exposed < min_samples=20
    sig_file = tmp_path / "sig.json"
    sig_file.write_text('{"error_rate": 0.05}', encoding="utf-8")
    backend1 = _FakeBackend()
    run_out1 = tmp_path / "run_sig.json"
    html_out1 = tmp_path / "rollout_sig.html"
    stdin1 = io.StringIO("APPROVE\n")
    monkeypatch.setattr(stdin1, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", stdin1)
    with patch("flagpilot.cli.FlagsmithManagementClient", return_value=backend1):
        code1 = main(
            [
                "run",
                "--feature",
                "checkout_v2",
                "--env",
                "Development",
                "--offline-evaluator",
                "--signal-file",
                str(sig_file),
                "--rollback-to-zero",
                "--identities-file",
                str(tmp_path / "ids.json"),
                "--audit-log",
                str(tmp_path / "audit_sig.log"),
                "--run-out",
                str(run_out1),
                "--report-out",
                str(html_out1),
            ]
        )
    assert code1 == 1
    assert backend1.calls == [("apply", 1), ("rollback", 0)]
    out1 = capsys.readouterr().out
    assert "[OBSERVED] Rollout finished with status: rolled_back" in out1

    run_json = json.loads(run_out1.read_text(encoding="utf-8"))
    assert run_json["is_synthetic"] is False
    assert run_json["traffic_label"] == "OBSERVED"
    assert run_json["outcomes"][0]["metrics"]["source_label"] == "OBSERVED"
    assert run_json["post_rollback_verification"]["verified"] is True
    html_text = html_out1.read_text(encoding="utf-8")
    assert "OBSERVED ERROR SIGNAL (SYNTHETIC IDENTITIES)" in html_text

    # 2. --signal-url timeout fails closed and scrubs credentials from logs/report
    backend2 = _FakeBackend()
    stdin2 = io.StringIO("APPROVE\n")
    monkeypatch.setattr(stdin2, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", stdin2)
    audit2 = tmp_path / "audit_url.log"
    secret_url = "https://ops:top-secret-token@metrics.example.com/err?key=k123"
    with (
        patch("flagpilot.cli.FlagsmithManagementClient", return_value=backend2),
        patch(
            "flagpilot.controller.requests.get",
            side_effect=requests.Timeout(f"timed out {secret_url}"),
        ),
    ):
        code2 = main(
            [
                "run",
                "--feature",
                "checkout_v2",
                "--env",
                "Development",
                "--offline-evaluator",
                "--signal-url",
                secret_url,
                "--signal-timeout",
                "1.0",
                "--rollback-to-zero",
                "--identities-file",
                str(tmp_path / "ids.json"),
                "--audit-log",
                str(audit2),
                "--run-out",
                str(tmp_path / "run_url.json"),
                "--report-out",
                str(tmp_path / "rollout_url.html"),
            ]
        )
    assert code2 == 1
    assert backend2.calls == [("apply", 1), ("rollback", 0)]
    audit2_text = audit2.read_text(encoding="utf-8")
    assert "fail-closed" in audit2_text
    assert "approve_run" in audit2_text
    assert "top-secret-token" not in audit2_text
    assert "k123" not in audit2_text
    run_url_data = json.loads((tmp_path / "run_url.json").read_text(encoding="utf-8"))
    assert run_url_data["outcomes"][0]["metrics"]["sample_count"] == 200
    assert run_url_data["outcomes"][0]["metrics"]["signal_error"] is not None
    assert run_url_data["outcomes"][0]["decision"]["observed_error_rate"] is None
    html_url_doc = (tmp_path / "rollout_url.html").read_text(encoding="utf-8")
    assert "N/A (signal error)" in html_url_doc
    assert "/ 200" in html_url_doc
    capsys.readouterr()
