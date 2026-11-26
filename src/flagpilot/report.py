"""Rollout summary, audit log reader, and self-contained HTML report generator."""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from flagpilot.planner import plan_from_dict, plan_to_dict
from flagpilot.types import Decision, RolloutRunResult, StageMetrics, StageOutcome, Step


def run_result_to_dict(result: RolloutRunResult) -> dict[str, Any]:
    """Serialize a RolloutRunResult into a JSON-compatible dictionary."""
    return {
        "plan": plan_to_dict(result.plan),
        "final_status": result.final_status,
        "rollback_applied": result.rollback_applied,
        "rollback_to_percentage": result.rollback_to_percentage,
        "is_synthetic": result.is_synthetic,
        "traffic_label": "SYNTHETIC DATA" if result.is_synthetic else "OBSERVED",
        "outcomes": [
            {
                "step": {
                    "percentage": outcome.step.percentage,
                    "min_samples": outcome.step.min_samples,
                    "max_error_rate": outcome.step.max_error_rate,
                },
                "metrics": {
                    "percentage": outcome.metrics.percentage,
                    "sample_count": outcome.metrics.sample_count,
                    "error_count": outcome.metrics.error_count,
                    "enabled_count": outcome.metrics.enabled_count,
                    "error_rate": outcome.metrics.error_rate,
                    "source_label": outcome.metrics.source_label,
                    "reported_error_rate": outcome.metrics.reported_error_rate,
                },
                "decision": {
                    "action": outcome.decision.action,
                    "reason": outcome.decision.reason,
                    "current_percentage": outcome.decision.current_percentage,
                    "next_percentage": outcome.decision.next_percentage,
                    "observed_error_rate": outcome.decision.observed_error_rate,
                    "sample_count": outcome.decision.sample_count,
                },
                "applied": outcome.applied,
            }
            for outcome in result.outcomes
        ],
    }


def run_result_from_dict(data: dict[str, Any]) -> RolloutRunResult:
    """Deserialize a RolloutRunResult from a JSON dictionary."""
    plan = plan_from_dict(data["plan"])
    outcomes: list[StageOutcome] = []
    for item in data.get("outcomes", []):
        step_data = item["step"]
        metrics_data = item["metrics"]
        decision_data = item["decision"]
        outcomes.append(
            StageOutcome(
                step=Step(
                    percentage=step_data["percentage"],
                    min_samples=step_data["min_samples"],
                    max_error_rate=step_data["max_error_rate"],
                ),
                metrics=StageMetrics(
                    percentage=metrics_data["percentage"],
                    sample_count=metrics_data["sample_count"],
                    error_count=metrics_data["error_count"],
                    enabled_count=metrics_data["enabled_count"],
                    source_label=metrics_data.get("source_label", "SYNTHETIC DATA"),
                    reported_error_rate=metrics_data.get("reported_error_rate"),
                ),
                decision=Decision(
                    action=decision_data["action"],
                    reason=decision_data["reason"],
                    current_percentage=decision_data["current_percentage"],
                    next_percentage=decision_data.get("next_percentage"),
                    observed_error_rate=decision_data["observed_error_rate"],
                    sample_count=decision_data["sample_count"],
                ),
                applied=bool(item["applied"]),
            )
        )
    return RolloutRunResult(
        plan=plan,
        final_status=data["final_status"],
        outcomes=tuple(outcomes),
        rollback_applied=bool(data.get("rollback_applied", False)),
        rollback_to_percentage=data.get("rollback_to_percentage"),
        is_synthetic=bool(data.get("is_synthetic", True)),
    )


def save_run_result(result: RolloutRunResult, path: Path) -> None:
    """Persist a RolloutRunResult JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run_result_to_dict(result), indent=2) + "\n", encoding="utf-8")


def load_run_result(path: Path) -> RolloutRunResult:
    """Load a RolloutRunResult JSON file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return run_result_from_dict(raw)


def load_audit_log(path: Path) -> list[dict[str, Any]]:
    """Load JSON-line decision entries from ``audit.log`` (returns empty if absent)."""
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def _match_audit_times(
    outcomes: Sequence[StageOutcome],
    audit_entries: Sequence[dict[str, Any]],
) -> list[str]:
    """Match stage outcomes in order to UTC timestamps from audit.log."""
    remaining = list(audit_entries)
    times: list[str] = []
    for outcome in outcomes:
        matched_time = "—"
        for idx, entry in enumerate(remaining):
            if (
                entry.get("old_percentage") == outcome.step.percentage
                and entry.get("action") == outcome.decision.action
            ):
                matched_time = str(entry.get("timestamp", "—"))
                remaining.pop(idx)
                break
        times.append(matched_time)
    return times


