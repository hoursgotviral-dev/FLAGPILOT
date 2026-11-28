"""Idempotent zero-UI bootstrap for a local self-hosted Flagsmith instance."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values, load_dotenv

from flagpilot.fsapi import FlagsmithConfig, FlagsmithManagementClient

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_EMAIL = "admin@flagpilot.example"
DEFAULT_PASSWORD = "FlagPilotLocalDev!2026"
DEFAULT_FIRST_NAME = "FlagPilot"
DEFAULT_LAST_NAME = "Admin"
DEFAULT_ORG_NAME = "FlagPilot Local"
DEFAULT_PROJECT_NAME = "FlagPilot Demo"
DEFAULT_ENV_NAME = "Development"
DEFAULT_FEATURE_NAME = "checkout_v2"
DEFAULT_SEGMENT_NAME = "flagpilot-checkout_v2-rollout"


class BootstrapError(RuntimeError):
    """Raised when bootstrapping self-hosted Flagsmith fails."""


def _default_admin_password() -> str:
    return os.environ.get("FLAGPILOT_ADMIN_PASSWORD") or DEFAULT_PASSWORD


@dataclass(frozen=True)
class BootstrapConfig:
    """Configuration for automated local Flagsmith bootstrap."""

    base_url: str = DEFAULT_BASE_URL
    email: str = DEFAULT_EMAIL
    password: str = field(default_factory=_default_admin_password, repr=False)
    first_name: str = DEFAULT_FIRST_NAME
    last_name: str = DEFAULT_LAST_NAME
    org_name: str = DEFAULT_ORG_NAME
    project_name: str = DEFAULT_PROJECT_NAME
    env_name: str = DEFAULT_ENV_NAME
    feature_name: str = DEFAULT_FEATURE_NAME
    segment_name: str = DEFAULT_SEGMENT_NAME
    env_path: Path = Path(".env")
    timeout_seconds: float = 10.0
    liveness_timeout_seconds: float = 300.0
    poll_interval_seconds: float = 2.0
    progress_interval_seconds: float = 15.0


@dataclass(frozen=True)
class BootstrapResult:
    """Summary of bootstrapped resources (secrets excluded from repr/output)."""

    api_url: str
    has_users_before: bool
    org_id: int
    project_id: int
    environment_id: int
    feature_id: int
    segment_id: int
    preserved_master_key: bool
    preserved_server_key: bool
    env_path: Path
    has_health_webhook: bool = False


def _scrub(text: str, secrets: Sequence[str]) -> str:
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


def _extract_results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return list(payload["results"])
    return []


def _ensure_grafana_health_provider(
    session: requests.Session,
    base: str,
    project_id: int,
    *,
    headers: dict[str, str],
    timeout: float,
    secrets: list[str],
    out: Any,
) -> str:
    """Idempotently fetch or create the project's Grafana Feature Health provider webhook URL."""
    url = f"{base}/api/v1/projects/{project_id}/feature-health/providers/"
    try:
        existing_payload = _request_json(
            session,
            "GET",
            url,
            headers=headers,
            timeout=timeout,
            secrets=secrets,
        )
        providers = _extract_results(existing_payload)
        provider = next(
            (p for p in providers if str(p.get("name", "")).strip().lower() == "grafana"),
            None,
        )
        if provider is None:
            provider = _request_json(
                session,
                "POST",
                url,
                headers=headers,
                json_body={"name": "Grafana"},
                timeout=timeout,
                secrets=secrets,
            )
            out.write(f"Created Grafana Feature Health provider for project id={project_id}\n")
        else:
            out.write(
                f"Preserved existing Grafana Feature Health provider for project id={project_id}\n"
            )
    except BootstrapError as exc:
        out.write(
            f"Warning: Grafana Feature Health provider unavailable ({_scrub(str(exc), secrets)}); "
            "continuing without FLAGSMITH_HEALTH_WEBHOOK_URL\n"
        )
        return ""

    webhook_url = str((provider or {}).get("webhook_url") or "").strip()
    if webhook_url:
        secrets.append(webhook_url)
    return webhook_url


def _request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = 10.0,
    secrets: Sequence[str] = (),
) -> Any:
    try:
        response = session.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise BootstrapError(f"{method} {url} failed: {_scrub(str(exc), secrets)}") from None

    if not response.ok:
        body = _scrub(response.text or "", secrets)
        raise BootstrapError(
            f"{method} {url} failed with HTTP {response.status_code}: {body}"
        )

    if response.status_code == 204 or not response.text.strip():
        return None
    return response.json()


