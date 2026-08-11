"""Reverses redactions using the in-memory mapping cache."""

from __future__ import annotations

from .mappings import MappingCache


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


class Unredactor:
    """Reverses redactions using the in-memory mapping cache."""

    def __init__(self, cache: MappingCache) -> None:
        self.cache = cache

    def unredact(self, text: str) -> str:
        """Replace all redaction tokens / replacements with original values."""
        reverse_map = self.cache.get_reverse_map()
        if not reverse_map:
            return text

        # Build a case-insensitive lookup: lowercase token -> original
        # This handles _match_case() in redactor.py which may change the
        # token case (e.g. __RDX_KEY_abc123__ -> __rdx_key_abc123__).
        ci_map = {k.lower(): v for k, v in reverse_map.items()}

        # Sort by token length (longest first) to avoid partial-match
        # corruption — e.g. replacing "__rdx_name_" before "__rdx_name_a1b2__".
        result = text
        for ci_token, original in sorted(
            ci_map.items(), key=lambda x: -len(x[0])
        ):
            # Case-insensitive replace, preserving the case style of the
            # replacement found in text (reverse of _match_case in redactor).
            lower_result = result.lower()
            lower_token = ci_token
            while True:
                pos = lower_result.find(lower_token)
                if pos == -1:
                    break
                # Extract the actual replacement text from the result
                found = result[pos:pos + len(ci_token)]
                # Match the case style of 'found' to the original
                restored = _match_case(found, original)
                result = result[:pos] + restored + result[pos + len(ci_token):]
                lower_result = result.lower()

        return result

    def unredact_value(self, token: str) -> str:
        """Un-redact a single token. Returns *token* unchanged if not found."""
        original = self.cache.unredact(token)
        return original if original is not None else token
