"""Tests for streaming client lifecycle and _cache→cache NameError fix.

These tests cover two bugs fixed in server.py:

1. httpx.ReadError on streaming — the `async with httpx.AsyncClient` block
   closed the client before StreamingResponse finished reading. The fix
   uses manual lifecycle management with cleanup in a `finally` block.

2. NameError: name '_cache' is not defined — the local variable is `cache`
   (a MappingCache instance), but 4 references used `_cache` (the module-level
   dict). This caused 500 Internal Server Error on every redacted request.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.requests import Request

from rdx.proxy.server import _forward_raw, proxy_messages


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

    request = Request(scope, receive)
    return request


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


# ---------------------------------------------------------------------------
# Bug 1: httpx.ReadError — client must stay alive during streaming
# ---------------------------------------------------------------------------


class TestStreamingClientLifecycle:
    """Verify the httpx client stays alive for the entire streaming response."""

    @pytest.mark.asyncio
    async def test_forward_raw_streaming_client_not_closed_prematurely(self) -> None:
        """In _forward_raw streaming mode, the client must not be closed
        before the stream is fully consumed."""
        chunks = [b'{"type":"message_start"}', b'{"type":"message_stop"}']
        fake_resp = _FakeStreamResponse(chunks)
        fake_client = _FakeAsyncClient(fake_resp, stream=True)

        with patch("rdx.proxy.server.httpx.AsyncClient", return_value=fake_client):
            request = _make_request({"model": "test", "stream": True, "messages": []})
            response = await _forward_raw(request, {"stream": True}, 30.0)

        # Client should NOT be closed yet — stream hasn't been consumed
        assert not fake_client.aclosed, "Client was closed before stream consumption"
        assert not fake_resp.aclosed, "Response was closed before stream consumption"

        # Now consume the stream
        collected = []
        async for chunk in response.body_iterator:
            collected.append(chunk)

        # After full consumption, both client and response should be closed
        assert fake_resp.aclosed, "Response was not closed after stream completed"
        assert fake_client.aclosed, "Client was not closed after stream completed"
        assert b"".join(collected) == b"".join(chunks)

    @pytest.mark.asyncio
    async def test_forward_raw_streaming_multi_chunk(self) -> None:
        """Streaming with many chunks — client stays alive until the last one."""
        chunks = [f"chunk-{i}\n".encode() for i in range(50)]
        fake_resp = _FakeStreamResponse(chunks)
        fake_client = _FakeAsyncClient(fake_resp, stream=True)

        with patch("rdx.proxy.server.httpx.AsyncClient", return_value=fake_client):
            request = _make_request({"model": "test", "stream": True, "messages": []})
            response = await _forward_raw(request, {"stream": True}, 30.0)

        # Consume all chunks
        collected = []
        async for chunk in response.body_iterator:
            collected.append(chunk)

        assert len(collected) == 50
        assert fake_client.aclosed
        assert fake_resp.aclosed

    @pytest.mark.asyncio
    async def test_forward_raw_non_streaming_closes_client(self) -> None:
        """Non-streaming path should close client immediately after response."""
        fake_client = _FakeAsyncClient({"status": "ok"})

        with patch("rdx.proxy.server.httpx.AsyncClient", return_value=fake_client):
            request = _make_request({"model": "test", "messages": []})
            response = await _forward_raw(request, {"stream": False}, 30.0)

        # Non-streaming: client should be closed right away
        assert fake_client.aclosed, "Client was not closed after non-streaming response"

    @pytest.mark.asyncio
    async def test_forward_raw_streaming_client_closed_on_exception(self) -> None:
        """If the stream raises an exception, the client is still closed in finally."""
        class _ErrorStreamResponse:
            status_code = 200
            aclosed = False

            async def aiter_bytes(self):
                yield b"first chunk"
                raise RuntimeError("connection lost")

            async def aclose(self):
                self.aclosed = True

        fake_resp = _ErrorStreamResponse()
        fake_client = _FakeAsyncClient(fake_resp, stream=True)

        with patch("rdx.proxy.server.httpx.AsyncClient", return_value=fake_client):
            request = _make_request({"model": "test", "stream": True, "messages": []})
            response = await _forward_raw(request, {"stream": True}, 30.0)

        # Consume the stream — should raise
        with pytest.raises(RuntimeError, match="connection lost"):
            async for _ in response.body_iterator:
                pass

        # Client and response should still be closed via finally
        assert fake_resp.aclosed, "Response was not closed after stream error"
        assert fake_client.aclosed, "Client was not closed after stream error"


# ---------------------------------------------------------------------------
# Bug 2: NameError _cache → cache
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
        # Create a temporary project with .redaction_rules
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
                {"role": "user", "content": "My key is sk-test-abc123"},
            ],
        }

        with patch("rdx.proxy.server.httpx.AsyncClient", return_value=fake_client):
            request = _make_request(body)
            # This should NOT raise NameError
            response = await proxy_messages(request)

        # Verify we got a valid response
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_proxy_messages_no_name_error_with_audit_enabled(self, tmp_path) -> None:
        """proxy_messages should not raise NameError when audit logging is enabled.

        The audit code paths (lines 235-245, 294-302, 345-353) were the primary
        sites of the _cache→cache bug. Enable audit to exercise them.
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
                {"role": "user", "content": "My key is sk-test-abc123"},
            ],
        }

        # Enable audit to exercise the _cache code paths
        old_audit = server_module._audit_enabled
        server_module._audit_enabled = True
        try:
            with patch("rdx.proxy.server.httpx.AsyncClient", return_value=fake_client):
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
                {"role": "user", "content": "My key is sk-test-abc123"},
            ],
        }

        with patch("rdx.proxy.server.httpx.AsyncClient", return_value=fake_client):
            request = _make_request(body)
            response = await proxy_messages(request)

        # Consume the streaming response — should not raise NameError
        collected = []
        async for chunk in response.body_iterator:
            collected.append(chunk)

        assert len(collected) > 0
        # Client should be closed after stream completes
        assert fake_client.aclosed

    @pytest.mark.asyncio
    async def test_proxy_messages_audit_streaming_no_name_error(self, tmp_path) -> None:
        """Streaming path with audit enabled — exercises _cache.get_reverse_map()."""
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
                {"role": "user", "content": "My key is sk-test-abc123"},
            ],
        }

        old_audit = server_module._audit_enabled
        server_module._audit_enabled = True
        try:
            with patch("rdx.proxy.server.httpx.AsyncClient", return_value=fake_client):
                request = _make_request(body)
                response = await proxy_messages(request)

            collected = []
            async for chunk in response.body_iterator:
                collected.append(chunk)

            assert len(collected) > 0
            assert fake_client.aclosed
        finally:
            server_module._audit_enabled = old_audit