def wait_for_liveness(
    session: requests.Session,
    base_url: str,
    *,
    timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 2.0,
    progress_interval_seconds: float = 15.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    output_stream: Any = None,
) -> None:
    """Poll ``<base_url>/health/liveness/`` until HTTP 200 is returned."""
    out = output_stream if output_stream is not None else sys.stdout
    liveness_url = f"{base_url.rstrip('/')}/health/liveness/"
    start = monotonic_fn()
    deadline = start + timeout_seconds
    next_progress = progress_interval_seconds
    last_error = "no response yet"
    while True:
        try:
            resp = session.request(method="GET", url=liveness_url, timeout=5.0)
            if resp.status_code == 200:
                return
            last_error = f"HTTP {resp.status_code}: {resp.text}"
        except requests.RequestException as exc:
            last_error = str(exc)

        now = monotonic_fn()
        if now >= deadline:
            raise BootstrapError(
                f"Timed out waiting for {liveness_url} after {timeout_seconds:.0f}s "
                f"(first start / database migration can take a few minutes): {last_error}"
            )
        elapsed = now - start
        if progress_interval_seconds > 0 and elapsed >= next_progress:
            out.write(
                f"Waiting for Flagsmith to start, first start can take a few minutes ({elapsed:.0f}s)...\n"
            )
            if hasattr(out, "flush"):
                out.flush()
            while next_progress <= elapsed:
                next_progress += progress_interval_seconds
        sleep_fn(poll_interval_seconds)


