"""Minimal ASGI proxy server that sits between Claude Code and the Anthropic API."""

from __future__ import annotations

import json
import logging
import os
import sys

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from rdx.core.mappings import MappingCache
from rdx.core.redactor import Redactor
from rdx.core.rules import load_rules
from rdx.core.unredactor import Unredactor
from rdx.detect.patterns import get_builtin_rules

from rdx.audit.logger import AuditLogger

from .handler import redact_request_body, unredact_response_body
from .stream import unredact_stream

logger = logging.getLogger(__name__)
def _get_audit_dir() -> "Path":
    from pathlib import Path
    rules_dir = os.environ.get("RDX_RULES_DIR")
    return Path(rules_dir) if rules_dir else Path.cwd()

_audit = AuditLogger(_get_audit_dir())
_audit_enabled = False
_log_bodies = False


def enable_audit() -> None:
    """Called by CLI before starting foreground server."""
    global _audit_enabled
    _audit_enabled = True


def enable_body_logging() -> None:
    """Called by CLI before starting foreground server."""
    global _log_bodies
    _log_bodies = True
_request_counter = 0


def _log_body(label: str, body: dict, request_id: int) -> None:
    """Write a full request/response body to the debug log directory."""
    if not _log_bodies:
        return
    import time
    debug_dir = _get_audit_dir() / ".claude" / "rdx_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%H%M%S")
    filename = f"{ts}_r{request_id:04d}_{label}.json"
    path = debug_dir / filename
    with open(path, "w") as f:
        json.dump(body, f, indent=2, default=str)
    print(f"[rdx] {label} → {path}", file=sys.stderr)

# Headers to forward from client to upstream.
_FORWARD_HEADERS = frozenset({
    "authorization",
    "anthropic-version",
    "anthropic-beta",
    "content-type",
})

DEFAULT_UPSTREAM = "https://api.anthropic.com"
DEFAULT_TIMEOUT = 300.0


def _get_upstream_url() -> str:
    return os.environ.get("RDX_UPSTREAM_URL", DEFAULT_UPSTREAM)


