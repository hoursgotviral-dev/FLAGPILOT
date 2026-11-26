"""Deterministic, rule-based staged rollout planner (no external LLM, no network)."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from flagpilot.types import BreachAction, Plan, Step

logger = logging.getLogger(__name__)

DEFAULT_STAGES: tuple[int, ...] = (1, 10, 50, 100)

SUPPORTED_PHRASES: tuple[str, ...] = (
    (
        "Feature & target: 'roll out <feature> to <N>%' | 'enable <feature> at <N>%' | "
        "'stage <feature> to <N>%'"
    ),
    "Staged percentages (optional): 'through 1%, 10%, 50%, 100%'",
    "Environment: 'in <environment>' | 'on <environment>' | 'in the <environment> environment'",
    (
        "Threshold & action: 'pause if errors exceed <T>%' | 'rollback if errors exceed <T>%' | "
        "'roll back if error rate exceeds <T>%'"
    ),
)

_FEATURE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:roll\s+out|rollout|enable|stage|deploy)\s+(?:feature\s+)?([A-Za-z0-9][A-Za-z0-9_.-]*)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bfeature\s+([A-Za-z0-9][A-Za-z0-9_.-]*)\b",
        re.IGNORECASE,
    ),
)

_STAGES_LIST_PATTERN = re.compile(
    r"\b(?:through|stages?)\s+((?:\d{1,3}\s*%(?:\s*(?:,|->|and)\s*)?)+)",
    re.IGNORECASE,
)

_TARGET_PERCENT_PATTERN = re.compile(
    r"\b(?:to|at|for)\s+(\d{1,3})\s*%",
    re.IGNORECASE,
)

_ENVIRONMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:in|on)\s+(?:the\s+)?([A-Za-z0-9][A-Za-z0-9_.-]*)\s+environment\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:in|on)\s+([A-Za-z0-9][A-Za-z0-9_.-]*)(?=\s*(?:,|;|\.|if\b|pause\b|roll\s*back\b|rollback\b|$))",
        re.IGNORECASE,
    ),
)

_THRESHOLD_PATTERN = re.compile(
    r"\b(?P<action>pause|roll\s*back|rollback)?\s*(?:if\s+)?(?:errors?|error\s+rate)\s*"
    r"(?:exceeds?|above|>|is\s+above)\s*(?P<pct>\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

_ACTION_THRESHOLD_ALT_PATTERN = re.compile(
    r"\b(?P<action>pause|roll\s*back|rollback)\s+(?:when|above)\s+(?P<pct>\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)


class PlanValidationError(ValueError):
    """Raised when a rollout plan specification is invalid."""


class PlanParseError(PlanValidationError):
    """Raised when a plain-English rollout request cannot be parsed."""

    def __init__(self, missing_parts: Sequence[str], details: str = "") -> None:
        self.missing_parts = tuple(missing_parts)
        parts_text = ", ".join(self.missing_parts)
        phrases = "\n  - ".join(SUPPORTED_PHRASES)
        detail_suffix = f" ({details})" if details else ""
        super().__init__(
            f"Could not parse rollout request: missing/invalid {parts_text}{detail_suffix}.\n"
            f"Supported phrases:\n  - {phrases}"
        )


def _validate_percentage(value: Any, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanValidationError(f"Percentage must be an integer, got {value!r}.")
    minimum = 0 if allow_zero else 1
    if not (minimum <= value <= 100):
        raise PlanValidationError(f"Percentage must be between {minimum} and 100, got {value}.")
    return value


def parse_plain_english_request(
    request: str,
    *,
    min_samples: int = 20,
    project_id: int | None = None,
    segment_name: str | None = None,
) -> Plan:
    """Deterministically parse a plain-English rollout request into a validated Plan."""
    text = " ".join(request.strip().split())
    if not text:
        raise PlanParseError(
            ("feature", "target percent", "environment", "threshold"),
            "request is empty",
        )

    missing: list[str] = []

    feature_name: str | None = None
    for pattern in _FEATURE_PATTERNS:
        match = pattern.search(text)
        if match:
            feature_name = match.group(1)
            break
    if not feature_name:
        missing.append("feature")

    stages: list[int] = []
    stages_match = _STAGES_LIST_PATTERN.search(text)
    if stages_match:
        stages = [int(num) for num in re.findall(r"(\d{1,3})\s*%", stages_match.group(1))]
    else:
        target_match = _TARGET_PERCENT_PATTERN.search(text)
        if target_match:
            stages = [int(target_match.group(1))]

    if not stages or any(not (1 <= pct <= 100) for pct in stages):
        missing.append("target percent")

    environment: str | None = None
    for pattern in _ENVIRONMENT_PATTERNS:
        match = pattern.search(text)
        if match:
            candidate = match.group(1)
            if candidate.lower() not in {"if", "errors", "error"}:
                environment = candidate
                break
    if not environment:
        missing.append("environment")

    threshold_match = _THRESHOLD_PATTERN.search(text) or _ACTION_THRESHOLD_ALT_PATTERN.search(text)
    threshold_pct: float | None = None
    on_breach: BreachAction = "pause"
    if threshold_match:
        raw_pct = float(threshold_match.group("pct"))
        if 0.0 <= raw_pct <= 100.0:
            threshold_pct = raw_pct / 100.0
        raw_action = (threshold_match.group("action") or "").lower().replace(" ", "")
        if raw_action == "rollback":
            on_breach = "rollback"
        elif raw_action == "pause":
            on_breach = "pause"
    if threshold_pct is None:
        missing.append("threshold")

    if missing:
        raise PlanParseError(missing)

    assert feature_name is not None and environment is not None and threshold_pct is not None
    return build_plan(
        feature_name=feature_name,
        environment=environment,
        stages=stages,
        min_samples=min_samples,
        max_error_rate=threshold_pct,
        project_id=project_id,
        segment_name=segment_name,
        on_breach=on_breach,
        description=f"Parsed from request: {text}",
        source_request=text,
    )


def _strip_json_fences(raw_text: str) -> str:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def is_llm_configured() -> bool:
    """Return True only when FLAGPILOT_LLM_PROVIDER and its matching API key are set."""
    provider = os.environ.get("FLAGPILOT_LLM_PROVIDER", "").strip().lower()
    if provider == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY", "").strip())
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY", "").strip())
    return False


def plan_with_llm(
    prompt: str,
    *,
    min_samples: int = 20,
    project_id: int | None = None,
    segment_name: str | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    timeout_seconds: float = 15.0,
    opener: Callable[..., Any] | None = None,
) -> Plan:
    """Optional thin LLM planner adapter targeting ``plan_from_dict`` with deterministic fallback.

    Off by default; uses standard-library HTTP (Gemini or OpenAI REST APIs) to request
    strict JSON conforming to ``Plan``, validates through ``plan_from_dict()``, and falls
    back gracefully to ``parse_plain_english_request()`` on missing keys, network errors,
    or invalid/malformed LLM JSON.
    """
    clean_prompt = prompt.strip()
    resolved_provider = (provider or os.environ.get("FLAGPILOT_LLM_PROVIDER", "")).strip().lower()
    if not resolved_provider:
        if os.environ.get("GEMINI_API_KEY", "").strip():
            resolved_provider = "gemini"
        elif os.environ.get("OPENAI_API_KEY", "").strip():
            resolved_provider = "openai"

    if resolved_provider == "gemini":
        resolved_key = (api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
    elif resolved_provider == "openai":
        resolved_key = (api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
    else:
        resolved_key = ""

    def _fallback(reason: str) -> Plan:
        cleaned_reason = reason.replace(resolved_key, "[REDACTED]") if resolved_key else reason
        logger.warning(
            "LLM planner fallback (%s); using deterministic parser.",
            cleaned_reason,
        )
        return parse_plain_english_request(
            prompt,
            min_samples=min_samples,
            project_id=project_id,
            segment_name=segment_name,
        )

    if not clean_prompt:
        return _fallback("empty prompt")
    if resolved_provider not in ("gemini", "openai") or not resolved_key:
        return _fallback("no supported FLAGPILOT_LLM_PROVIDER and API key configured")

    system_instruction = (
        "Return ONLY a single JSON object (no prose or markdown) matching this exact Plan schema:\n"
        "{\n"
        '  "feature_name": "string",\n'
        '  "environment": "string",\n'
        '  "steps": [{"percentage": 1, "min_samples": 20, "max_error_rate": 0.02}],\n'
        '  "rollback_percentage": 0,\n'
        '  "on_breach": "rollback" | "pause",\n'
        '  "segment_name": "optional string",\n'
        '  "project_id": null\n'
        "}\n"
        "Percentages in steps must be integers in 1..100 and strictly increasing. "
        "max_error_rate is a decimal fraction in 0.0..1.0 (e.g. 2% -> 0.02)."
    )

    if resolved_provider == "gemini":
        resolved_model = (
            model or os.environ.get("FLAGPILOT_LLM_MODEL", "gemini-2.5-flash")
        ).strip()
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{resolved_model}:generateContent"
        )
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": resolved_key,
        }
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": [{"text": clean_prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
            },
        }
    else:
        resolved_model = (model or os.environ.get("FLAGPILOT_LLM_MODEL", "gpt-4o-mini")).strip()
        base = os.environ.get("FLAGPILOT_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        url = f"{base}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {resolved_key}",
        }
        body = {
            "model": resolved_model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": clean_prompt},
            ],
        }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    open_fn = opener if opener is not None else urllib.request.urlopen

    try:
        with open_fn(req, timeout=timeout_seconds) as resp:
            raw_http = resp.read()
        envelope = json.loads(
            raw_http.decode("utf-8") if isinstance(raw_http, bytes) else str(raw_http)
        )
        if resolved_provider == "gemini":
            content_text = envelope["candidates"][0]["content"]["parts"][0]["text"]
        else:
            content_text = envelope["choices"][0]["message"]["content"]
        parsed_payload = json.loads(_strip_json_fences(str(content_text)))
        if not isinstance(parsed_payload, dict):
            raise PlanValidationError("LLM response must decode to a JSON object.")

        if project_id is not None:
            parsed_payload["project_id"] = project_id
        if segment_name:
            parsed_payload["segment_name"] = segment_name
        parsed_payload.setdefault("source_request", clean_prompt)
        if isinstance(parsed_payload.get("steps"), list):
            for step_item in parsed_payload["steps"]:
                if isinstance(step_item, dict):
                    step_item.setdefault("min_samples", min_samples)

        return plan_from_dict(parsed_payload)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ) as exc:
        return _fallback(f"{type(exc).__name__}: {exc}")


def build_plan(
    feature_name: str,
    environment: str,
    *,
    stages: Sequence[int] = DEFAULT_STAGES,
    min_samples: int = 20,
    max_error_rate: float = 0.02,
    project_id: int | None = None,
    segment_name: str | None = None,
    rollback_percentage: int = 0,
    on_breach: BreachAction = "rollback",
    description: str = "",
    source_request: str = "",
) -> Plan:
    """Create a validated deterministic rollout plan."""
    clean_feature = feature_name.strip()
    clean_env = environment.strip()
    if not clean_feature:
        raise PlanValidationError("feature_name must not be empty.")
    if not clean_env:
        raise PlanValidationError("environment must not be empty.")
    if not stages:
        raise PlanValidationError("stages must contain at least one percentage.")
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
        raise PlanValidationError("min_samples must be an integer >= 1.")
    if isinstance(max_error_rate, bool) or not (0.0 <= float(max_error_rate) <= 1.0):
        raise PlanValidationError("max_error_rate must be between 0.0 and 1.0.")
    if project_id is not None and (isinstance(project_id, bool) or not isinstance(project_id, int)):
        raise PlanValidationError("project_id must be an integer when provided.")
    if on_breach not in ("pause", "rollback"):
        raise PlanValidationError("on_breach must be 'pause' or 'rollback'.")

    validated_rollback = _validate_percentage(rollback_percentage, allow_zero=True)

    steps: list[Step] = []
    previous_pct = 0
    for raw_pct in stages:
        pct = _validate_percentage(raw_pct)
        if pct <= previous_pct:
            raise PlanValidationError(
                f"Rollout stages must be strictly increasing, got {list(stages)}."
            )
        previous_pct = pct
        steps.append(
            Step(
                percentage=pct,
                min_samples=min_samples,
                max_error_rate=float(max_error_rate),
            )
        )

    resolved_segment = (segment_name or f"flagpilot-{clean_feature}-rollout").strip()
    if not resolved_segment:
        raise PlanValidationError("segment_name must not be empty.")

    return Plan(
        feature_name=clean_feature,
        environment=clean_env,
        steps=tuple(steps),
        project_id=project_id,
        segment_name=resolved_segment,
        rollback_percentage=validated_rollback,
        on_breach=on_breach,
        description=description.strip(),
        source_request=source_request.strip(),
    )


def planned_api_calls(plan: Plan) -> list[str]:
    """Return the exact Flagsmith Management REST API calls that WOULD run."""
    project_ref = str(plan.project_id) if plan.project_id is not None else "<project_id>"
    segment_name = plan.segment_name or f"flagpilot-{plan.feature_name}-rollout"
    calls: list[str] = []
    if plan.project_id is None:
        calls.append("GET /api/v1/projects/  (resolve <project_id>)")
    calls.append(
        f"GET /api/v1/environments/?project={project_ref}  "
        f"(resolve <environment_id> and <environment_api_key> for {plan.environment!r})"
    )
    calls.append(
        f"GET /api/v1/projects/{project_ref}/features/  "
        f"(or POST /api/v1/projects/{project_ref}/features/ "
        f'with {{"name": "{plan.feature_name}", "type": "STANDARD", "default_enabled": false}})'
    )
    for idx, step in enumerate(plan.steps):
        if idx == 0:
            calls.append(
                f"GET /api/v1/projects/{project_ref}/segments/  "
                f"-> POST /api/v1/projects/{project_ref}/segments/ "
                f'(or PUT .../segments/<segment_id>/) with name="{segment_name}" and '
                f'PERCENTAGE_SPLIT value="{step.percentage}"'
            )
            calls.append(
                "GET /api/__future__/environments/<environment_api_key>/features/<feature_id>/  "
                "-> PATCH /api/__future__/environments/<environment_api_key>/features/<feature_id>/ "
                'with {"segment_overrides": [{"segment": {"id": <segment_id>}, "enabled": true, "priority": 0, "value": null}]}'
            )
        else:
            calls.append(
                f"PUT /api/v1/projects/{project_ref}/segments/<segment_id>/  "
                f'with PERCENTAGE_SPLIT value="{step.percentage}"'
            )
    return calls


def plan_risks(plan: Plan) -> list[str]:
    """Return deterministic risk disclosures for a rollout plan."""
    final_pct = plan.steps[-1].percentage
    return [
        "Percentage Split (% Split) hashes persistent user identities; anonymous SDK calls will not match.",
        f"Applying this plan immediately exposes up to {final_pct}% of identified users in {plan.environment}.",
        "Existing segment overrides on this feature may compete based on priority (FlagPilot sets priority=0).",
        "Endpoints target self-hosted Flagsmith Management REST API (verified against Flagsmith 2.295.2).",
    ]


def threshold_breach_behavior(plan: Plan) -> str:
    """Describe what happens if the configured error-rate threshold is breached."""
    step = plan.steps[0]
    if plan.on_breach == "pause":
        return (
            f"If observed error rate exceeds {step.max_error_rate:.2%} "
            f"(after at least {step.min_samples} samples), FlagPilot pauses the rollout, "
            "records the decision with UTC timestamp and reason to audit.log, "
            "and waits for operator review or an approved rollback."
        )
    return (
        f"If observed error rate exceeds {step.max_error_rate:.2%} "
        f"(after at least {step.min_samples} samples), FlagPilot halts advancement, "
        f"requests interactive APPROVE to roll back to the previous safe percentage "
        f"(or {plan.rollback_percentage}%), and logs the decision to audit.log."
    )


def render_plan_markdown(plan: Plan) -> str:
    """Render plan.md: what will change, exact API calls, risks, and breach handling."""
    stages_str = " -> ".join(f"{s.percentage}%" for s in plan.steps)
    lines = [
        f"# FlagPilot Rollout Plan: `{plan.feature_name}`",
        "",
    ]
    if plan.source_request:
        lines.extend([f"> Request: `{plan.source_request}`", ""])

    lines.extend(
        [
            "## What Will Change",
            f"- **Feature**: `{plan.feature_name}` (created disabled by default if absent)",
            f"- **Environment**: `{plan.environment}`",
            f"- **Segment**: `{plan.segment_name}` (`PERCENTAGE_SPLIT`, `property: null`)",
            f"- **Target Rollout Stages**: `{stages_str}`",
            f"- **Segment Override**: Enabled (`enabled: true`, `priority: 0`) for `{plan.environment}`",
            "",
            "## Exact API Calls That Would Run",
            "*(Dry plan only — no calls have been sent to Flagsmith.)*",
            "",
        ]
    )
    for idx, call in enumerate(planned_api_calls(plan), start=1):
        lines.append(f"{idx}. `{call}`")

    lines.extend(["", "## Risks"])
    for risk in plan_risks(plan):
        lines.append(f"- {risk}")

    lines.extend(
        [
            "",
            "## If Threshold Is Breached",
            f"- **Threshold**: `{plan.steps[0].max_error_rate:.2%}` (min samples: `{plan.steps[0].min_samples}`)",
            f"- **Configured Breach Action**: `{plan.on_breach}`",
            f"- {threshold_breach_behavior(plan)}",
        ]
    )
    return "\n".join(lines) + "\n"


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    """Serialize a Plan into a JSON-compatible dictionary."""
    return {
        "feature_name": plan.feature_name,
        "environment": plan.environment,
        "project_id": plan.project_id,
        "segment_name": plan.segment_name,
        "rollback_percentage": plan.rollback_percentage,
        "on_breach": plan.on_breach,
        "description": plan.description,
        "source_request": plan.source_request,
        "steps": [
            {
                "percentage": step.percentage,
                "min_samples": step.min_samples,
                "max_error_rate": step.max_error_rate,
            }
            for step in plan.steps
        ],
        "planned_api_calls": planned_api_calls(plan),
        "risks": plan_risks(plan),
        "breach_behavior": threshold_breach_behavior(plan),
    }


def plan_from_dict(data: dict[str, Any]) -> Plan:
    """Parse and validate a Plan from a decoded JSON dictionary."""
    if not isinstance(data, dict):
        raise PlanValidationError("Plan payload must be a JSON object.")

    feature_name = data.get("feature_name")
    environment = data.get("environment")
    if not isinstance(feature_name, str) or not isinstance(environment, str):
        raise PlanValidationError("Plan requires string fields 'feature_name' and 'environment'.")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanValidationError("Plan requires a non-empty list of 'steps'.")

    parsed_steps: list[Step] = []
    previous_pct = 0
    for idx, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            raise PlanValidationError(f"Step {idx} must be an object.")
        pct = _validate_percentage(item.get("percentage"))
        if pct <= previous_pct:
            raise PlanValidationError(f"Step {idx} percentage {pct} must exceed {previous_pct}.")
        previous_pct = pct

        min_samples = item.get("min_samples", 20)
        if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
            raise PlanValidationError(f"Step {idx} min_samples must be an integer >= 1.")

        max_error_rate = item.get("max_error_rate", 0.02)
        if isinstance(max_error_rate, bool) or not isinstance(max_error_rate, (int, float)):
            raise PlanValidationError(f"Step {idx} max_error_rate must be numeric.")
        if not (0.0 <= float(max_error_rate) <= 1.0):
            raise PlanValidationError(f"Step {idx} max_error_rate must be in [0.0, 1.0].")

        parsed_steps.append(
            Step(
                percentage=pct,
                min_samples=min_samples,
                max_error_rate=float(max_error_rate),
            )
        )

    project_id = data.get("project_id")
    if project_id is not None and (isinstance(project_id, bool) or not isinstance(project_id, int)):
        raise PlanValidationError("project_id must be an integer or null.")

    segment_name = data.get("segment_name") or f"flagpilot-{feature_name.strip()}-rollout"
    if not isinstance(segment_name, str) or not segment_name.strip():
        raise PlanValidationError("segment_name must be a non-empty string.")

    rollback_pct = _validate_percentage(data.get("rollback_percentage", 0), allow_zero=True)
    on_breach = data.get("on_breach", "rollback")
    if on_breach not in ("pause", "rollback"):
        raise PlanValidationError("on_breach must be 'pause' or 'rollback'.")

    description = data.get("description", "")
    source_request = data.get("source_request", "")
    if not isinstance(description, str) or not isinstance(source_request, str):
        raise PlanValidationError("description and source_request must be strings.")

    return Plan(
        feature_name=feature_name.strip(),
        environment=environment.strip(),
        steps=tuple(parsed_steps),
        project_id=project_id,
        segment_name=segment_name.strip(),
        rollback_percentage=rollback_pct,
        on_breach=on_breach,
        description=description.strip(),
        source_request=source_request.strip(),
    )


def save_plan(plan: Plan, path: Path) -> None:
    """Write a Plan as formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan_to_dict(plan), indent=2) + "\n", encoding="utf-8")


