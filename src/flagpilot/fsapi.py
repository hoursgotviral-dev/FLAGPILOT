"""Flagsmith REST Management API client.

Endpoints verified against local self-hosted Flagsmith 2.295.2.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import requests
from dotenv import load_dotenv

from flagpilot.types import Plan, RolloutChange

logger = logging.getLogger(__name__)


class FlagsmithAPIError(RuntimeError):
    """Raised when a Flagsmith Management API call fails, including sanitized body."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def _validate_percentage(percent: int) -> int:
    if isinstance(percent, bool) or not isinstance(percent, int):
        raise TypeError(f"percent must be an integer, got {percent!r}.")
    if not (0 <= percent <= 100):
        raise ValueError(f"percent must be between 0 and 100, got {percent}.")
    return percent


def _sanitize_secrets(text: str, secrets: tuple[str, ...]) -> str:
    sanitized = text
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized


@dataclass(frozen=True)
class FlagsmithConfig:
    """Configuration for connecting to the self-hosted Flagsmith Management API."""

    api_url: str
    admin_key: str = field(repr=False)
    environment_key: str | None = field(default=None, repr=False)
    project_id: int | None = None
    health_webhook_url: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> FlagsmithConfig:
        """Load Flagsmith URL and keys from environment variables (using python-dotenv)."""
        load_dotenv()
        api_url = os.environ.get("FLAGSMITH_API_URL", "").strip()
        admin_key = os.environ.get("FLAGSMITH_ADMIN_KEY", "").strip()
        environment_key = (
            os.environ.get("FLAGSMITH_ENVIRONMENT_KEY", "").strip()
            or os.environ.get("FLAGSMITH_SERVER_KEY", "").strip()
            or os.environ.get("FLAGSMITH_CLIENT_KEY", "").strip()
            or None
        )
        health_webhook_url = os.environ.get("FLAGSMITH_HEALTH_WEBHOOK_URL", "").strip() or None
        raw_project_id = os.environ.get("FLAGSMITH_PROJECT_ID", "").strip()
        project_id: int | None = None
        if raw_project_id:
            try:
                project_id = int(raw_project_id)
            except ValueError as exc:
                raise ValueError("FLAGSMITH_PROJECT_ID must be an integer.") from exc

        if not api_url:
            raise ValueError("FLAGSMITH_API_URL environment variable is required.")
        if not admin_key:
            raise ValueError("FLAGSMITH_ADMIN_KEY environment variable is required.")

        return cls(
            api_url=api_url,
            admin_key=admin_key,
            environment_key=environment_key,
            project_id=project_id,
            health_webhook_url=health_webhook_url,
        )


