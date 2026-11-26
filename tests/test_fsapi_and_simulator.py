"""Mocked unit tests for FlagsmithManagementClient, FlagsmithClient Protocol, and simulator."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from flagpilot.fsapi import FlagsmithAPIError, FlagsmithConfig, FlagsmithManagementClient
from flagpilot.planner import build_plan
from flagpilot.simulator import SYNTHETIC_LABEL, FlagsmithSDKEvaluator
from flagpilot.types import FlagsmithClient


def _mock_response(payload: Any, status_code: int = 200, text: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.text = text if text is not None else ("{}" if payload is not None else "")
    resp.json.return_value = payload
    return resp


class _FakeFlagsmithClient:
    """Minimal fake implementing FlagsmithClient Protocol for substitution."""

    def __init__(self) -> None:
        self.features: dict[str, dict[str, Any]] = {}
        self.segments: dict[int, dict[str, Any]] = {}
        self.overrides: dict[tuple[str, int | None], dict[str, Any]] = {}

    def create_feature(
        self,
        name: str,
        *,
        description: str = "",
        default_enabled: bool = False,
    ) -> dict[str, Any]:
        feat = {"id": len(self.features) + 1, "name": name, "default_enabled": default_enabled}
        self.features[name] = feat
        return feat

    def create_segment_with_percentage_split(self, name: str, percent: int) -> dict[str, Any]:
        seg_id = len(self.segments) + 1
        seg = {"id": seg_id, "name": name, "percent": percent}
        self.segments[seg_id] = seg
        return seg

    def attach_segment_override(
        self,
        feature_name: str,
        segment_id: int,
        *,
        enabled: bool = True,
        priority: int = 0,
    ) -> dict[str, Any]:
        state = {"feature_name": feature_name, "segment_id": segment_id, "enabled": enabled}
        self.overrides[(feature_name, segment_id)] = state
        return state

    def set_segment_percentage(
        self,
        segment_id: int,
        percent: int,
        *,
        name: str | None = None,
    ) -> dict[str, Any]:
        self.segments[segment_id]["percent"] = percent
        return self.segments[segment_id]

    def disable_override(
        self,
        feature_name: str,
        segment_id: int | None = None,
    ) -> dict[str, Any]:
        self.overrides[(feature_name, segment_id)]["enabled"] = False
        return self.overrides[(feature_name, segment_id)]

    def get_state(
        self,
        feature_name: str,
        segment_id: int | None = None,
    ) -> dict[str, Any]:
        return {
            "feature_name": feature_name,
            "segment_override": self.overrides.get((feature_name, segment_id)),
        }


def test_flagsmith_client_protocol_conformance() -> None:
    """Both FlagsmithManagementClient and a fake satisfy FlagsmithClient Protocol."""
    session = MagicMock()
    session.headers = {}
    real_client = FlagsmithManagementClient(
        FlagsmithConfig(api_url="http://localhost:8000", admin_key="tok"),
        session=session,
    )
    fake_client = _FakeFlagsmithClient()

    assert isinstance(real_client, FlagsmithClient)
    assert isinstance(fake_client, FlagsmithClient)

    fake_client.create_feature("checkout_v2")
    seg = fake_client.create_segment_with_percentage_split("rollout", 10)
    fake_client.attach_segment_override("checkout_v2", seg["id"])
    fake_client.set_segment_percentage(seg["id"], 50)
    fake_client.disable_override("checkout_v2", seg["id"])
    state = fake_client.get_state("checkout_v2", seg["id"])
    assert state["segment_override"]["enabled"] is False


def test_config_from_env_and_secret_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """FlagsmithConfig loads env vars and never exposes keys in repr."""
    monkeypatch.setattr("flagpilot.fsapi.load_dotenv", lambda: False)
    monkeypatch.setenv("FLAGSMITH_API_URL", "http://localhost:8000")
    monkeypatch.setenv("FLAGSMITH_ADMIN_KEY", "super-secret-admin-key")
    monkeypatch.setenv("FLAGSMITH_SERVER_KEY", "ser.secret-env-key")
    monkeypatch.setenv("FLAGSMITH_PROJECT_ID", "12")
    monkeypatch.setenv(
        "FLAGSMITH_HEALTH_WEBHOOK_URL",
        "http://localhost:8000/api/v1/feature-health/secret-webhook-token/",
    )

    cfg = FlagsmithConfig.from_env()
    assert cfg.api_url == "http://localhost:8000"
    assert cfg.project_id == 12
    assert cfg.health_webhook_url == "http://localhost:8000/api/v1/feature-health/secret-webhook-token/"
    assert "super-secret-admin-key" not in repr(cfg)
    assert "ser.secret-env-key" not in repr(cfg)
    assert "secret-webhook-token" not in repr(cfg)

    monkeypatch.delenv("FLAGSMITH_ADMIN_KEY")
    with pytest.raises(ValueError, match="FLAGSMITH_ADMIN_KEY"):
        FlagsmithConfig.from_env()


def test_fsapi_all_six_methods_with_mocks() -> None:
    """Verify all six Management REST API client methods with mocked HTTP responses."""
    session = MagicMock()
    session.headers = {}
    client = FlagsmithManagementClient(
        FlagsmithConfig(
            api_url="http://localhost:8000",
            admin_key="secret-admin-token",
            environment_key="env-key-1",
            project_id=42,
        ),
        session=session,
    )
    assert session.headers["Authorization"] == "Api-Key secret-admin-token"

    # 1. create_feature
    session.request.side_effect = [
        _mock_response([]),
        _mock_response({"id": 99, "name": "checkout_v2", "default_enabled": False}, 201),
    ]
    feat = client.create_feature("checkout_v2")
    assert feat["id"] == 99

    # 2. create_segment_with_percentage_split(name, percent)
    session.request.side_effect = [
        _mock_response([]),
        _mock_response({"id": 500, "name": "flagpilot-checkout_v2-rollout"}, 201),
    ]
    seg = client.create_segment_with_percentage_split("flagpilot-checkout_v2-rollout", 10)
    assert seg["id"] == 500
    post_seg = session.request.call_args_list[-1]
    assert post_seg.kwargs["json"]["rules"][0]["conditions"][0] == {
        "operator": "PERCENTAGE_SPLIT",
        "property": None,
        "value": "10",
    }

    # 3. set_segment_percentage
    session.request.side_effect = [
        _mock_response({"id": 500, "name": "flagpilot-checkout_v2-rollout"}),
        _mock_response({"id": 500, "name": "flagpilot-checkout_v2-rollout"}),
    ]
    updated_seg = client.set_segment_percentage(500, 50)
    assert updated_seg["id"] == 500
    put_seg = session.request.call_args_list[-1]
    assert put_seg.kwargs["method"] == "PUT"
    assert put_seg.kwargs["json"]["rules"][0]["conditions"][0]["value"] == "50"

    # 4. attach_segment_override (via /api/__future__/environments/{env}/features/{id}/)
    session.request.side_effect = [
        _mock_response([{"id": 7, "name": "prod", "api_key": "env-key-1"}]),
        _mock_response([{"id": 99, "name": "checkout_v2"}]),
        _mock_response(
            {"environment_default": {"enabled": False, "value": None}, "segment_overrides": []}
        ),
        _mock_response(
            {
                "environment_default": {"enabled": False, "value": None},
                "segment_overrides": [
                    {"segment": {"id": 500}, "priority": 0, "enabled": True, "value": None}
                ],
            }
        ),
    ]
    attached = client.attach_segment_override("checkout_v2", 500, enabled=True)
    assert attached["segment"]["id"] == 500
    assert attached["enabled"] is True
    patch_call = session.request.call_args_list[-1]
    assert patch_call.kwargs["method"] == "PATCH"
    assert patch_call.kwargs["url"].endswith("/api/__future__/environments/env-key-1/features/99/")
    assert "environment_default" not in patch_call.kwargs["json"]

    # 5. disable_override
    session.request.side_effect = [
        _mock_response([{"id": 7, "name": "prod", "api_key": "env-key-1"}]),
        _mock_response([{"id": 99, "name": "checkout_v2"}]),
        _mock_response(
            {
                "environment_default": {"enabled": False, "value": None},
                "segment_overrides": [
                    {"segment": {"id": 500}, "priority": 0, "enabled": True, "value": None}
                ],
            }
        ),
        _mock_response(
            {
                "environment_default": {"enabled": False, "value": None},
                "segment_overrides": [
                    {"segment": {"id": 500}, "priority": 0, "enabled": False, "value": None}
                ],
            }
        ),
    ]
    disabled = client.disable_override("checkout_v2", segment_id=500)
    assert disabled["enabled"] is False

    # 6. get_state
    session.request.side_effect = [
        _mock_response([{"id": 7, "name": "prod", "api_key": "env-key-1"}]),
        _mock_response([{"id": 99, "name": "checkout_v2"}]),
        _mock_response(
            {
                "environment_default": {"enabled": False, "value": None},
                "segment_overrides": [
                    {"segment": {"id": 500}, "priority": 0, "enabled": False, "value": None}
                ],
            }
        ),
    ]
    state = client.get_state("checkout_v2", segment_id=500)
    assert state["feature_id"] == 99
    assert state["default_state"]["enabled"] is False
    assert state["segment_override"]["segment"]["id"] == 500
    assert state["segment_override"]["enabled"] is False


def test_fsapi_errors_include_response_body_and_never_leak_keys() -> None:
    """API errors expose response body for debugging while scrubbing all secrets."""
    session = MagicMock()
    session.headers = {}
    secret_admin = "adm_very_secret_value"
    secret_env = "ser_very_secret_value"

    session.request.return_value = _mock_response(
        None,
        status_code=400,
        text=f'{{"detail":"bad operator","leaked":"{secret_admin} {secret_env}"}}',
    )
    client = FlagsmithManagementClient(
        FlagsmithConfig(
            api_url="http://localhost:8000",
            admin_key=secret_admin,
            environment_key=secret_env,
            project_id=1,
        ),
        session=session,
    )

    with pytest.raises(FlagsmithAPIError) as exc_info:
        client.create_feature("checkout_v2")

    err = exc_info.value
    assert err.status_code == 400
    assert "bad operator" in str(err)
    assert "bad operator" in err.response_body
    assert secret_admin not in str(err)
    assert secret_env not in str(err)
    assert secret_admin not in err.response_body
    assert secret_env not in err.response_body

    session.request.side_effect = requests.ConnectionError(f"failed with {secret_admin}")
    with pytest.raises(FlagsmithAPIError) as conn_exc:
        client.create_feature("checkout_v2")
    assert secret_admin not in str(conn_exc.value)


def test_fsapi_apply_stage_and_rollback_orchestration() -> None:
    """apply_stage and rollback compose the FlagsmithClient operations cleanly."""
    session = MagicMock()
    session.headers = {}
    session.request.side_effect = [
        # apply_stage(10%):
        _mock_response([{"id": 42, "name": "Demo"}]),  # GET projects/
        _mock_response([{"id": 7, "name": "prod", "api_key": "env-key-1"}]),  # GET environments/
        _mock_response([{"id": 99, "name": "checkout_v2"}]),  # GET features/
        _mock_response([]),  # GET segments/
        _mock_response({"id": 500, "name": "flagpilot-checkout_v2-rollout"}, 201),
        _mock_response([{"id": 7, "name": "prod", "api_key": "env-key-1"}]),
        _mock_response([{"id": 99, "name": "checkout_v2"}]),
        _mock_response(
            {"environment_default": {"enabled": False, "value": None}, "segment_overrides": []}
        ),
        _mock_response(
            {
                "environment_default": {"enabled": False, "value": None},
                "segment_overrides": [
                    {"segment": {"id": 500}, "priority": 0, "enabled": True, "value": None}
                ],
            }
        ),
        # rollback(0%):
        _mock_response([{"id": 42, "name": "Demo"}]),
        _mock_response([{"id": 7, "name": "prod", "api_key": "env-key-1"}]),
        _mock_response([{"id": 99, "name": "checkout_v2"}]),
        _mock_response([{"id": 500, "name": "flagpilot-checkout_v2-rollout"}]),
        _mock_response({"id": 500, "name": "flagpilot-checkout_v2-rollout"}),  # PUT segments/500/
        _mock_response([{"id": 7, "name": "prod", "api_key": "env-key-1"}]),
        _mock_response([{"id": 99, "name": "checkout_v2"}]),
        _mock_response(
            {
                "environment_default": {"enabled": False, "value": None},
                "segment_overrides": [
                    {"segment": {"id": 500}, "priority": 0, "enabled": True, "value": None}
                ],
            }
        ),
        _mock_response([{"id": 7, "name": "prod", "api_key": "env-key-1"}]),
        _mock_response([{"id": 99, "name": "checkout_v2"}]),
        _mock_response(
            {
                "environment_default": {"enabled": False, "value": None},
                "segment_overrides": [
                    {"segment": {"id": 500}, "priority": 0, "enabled": True, "value": None}
                ],
            }
        ),
        _mock_response(
            {
                "environment_default": {"enabled": False, "value": None},
                "segment_overrides": [
                    {"segment": {"id": 500}, "priority": 0, "enabled": False, "value": None}
                ],
            }
        ),
    ]

    client = FlagsmithManagementClient(
        FlagsmithConfig(api_url="http://localhost:8000", admin_key="tok"),
        session=session,
    )
    plan = build_plan("checkout_v2", "prod")
    change = client.apply_stage(plan, 10)
    assert change.percentage == 10
    assert change.enabled is True

    rb = client.rollback(plan)
    assert rb.percentage == 0
    assert rb.enabled is False


def test_simulator_sdk_evaluator_uses_persistent_identity() -> None:
    """FlagsmithSDKEvaluator passes persistent identifier to get_identity_flags."""
    mock_flags = MagicMock()
    mock_flags.is_feature_enabled.return_value = True
    mock_flags.get_feature_value.return_value = "v2"

    mock_sdk = MagicMock()
    mock_sdk.get_identity_flags.return_value = mock_flags

    with patch("flagpilot.simulator.Flagsmith", return_value=mock_sdk) as sdk_cls:
        evaluator = FlagsmithSDKEvaluator(
            environment_key="ser.fake",
            api_url="http://localhost:8000",
        )
        result = evaluator.evaluate("user-0001", "checkout_v2")

    sdk_cls.assert_called_once_with(
        environment_key="ser.fake",
        api_url="http://localhost:8000/api/v1/",
    )
    mock_sdk.get_identity_flags.assert_called_once_with(identifier="user-0001")
    assert result.enabled is True
    assert result.value == "v2"
    assert result.source_label == SYNTHETIC_LABEL

    with pytest.raises(ValueError):
        evaluator.evaluate("", "checkout_v2")


def test_notify_health_builds_alertmanager_payload_and_is_failsafe(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """notify_health sends Grafana/Alertmanager JSON, warns when unset/unreachable, never raises."""
    monkeypatch.delenv("FLAGSMITH_HEALTH_WEBHOOK_URL", raising=False)

    # Unset URL logs warning and returns False cleanly
    session_unset = MagicMock()
    session_unset.headers = {}
    client_unset = FlagsmithManagementClient(
        FlagsmithConfig(api_url="http://localhost:8000", admin_key="tok", project_id=1),
        session=session_unset,
    )
    with caplog.at_level("WARNING"):
        assert (
            client_unset.notify_health(
                "checkout_v2",
                "Development",
                status="firing",
                summary="spike",
                description="error_rate=0.2000 > max=0.0200",
            )
            is False
        )
    assert "FLAGSMITH_HEALTH_WEBHOOK_URL is not configured" in caplog.text
    session_unset.request.assert_not_called()

    # Configured URL posts firing and resolved payloads
    webhook = "http://localhost:8000/api/v1/feature-health/hook-secret-123/"
    session = MagicMock()
    session.headers = {}
    session.request.return_value = _mock_response({"status": "ok"}, 200)
    client = FlagsmithManagementClient(
        FlagsmithConfig(
            api_url="http://localhost:8000",
            admin_key="tok",
            project_id=1,
            health_webhook_url=webhook,
        ),
        session=session,
    )

    assert (
        client.notify_health(
            "checkout_v2",
            "Development",
            status="firing",
            summary="Rollback at 50%",
            description="Error rate 20.0% exceeded threshold 2.0%",
        )
        is True
    )
    call_firing = session.request.call_args.kwargs
    assert call_firing["method"] == "POST"
    assert call_firing["url"] == webhook
    firing_json = call_firing["json"]
    assert firing_json["status"] == "firing"
    alert = firing_json["alerts"][0]
    assert alert["labels"]["flagsmith_feature"] == "checkout_v2"
    assert alert["labels"]["flagsmith_environment"] == "Development"
    assert alert["annotations"]["summary"] == "Rollback at 50%"
    assert "20.0%" in alert["annotations"]["description"]
    assert alert["endsAt"] == "0001-01-01T00:00:00Z"

    assert (
        client.notify_health(
            "checkout_v2",
            "Development",
            status="resolved",
            summary="Rollout clean",
            description="Completed 4 stages",
        )
        is True
    )
    resolved_json = session.request.call_args.kwargs["json"]
    assert resolved_json["status"] == "resolved"
    assert resolved_json["alerts"][0]["endsAt"] != "0001-01-01T00:00:00Z"

    # Unreachable webhook or non-2xx never raises and scrubs secret webhook URL
    caplog.clear()
    session.request.return_value = _mock_response(None, 502, text=f"bad gateway for {webhook}")
    with caplog.at_level("WARNING"):
        assert (
            client.notify_health(
                "checkout_v2",
                "Development",
                status="firing",
                summary="s",
                description="d",
            )
            is False
        )
    assert "HTTP 502" in caplog.text
    assert "hook-secret-123" not in caplog.text

    caplog.clear()
    session.request.side_effect = requests.ConnectionError(f"failed reaching {webhook}")
    with caplog.at_level("WARNING"):
        assert (
            client.notify_health(
                "checkout_v2",
                "Development",
                status="firing",
                summary="s",
                description="d",
            )
            is False
        )
    assert "unreachable" in caplog.text
    assert "hook-secret-123" not in caplog.text