def _render_svg_chart(result: RolloutRunResult) -> str:
    """Render an inline SVG chart plotting exposure (%) and error rate (%) per step."""
    width = 760
    height = 280
    pad_left = 56
    pad_right = 56
    pad_top = 34
    pad_bottom = 48
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    outcomes = result.outcomes
    if not outcomes:
        return (
            f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="No rollout steps">'
            '<text x="50%" y="50%" text-anchor="middle" fill="currentColor">'
            "No rollout steps recorded</text></svg>"
        )

    exposures: list[float] = []
    error_rates: list[float] = []
    labels: list[str] = []
    for outcome in outcomes:
        m = outcome.metrics
        exp_pct = (m.enabled_count / m.sample_count * 100.0) if m.sample_count > 0 else 0.0
        err_pct = m.error_rate * 100.0
        exposures.append(exp_pct)
        error_rates.append(err_pct)
        labels.append(f"{outcome.step.percentage}%")

    max_err_pct = max(
        [o.step.max_error_rate * 100.0 for o in outcomes] + error_rates + [5.0]
    )
    err_scale_max = max(5.0, round(max_err_pct * 1.25, 1))

    count = len(outcomes)
    x_positions = [
        pad_left + (plot_w * 0.5 if count == 1 else (i / (count - 1)) * plot_w)
        for i in range(count)
    ]

    def y_exp(val: float) -> float:
        clamped = max(0.0, min(100.0, val))
        return pad_top + plot_h - (clamped / 100.0) * plot_h

    def y_err(val: float) -> float:
        clamped = max(0.0, min(err_scale_max, val))
        return pad_top + plot_h - (clamped / err_scale_max) * plot_h

    svg_parts: list[str] = [
        (
            f'<svg viewBox="0 0 {width} {height}" class="chart-svg" role="img" '
            'aria-label="Exposure and error rate per rollout step">'
        ),
    ]

    # Grid lines and axis labels (left: Exposure 0-100%, right: Error Rate 0-err_scale_max%)
    for tick in (0, 25, 50, 75, 100):
        y = y_exp(float(tick))
        err_tick = (tick / 100.0) * err_scale_max
        svg_parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" '
            'class="grid-line" />'
        )
        svg_parts.append(
            f'<text x="{pad_left - 8}" y="{y + 4:.1f}" text-anchor="end" class="axis-text">'
            f"{tick}%</text>"
        )
        svg_parts.append(
            f'<text x="{width - pad_right + 8}" y="{y + 4:.1f}" text-anchor="start" '
            f'class="axis-text err-axis">{err_tick:.1f}%</text>'
        )

    # Threshold dashed line
    threshold_pct = outcomes[0].step.max_error_rate * 100.0
    th_y = y_err(threshold_pct)
    svg_parts.append(
        f'<line x1="{pad_left}" y1="{th_y:.1f}" x2="{width - pad_right}" y2="{th_y:.1f}" '
        'class="threshold-line" />'
    )
    svg_parts.append(
        f'<text x="{width - pad_right - 4}" y="{th_y - 6:.1f}" text-anchor="end" '
        f'class="threshold-label">Threshold {threshold_pct:.1f}%</text>'
    )

    # Exposure bars
    bar_w = min(44.0, (plot_w / max(count, 1)) * 0.35)
    for x, exp_val, label in zip(x_positions, exposures, labels, strict=True):
        bar_y = y_exp(exp_val)
        bar_h = (pad_top + plot_h) - bar_y
        svg_parts.append(
            f'<rect x="{x - bar_w / 2:.1f}" y="{bar_y:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" rx="4" class="exp-bar">'
            f"<title>Step {html.escape(label)}: Exposure {exp_val:.1f}%</title></rect>"
        )

    # Exposure polyline
    if count > 1:
        exp_pts = " ".join(
            f"{x:.1f},{y_exp(v):.1f}" for x, v in zip(x_positions, exposures, strict=True)
        )
        svg_parts.append(f'<polyline points="{exp_pts}" class="exp-line" />')

    # Error rate polyline + markers
    if count > 1:
        err_pts = " ".join(
            f"{x:.1f},{y_err(v):.1f}" for x, v in zip(x_positions, error_rates, strict=True)
        )
        svg_parts.append(f'<polyline points="{err_pts}" class="err-line" />')

    for x, err_val, label in zip(x_positions, error_rates, labels, strict=True):
        cy = y_err(err_val)
        svg_parts.append(
            f'<circle cx="{x:.1f}" cy="{cy:.1f}" r="5" class="err-dot">'
            f"<title>Step {html.escape(label)}: Error rate {err_val:.2f}%</title></circle>"
        )
        svg_parts.append(
            f'<text x="{x:.1f}" y="{height - 18}" text-anchor="middle" class="axis-text">'
            f"{html.escape(label)}</text>"
        )

    # Legend
    svg_parts.extend(
        [
            f'<rect x="{pad_left}" y="8" width="12" height="12" rx="2" class="exp-bar" />',
            f'<text x="{pad_left + 18}" y="18" class="legend-text">Exposed Users (%)</text>',
            f'<circle cx="{pad_left + 160}" cy="14" r="5" class="err-dot" />',
            f'<text x="{pad_left + 170}" y="18" class="legend-text">Error Rate (%)</text>',
            (
                f'<line x1="{pad_left + 280}" y1="14" x2="{pad_left + 300}" y2="14" '
                'class="threshold-line" />'
            ),
            f'<text x="{pad_left + 306}" y="18" class="legend-text">Error Threshold</text>',
            "</svg>",
        ]
    )
    return "\n".join(svg_parts)


