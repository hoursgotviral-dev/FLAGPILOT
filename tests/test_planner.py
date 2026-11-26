"""Tests for deterministic plain-English planner, plan.md rendering, and import isolation."""

from __future__ import annotations

import ast
import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from flagpilot.planner import (
    SUPPORTED_PHRASES,
    PlanParseError,
    build_plan,
    is_llm_configured,
    parse_plain_english_request,
    plan_with_llm,
    render_plan_markdown,
    render_plan_summary,
    save_plan_artifacts,
)


def test_planner_imports_neither_requests_nor_flagsmith() -> None:
    """planner.py must never import requests or flagsmith."""
    planner_path = Path(__file__).resolve().parents[1] / "src" / "flagpilot" / "planner.py"
    tree = ast.parse(planner_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module.split(".")[0])

    assert "requests" not in imported_modules
    assert "flagsmith" not in imported_modules


def test_parse_plain_english_request_and_artifacts(tmp_path: Path) -> None:
    """Deterministic parser extracts feature, percent, env, threshold and writes json+md."""
    request = "roll out checkout_v2 to 10% in Development, pause if errors exceed 2%"
    plan = parse_plain_english_request(request)

    assert plan.feature_name == "checkout_v2"
    assert plan.environment == "Development"
    assert plan.on_breach == "pause"
    assert [s.percentage for s in plan.steps] == [10]
    assert plan.steps[0].max_error_rate == pytest.approx(0.02)

    json_path, md_path = save_plan_artifacts(
        plan,
        json_path=tmp_path / "plan.json",
        markdown_path=tmp_path / "plan.md",
    )
    decoded = json.loads(json_path.read_text(encoding="utf-8"))
    assert decoded["feature_name"] == "checkout_v2"
    assert decoded["environment"] == "Development"
    assert decoded["planned_api_calls"]

    md_text = md_path.read_text(encoding="utf-8")
    assert "## What Will Change" in md_text
    assert "## Exact API Calls That Would Run" in md_text
    assert "## Risks" in md_text
    assert "## If Threshold Is Breached" in md_text
    assert "PERCENTAGE_SPLIT" in md_text
    assert "checkout_v2" in md_text
    assert "Development" in md_text


def test_parse_unparseable_request_names_missing_part_and_supported_phrases() -> None:
    """Unparseable requests say which part failed and list supported phrases."""
    with pytest.raises(PlanParseError) as exc_info:
        parse_plain_english_request("do something random without details")

    message = str(exc_info.value)
    assert "feature" in message
    assert "target percent" in message
    assert "environment" in message
    assert "threshold" in message
    for phrase in SUPPORTED_PHRASES:
        assert phrase in message

    with pytest.raises(PlanParseError) as partial_exc:
        parse_plain_english_request("roll out checkout_v2 to 10% in Development")
    assert "threshold" in str(partial_exc.value)
    assert "Supported phrases" in str(partial_exc.value)


def test_render_plan_markdown_describes_rollback_and_pause_modes() -> None:
    """plan.md clearly documents threshold breach behavior for both pause and rollback."""
    pause_plan = parse_plain_english_request(
        "enable smart_search at 25% in Staging, pause if errors exceed 1.5%"
    )
    rollback_plan = parse_plain_english_request(
        "roll out smart_search through 1%, 10%, 50%, 100% in Production, rollback if errors exceed 2%"
    )

    assert pause_plan.on_breach == "pause"
    assert "pauses the rollout" in render_plan_markdown(pause_plan)
    assert rollback_plan.on_breach == "rollback"
    assert [s.percentage for s in rollback_plan.steps] == [1, 10, 50, 100]
    assert "roll back" in render_plan_markdown(rollback_plan)


def test_render_plan_summary_rich_panel_and_plain_fallback() -> None:
    """render_plan_summary outputs bordered Rich panel without ANSI on pipes and plain fallback."""
    plan = build_plan("checkout_v2", "Development", stages=(1, 10, 50, 100))

    rich_buf = io.StringIO()
    render_plan_summary(plan, plain=False, output_stream=rich_buf)
    rich_out = rich_buf.getvalue()
    assert "\x1b[" not in rich_out
    assert "FlagPilot Rollout Plan" in rich_out
    assert "checkout_v2" in rich_out
    assert "Development" in rich_out
    assert "0% → 1% → 10% → 50% → 100%" in rich_out
    assert "2.00%" in rich_out
    assert "Planned Flagsmith API Calls" in rich_out

    plain_buf = io.StringIO()
    render_plan_summary(plan, plain=True, output_stream=plain_buf)
    plain_out = plain_buf.getvalue()
    assert "\x1b[" not in plain_out
    assert "╭" not in plain_out
    assert "Stage Progression: 0% -> 1% -> 10% -> 50% -> 100%" in plain_out
    assert "Planned Flagsmith API Calls:" in plain_out