# ---------------------------------------------------------------------------
# Integration: verify no _cache references remain in source
# ---------------------------------------------------------------------------


class TestNoCacheUnderscoreReferences:
    """Ensure the source code has no remaining _cache attribute references
    inside proxy_messages (the bug was _cache→cache)."""

    def test_no_cache_underscore_in_proxy_messages(self) -> None:
        """The proxy_messages function should not reference _cache (module-level
        dict) for per-project operations. It should use the local `cache` variable."""
        import inspect
        from rdx.proxy.server import proxy_messages

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
        import inspect
        from rdx.proxy.server import _forward_raw

        source = inspect.getsource(_forward_raw)
        assert "async with httpx.AsyncClient" not in source, (
            "_forward_raw uses 'async with httpx.AsyncClient' which causes "
            "httpx.ReadError on streaming responses."
        )

    def test_no_async_with_httpx_in_proxy_messages_streaming(self) -> None:
        """proxy_messages should not use 'async with httpx.AsyncClient' —
        the context manager closes the client before streaming finishes."""
        import inspect
        from rdx.proxy.server import proxy_messages

        source = inspect.getsource(proxy_messages)
        assert "async with httpx.AsyncClient" not in source, (
            "proxy_messages uses 'async with httpx.AsyncClient' which causes "
            "httpx.ReadError on streaming responses."
        )