def render_html_report(
    result: RolloutRunResult,
    *,
    audit_entries: Sequence[dict[str, Any]] = (),
) -> str:
    """Render a self-contained HTML rollout report with inline CSS, SVG chart, and light/dark."""
    plan = result.plan
    stages_str = " → ".join(f"{step.percentage}%" for step in plan.steps)
    threshold_str = f"{plan.steps[0].max_error_rate:.2%}" if plan.steps else "—"
    matched_times = _match_audit_times(result.outcomes, audit_entries)

    # Determine rollback banner reason from outcomes or audit.log
    rollback_reason = ""
    for outcome in result.outcomes:
        if outcome.decision.action == "rollback":
            rollback_reason = outcome.decision.reason
            break
    if not rollback_reason:
        for entry in reversed(audit_entries):
            if entry.get("action") == "rollback":
                rollback_reason = str(entry.get("reason", "Rollback executed."))
                break

    show_rollback_banner = (
        result.rollback_applied
        or result.final_status == "rolled_back"
        or bool(rollback_reason)
    )
    rollback_target_text = (
        f" to {result.rollback_to_percentage}%"
        if result.rollback_to_percentage is not None
        else ""
    )

    synthetic_banner = ""
    if result.is_synthetic:
        synthetic_banner = (
            '<div class="banner banner-synthetic" role="status">'
            "<strong>SYNTHETIC DATA:</strong> This rollout used simulated persistent-identity "
            "traffic and synthetic error rates rather than live production user traffic."
            "</div>"
        )

    rollback_banner = ""
    if show_rollback_banner:
        safe_reason = html.escape(rollback_reason or "Threshold breached.")
        rollback_banner = (
            '<div class="banner banner-rollback" role="alert">'
            f"<strong>ROLLBACK TRIGGERED{html.escape(rollback_target_text)}:</strong> "
            f"{safe_reason}</div>"
        )

    step_rows: list[str] = []
    for outcome, timestamp in zip(result.outcomes, matched_times, strict=True):
        m = outcome.metrics
        d = outcome.decision
        exposed_pct = (m.enabled_count / m.sample_count * 100.0) if m.sample_count > 0 else 0.0
        badge_cls = "badge-danger" if d.action == "rollback" else "badge-ok"
        step_rows.append(
            "<tr>"
            f"<td>{outcome.step.percentage}%</td>"
            f"<td>{m.enabled_count} / {m.sample_count} ({exposed_pct:.1f}%)</td>"
            f"<td>{m.error_rate:.2%}</td>"
            f'<td><span class="badge {badge_cls}">{html.escape(d.action)}</span> '
            f'<span class="muted">{html.escape(d.reason)}</span></td>'
            f'<td class="mono">{html.escape(timestamp)}</td>'
            "</tr>"
        )

    audit_rows: list[str] = []
    for entry in audit_entries:
        audit_rows.append(
            "<tr>"
            f'<td class="mono">{html.escape(str(entry.get("timestamp", "")))}</td>'
            f"<td>{html.escape(str(entry.get('action', '')))}</td>"
            f"<td>{html.escape(str(entry.get('old_percentage', '')))}% → "
            f"{html.escape(str(entry.get('new_percentage', '')))}%</td>"
            f"<td>{html.escape(str(entry.get('reason', '')))}</td>"
            "</tr>"
        )

    audit_section = ""
    if audit_rows:
        audit_section = (
            '<section class="card">'
            "<h2>Audit Log</h2>"
            '<div class="table-wrap"><table><thead><tr>'
            "<th>Time (UTC)</th><th>Action</th><th>Change</th><th>Reason</th>"
            f"</tr></thead><tbody>{''.join(audit_rows)}</tbody></table></div>"
            "</section>"
        )

    svg_chart = _render_svg_chart(result)
    feature_esc = html.escape(plan.feature_name)
    env_esc = html.escape(plan.environment)
    segment_esc = html.escape(plan.segment_name or f"flagpilot-{plan.feature_name}-rollout")
    status_esc = html.escape(result.final_status)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>FlagPilot Rollout Report — {feature_esc}</title>
  <style>
    :root {{
      --bg: #f8fafc;
      --surface: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --border: #e2e8f0;
      --accent: #2563eb;
      --danger: #dc2626;
      --warn-bg: #fef3c7;
      --warn-border: #f59e0b;
      --warn-text: #78350f;
      --danger-bg: #fee2e2;
      --danger-border: #ef4444;
      --danger-text: #7f1d1d;
      --grid: #cbd5e1;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0b1120;
        --surface: #111827;
        --text: #f1f5f9;
        --muted: #94a3b8;
        --border: #1e293b;
        --accent: #60a5fa;
        --danger: #f87171;
        --warn-bg: #451a03;
        --warn-border: #d97706;
        --warn-text: #fde68a;
        --danger-bg: #450a0a;
        --danger-border: #dc2626;
        --danger-text: #fecaca;
        --grid: #334155;
      }}
    }}
    #theme-toggle {{ position: absolute; opacity: 0; pointer-events: none; }}
    #theme-toggle:checked ~ .page {{
      --bg: #0b1120;
      --surface: #111827;
      --text: #f1f5f9;
      --muted: #94a3b8;
      --border: #1e293b;
      --accent: #60a5fa;
      --danger: #f87171;
      --warn-bg: #451a03;
      --warn-border: #d97706;
      --warn-text: #fde68a;
      --danger-bg: #450a0a;
      --danger-border: #dc2626;
      --danger-text: #fecaca;
      --grid: #334155;
    }}
    @media (prefers-color-scheme: dark) {{
      #theme-toggle:checked ~ .page {{
        --bg: #f8fafc;
        --surface: #ffffff;
        --text: #0f172a;
        --muted: #475569;
        --border: #e2e8f0;
        --accent: #2563eb;
        --danger: #dc2626;
        --warn-bg: #fef3c7;
        --warn-border: #f59e0b;
        --warn-text: #78350f;
        --danger-bg: #fee2e2;
        --danger-border: #ef4444;
        --danger-text: #7f1d1d;
        --grid: #cbd5e1;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .page {{
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      padding: 2rem 1.25rem;
    }}
    .container {{ max-width: 960px; margin: 0 auto; display: grid; gap: 1.25rem; }}
    .header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem; }}
    .theme-btn {{
      cursor: pointer;
      padding: 0.4rem 0.85rem;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--surface);
      color: var(--text);
      font-size: 0.875rem;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.25rem;
    }}
    .banner {{
      padding: 0.9rem 1.1rem;
      border-radius: 8px;
      border-left: 4px solid;
    }}
    .banner-synthetic {{
      background: var(--warn-bg);
      border-color: var(--warn-border);
      color: var(--warn-text);
    }}
    .banner-rollback {{
      background: var(--danger-bg);
      border-color: var(--danger-border);
      color: var(--danger-text);
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 0.75rem;
    }}
    .kv {{ padding: 0.65rem 0.75rem; border: 1px solid var(--border); border-radius: 8px; }}
    .kv-label {{ font-size: 0.75rem; text-transform: uppercase; color: var(--muted); }}
    .kv-val {{ font-weight: 600; word-break: break-word; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; }}
    th, td {{ text-align: left; padding: 0.65rem 0.6rem; border-bottom: 1px solid var(--border); vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; font-size: 0.8rem; text-transform: uppercase; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.82rem; }}
    .muted {{ color: var(--muted); font-size: 0.85rem; }}
    .badge {{
      display: inline-block;
      padding: 0.12rem 0.45rem;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      text-transform: uppercase;
    }}
    .badge-ok {{ background: rgba(37, 99, 235, 0.14); color: var(--accent); }}
    .badge-danger {{ background: rgba(220, 38, 38, 0.16); color: var(--danger); }}
    .chart-svg {{ width: 100%; height: auto; display: block; }}
    .grid-line {{ stroke: var(--grid); stroke-width: 1; stroke-dasharray: 2 3; }}
    .axis-text {{ fill: var(--muted); font-size: 11px; }}
    .err-axis {{ fill: var(--danger); }}
    .legend-text {{ fill: var(--text); font-size: 12px; }}
    .exp-bar {{ fill: var(--accent); opacity: 0.35; }}
    .exp-line {{ fill: none; stroke: var(--accent); stroke-width: 2.5; }}
    .err-line {{ fill: none; stroke: var(--danger); stroke-width: 2.5; }}
    .err-dot {{ fill: var(--danger); }}
    .threshold-line {{ stroke: var(--danger); stroke-width: 1.5; stroke-dasharray: 6 4; }}
    .threshold-label {{ fill: var(--danger); font-size: 11px; font-weight: 600; }}
  </style>
