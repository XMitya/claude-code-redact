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
_audit_enabled = os.environ.get("RDX_AUDIT", "").lower() in ("1", "true", "yes")
_log_bodies = os.environ.get("RDX_LOG_BODIES", "").lower() in ("1", "true", "yes")
_log_headers = os.environ.get("RDX_LOG_HEADERS", "").lower() in ("1", "true", "yes")
_no_unredact = False


def enable_audit() -> None:
    """Called by CLI before starting foreground server."""
    global _audit_enabled
    _audit_enabled = True


def disable_unredact() -> None:
    """Called by CLI — responses pass through without un-redaction."""
    global _no_unredact
    _no_unredact = True


def enable_body_logging() -> None:
    """Called by CLI before starting foreground server."""
    global _log_bodies
    _log_bodies = True
_request_counter = 0


def _log_body(label: str, body: dict, request_id: int, project_dir: "Path | None" = None) -> None:
    """Write a full request/response body to the debug log directory."""
    if not _log_bodies:
        return
    import time
    from pathlib import Path
    base = project_dir if project_dir else _get_audit_dir()
    debug_dir = base / ".claude" / "rdx_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%H%M%S")
    filename = f"{ts}_r{request_id:04d}_{label}.json"
    path = debug_dir / filename
    with open(path, "w") as f:
        json.dump(body, f, indent=2, default=str)
    print(f"[rdx] {label} → {path}", file=sys.stderr)
    # Also print a truncated summary to stderr for quick inspection
    body_str = json.dumps(body, default=str)
    if len(body_str) > 2000:
        print(f"[rdx] {label} (truncated): {body_str[:2000]}...", file=sys.stderr)
    else:
        print(f"[rdx] {label}: {body_str}", file=sys.stderr)

# Headers that should NOT be forwarded to upstream — httpx manages these
# automatically (content-length is recalculated, host is derived from URL).
_DROP_HEADERS = frozenset({
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "accept-encoding",
})

DEFAULT_UPSTREAM = "https://api.anthropic.com"
DEFAULT_TIMEOUT = 300.0

# Shared httpx.AsyncClient with connection pooling.
# Creating a new client per request causes excessive TCP/TLS connections
# through corporate proxies, which can trigger 429 rate limits.
_shared_client: httpx.AsyncClient | None = None


