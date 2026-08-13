"""SSE streaming handler for un-redacting Anthropic API responses."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from rdx.core.unredactor import Unredactor

# Maximum buffer size before flushing as-is (not a redaction token).
_MAX_BUFFER = 512

# Regex to find start of a redaction token: __RDX_ or __rdx_ (case-insensitive)
# followed by CATEGORY_ and hex hash, ending with __
_TOKEN_RE = re.compile(r"__rdx_\w+_[0-9a-f]{8}__", re.IGNORECASE)
# Prefix that marks the start of a redaction token (any case)
_TOKEN_PREFIXES = ("__RDX_", "__rdx_")
_TOKEN_SUFFIX = "__"


def _unredact_value(value: Any, unredactor: Unredactor) -> Any:
    """Recursively un-redact string values in dicts/lists."""
    if isinstance(value, str):
        return unredactor.unredact(value)
    if isinstance(value, dict):
        return {k: _unredact_value(v, unredactor) for k, v in value.items()}
    if isinstance(value, list):
        return [_unredact_value(item, unredactor) for item in value]
    return value


def _format_sse(event: str, data: str) -> bytes:
    """Format an SSE event as bytes."""
    return f"event: {event}\ndata: {data}\n\n".encode()


class TextDeltaBuffer:
    """Buffers text_delta tokens to handle redaction tokens split across events.

    Handles two kinds of redaction markers that may be split across
    streaming deltas:

    1. Auto-generated tokens: ``__RDX_CATEGORY_hash__`` (or lowercase).
       Buffered from the ``__RDX_`` prefix until the closing ``__``.

    2. Format-preserving replacements (e.g. ``AppStore``): plain words
       that may be split across deltas (e.g. ``App`` + ``Store``).
       The buffer keeps a tail that could be the start of a known
       replacement, so the next delta can complete it.
    """

    def __init__(self, unredactor: Unredactor) -> None:
        self.unredactor = unredactor
        self._buffer = ""

    def feed(self, text: str) -> str:
        """Feed new text and return any text ready to be emitted."""
        self._buffer += text
        return self._flush()

    def flush_remaining(self) -> str:
        """Flush whatever is left in the buffer (end of stream)."""
        out = self.unredactor.unredact(self._buffer)
        self._buffer = ""
        return out

    def _find_token_prefix(self, text: str) -> int:
        """Find the earliest occurrence of any token prefix (case-insensitive)."""
        lower_text = text.lower()
        positions = [lower_text.find(p.lower()) for p in _TOKEN_PREFIXES]
        positions = [p for p in positions if p != -1]
        return min(positions) if positions else -1

    def _flush(self) -> str:
        """Emit as much completed text as possible from the buffer."""
        output_parts: list[str] = []

        while self._buffer:
            prefix_pos = self._find_token_prefix(self._buffer)

            if prefix_pos == -1:
                # No __RDX_ token prefix in buffer.
                # Check for format-preserving replacements that may be
                # split across deltas (e.g. "App" + "Store" = "AppStore").
                keep = self._partial_replacement_length()
                if keep > 0:
                    # The tail might be the start of a replacement word.
                    # But first, unredact and emit everything before it.
                    emit_part = self._buffer[:-keep]
                    if emit_part:
                        output_parts.append(self.unredactor.unredact(emit_part))
                    self._buffer = self._buffer[-keep:]
                    # Also check __RDX_ partial prefix
                    rdx_keep = self._partial_prefix_length()
                    if rdx_keep > 0 and rdx_keep < len(self._buffer):
                        output_parts.append(self.unredactor.unredact(self._buffer[:-rdx_keep]))
                        self._buffer = self._buffer[-rdx_keep:]
                else:
                    # Check for partial __RDX_ prefix
                    rdx_keep = self._partial_prefix_length()
                    if rdx_keep > 0:
                        output_parts.append(self.unredactor.unredact(self._buffer[:-rdx_keep]))
                        self._buffer = self._buffer[-rdx_keep:]
                    else:
                        output_parts.append(self.unredactor.unredact(self._buffer))
                        self._buffer = ""
                break

            if prefix_pos > 0:
                # Emit everything before the prefix.
                output_parts.append(self.unredactor.unredact(self._buffer[:prefix_pos]))
                self._buffer = self._buffer[prefix_pos:]

            # Buffer starts with a token prefix — look for closing __
            # Determine which prefix matched
            matched_prefix = next(p for p in _TOKEN_PREFIXES if self._buffer.startswith(p))
            suffix_search_start = len(matched_prefix)
            suffix_pos = self._buffer.find(_TOKEN_SUFFIX, suffix_search_start)

            if suffix_pos != -1:
                # Found a complete token.
                token = self._buffer[: suffix_pos + len(_TOKEN_SUFFIX)]
                self._buffer = self._buffer[suffix_pos + len(_TOKEN_SUFFIX) :]
                output_parts.append(self.unredactor.unredact(token))
            elif len(self._buffer) > _MAX_BUFFER:
                # Buffer too large — not a real token, flush as-is.
                output_parts.append(self.unredactor.unredact(self._buffer))
                self._buffer = ""
                break
            else:
                # Incomplete token — keep buffering.
                break

        return "".join(output_parts)

    def _partial_prefix_length(self) -> int:
        """Check if the buffer ends with a partial __RDX_ token prefix.

        Returns the length of the trailing partial match (0 if none).
        """
        max_len = max(len(p) for p in _TOKEN_PREFIXES)
        lower_buffer = self._buffer.lower()
        for length in range(min(len(self._buffer), max_len), 0, -1):
            for prefix in _TOKEN_PREFIXES:
                if length <= len(prefix) and prefix.lower()[:length] == lower_buffer[-length:]:
                    return length
        return 0

    def _partial_replacement_length(self) -> int:
        """Check if the buffer ends with a partial format-preserving replacement.

        Returns the length of the trailing partial match (0 if none).
        Only considers non-token replacements (e.g. ``AppStore``), not
        ``__RDX_`` tokens (those are handled by _partial_prefix_length).
        """
        reverse_map_all = self.unredactor.cache.get_reverse_map_all()
        if not reverse_map_all:
            return 0

        lower_buffer = self._buffer.lower()
        max_keep = 0

        for replacement in reverse_map_all:
            if replacement.lower().startswith(_TOKEN_PREFIXES):
                continue  # __RDX_ tokens handled separately
            r_lower = replacement.lower()
            # Check if the buffer tail matches the start of this replacement.
            # E.g. buffer ends with "app", replacement is "appstore" -> match length 3.
            max_possible = min(len(self._buffer), len(r_lower) - 1)
            for length in range(max_possible, 0, -1):
                if r_lower[:length] == lower_buffer[-length:]:
                    if length > max_keep:
                        max_keep = length
                    break

        return max_keep


class ToolUseBuffer:
    """Buffers tool_use input_json_delta fragments until content_block_stop."""

    def __init__(self) -> None:
        self._buffers: dict[int, str] = {}

    def feed(self, index: int, json_fragment: str) -> None:
        """Accumulate a JSON fragment for the given content block index."""
        if index not in self._buffers:
            self._buffers[index] = ""
        self._buffers[index] += json_fragment

    def flush(self, index: int, unredactor: Unredactor) -> str | None:
        """Flush and un-redact the complete JSON for a content block index."""
        raw = self._buffers.pop(index, None)
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
            unredacted = _unredact_value(parsed, unredactor)
            return json.dumps(unredacted)
        except json.JSONDecodeError:
            # If we can't parse, just do string-level un-redaction
            return unredactor.unredact(raw)


async def unredact_stream(
    upstream_response: httpx.Response,
    unredactor: Unredactor,
) -> AsyncGenerator[bytes, None]:
    """Process SSE stream from Anthropic, un-redact text content, yield events."""
    text_buffer = TextDeltaBuffer(unredactor)
    thinking_buffer = TextDeltaBuffer(unredactor)
    tool_buffer = ToolUseBuffer()
    event_type = "message"  # Default if data arrives before an event line

    async for line in upstream_response.aiter_lines():
        if not line:
            continue

        # SSE format: lines are either "event: <type>" or "data: <json>"
        if line.startswith("event: "):
            event_type = line[len("event: "):]
            continue  # Will be emitted with its data line

        if not line.startswith("data: "):
            continue

        raw_data = line[len("data: "):]

        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            # Not JSON (e.g., "[DONE]") — pass through.
            yield f"event: {event_type}\ndata: {raw_data}\n\n".encode()
            continue

        msg_type = data.get("type", "")

        # Diagnostic: log if reverse_map is non-empty and this data contains
        # any known replacement (either __RDX_ token or format-preserving)
        reverse_map = unredactor.cache.get_reverse_map()
        if reverse_map:
            lower_data = raw_data.lower()
            for replacement in reverse_map:
                if replacement.lower() in lower_data:
                    print(f"[rdx][unredact] {msg_type} contains replacement '{replacement[:30]}': {raw_data[:200]}", file=sys.stderr)
                    break

        if msg_type == "content_block_delta":
            delta = data.get("delta", {})
            delta_type = delta.get("type", "")
            index = data.get("index", 0)

            if delta_type == "text_delta":
                original_text = delta.get("text", "")
                if not original_text:
                    # Empty text delta — pass through as-is
                    yield _format_sse(event_type, json.dumps(data))
                    continue
                # First unredact format-preserving replacements (whole words)
                unredacted = unredactor.unredact(original_text)
                # Then feed through buffer for __RDX_ tokens that may be
                # split across deltas
                emitted = text_buffer.feed(unredacted)
                if emitted:
                    delta["text"] = emitted
                    data["delta"] = delta
                    yield _format_sse(event_type, json.dumps(data))
                continue

            if delta_type == "thinking_delta":
                original_text = delta.get("thinking", "")
                if not original_text:
                    yield _format_sse(event_type, json.dumps(data))
                    continue
                unredacted = unredactor.unredact(original_text)
                emitted = thinking_buffer.feed(unredacted)
                if emitted:
                    delta["thinking"] = emitted
                    data["delta"] = delta
                    yield _format_sse(event_type, json.dumps(data))
                continue

            if delta_type == "input_json_delta":
                tool_buffer.feed(index, delta.get("partial_json", ""))
                # Buffer — don't yield yet. We'll emit the complete
                # unredacted JSON at content_block_stop.
                continue

        if msg_type == "content_block_stop":
            index = data.get("index", 0)
            # Flush any remaining text in the buffer
            remaining = text_buffer.flush_remaining()
            if remaining:
                text_delta_event = {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": remaining},
                }
                yield _format_sse("content_block_delta", json.dumps(text_delta_event))

            # Flush any remaining thinking text
            remaining_thinking = thinking_buffer.flush_remaining()
            if remaining_thinking:
                thinking_delta_event = {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "thinking_delta", "thinking": remaining_thinking},
                }
                yield _format_sse("content_block_delta", json.dumps(thinking_delta_event))

            # Emit unredacted tool_use JSON as a single input_json_delta
            unredacted_json = tool_buffer.flush(index, unredactor)
            if unredacted_json is not None:
                tool_delta_event = {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": unredacted_json},
                }
                yield _format_sse("content_block_delta", json.dumps(tool_delta_event))

        # Pass through the event as-is (message_start, message_delta,
        # message_stop, ping, content_block_start, content_block_stop, etc.)
        yield _format_sse(event_type, json.dumps(data))