</head>
<body>
  <input type="checkbox" id="theme-toggle" aria-label="Toggle light or dark mode" />
  <div class="page">
    <main class="container">
      <header class="header">
        <div>
          <h1 style="margin:0;">FlagPilot Rollout Report</h1>
          <div class="muted">Feature <strong>{feature_esc}</strong> · Environment <strong>{env_esc}</strong></div>
        </div>
        <label for="theme-toggle" class="theme-btn">Toggle Light / Dark</label>
      </header>
      {synthetic_banner}
      {rollback_banner}
      <section class="card">
        <h2>Plan Summary</h2>
        <div class="summary-grid">
          <div class="kv"><div class="kv-label">Feature</div><div class="kv-val">{feature_esc}</div></div>
          <div class="kv"><div class="kv-label">Environment</div><div class="kv-val">{env_esc}</div></div>
          <div class="kv"><div class="kv-label">Segment</div><div class="kv-val">{segment_esc}</div></div>
          <div class="kv"><div class="kv-label">Planned Stages</div><div class="kv-val">{html.escape(stages_str)}</div></div>
          <div class="kv"><div class="kv-label">Error Threshold</div><div class="kv-val">{html.escape(threshold_str)}</div></div>
          <div class="kv"><div class="kv-label">Final Status</div><div class="kv-val">{status_esc}</div></div>
        </div>
      </section>
      <section class="card">
        <h2>Exposure &amp; Error Rate per Step</h2>
        {svg_chart}
      </section>
      <section class="card">
        <h2>Rollout Steps</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Percentage</th>
                <th>Exposed Users</th>
                <th>Error Rate</th>
                <th>Decision</th>
                <th>Time (UTC)</th>
              </tr>
            </thead>
            <tbody>
              {''.join(step_rows)}
            </tbody>
          </table>
        </div>
      </section>
      {audit_section}
    </main>
  </div>