def _upsert_env_file(env_path: Path, updates: dict[str, str]) -> None:
    """Update or append key=value entries in ``env_path`` without printing secrets."""
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key, _ = line.split("=", 1)
        clean_key = key.strip()
        if clean_key in remaining:
            new_lines.append(f"{clean_key}={remaining.pop(clean_key)}")
        else:
            new_lines.append(line)

    for key, value in remaining.items():
        new_lines.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _is_master_key_valid(
    session: requests.Session,
    base_url: str,
    master_key: str,
    timeout: float,
) -> bool:
    if not master_key:
        return False
    url = f"{base_url.rstrip('/')}/api/v1/projects/"
    try:
        resp = session.request(
            method="GET",
            url=url,
            headers={"Authorization": f"Api-Key {master_key}", "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException:
        return False
    return resp.ok


def _is_server_key_valid(
    session: requests.Session,
    base_url: str,
    server_key: str,
    timeout: float,
) -> bool:
    if not server_key or not server_key.startswith("ser."):
        return False
    url = f"{base_url.rstrip('/')}/api/v1/environment-document/"
    try:
        resp = session.request(
            method="GET",
            url=url,
            headers={"X-Environment-Key": server_key, "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException:
        return False
    return resp.ok


def bootstrap_flagsmith(
    config: BootstrapConfig,
    *,
    session: requests.Session | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    output_stream: Any = None,
) -> BootstrapResult:
    """Run idempotent zero-UI Flagsmith bootstrap and write values to ``config.env_path``."""
    out = output_stream if output_stream is not None else sys.stdout
    http = session or requests.Session()
    base = config.base_url.rstrip("/")
    secrets: list[str] = [config.password]

    existing_env: dict[str, str] = {}
    if config.env_path.exists():
        existing_env = {
            k: v for k, v in dotenv_values(config.env_path).items() if k and v is not None
        }
        for secret_var in (
            "FLAGSMITH_ADMIN_KEY",
            "FLAGSMITH_SERVER_KEY",
            "FLAGSMITH_CLIENT_KEY",
            "FLAGSMITH_HEALTH_WEBHOOK_URL",
        ):
            if existing_env.get(secret_var):
                secrets.append(existing_env[secret_var])

    # 1. Poll /health/liveness/ and inspect /version/
    wait_for_liveness(
        http,
        base,
        timeout_seconds=config.liveness_timeout_seconds,
        poll_interval_seconds=config.poll_interval_seconds,
        progress_interval_seconds=config.progress_interval_seconds,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
        output_stream=out,
    )
    out.write(f"Flagsmith liveness OK at {base}/health/liveness/\n")

    version_data = _request_json(
        http,
        "GET",
        f"{base}/version/",
        timeout=config.timeout_seconds,
        secrets=secrets,
    )
    has_users = bool((version_data or {}).get("self_hosted_data", {}).get("has_users", False))
    out.write(f"Flagsmith /version/ self_hosted_data.has_users={has_users}\n")

    # 2. Authenticate or create initial superuser
    if not has_users:
        auth_data = _request_json(
            http,
            "POST",
            f"{base}/api/v1/auth/users/",
            json_body={
                "email": config.email,
                "password": config.password,
                "re_password": config.password,
                "first_name": config.first_name,
                "last_name": config.last_name,
                "superuser": True,
            },
            timeout=config.timeout_seconds,
            secrets=secrets,
        )
        out.write(f"Created initial superuser ({config.email})\n")
    else:
        auth_data = _request_json(
            http,
            "POST",
            f"{base}/api/v1/auth/login/",
            json_body={"email": config.email, "password": config.password},
            timeout=config.timeout_seconds,
            secrets=secrets,
        )
        out.write(f"Authenticated existing user ({config.email})\n")

    user_token = str(auth_data["key"])
    secrets.append(user_token)
    token_headers = {
        "Authorization": f"Token {user_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # 3. Get or create Organisation and mint/preserve Master API Key
    orgs_payload = _request_json(
        http,
        "GET",
        f"{base}/api/v1/organisations/",
        headers=token_headers,
        timeout=config.timeout_seconds,
        secrets=secrets,
    )
    orgs = _extract_results(orgs_payload)
    org = next((o for o in orgs if o.get("name") == config.org_name), None)
    if org is None:
        org = _request_json(
            http,
            "POST",
            f"{base}/api/v1/organisations/",
            headers=token_headers,
            json_body={"name": config.org_name},
            timeout=config.timeout_seconds,
            secrets=secrets,
        )
    org_id = int(org["id"])

    existing_master = existing_env.get("FLAGSMITH_ADMIN_KEY", "").strip()
    preserved_master = _is_master_key_valid(http, base, existing_master, config.timeout_seconds)
    if preserved_master:
        master_key = existing_master
        out.write(f"Preserved existing valid Master API Key for organisation id={org_id}\n")
    else:
        key_payload = _request_json(
            http,
            "POST",
            f"{base}/api/v1/organisations/{org_id}/master-api-keys/",
            headers=token_headers,
            json_body={"name": "flagpilot-bootstrap-admin", "is_admin": True},
            timeout=config.timeout_seconds,
            secrets=secrets,
        )
        master_key = str(key_payload["key"])
        secrets.append(master_key)
        out.write(f"Minted new Master API Key for organisation id={org_id}\n")

    api_key_headers = {
        "Authorization": f"Api-Key {master_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # 4. Get or create Project
    projects_payload = _request_json(
        http,
        "GET",
        f"{base}/api/v1/projects/",
        headers=api_key_headers,
        timeout=config.timeout_seconds,
        secrets=secrets,
    )
    projects = _extract_results(projects_payload)
    project = next((p for p in projects if p.get("name") == config.project_name), None)
    if project is None:
        project = _request_json(
            http,
            "POST",
            f"{base}/api/v1/projects/",
            headers=api_key_headers,
            json_body={"name": config.project_name, "organisation": org_id},
            timeout=config.timeout_seconds,
            secrets=secrets,
        )
    project_id = int(project["id"])

    # 5. Get or create Environment and Server-Side SDK key
    envs_payload = _request_json(
        http,
        "GET",
        f"{base}/api/v1/environments/",
        headers=api_key_headers,
        params={"project": project_id},
        timeout=config.timeout_seconds,
        secrets=secrets,
    )
    envs = [
        e for e in _extract_results(envs_payload) if int(e.get("project", project_id)) == project_id
    ]
    env = next((e for e in envs if e.get("name") == config.env_name), None)
    if env is None:
        env = _request_json(
            http,
            "POST",
            f"{base}/api/v1/environments/",
            headers=api_key_headers,
            json_body={"name": config.env_name, "project": project_id},
            timeout=config.timeout_seconds,
            secrets=secrets,
        )
    environment_id = int(env["id"])
    client_key = str(env["api_key"])
    secrets.append(client_key)

    existing_server = existing_env.get("FLAGSMITH_SERVER_KEY", "").strip()
    preserved_server = _is_server_key_valid(http, base, existing_server, config.timeout_seconds)
    if preserved_server:
        server_key = existing_server
        out.write(f"Preserved existing valid server key for environment id={environment_id}\n")
    else:
        server_payload = _request_json(
            http,
            "POST",
            f"{base}/api/v1/environments/{client_key}/api-keys/",
            headers=api_key_headers,
            json_body={"name": "flagpilot-server-key"},
            timeout=config.timeout_seconds,
            secrets=secrets,
        )
        server_key = str(server_payload["key"])
        secrets.append(server_key)
        out.write(f"Created server-side SDK key for environment id={environment_id}\n")

    # 6. Create demo feature (default OFF), 0% PERCENTAGE_SPLIT segment, and attach override
    mgmt = FlagsmithManagementClient(
        FlagsmithConfig(
            api_url=base,
            admin_key=master_key,
            environment_key=client_key,
            project_id=project_id,
            timeout_seconds=config.timeout_seconds,
        ),
        session=http,
    )
    feature = mgmt.create_feature(config.feature_name, default_enabled=False, project_id=project_id)
    feature_id = int(feature["id"])

    segment = mgmt.create_segment_with_percentage_split(
        config.segment_name,
        0,
        project_id=project_id,
    )
    segment_id = int(segment["id"])

    mgmt.attach_segment_override(
        config.feature_name,
        segment_id,
        enabled=True,
        project_id=project_id,
        environment=client_key,
    )

    # 7. Idempotently get or create Grafana Feature Health provider webhook URL
    health_webhook_url = _ensure_grafana_health_provider(
        http,
        base,
        project_id,
        headers=api_key_headers,
        timeout=config.timeout_seconds,
        secrets=secrets,
        out=out,
    )
    if not health_webhook_url:
        health_webhook_url = existing_env.get("FLAGSMITH_HEALTH_WEBHOOK_URL", "").strip()
        if health_webhook_url:
            secrets.append(health_webhook_url)

    # 8. Persist non-printed config + secrets into local .env
    env_updates: dict[str, str] = {
        "FLAGSMITH_API_URL": base,
        "FLAGSMITH_ADMIN_KEY": master_key,
        "FLAGSMITH_CLIENT_KEY": client_key,
        "FLAGSMITH_SERVER_KEY": server_key,
        "FLAGSMITH_ENVIRONMENT_KEY": client_key,
        "FLAGSMITH_PROJECT_ID": str(project_id),
        "FLAGSMITH_FEATURE_ID": str(feature_id),
        "FLAGSMITH_SEGMENT_ID": str(segment_id),
    }
    if health_webhook_url:
        env_updates["FLAGSMITH_HEALTH_WEBHOOK_URL"] = health_webhook_url

    _upsert_env_file(config.env_path, env_updates)

    out.write(
        f"Bootstrapped org_id={org_id} project_id={project_id} "
        f"environment_id={environment_id} feature_id={feature_id} "
        f"segment_id={segment_id} (0% split, override enabled=true); "
        f"wrote {config.env_path} (secrets redacted).\n"
    )
    return BootstrapResult(
        api_url=base,
        has_users_before=has_users,
        org_id=org_id,
        project_id=project_id,
        environment_id=environment_id,
        feature_id=feature_id,
        segment_id=segment_id,
        preserved_master_key=preserved_master,
        preserved_server_key=preserved_server,
        env_path=config.env_path,
        has_health_webhook=bool(health_webhook_url),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``python -m flagpilot.bootstrap``."""
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="python -m flagpilot.bootstrap",
        description="Bootstrap local self-hosted Flagsmith and write .env.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--org-name", default=DEFAULT_ORG_NAME)
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--env-name", default=DEFAULT_ENV_NAME)
    parser.add_argument("--feature-name", default=DEFAULT_FEATURE_NAME)
    parser.add_argument("--segment-name", default=DEFAULT_SEGMENT_NAME)
    parser.add_argument(
        "--wait-only",
        action="store_true",
        help="Wait for Flagsmith /health/liveness/ up to 300s without provisioning.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    cfg = BootstrapConfig(
        base_url=args.base_url,
        env_path=args.env_file,
        org_name=args.org_name,
        project_name=args.project_name,
        env_name=args.env_name,
        feature_name=args.feature_name,
        segment_name=args.segment_name,
    )
    try:
        if args.wait_only:
            wait_for_liveness(
                requests.Session(),
                cfg.base_url,
                timeout_seconds=cfg.liveness_timeout_seconds,
                poll_interval_seconds=cfg.poll_interval_seconds,
                progress_interval_seconds=cfg.progress_interval_seconds,
            )
            print(f"Flagsmith liveness OK at {cfg.base_url.rstrip('/')}/health/liveness/")
        else:
            bootstrap_flagsmith(cfg)
    except BootstrapError as exc:
        print(f"Bootstrap error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

