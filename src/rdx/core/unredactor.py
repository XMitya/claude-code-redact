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


def _pick_original(originals: list[str], found: str) -> str:
    """Pick the best original from multiple candidates sharing one replacement.

    Always prefers Latin/ASCII originals — format-preserving replacements
    are always Latin, and in code-editing contexts the Latin original is
    the one that matters (identifiers, ticket keys, URLs).

    When multiple ASCII originals exist with different cases (e.g. ProjectX,
    PROJECTX, projectx), pick the one whose case style matches the replacement
    text found in the response. This ensures that ``SUBSTITUTE`` restores as
    ``PROJECTX`` (upper), ``substitute`` as ``projectx`` (lower), and
    ``Substitute`` as ``ProjectX`` (mixed/title).
    """
    ascii_originals = [o for o in originals if o.isascii()]
    if not ascii_originals:
        return originals[0]

    if len(ascii_originals) == 1:
        return ascii_originals[0]

    # Try to find an ASCII original whose case style matches the found text
    found_upper = found.isupper()
    found_lower = found.islower()
    found_title = found.istitle()

    # If found is title case, also match mixed-case originals (e.g. ProjectX)
    # because Python's istitle() returns False for "ProjectX" (X has no
    # following lowercase letter), but it's still the right original for
    # a title-case replacement like "Substitute".
    found_mixed = not (found_upper or found_lower)

    for orig in ascii_originals:
        if found_upper and orig.isupper():
            return orig
        if found_lower and orig.islower():
            return orig
        if found_title and orig.istitle():
            return orig
        if found_mixed and not (orig.isupper() or orig.islower()):
            return orig

    # No case match — prefer the one that's NOT all-upper (title/mixed
    # is more common in identifiers like ProjectXVerCode...)
    for orig in ascii_originals:
        if not orig.isupper() and not orig.islower():
            return orig

    return ascii_originals[0]


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
                    # Single original: match case of found text
                    restored = _match_case(found, original) if not is_token else original
                else:
                    # Multiple originals share this replacement — pick
                    # the best Latin original matching the found case.
                    picked = _pick_original(originals, found)

                    # Check if _pick_original found an exact case match.
                    # If so, return as-is. Otherwise, apply _match_case
                    # to adjust the picked original to the found case.
                    case_matches = (
                        (found.isupper() and picked.isupper())
                        or (found.islower() and picked.islower())
                        or (found.istitle() and picked.istitle())
                        # mixed case (not all-upper, not all-lower): e.g.
                        # found="Substitute" (istitle=True), picked="ProjectX"
                        # (not istitle, but mixed) — treat as match
                        or (not found.isupper() and not found.islower()
                            and not picked.isupper() and not picked.islower())
                    )
                    if case_matches or is_token:
                        restored = picked
                    else:
                        restored = _match_case(found, picked)

                result = result[:pos] + restored + result[pos + len(ci_token):]
                lower_result = result.lower()

        return result

    def unredact_value(self, token: str) -> str:
        """Un-redact a single token. Returns *token* unchanged if not found."""
        original = self.cache.unredact(token)
        return original if original is not None else token
