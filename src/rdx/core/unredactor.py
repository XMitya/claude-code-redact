"""Reverses redactions using the in-memory mapping cache."""

from __future__ import annotations

from .mappings import MappingCache

# Prefixes for auto-generated tokens — these have a fixed format and
# _match_case should NOT be applied to them (the original is stored as-is).
_TOKEN_PREFIXES = ("__rdx_",)


def _is_token(replacement: str) -> bool:
    """Return True if replacement is an auto-generated __RDX_ token."""
    return replacement.lower().startswith(_TOKEN_PREFIXES)


def _match_case(template: str, text: str) -> str:
    """Adjust text to match the case style of template.

    - ALL UPPER → ALL UPPER
    - all lower → all lower
    - Title Case → Title Case
    - Otherwise → text as-is
    """
    if template.isupper():
        return text.upper()
    if template.islower():
        return text.lower()
    if template.istitle():
        return text.title()
    return text


def _pick_original(originals: list[str]) -> str:
    """Pick the best original from multiple candidates sharing one replacement.

    Always prefers Latin/ASCII originals. Format-preserving replacements
    are always Latin (e.g. ``AppStore``), and in code-editing contexts the
    Latin original (e.g. ``RUSTORE``) is the one that matters — it appears
    in identifiers, ticket keys, URLs, etc. Restoring ``РУСТОР`` instead
    of ``RUSTORE`` corrupts file edits.
    """
    for orig in originals:
        if orig.isascii():
            return orig
    return originals[0]


class Unredactor:
    """Reverses redactions using the in-memory mapping cache."""

    def __init__(self, cache: MappingCache) -> None:
        self.cache = cache

    def unredact(self, text: str) -> str:
        """Replace all redaction tokens / replacements with original values."""
        reverse_map_all = self.cache.get_reverse_map_all()
        if not reverse_map_all:
            return text

        # Build a case-insensitive lookup: lowercase replacement -> (originals, raw_replacement)
        # This handles _match_case() in redactor.py which may change the
        # token case (e.g. __RDX_KEY_abc123__ -> __rdx_key_abc123__).
        ci_map: dict[str, tuple[list[str], str]] = {
            k.lower(): (v, k) for k, v in reverse_map_all.items()
        }

        # Sort by replacement length (longest first) to avoid partial-match
        # corruption — e.g. replacing "AppSto" before "AppStore".
        result = text
        for ci_token, (originals, raw_replacement) in sorted(
            ci_map.items(), key=lambda x: -len(x[0])
        ):
            is_token = _is_token(raw_replacement)
            lower_result = result.lower()
            while True:
                pos = lower_result.find(ci_token)
                if pos == -1:
                    break
                found = result[pos:pos + len(ci_token)]

                if len(originals) == 1:
                    original = originals[0]
                else:
                    # Multiple originals share this replacement — always
                    # prefer Latin (code-editing safety).
                    original = _pick_original(originals)

                # For __RDX_ tokens, _match_case produces wrong results because
                # "__RDX_A__".isupper() is True (underscores are not cased),
                # which would turn "bob" into "BOB". The original is already
                # stored in its correct case — return as-is.
                if is_token:
                    restored = original
                else:
                    # Format-preserving replacement: match case of found text
                    restored = _match_case(found, original)

                result = result[:pos] + restored + result[pos + len(ci_token):]
                lower_result = result.lower()

        return result

    def unredact_value(self, token: str) -> str:
        """Un-redact a single token. Returns *token* unchanged if not found."""
        original = self.cache.unredact(token)
        return original if original is not None else token