def _get_client(timeout: float) -> httpx.AsyncClient:
    """Return a shared AsyncClient with connection pooling and HTTP/2."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=timeout,
            http2=True,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=60,
            ),
        )
    return _shared_client


def _build_upstream_headers(request: Request) -> dict[str, str]:
    """Forward all request headers to upstream, except hop-by-hop and
    content-length (httpx recalculates these automatically)."""
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _DROP_HEADERS
    }


def _get_upstream_url() -> str:
    return os.environ.get("RDX_UPSTREAM_URL", DEFAULT_UPSTREAM)


def _get_timeout() -> float:
    """Get timeout from RDX_TIMEOUT env var (seconds), default 300."""
    try:
        return float(os.environ.get("RDX_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except (ValueError, TypeError):
        return DEFAULT_TIMEOUT


def _extract_project_dir(body: dict) -> "Path | None":
    """Extract the working directory from the system prompt in the request body."""
    import re
    from pathlib import Path

    system = body.get("system", [])
    if isinstance(system, str):
        texts = [system]
    elif isinstance(system, list):
        texts = [b.get("text", "") for b in system if isinstance(b, dict)]
    else:
        texts = []

    for text in texts:
        m = re.search(r"Primary working directory:\s*(\S+)", text)
        if m:
            p = Path(m.group(1))
            if p.is_dir():
                return p
    return None


def _build_rules_for_project(project_dir: "Path | None") -> list:
    """Load user rules for a project and merge with builtins.

    Returns empty list if project has no .redaction_rules.
    """
    from pathlib import Path

    if project_dir is None:
        # Fallback to RDX_RULES_DIR or cwd
        rules_dir = os.environ.get("RDX_RULES_DIR")
        project_dir = Path(rules_dir) if rules_dir else Path.cwd()

    rules_file = project_dir / ".redaction_rules"
    if not rules_file.exists():
        return []  # No rules → skip redaction entirely

    user_rules = load_rules(project_dir)
    builtin_rules = get_builtin_rules()
    seen_ids = {r.id for r in user_rules}
    merged = list(user_rules)
    for r in builtin_rules:
        if r.id not in seen_ids:
            merged.append(r)
    return merged


# Global mapping cache — shared across all projects so that tokens
# created in one project's request can be unredacted in any response,
# even when the current request goes through _forward_raw (no rules).
_global_cache = MappingCache()


def _get_cache(project_dir: "Path | None") -> MappingCache:
    return _global_cache


async def _forward_raw(request: Request, body: dict, timeout: float) -> StreamingResponse | JSONResponse:
    """Forward request to upstream without any redaction.

    Still un-redacts the response using the global cache — tokens from
    prior requests in the same session may appear in Anthropic's response.
    """
    headers = _build_upstream_headers(request)
    upstream_url = _get_upstream_url() + "/v1/messages"
    # Preserve query string (e.g. ?beta=true) from the original request
    qs = str(request.url.query) if request.url.query else ""
    if qs:
        upstream_url = f"{upstream_url}?{qs}"

    unredactor = Unredactor(_global_cache)

    # NOTE: Do NOT use `async with` — the client must stay alive for the
    # lifetime of the StreamingResponse.  Closing it early causes httpx.ReadError.
    client = _get_client(timeout)
    if body.get("stream", False):
        resp = await client.send(
            client.build_request("POST", upstream_url, headers=headers,
                                 content=json.dumps(body).encode()),
            stream=True,
        )
        if resp.status_code != 200:
            error_body = await resp.aread()
            await resp.aclose()
            try:
                error_json = json.loads(error_body)
            except json.JSONDecodeError:
                error_json = {"error": error_body.decode(errors="replace")}
            print(f"[rdx] _forward_raw upstream {resp.status_code}: {error_json}", file=sys.stderr)
            return JSONResponse(error_json, status_code=resp.status_code)

        async def _raw_stream():
            try:
                stream_src = (
                    resp.aiter_bytes()
                    if _no_unredact
                    else unredact_stream(resp, unredactor)
                )
                async for chunk in stream_src:
                    yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(_raw_stream(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache"})
    else:
        resp = await client.post(upstream_url, headers=headers,
                                 content=json.dumps(body).encode())
        response_body = resp.json()
        try:
            response_body = unredact_response_body(response_body, unredactor)
        except Exception:
            logger.exception("Un-redaction failed in _forward_raw — passing through as-is")
        return JSONResponse(response_body, status_code=resp.status_code)


async def health(request: Request) -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({"status": "ok", "mappings": _global_cache.stats()["mappings"]})


async def hello(request: Request) -> JSONResponse:
    """Connectivity check endpoint for Claude Code.

    Claude Code sends HEAD /api/hello before each session to verify
    the API base URL is reachable.  Return 200 so it doesn't log a 404.
    """
    return JSONResponse({"status": "ok"})


async def proxy_messages(request: Request) -> StreamingResponse | JSONResponse:
    """Proxy POST /v1/messages — redact outgoing, un-redact incoming."""
    import time as _time
    global _request_counter
    _request_counter += 1
    req_id = _request_counter

    timeout = _get_timeout()
    raw_body = await request.body()
    body = json.loads(raw_body)
    print(f"[rdx] req#{req_id} original stream={body.get('stream', 'MISSING')} model={body.get('model', 'MISSING')}", file=sys.stderr)

    # Detect which project this request belongs to
    project_dir = _extract_project_dir(body)
    _log_body("1_original_request", body, req_id, project_dir)
    rules = _build_rules_for_project(project_dir)

    if not rules:
        # No redaction rules for this project — pass through untouched
        print(f"[rdx] req#{req_id} no rules for {project_dir or 'unknown'} — passthrough", file=sys.stderr)
        return await _forward_raw(request, body, timeout)

    cache = _get_cache(project_dir)
    redactor = Redactor(rules, cache)
    unredactor = Unredactor(cache)

    t0 = _time.monotonic()
    try:
        redacted_body = redact_request_body(body, redactor)
    except Exception:
        logger.exception("Redaction failed on request body — passing through unredacted")
        redacted_body = body
    redact_ms = (_time.monotonic() - t0) * 1000
    _log_body("2_redacted_request", redacted_body, req_id, project_dir)
    mappings_count = cache.stats()['mappings']
    print(f"[rdx] req#{req_id} redaction: {redact_ms:.1f}ms | {mappings_count} mappings", file=sys.stderr)
    if _log_headers:
        print(f"[rdx] req#{req_id} incoming headers: {dict(request.headers)}", file=sys.stderr)

    # If no redactions were applied, send the original raw body unchanged.
    # Re-serializing via json.dumps changes byte layout (whitespace, key order),
    # which can break prompt caching and fingerprinting at Anthropic.
    if mappings_count == 0:
        # Use raw body — same bytes Claude Code sent
        body_to_send = raw_body
        is_streaming = body.get("stream", False)
        # Forward as raw passthrough with full headers
        headers = _build_upstream_headers(request)
        upstream_url = _get_upstream_url() + "/v1/messages"
        qs = str(request.url.query) if request.url.query else ""
        if qs:
            upstream_url = f"{upstream_url}?{qs}"
        print(f"[rdx] req#{req_id} 0 mappings — sending raw body ({len(raw_body)} bytes), is_streaming={is_streaming}", file=sys.stderr)
        if _log_bodies:
            body_preview = raw_body.decode(errors="replace")[:2000]
            print(f"[rdx] req#{req_id} raw body: {body_preview}{'...' if len(raw_body) > 2000 else ''}", file=sys.stderr)
        client = _get_client(timeout)
        if is_streaming:
            upstream_resp = await client.send(
                client.build_request("POST", upstream_url, headers=headers, content=body_to_send),
                stream=True,
            )
            if upstream_resp.status_code != 200:
                error_body = await upstream_resp.aread()
                await upstream_resp.aclose()
                try:
                    error_json = json.loads(error_body)
                except json.JSONDecodeError:
                    error_json = {"error": error_body.decode(errors="replace")}
                print(f"[rdx] req#{req_id} upstream {upstream_resp.status_code}: {error_json}", file=sys.stderr)
                return JSONResponse(error_json, status_code=upstream_resp.status_code)

            async def _raw_passthrough_stream():
                try:
                    stream_src = (
                        upstream_resp.aiter_bytes()
                        if _no_unredact
                        else unredact_stream(upstream_resp, unredactor)
                    )
                    async for chunk in stream_src:
                        yield chunk
                finally:
                    await upstream_resp.aclose()

            return StreamingResponse(_raw_passthrough_stream(), media_type="text/event-stream",
                                     headers={"cache-control": "no-cache"})
        else:
            upstream_resp = await client.post(upstream_url, headers=headers, content=body_to_send)
            if upstream_resp.status_code != 200:
                try:
                    error_json = upstream_resp.json()
                except (json.JSONDecodeError, ValueError):
                    error_json = {"error": upstream_resp.text}
                print(f"[rdx] req#{req_id} upstream {upstream_resp.status_code}: {error_json}", file=sys.stderr)
                return JSONResponse(error_json, status_code=upstream_resp.status_code)
            # Even with 0 mappings, response may contain tokens from
            # prior requests in the same session (same cache).
            response_body = upstream_resp.json()
            try:
                response_body = unredact_response_body(response_body, unredactor)
            except Exception:
                logger.exception("Un-redaction failed on 0-mappings response — passing through as-is")
            return JSONResponse(response_body, status_code=upstream_resp.status_code)

    # Log outgoing redactions (only when audit enabled)
    if _audit_enabled:
        all_redactions = cache.get_all_redactions()
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
    headers = _build_upstream_headers(request)

    upstream_url = _get_upstream_url() + "/v1/messages"
    # Preserve query string (e.g. ?beta=true) from the original request
    qs = str(request.url.query) if request.url.query else ""
    if qs:
        upstream_url = f"{upstream_url}?{qs}"

    print(f"[rdx] req#{req_id} is_streaming={is_streaming} upstream_url={upstream_url}", file=sys.stderr)
    if _log_headers:
        print(f"[rdx] req#{req_id} forward headers: {headers}", file=sys.stderr)

    # NOTE: Do NOT use `async with` — the client must stay alive for the
    # lifetime of the StreamingResponse.  Closing it early causes httpx.ReadError.
    client = _get_client(timeout)
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
            await upstream_resp.aclose()
            try:
                error_json = json.loads(error_body)
            except json.JSONDecodeError:
                error_json = {"error": error_body.decode(errors="replace")}
            print(f"[rdx] req#{req_id} upstream {upstream_resp.status_code}: {error_json}", file=sys.stderr)
            if _log_headers:
                print(f"[rdx] req#{req_id} upstream headers: {dict(upstream_resp.headers)}", file=sys.stderr)
            return JSONResponse(
                error_json,
                status_code=upstream_resp.status_code,
            )

        async def _logged_stream():
            try:
                stream_src = (
                    upstream_resp.aiter_bytes()
                    if _no_unredact
                    else unredact_stream(upstream_resp, unredactor)
                )
                async for chunk in stream_src:
                    yield chunk
                # Log after stream completes
                if _audit_enabled:
                    reverse_map = cache.get_reverse_map()
                    if reverse_map:
                        _audit.log(
                            "unredact", "incoming",
                            tool="proxy",
                            count=len(reverse_map),
                            detail=f"{len(reverse_map)} values available for un-redaction (streaming)",
                        )
            finally:
                await upstream_resp.aclose()

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
            print(f"[rdx] req#{req_id} upstream {upstream_resp.status_code}: {error_json}", file=sys.stderr)
            if _log_headers:
                print(f"[rdx] req#{req_id} upstream resp headers: {dict(upstream_resp.headers)}", file=sys.stderr)
            return JSONResponse(
                error_json,
                status_code=upstream_resp.status_code,
            )

        response_body = upstream_resp.json()
        _log_body("3_raw_response", response_body, req_id, project_dir)

        if _no_unredact:
            return JSONResponse(response_body)

        t1 = _time.monotonic()
        try:
            unredacted_body = unredact_response_body(response_body, unredactor)
            unredact_ms = (_time.monotonic() - t1) * 1000
            _log_body("4_unredacted_response", unredacted_body, req_id, project_dir)
            print(f"[rdx] req#{req_id} un-redaction: {unredact_ms:.1f}ms", file=sys.stderr)
            if _audit_enabled:
                reverse_map = cache.get_reverse_map()
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
    headers = _build_upstream_headers(request)

    body = await request.body()
    upstream_url = _get_upstream_url() + "/v1/messages/count_tokens"
    # Preserve query string from the original request
    qs = str(request.url.query) if request.url.query else ""
    if qs:
        upstream_url = f"{upstream_url}?{qs}"
    timeout = _get_timeout()

    client = _get_client(timeout)
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
        Route("/api/hello", hello, methods=["GET", "HEAD"]),
        Route("/v1/messages", proxy_messages, methods=["POST"]),
        Route("/v1/messages/count_tokens", proxy_count_tokens, methods=["POST"]),
    ],
)