def _get_timeout() -> float:
    """Get timeout from RDX_TIMEOUT env var (seconds), default 300."""
    try:
        return float(os.environ.get("RDX_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except (ValueError, TypeError):
        return DEFAULT_TIMEOUT


def _build_rules() -> list:
    """Load user rules and merge with builtins.

    Uses RDX_RULES_DIR env var if set, otherwise cwd.
    """
    from pathlib import Path
    rules_dir = os.environ.get("RDX_RULES_DIR")
    project_dir = Path(rules_dir) if rules_dir else None
    user_rules = load_rules(project_dir)
    builtin_rules = get_builtin_rules()
    # User rules first so they take priority in scanning
    seen_ids = {r.id for r in user_rules}
    merged = list(user_rules)
    for r in builtin_rules:
        if r.id not in seen_ids:
            merged.append(r)
    return merged


# Shared mapping cache — lives for the lifetime of the server process.
_cache = MappingCache()


async def health(request: Request) -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({"status": "ok"})


async def proxy_messages(request: Request) -> StreamingResponse | JSONResponse:
    """Proxy POST /v1/messages — redact outgoing, un-redact incoming."""
    import time as _time
    global _request_counter
    _request_counter += 1
    req_id = _request_counter

    rules = _build_rules()
    redactor = Redactor(rules, _cache)
    unredactor = Unredactor(_cache)
    timeout = _get_timeout()

    body = await request.json()
    _log_body("1_original_request", body, req_id)

    t0 = _time.monotonic()
    try:
        redacted_body = redact_request_body(body, redactor)
    except Exception:
        logger.exception("Redaction failed on request body — passing through unredacted")
        redacted_body = body
    redact_ms = (_time.monotonic() - t0) * 1000
    _log_body("2_redacted_request", redacted_body, req_id)
    print(f"[rdx] req#{req_id} redaction: {redact_ms:.1f}ms | {_cache.stats()['mappings']} mappings", file=sys.stderr)

    # Log outgoing redactions (only when audit enabled)
    if _audit_enabled:
        all_redactions = _cache.get_all_redactions()
        if all_redactions:
            rule_ids = list({r.rule_id for r in all_redactions})
            _audit.log(
                "redact", "outgoing",
                tool="proxy",
                rule_ids=rule_ids,
                count=len(all_redactions),
                detail=f"{len(all_redactions)} values redacted in API request",
            )

    is_streaming = redacted_body.get("stream", False)

    # Build upstream headers
    headers = {}
    for key in _FORWARD_HEADERS:
        value = request.headers.get(key)
        if value is not None:
            headers[key] = value

    upstream_url = _get_upstream_url() + "/v1/messages"

    async with httpx.AsyncClient(timeout=timeout) as client:
        if is_streaming:
            upstream_resp = await client.send(
                client.build_request(
                    "POST",
                    upstream_url,
                    headers=headers,
                    content=json.dumps(redacted_body).encode(),
                ),
                stream=True,
            )

            if upstream_resp.status_code != 200:
                error_body = await upstream_resp.aread()
                try:
                    error_json = json.loads(error_body)
                except json.JSONDecodeError:
                    error_json = {"error": error_body.decode(errors="replace")}
                return JSONResponse(
                    error_json,
                    status_code=upstream_resp.status_code,
                )

            async def _logged_stream():
                async for chunk in unredact_stream(upstream_resp, unredactor):
                    yield chunk
                # Log after stream completes
                if _audit_enabled:
                    reverse_map = _cache.get_reverse_map()
                    if reverse_map:
                        _audit.log(
                            "unredact", "incoming",
                            tool="proxy",
                            count=len(reverse_map),
                            detail=f"{len(reverse_map)} values available for un-redaction (streaming)",
                        )

            return StreamingResponse(
                _logged_stream(),
                media_type="text/event-stream",
                headers={
                    "cache-control": "no-cache",
                    "connection": "keep-alive",
                },
            )
        else:
            upstream_resp = await client.post(
                upstream_url,
                headers=headers,
                content=json.dumps(redacted_body).encode(),
            )

            if upstream_resp.status_code != 200:
                try:
                    error_json = upstream_resp.json()
                except (json.JSONDecodeError, ValueError):
                    error_json = {"error": upstream_resp.text}
                return JSONResponse(
                    error_json,
                    status_code=upstream_resp.status_code,
                )

            response_body = upstream_resp.json()
            _log_body("3_raw_response", response_body, req_id)
            t1 = _time.monotonic()
            try:
                unredacted_body = unredact_response_body(response_body, unredactor)
                unredact_ms = (_time.monotonic() - t1) * 1000
                _log_body("4_unredacted_response", unredacted_body, req_id)
                print(f"[rdx] req#{req_id} un-redaction: {unredact_ms:.1f}ms", file=sys.stderr)
                if _audit_enabled:
                    reverse_map = _cache.get_reverse_map()
                    if reverse_map:
                        _audit.log(
                            "unredact", "incoming",
                            tool="proxy",
                            count=len(reverse_map),
                            detail=f"{len(reverse_map)} values un-redacted in API response",
                        )
            except Exception:
                logger.exception("Un-redaction failed on response — passing through as-is")
                unredacted_body = response_body
            return JSONResponse(unredacted_body)


async def proxy_count_tokens(request: Request) -> JSONResponse:
    """Proxy POST /v1/messages/count_tokens — passthrough, no redaction."""
    headers = {}
    for key in _FORWARD_HEADERS:
        value = request.headers.get(key)
        if value is not None:
            headers[key] = value

    body = await request.body()
    upstream_url = _get_upstream_url() + "/v1/messages/count_tokens"
    timeout = _get_timeout()

    async with httpx.AsyncClient(timeout=timeout) as client:
        upstream_resp = await client.post(
            upstream_url,
            headers=headers,
            content=body,
        )
        return JSONResponse(
            upstream_resp.json(),
            status_code=upstream_resp.status_code,
        )


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/v1/messages", proxy_messages, methods=["POST"]),
        Route("/v1/messages/count_tokens", proxy_count_tokens, methods=["POST"]),
    ],
)
