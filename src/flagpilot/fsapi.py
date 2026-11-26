"""Flagsmith REST Management API client.

All Flagsmith HTTP calls live exclusively in this module and are marked
``UNVERIFIED: check against local instance`` until verified against a running
self-hosted Flagsmith server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import requests
from dotenv import load_dotenv

from flagpilot.types import Plan, RolloutChange


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
        )


class FlagsmithManagementClient:
    """REST Management API client implementing FlagsmithClient and RolloutBackend.

    Uses Flagsmith Management API tokens (``Authorization: Api-Key <token>``)
    and ``PERCENTAGE_SPLIT`` segment rules per Flagsmith documentation.
    """

    def __init__(
        self,
        config: FlagsmithConfig,
        session: requests.Session | None = None,
    ) -> None:
        self._config = config
        self._base_url = self._normalize_api_v1_url(config.api_url)
        self._timeout = config.timeout_seconds
        self._secrets: tuple[str, ...] = tuple(
            s for s in (config.admin_key, config.environment_key or "") if s
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
    def _normalize_api_v1_url(api_url: str) -> str:
        clean = api_url.rstrip("/")
        if clean.endswith("/api/v1"):
            return f"{clean}/"
        return f"{clean}/api/v1/"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path.lstrip('/')}"
        try:
            # UNVERIFIED: check against local instance
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
                f"Flagsmith Management API {method} {path} request error: {safe_exc}"
            ) from None

        if not response.ok:
            safe_body = _sanitize_secrets(response.text or "", self._secrets)
            raise FlagsmithAPIError(
                f"Flagsmith Management API {method} {path} failed "
                f"(HTTP {response.status_code}): {safe_body}",
                status_code=response.status_code,
                response_body=safe_body,
            )

        if response.status_code == 204 or not response.text.strip():
            return None
        return response.json()

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
        # UNVERIFIED: check against local instance
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
        # UNVERIFIED: check against local instance
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

    def _find_feature(self, project_id: int, feature_name: str) -> dict[str, Any] | None:
        # UNVERIFIED: check against local instance
        payload = self._request("GET", f"projects/{project_id}/features/")
        for feat in self._extract_results(payload):
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

    def _list_feature_states(self, environment_api_key: str) -> list[dict[str, Any]]:
        # UNVERIFIED: check against local instance
        payload = self._request("GET", f"environments/{environment_api_key}/featurestates/")
        return self._extract_results(payload)

    @staticmethod
    def _state_feature_id(state: dict[str, Any]) -> int | None:
        feature = state.get("feature")
        if isinstance(feature, dict) and "id" in feature:
            return int(feature["id"])
        if isinstance(feature, int):
            return feature
        return None

    @staticmethod
    def _state_segment_id(state: dict[str, Any]) -> int | None:
        feature_segment = state.get("feature_segment")
        if isinstance(feature_segment, dict) and feature_segment.get("segment") is not None:
            return int(feature_segment["segment"])
        return None

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

        # UNVERIFIED: check against local instance
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

        # UNVERIFIED: check against local instance
        payload = self._request("GET", f"projects/{resolved_project}/segments/")
        existing = next(
            (seg for seg in self._extract_results(payload) if seg.get("name") == clean_name),
            None,
        )
        body = {
            "name": clean_name,
            "description": f"FlagPilot staged rollout ({validated_percent}%)",
            "project": resolved_project,
            "rules": self._percentage_split_rules(validated_percent),
        }
        if existing is None:
            # UNVERIFIED: check against local instance
            created = self._request(
                "POST",
                f"projects/{resolved_project}/segments/",
                json_body=body,
            )
            return dict(created)

        segment_id = int(existing["id"])
        # UNVERIFIED: check against local instance
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
            # UNVERIFIED: check against local instance
            current = self._request(
                "GET",
                f"projects/{resolved_project}/segments/{segment_id}/",
            )
            segment_name = str(current["name"])

        # UNVERIFIED: check against local instance
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
        """Attach (or update) a segment override for a feature in the target environment."""
        resolved_project = self.resolve_project_id(project_id)
        environment_id, env_api_key = self.resolve_environment(resolved_project, environment)
        feature_id = self._get_feature_id(resolved_project, feature_name)

        for state in self._list_feature_states(env_api_key):
            if (
                self._state_feature_id(state) == feature_id
                and self._state_segment_id(state) == segment_id
            ):
                state_id = int(state["id"])
                # UNVERIFIED: check against local instance
                patched = self._request(
                    "PATCH",
                    f"environments/{env_api_key}/featurestates/{state_id}/",
                    json_body={"enabled": enabled},
                )
                return dict(patched)

        # UNVERIFIED: check against local instance
        created_segment = self._request(
            "POST",
            "features/feature-segments/",
            json_body={
                "feature": feature_id,
                "segment": segment_id,
                "environment": environment_id,
                "priority": priority,
            },
        )
        feature_segment_id = int(created_segment["id"])

        # UNVERIFIED: check against local instance
        created_state = self._request(
            "POST",
            "features/featurestates/",
            json_body={
                "enabled": enabled,
                "feature": feature_id,
                "environment": environment_id,
                "feature_segment": feature_segment_id,
            },
        )
        return dict(created_state)

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

        for state in self._list_feature_states(env_api_key):
            if self._state_feature_id(state) != feature_id:
                continue
            state_seg_id = self._state_segment_id(state)
            if (segment_id is None and state_seg_id is None) or (
                segment_id is not None and state_seg_id == segment_id
            ):
                state_id = int(state["id"])
                # UNVERIFIED: check against local instance
                patched = self._request(
                    "PATCH",
                    f"environments/{env_api_key}/featurestates/{state_id}/",
                    json_body={"enabled": False},
                )
                return dict(patched)

        target_desc = f"segment_id={segment_id}" if segment_id is not None else "environment default"
        raise FlagsmithAPIError(
            f"No feature state found for feature {feature_name!r} ({target_desc})."
        )

    def get_state(
        self,
        feature_name: str,
        segment_id: int | None = None,
        *,
        project_id: int | None = None,
        environment: str | None = None,
    ) -> dict[str, Any]:
        """Get current feature state and segment overrides in the target environment."""
        resolved_project = self.resolve_project_id(project_id)
        environment_id, env_api_key = self.resolve_environment(resolved_project, environment)
        feature_id = self._get_feature_id(resolved_project, feature_name)
        matching_states = [
            state
            for state in self._list_feature_states(env_api_key)
            if self._state_feature_id(state) == feature_id
        ]

        default_state = next(
            (s for s in matching_states if self._state_segment_id(s) is None),
            None,
        )
        segment_overrides = [s for s in matching_states if self._state_segment_id(s) is not None]
        selected_override = (
            next(
                (s for s in segment_overrides if self._state_segment_id(s) == segment_id),
                None,
            )
            if segment_id is not None
            else None
        )

        return {
            "project_id": resolved_project,
            "environment_id": environment_id,
            "feature_name": feature_name,
            "feature_id": feature_id,
            "default_state": default_state,
            "segment_override": selected_override,
            "segment_overrides": segment_overrides,
        }

    def apply_stage(self, plan: Plan, percentage: int) -> RolloutChange:
        """Apply a staged rollout percentage via a PERCENTAGE_SPLIT segment override."""
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
        """Roll back the staged rollout segment to ``plan.rollback_percentage``."""
        return self.apply_stage(plan, plan.rollback_percentage)