</body>
</html>
"""


def save_html_report(
    result: RolloutRunResult,
    path: Path = Path("reports/rollout.html"),
    *,
    audit_entries: Sequence[dict[str, Any]] = (),
) -> Path:
    """Write the static self-contained HTML rollout report to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html_report(result, audit_entries=audit_entries), encoding="utf-8")
    return path


def render_markdown_report(result: RolloutRunResult) -> str:
    """Render a human-readable Markdown rollout report."""
    lines = [
        f"# FlagPilot Rollout Report: `{result.plan.feature_name}`",
        "",
        f"- **Environment**: `{result.plan.environment}`",
        f"- **Segment**: `{result.plan.segment_name}`",
        f"- **Final Status**: `{result.final_status}`",
        f"- **Rollback Applied**: `{result.rollback_applied}`",
    ]
    if result.is_synthetic:
        lines.append("- **Data Source**: `SYNTHETIC DATA` (simulated identity traffic)")
    lines.extend(
        [
            "",
            "| Stage | Samples | Enabled | Errors | Error Rate | Source | Decision | Reason |",
            "| ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
        ]
    )
    for outcome in result.outcomes:
        m = outcome.metrics
        d = outcome.decision
        lines.append(
            f"| {outcome.step.percentage}% | {m.sample_count} | {m.enabled_count} | "
            f"{m.error_count} | {m.error_rate:.2%} | {m.source_label} | "
            f"`{d.action}` | {d.reason} |"
        )
    return "\n".join(lines) + "\n"