def save_plan_artifacts(
    plan: Plan,
    *,
    json_path: Path = Path("plan.json"),
    markdown_path: Path = Path("plan.md"),
) -> tuple[Path, Path]:
    """Write both ``plan.json`` and ``plan.md`` without contacting Flagsmith."""
    save_plan(plan, json_path)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_plan_markdown(plan), encoding="utf-8")
    return json_path, markdown_path


def load_plan(path: Path) -> Plan:
    """Load and validate a Plan from a JSON file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return plan_from_dict(raw)


def _supports_unicode(stream: TextIO) -> bool:
    encoding = (getattr(stream, "encoding", None) or "utf-8").lower()
    return "utf" in encoding


def format_plan_summary_plain(plan: Plan) -> str:
    """Format a deterministic plain-text summary of a rollout plan."""
    progression = "0% -> " + " -> ".join(f"{step.percentage}%" for step in plan.steps)
    threshold = plan.steps[0].max_error_rate
    min_samples = plan.steps[0].min_samples
    segment = plan.segment_name or f"flagpilot-{plan.feature_name}-rollout"
    calls = planned_api_calls(plan)
    lines = [
        f"FlagPilot Rollout Plan: {plan.feature_name}",
        f"Environment: {plan.environment}",
        f"Segment: {segment} (PERCENTAGE_SPLIT)",
        f"Stage Progression: {progression}",
        f"Safety Threshold: max_error_rate <= {threshold:.2%} (min_samples={min_samples}, on_breach={plan.on_breach} -> {plan.rollback_percentage}%)",
        "Planned Flagsmith API Calls:",
    ]
    for idx, call in enumerate(calls, start=1):
        lines.append(f"  {idx}. {call}")
    return "\n".join(lines) + "\n"


def render_plan_summary(
    plan: Plan,
    *,
    plain: bool = False,
    output_stream: TextIO | None = None,
) -> None:
    """Render a bordered Rich summary panel for ``flagpilot plan`` (or plain fallback)."""
    out = output_stream if output_stream is not None else sys.stdout
    if plain:
        out.write(format_plan_summary_plain(plan))
        out.flush()
        return

    try:
        from rich.console import Console, Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
    except ImportError:
        out.write(format_plan_summary_plain(plan))
        out.flush()
        return

    arrow = "→" if _supports_unicode(out) else "->"
    progression = f"0% {arrow} " + f" {arrow} ".join(f"{step.percentage}%" for step in plan.steps)
    threshold = plan.steps[0].max_error_rate
    min_samples = plan.steps[0].min_samples
    segment = plan.segment_name or f"flagpilot-{plan.feature_name}-rollout"
    is_tty = bool(getattr(out, "isatty", lambda: False)())

    meta_table = Table.grid(padding=(0, 2))
    meta_table.add_column(style="bold cyan", no_wrap=True)
    meta_table.add_column()
    meta_table.add_row("Target Flag", plan.feature_name)
    meta_table.add_row("Environment", plan.environment)
    meta_table.add_row("Segment", f"{segment} (PERCENTAGE_SPLIT)")
    meta_table.add_row("Stage Progression", progression)
    meta_table.add_row(
        "Safety Threshold",
        f"error_rate <= {threshold:.2%} (min_samples={min_samples}, on_breach={plan.on_breach} {arrow} {plan.rollback_percentage}%)",
    )

    calls = planned_api_calls(plan)
    calls_header = Text("Planned Flagsmith API Calls (dry plan — zero calls sent):", style="bold")
    calls_lines = [Text(f"  {idx}. {call}", style="dim") for idx, call in enumerate(calls, start=1)]

    panel = Panel(
        Group(meta_table, Text(""), calls_header, *calls_lines),
        title="FlagPilot Rollout Plan",
        border_style="cyan",
        padding=(1, 2),
    )
    console = Console(
        file=out,
        no_color=not is_tty,
        safe_box=True,
        highlight=False,
        width=None if is_tty else 120,
    )
    console.print(panel)
