"""Mocked unit tests for the closed-loop canary demo checkout service."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from flagpilot.controller import OBSERVED_LABEL, HttpSignalProvider
from flagpilot.planner import build_plan
from flagpilot.simulator import OfflineFakeEvaluator

_SERVICE_PATH = Path(__file__).resolve().parents[1] / "examples" / "checkout_service.py"
_SPEC = importlib.util.spec_from_file_location("checkout_service", _SERVICE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["checkout_service"] = _MODULE
_SPEC.loader.exec_module(_MODULE)
CheckoutServiceApp = _MODULE.CheckoutServiceApp
create_server = _MODULE.create_server


class _RefreshTrackingEvaluator(OfflineFakeEvaluator):
    """OfflineFakeEvaluator tracking refresh() calls to confirm stage updates take effect."""

    def __init__(self, rollout_percentage: int = 0) -> None:
        super().__init__(rollout_percentage=rollout_percentage)
        self.refresh_calls = 0

    def refresh(self) -> None:
        self.refresh_calls += 1


def _http_get_json(url: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_checkout_endpoint_returns_200_when_off_and_deterministic_500_when_enabled() -> None:
    """GET /checkout returns 200 when feature is off and deterministic 500s when enabled."""
    evaluator = _RefreshTrackingEvaluator(rollout_percentage=0)
    app = CheckoutServiceApp(
        evaluator,
        feature_name="checkout_v2",
        fault_rate=0.50,
        user_count=200,
        output_stream=io.StringIO(),
    )

    # 0% rollout -> all requests return HTTP 200
    for idx in range(25):
        status, body = app.handle_checkout(f"user_{idx:03d}")
        assert status == 200
        assert body["enabled"] is False
        assert body["status"] == "ok"
    assert evaluator.refresh_calls == 25

    # Missing user parameter -> 400
    bad_status, bad_body = app.handle_checkout("   ")
    assert bad_status == 400
    assert "error" in bad_body

    # 100% rollout with 1.0 fault rate -> always 500; with 0.50 -> deterministic mix
    evaluator.set_percentage(100)
    statuses_50 = [app.handle_checkout(f"user_{idx:03d}")[0] for idx in range(40)]
    assert statuses_50 == [app.handle_checkout(f"user_{idx:03d}")[0] for idx in range(40)]
    assert 200 in statuses_50 and 500 in statuses_50

    app_always_fail = CheckoutServiceApp(
        OfflineFakeEvaluator(rollout_percentage=100),
        fault_rate=1.0,
        output_stream=io.StringIO(),
    )
    status_fail, body_fail = app_always_fail.handle_checkout("user_001")
    assert status_fail == 500
    assert body_fail["enabled"] is True
    assert body_fail["status"] == "error"


def test_live_http_server_self_drive_and_no_self_drive_compatible_with_http_signal_provider() -> None:
    """HTTP /checkout and /health-rate serve valid JSON directly consumable by HttpSignalProvider."""
    evaluator = _RefreshTrackingEvaluator(rollout_percentage=1)
    log_buf = io.StringIO()
    app = CheckoutServiceApp(
        evaluator,
        feature_name="checkout_v2",
        fault_rate=0.50,
        user_count=200,
        self_drive=True,
        output_stream=log_buf,
    )
    server = create_server(app, host="127.0.0.1", port=0)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        base_url = f"http://127.0.0.1:{port}"
        plan = build_plan("checkout_v2", "Development", stages=(1, 10, 50, 100), max_error_rate=0.02)
        provider = HttpSignalProvider(f"{base_url}/health-rate", timeout_seconds=5.0)

        # Stage 1%: low exposure stays <= 2% threshold
        m_1 = provider.collect(plan, plan.steps[0])
        assert m_1.source_label == OBSERVED_LABEL
        assert m_1.error_rate <= 0.02
        assert evaluator.refresh_calls == 1

        # Verify raw JSON schema on /health-rate
        raw_status, health_json = _http_get_json(f"{base_url}/health-rate")
        assert raw_status == 200
        assert set(health_json) == {"error_rate", "requests", "errors", "exposed"}
        assert health_json["requests"] == 200
        assert health_json["exposed"] > 0

        # Stage 50%: spike clears > 2% threshold
        evaluator.set_percentage(50)
        m_50 = provider.collect(plan, plan.steps[2])
        assert m_50.error_rate > 0.02

        # Live /checkout HTTP check on known exposed vs failing user
        evaluator.set_percentage(100)
        c_status, c_body = _http_get_json(f"{base_url}/checkout?user=user_000")
        assert c_status in (200, 500)
        assert c_body["feature"] == "checkout_v2"
        assert c_body["user"] == "user_000"

        missing_status, _ = _http_get_json(f"{base_url}/unknown")
        assert missing_status == 404
        assert "mode=self-drive" in log_buf.getvalue()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_no_self_drive_sliding_window_tracks_checkout_requests() -> None:
    """With --no-self-drive, /health-rate computes error_rate over recent /checkout requests."""
    evaluator = OfflineFakeEvaluator(rollout_percentage=100)
    app_always_fail = CheckoutServiceApp(
        evaluator,
        fault_rate=1.0,
        self_drive=False,
        window_size=4,
        output_stream=io.StringIO(),
    )
    assert app_always_fail.collect_health_rate() == {
        "error_rate": 0.0,
        "requests": 0,
        "errors": 0,
        "exposed": 0,
    }

    evaluator.set_percentage(0)
    app_always_fail.handle_checkout("user_001")
    app_always_fail.handle_checkout("user_002")
    evaluator.set_percentage(100)
    app_always_fail.handle_checkout("user_003")

    stats = app_always_fail.collect_health_rate()
    assert stats["requests"] == 3
    assert stats["exposed"] == 1
    assert stats["errors"] == 1
    assert stats["error_rate"] == pytest.approx(1 / 3, rel=1e-4)
