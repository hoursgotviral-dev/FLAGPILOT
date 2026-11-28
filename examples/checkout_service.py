"""Lightweight closed-loop canary demo target backed by Flagsmith SDK and ThreadingHTTPServer.

Usage:
    python examples/checkout_service.py
    python examples/checkout_service.py --port 8085 --fault-rate 0.50 --users 200
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
from collections import deque
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TextIO
from urllib.parse import parse_qs, urlsplit

from flagpilot.simulator import (
    DEFAULT_USER_COUNT,
    FlagsmithSDKEvaluator,
    OfflineFakeEvaluator,
    load_or_create_identities,
)
from flagpilot.types import FlagEvaluator

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8085
DEFAULT_FEATURE = "checkout_v2"
DEFAULT_FAULT_RATE = 0.50


def deterministic_fault(user_id: str, feature_name: str, fault_rate: float) -> bool:
    """Deterministically decide whether an exposed request faults in [0.0, 1.0]."""
    if fault_rate <= 0.0:
        return False
    if fault_rate >= 1.0:
        return True
    digest = hashlib.sha256(f"fault:{feature_name}:{user_id}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return bucket < fault_rate


class CheckoutServiceApp:
    """Thread-safe handler logic evaluating Flagsmith flags for /checkout and /health-rate."""

    def __init__(
        self,
        evaluator: FlagEvaluator,
        *,
        feature_name: str = DEFAULT_FEATURE,
        fault_rate: float = DEFAULT_FAULT_RATE,
        user_count: int = DEFAULT_USER_COUNT,
        self_drive: bool = True,
        window_size: int = DEFAULT_USER_COUNT,
        output_stream: TextIO | None = None,
    ) -> None:
        if not feature_name.strip():
            raise ValueError("feature_name must not be empty.")
        if not (0.0 <= fault_rate <= 1.0):
            raise ValueError("fault_rate must be between 0.0 and 1.0.")
        if user_count < 1:
            raise ValueError("user_count must be >= 1.")
        if window_size < 1:
            raise ValueError("window_size must be >= 1.")

        self._evaluator = evaluator
        self._feature_name = feature_name.strip()
        self._fault_rate = float(fault_rate)
        self._identities: tuple[str, ...] = tuple(load_or_create_identities(user_count))
        self._self_drive = self_drive
        self._window: deque[tuple[bool, bool]] = deque(maxlen=window_size)
        self._lock = threading.Lock()
        self._out = output_stream if output_stream is not None else sys.stdout

    def _refresh_evaluator(self) -> None:
        refresh = getattr(self._evaluator, "refresh", None)
        if callable(refresh):
            refresh()

    def _evaluate_user(
        self,
        user_id: str,
        *,
        record_window: bool = True,
    ) -> tuple[int, dict[str, Any]]:
        evaluation = self._evaluator.evaluate(user_id, self._feature_name)
        is_error = (
            deterministic_fault(user_id, self._feature_name, self._fault_rate)
            if evaluation.enabled
            else False
        )
        status_code = 500 if is_error else 200
        if record_window:
            self._window.append((evaluation.enabled, is_error))
        payload: dict[str, Any] = {
            "status": "error" if is_error else "ok",
            "user": user_id,
            "feature": self._feature_name,
            "enabled": evaluation.enabled,
            "http_status": status_code,
        }
        return status_code, payload

    def handle_checkout(
        self,
        user_id: str,
        *,
        refresh: bool = True,
    ) -> tuple[int, dict[str, Any]]:
        """Evaluate checkout_v2 for ``user_id`` and return (HTTP status, JSON payload)."""
        clean_user = user_id.strip()
        if not clean_user:
            return 400, {"error": "Missing or empty 'user' query parameter."}
        with self._lock:
            if refresh:
                self._refresh_evaluator()
            return self._evaluate_user(clean_user, record_window=True)

    def collect_health_rate(self) -> dict[str, Any]:
        """Compute current error rate via self-drive persistent-user burst or sliding window."""
        with self._lock:
            if self._self_drive:
                self._refresh_evaluator()
                exposed = 0
                errors = 0
                for uid in self._identities:
                    status_code, body = self._evaluate_user(uid, record_window=False)
                    if body["enabled"]:
                        exposed += 1
                    if status_code >= 500:
                        errors += 1
                total = len(self._identities)
                mode = "self-drive"
            else:
                snapshot = list(self._window)
                total = len(snapshot)
                exposed = sum(1 for is_exposed, _ in snapshot if is_exposed)
                errors = sum(1 for _, is_err in snapshot if is_err)
                mode = "window"

        error_rate = (errors / total) if total > 0 else 0.0
        self._out.write(
            f"[checkout_service] mode={mode} feature={self._feature_name} "
            f"requests={total} exposed={exposed} errors={errors} "
            f"error_rate={error_rate:.2%}\n"
        )
        self._out.flush()
        return {
            "error_rate": round(error_rate, 6),
            "requests": total,
            "errors": errors,
            "exposed": exposed,
        }


def make_handler_class(app: CheckoutServiceApp) -> type[BaseHTTPRequestHandler]:
    """Build a ThreadingHTTPServer request handler bound to ``app``."""

    class _Handler(BaseHTTPRequestHandler):
        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            encoded = (json.dumps(payload) + "\n").encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/checkout":
                query = parse_qs(parsed.query)
                user_id = query.get("user", [""])[0]
                status_code, payload = app.handle_checkout(user_id, refresh=True)
                self._send_json(status_code, payload)
                return
            if parsed.path == "/health-rate":
                self._send_json(200, app.collect_health_rate())
                return
            self._send_json(404, {"error": f"Not found: {parsed.path}"})

        def log_message(self, format: str, *args: Any) -> None:
            """Suppress default per-request access logs to keep split-screen demo readable."""
            del format, args

    return _Handler


def create_server(
    app: CheckoutServiceApp,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Create a ThreadingHTTPServer serving /checkout and /health-rate."""
    return ThreadingHTTPServer((host, port), make_handler_class(app))


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for the live canary demo checkout service."""
    parser = argparse.ArgumentParser(
        description="Lightweight Flagsmith-backed checkout canary target for --signal-url demos.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--feature", default=DEFAULT_FEATURE)
    parser.add_argument(
        "--fault-rate",
        type=float,
        default=DEFAULT_FAULT_RATE,
        help="Deterministic HTTP 500 rate when feature is enabled (default: 0.50)",
    )
    parser.add_argument(
        "--users",
        type=int,
        default=DEFAULT_USER_COUNT,
        help="Number of persistent user_000.. identities for --self-drive bursts (default: 200)",
    )
    parser.add_argument(
        "--self-drive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Evaluate a fresh burst of persistent users on GET /health-rate (default: enabled)",
    )
    parser.add_argument(
        "--window-size",
        type=int,
        default=DEFAULT_USER_COUNT,
        help="Sliding window size of /checkout requests used with --no-self-drive",
    )
    parser.add_argument(
        "--offline-evaluator",
        action="store_true",
        help="Use OfflineFakeEvaluator instead of Flagsmith SDK (for local dry runs)",
    )
    parser.add_argument(
        "--rollout-pct",
        type=int,
        default=0,
        help="Rollout percentage when --offline-evaluator is used",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Start the HTTP checkout canary service until interrupted."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        evaluator: FlagEvaluator = (
            OfflineFakeEvaluator(rollout_percentage=args.rollout_pct)
            if args.offline_evaluator
            else FlagsmithSDKEvaluator.from_env()
        )
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    app = CheckoutServiceApp(
        evaluator,
        feature_name=args.feature,
        fault_rate=args.fault_rate,
        user_count=args.users,
        self_drive=args.self_drive,
        window_size=args.window_size,
    )
    server = create_server(app, host=args.host, port=args.port)
    mode = "self-drive" if args.self_drive else "window"
    print(
        f"Checkout canary service listening on http://{args.host}:{args.port} "
        f"(feature={args.feature}, fault_rate={args.fault_rate:.0%}, mode={mode}, users={args.users})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down checkout canary service.", flush=True)
    finally:
        server.server_close()
        close_fn = getattr(evaluator, "close", None)
        if callable(close_fn):
            close_fn()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