class FlagsmithManagementClient:
    """REST Management API client implementing FlagsmithClient and RolloutBackend.

    Uses Flagsmith Management API tokens (``Authorization: Api-Key <token>``),
    DRF trailing slashes, ``PERCENTAGE_SPLIT`` segment rules, and the unified
    ``/api/__future__/environments/{env_key}/features/{feature_id}/`` endpoint
    that works across both Feature Versioning v1 and v2 environments.
    """

    def __init__(
        self,
        config: FlagsmithConfig,
        session: requests.Session | None = None,
    ) -> None:
        self._config = config
        self._root_url = self._normalize_root_url(config.api_url)
        self._base_url = f"{self._root_url}api/v1/"
        self._timeout = config.timeout_seconds
        self._secrets: tuple[str, ...] = tuple(
            s
            for s in (
                config.admin_key,
                config.environment_key or "",
                config.health_webhook_url or "",
            )
            if s
        )
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Api-Key {config.admin_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    @classmethod
    def from_env(cls, session: requests.Session | None = None) -> FlagsmithManagementClient:
        """Construct a client configured from environment variables."""
        return cls(FlagsmithConfig.from_env(), session=session)

    @staticmethod
    def _normalize_root_url(api_url: str) -> str:
        clean = api_url.rstrip("/").removesuffix("/api/v1")
        return f"{clean}/"

    @staticmethod
    def _ensure_trailing_slash(path: str) -> str:
        return path if path.endswith("/") else f"{path}/"

    def _request_url(
        self,
        method: str,
        url: str,
        *,
        label: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self._session.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            safe_exc = _sanitize_secrets(str(exc), self._secrets)
            raise FlagsmithAPIError(
                f"Flagsmith Management API {method} {label} request error: {safe_exc}"
            ) from None

        if not response.ok:
            safe_body = _sanitize_secrets(response.text or "", self._secrets)
            raise FlagsmithAPIError(
                f"Flagsmith Management API {method} {label} failed "
                f"(HTTP {response.status_code}): {safe_body}",
                status_code=response.status_code,
                response_body=safe_body,
            )

        if response.status_code == 204 or not response.text.strip():
            return None
        return response.json()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        normalized_path = self._ensure_trailing_slash(path.lstrip("/"))
        if normalized_path.startswith("api/"):
            url = f"{self._root_url}{normalized_path}"
        else:
            url = f"{self._base_url}{normalized_path}"
        return self._request_url(
            method,
            url,
            label=normalized_path,
            params=params,
            json_body=json_body,
        )

    def _iter_paginated_results(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield items across Flagsmith DRF paginated responses following ``next`` links."""
        payload = self._request("GET", path, params=params)
        yield from self._extract_results(payload)
        seen_urls: set[str] = set()
        while isinstance(payload, dict) and payload.get("next"):
            raw_next = str(payload["next"]).strip()
            if not raw_next:
                break
            next_url = (
                raw_next
                if raw_next.startswith(("http://", "https://"))
                else f"{self._root_url}{raw_next.lstrip('/')}"
            )
            if next_url in seen_urls:
                break
            seen_urls.add(next_url)
            payload = self._request_url("GET", next_url, label=path)
            yield from self._extract_results(payload)

    @staticmethod
    def _extract_results(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return list(payload)
        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
            return list(payload["results"])
        return []

    @staticmethod
    def _percentage_split_rules(percent: int) -> list[dict[str, Any]]:
        return [
            {
                "type": "ALL",
                "rules": [],
                "conditions": [
                    {
                        "operator": "PERCENTAGE_SPLIT",
                        "property": None,
                        "value": str(percent),
                    }
                ],
            }
        ]

    def resolve_project_id(self, project_id: int | None = None) -> int:
        """Return explicit/configured project_id or resolve the first project."""
        if project_id is not None:
            return project_id
        if self._config.project_id is not None:
            return self._config.project_id
        payload = self._request("GET", "projects/")
        projects = self._extract_results(payload)
        if not projects:
            raise FlagsmithAPIError("No Flagsmith projects found for the configured credentials.")
        return int(projects[0]["id"])

    def resolve_environment(
        self,
        project_id: int,
        environment: str | None = None,
    ) -> tuple[int, str]:
        """Resolve environment numeric ID and API key from name/key or config."""
        target = environment or self._config.environment_key
        payload = self._request("GET", "environments/", params={"project": project_id})
        environments = self._extract_results(payload)
        if not environments:
            raise FlagsmithAPIError(f"No environments found in Flagsmith project {project_id}.")
        if target is None:
            first = environments[0]
            return int(first["id"]), str(first["api_key"])
        for env in environments:
            if env.get("api_key") == target or env.get("name") == target:
                return int(env["id"]), str(env["api_key"])
        raise FlagsmithAPIError(
            f"Environment {target!r} not found in Flagsmith project {project_id}."
        )

    def get_environment_details(self, environment_api_key: str) -> dict[str, Any]:
        """Return environment metadata including ``use_v2_feature_versioning``."""
        payload = self._request("GET", f"environments/{environment_api_key}/")
        return dict(payload)

    def _find_feature(self, project_id: int, feature_name: str) -> dict[str, Any] | None:
        for feat in self._iter_paginated_results(f"projects/{project_id}/features/"):
            if feat.get("name") == feature_name:
                return feat
        return None

    def _get_feature_id(self, project_id: int, feature_name: str) -> int:
        feat = self._find_feature(project_id, feature_name)
        if feat is None:
            raise FlagsmithAPIError(
                f"Feature {feature_name!r} not found in Flagsmith project {project_id}."
            )
        return int(feat["id"])

    def _get_future_feature_state(
        self,
        environment_api_key: str,
        feature_id: int,
    ) -> dict[str, Any]:
        """Fetch environment default and all segment overrides (works in v1 and v2)."""
        payload = self._request(
            "GET",
            f"api/__future__/environments/{environment_api_key}/features/{feature_id}/",
        )
        return dict(payload)

    def _patch_future_feature_state(
        self,
        environment_api_key: str,
        feature_id: int,
        json_body: dict[str, Any],
    ) -> dict[str, Any]:
        """Patch environment default or segment overrides (works in v1 and v2)."""
        payload = self._request(
            "PATCH",
            f"api/__future__/environments/{environment_api_key}/features/{feature_id}/",
            json_body=json_body,
        )
        return dict(payload)

    @staticmethod
    def _override_segment_id(override: dict[str, Any]) -> int | None:
        seg = override.get("segment")
        raw_id = seg.get("id") if isinstance(seg, dict) else seg
        return int(raw_id) if raw_id is not None else None

    @classmethod
    def _find_segment_override(
        cls,
        future_state: dict[str, Any],
        segment_id: int,
    ) -> dict[str, Any] | None:
        for override in future_state.get("segment_overrides", []):
            if cls._override_segment_id(override) == segment_id:
                return dict(override)
        return None

    @classmethod
    def _merge_segment_overrides(
        cls,
        existing_overrides: Sequence[dict[str, Any]],
        segment_id: int,
        target_override: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Update/insert ``target_override`` while preserving other segment overrides."""
        merged: list[dict[str, Any]] = []
        replaced = False
        for entry in existing_overrides:
            other_id = cls._override_segment_id(entry)
            if other_id == segment_id:
                if not replaced:
                    merged.append(target_override)
                    replaced = True
            elif other_id is not None:
                merged.append(
                    {
                        "segment": {"id": other_id},
                        "enabled": bool(entry.get("enabled", False)),
                        "priority": entry.get("priority", 0),
                        "value": entry.get("value"),
                    }
                )
        if not replaced:
            merged.append(target_override)
        return merged

    def create_feature(
        self,
        name: str,
        *,
        description: str = "",
        default_enabled: bool = False,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a standard project feature flag (or return it if it already exists)."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Feature name must not be empty.")
        resolved_project = self.resolve_project_id(project_id)
        existing = self._find_feature(resolved_project, clean_name)
        if existing is not None:
            return existing

        created = self._request(
            "POST",
            f"projects/{resolved_project}/features/",
            json_body={
                "name": clean_name,
                "type": "STANDARD",
                "default_enabled": default_enabled,
                "description": description,
            },
        )
        return dict(created)

    def create_segment_with_percentage_split(
        self,
        name: str,
        percent: int,
        *,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """Create (or update) a project segment using the PERCENTAGE_SPLIT operator."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Segment name must not be empty.")
        validated_percent = _validate_percentage(percent)
        resolved_project = self.resolve_project_id(project_id)

        existing = next(
            (
                seg
                for seg in self._iter_paginated_results(f"projects/{resolved_project}/segments/")
                if seg.get("name") == clean_name
            ),
            None,
        )
        body = {
            "name": clean_name,
            "description": f"FlagPilot staged rollout ({validated_percent}%)",
            "project": resolved_project,
            "rules": self._percentage_split_rules(validated_percent),
        }
        if existing is None:
            created = self._request(
                "POST",
                f"projects/{resolved_project}/segments/",
                json_body=body,
            )
            return dict(created)

        segment_id = int(existing["id"])
        updated = self._request(
            "PUT",
            f"projects/{resolved_project}/segments/{segment_id}/",
            json_body=body,
        )
        return dict(updated)

    def set_segment_percentage(
        self,
        segment_id: int,
        percent: int,
        *,
        name: str | None = None,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """Update the PERCENTAGE_SPLIT rule value on an existing project segment."""
        validated_percent = _validate_percentage(percent)
        resolved_project = self.resolve_project_id(project_id)
        segment_name = name
        if segment_name is None:
            current = self._request(
                "GET",
                f"projects/{resolved_project}/segments/{segment_id}/",
            )
            segment_name = str(current["name"])

        updated = self._request(
            "PUT",
            f"projects/{resolved_project}/segments/{segment_id}/",
            json_body={
                "name": segment_name,
                "description": f"FlagPilot staged rollout ({validated_percent}%)",
                "project": resolved_project,
                "rules": self._percentage_split_rules(validated_percent),
            },
        )
        return dict(updated)

    def attach_segment_override(
        self,
        feature_name: str,
        segment_id: int,
        *,
        enabled: bool = True,
        priority: int = 0,
        project_id: int | None = None,
        environment: str | None = None,
    ) -> dict[str, Any]:
        """Attach (or update) a segment override without altering the global default."""
        resolved_project = self.resolve_project_id(project_id)
        _, env_api_key = self.resolve_environment(resolved_project, environment)
        feature_id = self._get_feature_id(resolved_project, feature_name)
        current = self._get_future_feature_state(env_api_key, feature_id)
        existing = self._find_segment_override(current, segment_id)

        target_override = {
            "segment": {"id": segment_id},
            "enabled": enabled,
            "priority": existing.get("priority", priority) if existing else priority,
            "value": existing.get("value") if existing else None,
        }
        merged_overrides = self._merge_segment_overrides(
            current.get("segment_overrides", []),
            segment_id,
            target_override,
        )
        updated = self._patch_future_feature_state(
            env_api_key,
            feature_id,
            {"segment_overrides": merged_overrides},
        )
        result = self._find_segment_override(updated, segment_id)
        if result is None:
            raise FlagsmithAPIError(
                f"Segment override segment_id={segment_id} missing after PATCH on {feature_name!r}."
            )
        return result

    def disable_override(
        self,
        feature_name: str,
        segment_id: int | None = None,
        *,
        project_id: int | None = None,
        environment: str | None = None,
    ) -> dict[str, Any]:
        """Disable a segment override (or environment default when segment_id is None)."""
        resolved_project = self.resolve_project_id(project_id)
        _, env_api_key = self.resolve_environment(resolved_project, environment)
        feature_id = self._get_feature_id(resolved_project, feature_name)
        current = self._get_future_feature_state(env_api_key, feature_id)

        if segment_id is None:
            default_val = (current.get("environment_default") or {}).get("value")
            updated = self._patch_future_feature_state(
                env_api_key,
                feature_id,
                {"environment_default": {"enabled": False, "value": default_val}},
            )
            return dict(updated.get("environment_default") or {"enabled": False})

        existing = self._find_segment_override(current, segment_id)
        if existing is None:
            raise FlagsmithAPIError(
                f"No feature state found for feature {feature_name!r} (segment_id={segment_id})."
            )

        target_override = {
            "segment": {"id": segment_id},
            "enabled": False,
            "priority": existing.get("priority", 0),
            "value": existing.get("value"),
        }
        merged_overrides = self._merge_segment_overrides(
            current.get("segment_overrides", []),
            segment_id,
            target_override,
        )
        updated = self._patch_future_feature_state(
            env_api_key,
            feature_id,
            {"segment_overrides": merged_overrides},
        )
        result = self._find_segment_override(updated, segment_id)
        if result is None:
            raise FlagsmithAPIError(
                f"Segment override segment_id={segment_id} missing after disable PATCH."
            )
        return result

    def get_state(
        self,
        feature_name: str,
        segment_id: int | None = None,
        *,
        project_id: int | None = None,
        environment: str | None = None,
    ) -> dict[str, Any]:
        """Get current environment default and segment overrides (v1 and v2 compatible)."""
        resolved_project = self.resolve_project_id(project_id)
        environment_id, env_api_key = self.resolve_environment(resolved_project, environment)
        feature_id = self._get_feature_id(resolved_project, feature_name)
        future_state = self._get_future_feature_state(env_api_key, feature_id)
        segment_overrides = list(future_state.get("segment_overrides", []))
        selected_override = (
            self._find_segment_override(future_state, segment_id)
            if segment_id is not None
            else None
        )

        return {
            "project_id": resolved_project,
            "environment_id": environment_id,
            "feature_name": feature_name,
            "feature_id": feature_id,
            "default_state": future_state.get("environment_default"),
            "segment_override": selected_override,
            "segment_overrides": segment_overrides,
        }

    def apply_stage(self, plan: Plan, percentage: int) -> RolloutChange:
        """Apply a staged rollout percentage via PERCENTAGE_SPLIT without enabling global default."""
        segment_name = plan.segment_name or f"flagpilot-{plan.feature_name}-rollout"
        project_id = self.resolve_project_id(plan.project_id)
        _, env_api_key = self.resolve_environment(project_id, plan.environment)
        feature = self.create_feature(plan.feature_name, project_id=project_id)
        segment = self.create_segment_with_percentage_split(
            segment_name,
            percentage,
            project_id=project_id,
        )
        segment_id = int(segment["id"])
        enabled = percentage > 0
        if enabled:
            self.attach_segment_override(
                plan.feature_name,
                segment_id,
                enabled=True,
                project_id=project_id,
                environment=env_api_key,
            )
        else:
            state = self.get_state(
                plan.feature_name,
                segment_id=segment_id,
                project_id=project_id,
                environment=env_api_key,
            )
            if state["segment_override"] is not None:
                self.disable_override(
                    plan.feature_name,
                    segment_id=segment_id,
                    project_id=project_id,
                    environment=env_api_key,
                )
        return RolloutChange(
            feature_name=plan.feature_name,
            environment=plan.environment,
            segment_name=segment_name,
            percentage=percentage,
            enabled=enabled,
            details=f"project={project_id} feature_id={feature['id']} segment_id={segment_id}",
        )

    def rollback(self, plan: Plan) -> RolloutChange:
        """Roll back the staged rollout segment to ``plan.rollback_percentage`` (default 0%)."""
        return self.apply_stage(plan, plan.rollback_percentage)

    def get_feature_health_events(self, project_id: int | None = None) -> list[dict[str, Any]]:
        """List Feature Health events recorded for a project."""
        resolved_project = self.resolve_project_id(project_id)
        payload = self._request("GET", f"projects/{resolved_project}/feature-health/events/")
        return self._extract_results(payload)

    def build_alertmanager_payload(
        self,
        feature_name: str,
        environment: str,
        *,
        status: Literal["firing", "resolved"],
        summary: str,
        description: str,
    ) -> dict[str, Any]:
        """Build an Alertmanager-compatible payload matching Flagsmith GrafanaWebhookData."""
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        if status == "firing":
            starts_at = now_iso
            ends_at = "0001-01-01T00:00:00Z"
        else:
            starts_at = (now - timedelta(seconds=1)).isoformat()
            ends_at = now_iso

        labels = {
            "alertname": "FlagPilotErrorRateSpike",
            "flagsmith_feature": feature_name,
            "flagsmith_environment": environment,
        }
        annotations = {
            "summary": summary,
            "description": description,
        }
        return {
            "status": status,
            "alerts": [
                {
                    "status": status,
                    "labels": labels,
                    "annotations": annotations,
                    "startsAt": starts_at,
                    "endsAt": ends_at,
                    "generatorURL": self._root_url,
                    "fingerprint": f"flagpilot-{feature_name}-{environment}",
                }
            ],
            "commonLabels": labels,
            "commonAnnotations": annotations,
        }

    def notify_health(
        self,
        feature_name: str,
        environment: str,
        *,
        status: Literal["firing", "resolved"],
        summary: str,
        description: str,
    ) -> bool:
        """Fail-safe POST of Alertmanager JSON to ``FLAGSMITH_HEALTH_WEBHOOK_URL``."""
        webhook_url = (
            self._config.health_webhook_url
            or os.environ.get("FLAGSMITH_HEALTH_WEBHOOK_URL", "").strip()
        )
        if not webhook_url:
            logger.warning(
                "FLAGSMITH_HEALTH_WEBHOOK_URL is not configured; skipping Flagsmith Feature Health (%s) notification.",
                status,
            )
            return False

        payload = self.build_alertmanager_payload(
            feature_name,
            environment,
            status=status,
            summary=summary,
            description=description,
        )
        secrets = (*self._secrets, webhook_url)
        try:
            response = self._session.request(
                method="POST",
                url=webhook_url,
                json=payload,
                timeout=self._timeout,
            )
            if not response.ok:
                logger.warning(
                    "Flagsmith Feature Health webhook (%s) returned HTTP %s: %s",
                    status,
                    response.status_code,
                    _sanitize_secrets(response.text or "", secrets),
                )
                return False
        except requests.RequestException as exc:
            logger.warning(
                "Flagsmith Feature Health webhook (%s) unreachable: %s",
                status,
                _sanitize_secrets(str(exc), secrets),
            )
            return False
        return True
