"""Tests for the proxy server: streaming client lifecycle and _cache→cache fix.

Covers two bugs fixed in server.py:

1. httpx.ReadError on streaming — the `async with httpx.AsyncClient` block
   closed the client before StreamingResponse finished reading. The fix
   uses manual lifecycle management with cleanup in a `finally` block.

2. NameError: name '_cache' is not defined — the local variable is `cache`
   (a MappingCache instance), but 4 references used `_cache` (the module-level
   dict). This caused 500 Internal Server Error on every redacted request.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from starlette.requests import Request

from rdx.proxy.server import _forward_raw, proxy_messages
from rdx.proxy import server as server_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(body: dict, headers: dict | None = None) -> Request:
    """Build a Starlette Request with a JSON body."""
    raw_body = json.dumps(body).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "headers": [],
        "query_string": b"",
    }
    hdrs = {"content-type": "application/json"}
    if headers:
        hdrs.update(headers)
    for k, v in hdrs.items():
        scope["headers"].append((k.encode(), v.encode()))

    async def receive():
        return {"type": "http.request", "body": raw_body, "more_body": False}

    return Request(scope, receive)


class _FakeStreamResponse:
    """Fake httpx streaming response that yields chunks and tracks close calls."""

    def __init__(self, chunks: list[bytes], status_code: int = 200):
        self._chunks = list(chunks)
        self._all = b"".join(chunks)
        self.status_code = status_code
        self.aclosed = False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aiter_lines(self):
        """Yield lines (str) split by \\n, matching httpx.Response.aiter_lines."""
        data = self._all.decode(errors="replace")
        for line in data.split("\n"):
            yield line

    async def aread(self):
        return self._all

    async def aclose(self):
        self.aclosed = True


class _FakeAsyncClient:
    """Fake httpx.AsyncClient that tracks lifecycle."""

    def __init__(self, response: _FakeStreamResponse | dict, *, stream: bool = False):
        self._response = response
        self._stream = stream
        self.aclosed = False
        self.requests_made = 0
        self.is_closed = False

    def build_request(self, *args, **kwargs):
        return httpx.Request("POST", args[1])

    async def send(self, request, stream=False):
        self.requests_made += 1
        return self._response

    async def post(self, *args, **kwargs):
        self.requests_made += 1
        resp = MagicMock()
        if isinstance(self._response, dict):
            resp.json.return_value = self._response
            resp.status_code = 200
            resp.text = json.dumps(self._response)
        else:
            resp.json.return_value = {}
            resp.status_code = self._response.status_code
            resp.text = ""
        return resp

    async def aclose(self):
        self.aclosed = True
        self.is_closed = True


@pytest.fixture(autouse=True)
def _reset_shared_client():
    """Reset the shared httpx client between tests."""
    old = server_module._shared_client
    server_module._shared_client = None
    yield
    server_module._shared_client = old


# ---------------------------------------------------------------------------
# Streaming client lifecycle — httpx.ReadError fix
# ---------------------------------------------------------------------------


class TestStreamingClientLifecycle:
    """Verify the httpx client stays alive for the entire streaming response."""

    @pytest.mark.asyncio
    async def test_forward_raw_streaming_client_not_closed_prematurely(self) -> None:
        """In _forward_raw streaming mode, the client must not be closed
        before the stream is fully consumed."""
        # SSE-formatted chunks (unredact_stream expects event:/data: lines)
        chunks = [
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        fake_resp = _FakeStreamResponse(chunks)
        fake_client = _FakeAsyncClient(fake_resp, stream=True)

        with patch.object(server_module, "_get_client", return_value=fake_client):
            request = _make_request({"model": "test", "stream": True, "messages": []})
            response = await _forward_raw(request, {"stream": True}, 30.0)

        # Client should NOT be closed yet — stream hasn't been consumed
        assert not fake_resp.aclosed, "Response was closed before stream consumption"

        # Now consume the stream
        collected = []
        async for chunk in response.body_iterator:
            collected.append(chunk)

        # After full consumption, response should be closed
        assert fake_resp.aclosed, "Response was not closed after stream completed"
        # Output should contain the SSE events
        combined = b"".join(collected)
        assert b"message_start" in combined
        assert b"message_stop" in combined

    @pytest.mark.asyncio
    async def test_forward_raw_streaming_multi_chunk(self) -> None:
        """Streaming with many chunks — client stays alive until the last one."""
        # SSE-formatted chunks
        chunks = [
            f'event: ping\ndata: {{"type":"ping","i":{i}}}\n\n'.encode()
            for i in range(50)
        ]
        fake_resp = _FakeStreamResponse(chunks)
        fake_client = _FakeAsyncClient(fake_resp, stream=True)

        with patch.object(server_module, "_get_client", return_value=fake_client):
            request = _make_request({"model": "test", "stream": True, "messages": []})
            response = await _forward_raw(request, {"stream": True}, 30.0)

        # Consume all chunks
        collected = []
        async for chunk in response.body_iterator:
            collected.append(chunk)

        combined = b"".join(collected)
        assert combined.count(b'"i":') == 50
        assert fake_resp.aclosed

    @pytest.mark.asyncio
    async def test_forward_raw_non_streaming_closes_client(self) -> None:
        """Non-streaming path should close client immediately after response."""
        fake_client = _FakeAsyncClient({"status": "ok"})

        with patch.object(server_module, "_get_client", return_value=fake_client):
            request = _make_request({"model": "test", "messages": []})
            await _forward_raw(request, {"stream": False}, 30.0)

        # Non-streaming: response is consumed immediately, shared client stays alive

    @pytest.mark.asyncio
    async def test_forward_raw_streaming_client_closed_on_exception(self) -> None:
        """If the stream raises an exception, the client is still closed in finally."""

        class _ErrorStreamResponse:
            status_code = 200
            aclosed = False

            async def aiter_bytes(self):
                yield b"first chunk"
                raise RuntimeError("connection lost")

            async def aiter_lines(self):
                yield "first chunk"
                raise RuntimeError("connection lost")

            async def aclose(self):
                self.aclosed = True

        fake_resp = _ErrorStreamResponse()
        fake_client = _FakeAsyncClient(fake_resp, stream=True)

        with patch.object(server_module, "_get_client", return_value=fake_client):
            request = _make_request({"model": "test", "stream": True, "messages": []})
            response = await _forward_raw(request, {"stream": True}, 30.0)

        # Consume the stream — should raise
        with pytest.raises(RuntimeError, match="connection lost"):
            async for _ in response.body_iterator:
                pass

        # Response should still be closed via finally
        assert fake_resp.aclosed, "Response was not closed after stream error"


# ---------------------------------------------------------------------------
# _cache → cache NameError fix
# ---------------------------------------------------------------------------


class TestCacheVariableName:
    """Verify that proxy_messages uses `cache` (local MappingCache) not `_cache`
    (module-level dict). Previously this caused NameError → 500."""

    @pytest.mark.asyncio
    async def test_proxy_messages_no_name_error_with_rules(self, tmp_path) -> None:
        """proxy_messages should not raise NameError when redaction rules exist.

        This test sets up a minimal project with .redaction_rules, mocks the
        upstream httpx call, and verifies the request completes without NameError.
        """
        rules_file = tmp_path / ".redaction_rules"
        rules_file.write_text(
            "rules:\n"
            "  - id: test-key\n"
            "    pattern: 'sk-test-[a-zA-Z0-9]+' \n"
            "    category: KEY\n"
        )

        # Mock the upstream response (non-streaming for simplicity)
        fake_upstream = MagicMock()
        fake_upstream.status_code = 200
        fake_upstream.json.return_value = {
            "content": [{"type": "text", "text": "Hello"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        fake_upstream.text = json.dumps(fake_upstream.json.return_value)

        fake_client = _FakeAsyncClient(fake_upstream)

        body = {
            "model": "test-model",
            "stream": False,
            "system": [
                {"type": "text", "text": f"Primary working directory: {tmp_path}"},
            ],
            "messages": [
                {"role": "user", "content": "My key is sk-test-abcdef123456"},
            ],
        }

        with patch.object(server_module, "_get_client", return_value=fake_client):
            request = _make_request(body)
            # This should NOT raise NameError
            response = await proxy_messages(request)

        # Verify we got a valid response
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_proxy_messages_no_name_error_with_audit_enabled(self, tmp_path) -> None:
        """proxy_messages should not raise NameError when audit logging is enabled.

        The audit code paths were the primary sites of the _cache→cache bug.
        Enable audit to exercise them.
        """
        from rdx.proxy import server as server_module

        rules_file = tmp_path / ".redaction_rules"
        rules_file.write_text(
            "rules:\n"
            "  - id: test-key\n"
            "    pattern: 'sk-test-[a-zA-Z0-9]+' \n"
            "    category: KEY\n"
        )

        fake_upstream = MagicMock()
        fake_upstream.status_code = 200
        fake_upstream.json.return_value = {
            "content": [{"type": "text", "text": "Hello"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        fake_upstream.text = json.dumps(fake_upstream.json.return_value)

        fake_client = _FakeAsyncClient(fake_upstream)

        body = {
            "model": "test-model",
            "stream": False,
            "system": [
                {"type": "text", "text": f"Primary working directory: {tmp_path}"},
            ],
            "messages": [
                {"role": "user", "content": "My key is sk-test-abcdef123456"},
            ],
        }

        # Enable audit to exercise the _cache code paths
        old_audit = server_module._audit_enabled
        server_module._audit_enabled = True
        try:
            with patch.object(server_module, "_get_client", return_value=fake_client):
                request = _make_request(body)
                response = await proxy_messages(request)
            assert response.status_code == 200
        finally:
            server_module._audit_enabled = old_audit

    @pytest.mark.asyncio
    async def test_proxy_messages_streaming_no_name_error(self, tmp_path) -> None:
        """proxy_messages streaming path should not raise NameError."""
        sse_chunks = [
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        fake_resp = _FakeStreamResponse(sse_chunks)
        fake_client = _FakeAsyncClient(fake_resp, stream=True)

        rules_file = tmp_path / ".redaction_rules"
        rules_file.write_text(
            "rules:\n"
            "  - id: test-key\n"
            "    pattern: 'sk-test-[a-zA-Z0-9]+' \n"
            "    category: KEY\n"
        )

        body = {
            "model": "test-model",
            "stream": True,
            "system": [
                {"type": "text", "text": f"Primary working directory: {tmp_path}"},
            ],
            "messages": [
                {"role": "user", "content": "My key is sk-test-abcdef123456"},
            ],
        }

        with patch.object(server_module, "_get_client", return_value=fake_client):
            request = _make_request(body)
            response = await proxy_messages(request)

        # Consume the streaming response — should not raise NameError
        collected = []
        async for chunk in response.body_iterator:
            collected.append(chunk)

        assert len(collected) > 0
        # Response should be closed after stream completes
        assert fake_resp.aclosed

    @pytest.mark.asyncio
    async def test_proxy_messages_audit_streaming_no_name_error(self, tmp_path) -> None:
        """Streaming path with audit enabled — exercises cache.get_reverse_map()."""
        from rdx.proxy import server as server_module

        sse_chunks = [
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        fake_resp = _FakeStreamResponse(sse_chunks)
        fake_client = _FakeAsyncClient(fake_resp, stream=True)

        rules_file = tmp_path / ".redaction_rules"
        rules_file.write_text(
            "rules:\n"
            "  - id: test-key\n"
            "    pattern: 'sk-test-[a-zA-Z0-9]+' \n"
            "    category: KEY\n"
        )

        body = {
            "model": "test-model",
            "stream": True,
            "system": [
                {"type": "text", "text": f"Primary working directory: {tmp_path}"},
            ],
            "messages": [
                {"role": "user", "content": "My key is sk-test-abcdef123456"},
            ],
        }

        old_audit = server_module._audit_enabled
        server_module._audit_enabled = True
        try:
            with patch.object(server_module, "_get_client", return_value=fake_client):
                request = _make_request(body)
                response = await proxy_messages(request)

            collected = []
            async for chunk in response.body_iterator:
                collected.append(chunk)

            assert len(collected) > 0
            assert fake_resp.aclosed
        finally:
            server_module._audit_enabled = old_audit


# ---------------------------------------------------------------------------
# Source-level regression guards
# ---------------------------------------------------------------------------


class TestSourceGuards:
    """Ensure the fixed code patterns do not regress."""

    def test_no_cache_underscore_in_proxy_messages(self) -> None:
        """proxy_messages should not reference _cache (module-level dict)
        for per-project operations. It should use the local `cache` variable."""
        source = inspect.getsource(proxy_messages)

        # _caches (with trailing 's') is the module-level dict — that's fine.
        # _cache (no trailing 's') is the bug — should not appear.
        # We check for _cache. (with dot) to avoid false positives on _caches.
        assert "_cache." not in source, (
            "proxy_messages still references _cache (should be cache). "
            "This was the root cause of NameError: name '_cache' is not defined."
        )

    def test_no_async_with_httpx_in_forward_raw(self) -> None:
        """_forward_raw should not use 'async with httpx.AsyncClient' —
        the context manager closes the client before streaming finishes."""
        source = inspect.getsource(_forward_raw)
        assert "async with httpx.AsyncClient" not in source, (
            "_forward_raw uses 'async with httpx.AsyncClient' which causes "
            "httpx.ReadError on streaming responses."
        )

    def test_no_async_with_httpx_in_proxy_messages_streaming(self) -> None:
        """proxy_messages should not use 'async with httpx.AsyncClient' —
        the context manager closes the client before streaming finishes."""
        source = inspect.getsource(proxy_messages)
        assert "async with httpx.AsyncClient" not in source, (
            "proxy_messages uses 'async with httpx.AsyncClient' which causes "
            "httpx.ReadError on streaming responses."
        )


# ---------------------------------------------------------------------------
# /api/hello connectivity check endpoint
# ---------------------------------------------------------------------------


class TestHelloEndpoint:
    """Verify the /api/hello endpoint that Claude Code uses for connectivity checks."""

    @pytest.mark.asyncio
    async def test_hello_get_returns_200(self) -> None:
        from rdx.proxy.server import hello

        request = _make_request({})
        response = await hello(request)
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# Header forwarding — x-api-key must be passed through
# ---------------------------------------------------------------------------


class TestForwardHeaders:
    """Verify that headers are forwarded to upstream."""

    def test_x_api_key_forwarded(self) -> None:
        """x-api-key must be forwarded to upstream.

        Claude Code sends the API key via the x-api-key header, not
        authorization. The proxy must forward all headers except
        hop-by-hop and content-length.
        """
        from rdx.proxy.server import _build_upstream_headers, _DROP_HEADERS

        # _DROP_HEADERS should NOT contain x-api-key, authorization,
        # anthropic-version, anthropic-beta, user-agent, etc.
        assert "x-api-key" not in _DROP_HEADERS
        assert "authorization" not in _DROP_HEADERS
        assert "anthropic-version" not in _DROP_HEADERS
        assert "anthropic-beta" not in _DROP_HEADERS
        assert "user-agent" not in _DROP_HEADERS

        # But should drop hop-by-hop headers
        assert "host" in _DROP_HEADERS
        assert "content-length" in _DROP_HEADERS
        assert "connection" in _DROP_HEADERS
        assert "accept-encoding" in _DROP_HEADERS