def _fake_http_response(payload: dict[str, object]) -> MagicMock:
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    return resp


def test_plan_with_llm_zero_key_default_off_and_deterministic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero-key default keeps LLM disabled and falls back cleanly when unconfigured."""
    monkeypatch.delenv("FLAGPILOT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert is_llm_configured() is False

    # Even with a key present, missing FLAGPILOT_LLM_PROVIDER keeps default deterministic
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key-unused")
    assert is_llm_configured() is False

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    opener = MagicMock()
    plan = plan_with_llm(
        "roll out checkout_v2 to 10% in Development, pause if errors exceed 2%",
        opener=opener,
    )
    opener.assert_not_called()
    assert plan.feature_name == "checkout_v2"
    assert plan.environment == "Development"
    assert [s.percentage for s in plan.steps] == [10]
    assert plan.on_breach == "pause"


def test_plan_with_llm_valid_gemini_and_openai_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid strict JSON from Gemini or OpenAI validates cleanly through plan_from_dict()."""
    llm_plan_json = {
        "feature_name": "smart_checkout",
        "environment": "Production",
        "steps": [
            {"percentage": 1, "max_error_rate": 0.015},
            {"percentage": 10, "max_error_rate": 0.015},
            {"percentage": 50, "max_error_rate": 0.015},
        ],
        "rollback_percentage": 0,
        "on_breach": "rollback",
    }

    # 1. Gemini provider via env + standard HTTP opener
    monkeypatch.setenv("FLAGPILOT_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-secret-123")
    assert is_llm_configured() is True

    gemini_opener = MagicMock(
        return_value=_fake_http_response(
            {"candidates": [{"content": {"parts": [{"text": json.dumps(llm_plan_json)}]}}]}
        )
    )
    gem_plan = plan_with_llm(
        "Gradually roll out smart_checkout in Production up to 50% and roll back above 1.5%",
        min_samples=30,
        opener=gemini_opener,
    )
    assert gem_plan.feature_name == "smart_checkout"
    assert gem_plan.environment == "Production"
    assert [s.percentage for s in gem_plan.steps] == [1, 10, 50]
    assert all(s.min_samples == 30 for s in gem_plan.steps)
    assert gem_plan.on_breach == "rollback"
    req_gem = gemini_opener.call_args.args[0]
    assert "gem-secret-123" not in req_gem.full_url
    assert req_gem.headers["X-goog-api-key"] == "gem-secret-123"

    # 2. OpenAI provider (with fenced JSON response)
    monkeypatch.setenv("FLAGPILOT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-123")
    fenced = "```json\n" + json.dumps(llm_plan_json) + "\n```"
    openai_opener = MagicMock(
        return_value=_fake_http_response({"choices": [{"message": {"content": fenced}}]})
    )
    oai_plan = plan_with_llm("roll out smart checkout safely", opener=openai_opener)
    assert oai_plan.feature_name == "smart_checkout"
    assert [s.percentage for s in oai_plan.steps] == [1, 10, 50]
    req_oai = openai_opener.call_args.args[0]
    assert req_oai.headers["Authorization"] == "Bearer sk-openai-123"


def test_plan_with_llm_malformed_json_schema_error_and_secret_redaction(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed JSON, invalid Plan schema, and HTTP failures fall back safely without leaking keys."""
    monkeypatch.setenv("FLAGPILOT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-never-leak-me")
    prompt = "roll out checkout_v2 to 10% in Development, pause if errors exceed 2%"

    # a) Malformed JSON text -> deterministic parser fallback
    bad_json_opener = MagicMock(
        return_value=_fake_http_response({"choices": [{"message": {"content": "{not json"}}]})
    )
    with caplog.at_level("WARNING"):
        plan_a = plan_with_llm(prompt, opener=bad_json_opener)
    assert plan_a.feature_name == "checkout_v2"
    assert [s.percentage for s in plan_a.steps] == [10]

    # b) Invalid Plan schema (non-increasing percentages) -> rejected by plan_from_dict -> fallback
    bad_schema_opener = MagicMock(
        return_value=_fake_http_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "feature_name": "checkout_v2",
                                    "environment": "Development",
                                    "steps": [{"percentage": 50}, {"percentage": 10}],
                                }
                            )
                        }
                    }
                ]
            }
        )
    )
    plan_b = plan_with_llm(prompt, opener=bad_schema_opener)
    assert [s.percentage for s in plan_b.steps] == [10]

    # c) Network failure containing key -> scrubbed in warning log + fallback
    caplog.clear()
    err_opener = MagicMock(
        side_effect=urllib.error.URLError("upstream failure token=sk-never-leak-me")
    )
    with caplog.at_level("WARNING"):
        plan_c = plan_with_llm(prompt, opener=err_opener)
    assert plan_c.feature_name == "checkout_v2"
    assert "sk-never-leak-me" not in caplog.text
    assert "[REDACTED]" in caplog.text

