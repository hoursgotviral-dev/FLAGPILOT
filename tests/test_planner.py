"""Tests for deterministic plain-English planner, plan.md rendering, and import isolation."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from flagpilot.planner import (
    SUPPORTED_PHRASES,
    PlanParseError,
    parse_plain_english_request,
    render_plan_markdown,
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

