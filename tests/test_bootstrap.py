"""Unit tests for idempotent Flagsmith bootstrap using mocked HTTP responses."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from flagpilot.bootstrap import BootstrapConfig, bootstrap_flagsmith


def _resp(payload: Any, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.ok = 200 <= status_code < 300
    r.text = "{}" if payload is not None else ""
    r.json.return_value = payload
    return r


def test_bootstrap_initial_and_idempotent_rerun(tmp_path: Path) -> None:
    """First run creates superuser/org/keys/demo flag; second run logs in and preserves keys."""
    env_file = tmp_path / ".env"
    session = MagicMock()
    session.headers = {}

    # Run 1: has_users=False -> creates superuser, org, master key, project, env, server key, flag, segment, override
    session.request.side_effect = [
        _resp({"status": "ok"}),  # GET /health/liveness/
        _resp({"self_hosted_data": {"has_users": False}}),  # GET /version/
        _resp({"key": "user-tok-1"}, 201),  # POST /api/v1/auth/users/
        _resp([]),  # GET /api/v1/organisations/
        _resp({"id": 10, "name": "FlagPilot Local"}, 201),  # POST /api/v1/organisations/
        _resp({"key": "adm_master_1"}, 201),  # POST /api/v1/organisations/10/master-api-keys/
        _resp([]),  # GET /api/v1/projects/
        _resp({"id": 20, "name": "FlagPilot Demo"}, 201),  # POST /api/v1/projects/
        _resp([]),  # GET /api/v1/environments/
        _resp({"id": 30, "name": "Development", "api_key": "cli_key_1"}, 201),  # POST /api/v1/environments/
        _resp({"key": "ser.srv_key_1"}, 201),  # POST /api/v1/environments/cli_key_1/api-keys/
        # FlagsmithManagementClient:
        _resp([]),  # GET projects/20/features/
        _resp({"id": 40, "name": "checkout_v2"}, 201),  # POST projects/20/features/
        _resp([]),  # GET projects/20/segments/
        _resp({"id": 50, "name": "flagpilot-checkout_v2-rollout"}, 201),  # POST projects/20/segments/
        _resp([{"id": 30, "name": "Development", "api_key": "cli_key_1"}]),  # GET environments/
        _resp([{"id": 40, "name": "checkout_v2"}]),  # GET projects/20/features/
        _resp(
            {"environment_default": {"enabled": False, "value": None}, "segment_overrides": []}
        ),
        _resp(
            {
                "environment_default": {"enabled": False, "value": None},
                "segment_overrides": [
                    {"segment": {"id": 50}, "priority": 0, "enabled": True, "value": None}
                ],
            }
        ),
        # Feature Health provider (GET empty -> POST Grafana)
        _resp([]),
        _resp(
            {
                "name": "Grafana",
                "project": 20,
                "webhook_url": "http://localhost:8000/api/v1/feature-health/hook-tok-1/",
            },
            201,
        ),
    ]

    out1 = io.StringIO()
    cfg = BootstrapConfig(base_url="http://localhost:8000", env_path=env_file)
    res1 = bootstrap_flagsmith(cfg, session=session, output_stream=out1)

    assert res1.has_users_before is False
    assert res1.org_id == 10
    assert res1.project_id == 20
    assert res1.environment_id == 30
    assert res1.feature_id == 40
    assert res1.segment_id == 50
    assert res1.preserved_master_key is False
    assert res1.preserved_server_key is False
    assert res1.has_health_webhook is True
    assert "adm_master_1" not in out1.getvalue()
    assert "ser.srv_key_1" not in out1.getvalue()
    assert "user-tok-1" not in out1.getvalue()
    assert "hook-tok-1" not in out1.getvalue()

    env_text = env_file.read_text(encoding="utf-8")
    assert "FLAGSMITH_API_URL=http://localhost:8000" in env_text
    assert "FLAGSMITH_ADMIN_KEY=adm_master_1" in env_text
    assert "FLAGSMITH_CLIENT_KEY=cli_key_1" in env_text
    assert "FLAGSMITH_SERVER_KEY=ser.srv_key_1" in env_text
    assert "FLAGSMITH_PROJECT_ID=20" in env_text
    assert "FLAGSMITH_FEATURE_ID=40" in env_text
    assert "FLAGSMITH_SEGMENT_ID=50" in env_text
    assert (
        "FLAGSMITH_HEALTH_WEBHOOK_URL=http://localhost:8000/api/v1/feature-health/hook-tok-1/"
        in env_text
    )

    # Run 2: has_users=True -> logs in, preserves existing master & server keys, updates existing resources
    session2 = MagicMock()
    session2.headers = {}
    session2.request.side_effect = [
        _resp({"status": "ok"}),  # GET /health/liveness/
        _resp({"self_hosted_data": {"has_users": True}}),  # GET /version/
        _resp({"key": "user-tok-2"}),  # POST /api/v1/auth/login/
        _resp([{"id": 10, "name": "FlagPilot Local"}]),  # GET /api/v1/organisations/
        _resp([{"id": 20}]),  # _is_master_key_valid -> GET /api/v1/projects/
        _resp([{"id": 20, "name": "FlagPilot Demo"}]),  # GET /api/v1/projects/
        _resp([{"id": 30, "name": "Development", "api_key": "cli_key_1", "project": 20}]),
        _resp({"flags": []}),  # _is_server_key_valid -> GET /api/v1/environment-document/
        # FlagsmithManagementClient:
        _resp([{"id": 40, "name": "checkout_v2"}]),  # GET projects/20/features/
        _resp([{"id": 50, "name": "flagpilot-checkout_v2-rollout"}]),  # GET projects/20/segments/
        _resp({"id": 50, "name": "flagpilot-checkout_v2-rollout"}),  # PUT projects/20/segments/50/
        _resp([{"id": 30, "name": "Development", "api_key": "cli_key_1"}]),  # GET environments/
        _resp([{"id": 40, "name": "checkout_v2"}]),  # GET projects/20/features/
        _resp(
            {
                "environment_default": {"enabled": False, "value": None},
                "segment_overrides": [
                    {"segment": {"id": 50}, "priority": 0, "enabled": True, "value": None}
                ],
            }
        ),
        _resp(
            {
                "environment_default": {"enabled": False, "value": None},
                "segment_overrides": [
                    {"segment": {"id": 50}, "priority": 0, "enabled": True, "value": None}
                ],
            }
        ),
        # Feature Health provider already exists -> preserves without POST
        _resp(
            [
                {
                    "name": "Grafana",
                    "project": 20,
                    "webhook_url": "http://localhost:8000/api/v1/feature-health/hook-tok-1/",
                }
            ]
        ),
    ]

    out2 = io.StringIO()
    res2 = bootstrap_flagsmith(cfg, session=session2, output_stream=out2)
    assert res2.has_users_before is True
    assert res2.preserved_master_key is True
    assert res2.preserved_server_key is True
    assert res2.feature_id == 40
    assert res2.segment_id == 50
    assert res2.has_health_webhook is True
    assert "adm_master_1" not in out2.getvalue()
    assert "ser.srv_key_1" not in out2.getvalue()
    assert "hook-tok-1" not in out2.getvalue()

