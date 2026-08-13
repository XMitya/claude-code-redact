"""Tests for rdx.core.unredactor."""

from __future__ import annotations

from rdx.core.mappings import MappingCache
from rdx.core.models import Rule
from rdx.core.redactor import Redactor
from rdx.core.unredactor import Unredactor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _name_rule(replacement: str | None = None) -> Rule:
    return Rule(
        id="name-pablo",
        pattern="pablo",
        is_regex=False,
        action="redact",
        replacement=replacement,
        category="NAME",
        description="Redact name",
    )


def _email_rule(replacement: str | None = None) -> Rule:
    return Rule(
        id="email",
        pattern=r"[\w.+-]+@[\w-]+\.[\w.]+",
        is_regex=True,
        action="redact",
        replacement=replacement,
        category="EMAIL",
        description="Redact email",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFormatPreservingUnredact:
    def test_unredact_format_preserving(self) -> None:
        cache = MappingCache()
        cache.get_or_create("r1", "pablo", "NAME", "peter")
        unredactor = Unredactor(cache)
        assert unredactor.unredact("hello peter") == "hello pablo"

    def test_unredact_single_value(self) -> None:
        cache = MappingCache()
        cache.get_or_create("r1", "pablo", "NAME", "peter")
        unredactor = Unredactor(cache)
        assert unredactor.unredact_value("peter") == "pablo"


class TestTokenUnredact:
    def test_unredact_auto_token(self) -> None:
        cache = MappingCache()
        token = cache.get_or_create("r1", "pablo", "NAME")
        unredactor = Unredactor(cache)
        assert unredactor.unredact(f"hello {token}") == "hello pablo"
        assert token.startswith("__RDX_NAME_")

    def test_unredact_value_auto_token(self) -> None:
        cache = MappingCache()
        token = cache.get_or_create("r1", "pablo", "NAME")
        unredactor = Unredactor(cache)
        assert unredactor.unredact_value(token) == "pablo"


class TestMultipleValues:
    def test_unredact_multiple_replacements(self) -> None:
        cache = MappingCache()
        cache.get_or_create("r1", "pablo", "NAME", "peter")
        cache.get_or_create("r2", "foo@bar.com", "EMAIL", "hidden@example.com")
        unredactor = Unredactor(cache)
        text = "peter wrote to hidden@example.com"
        result = unredactor.unredact(text)
        assert result == "pablo wrote to foo@bar.com"


class TestUnknownToken:
    def test_unknown_token_passes_through(self) -> None:
        cache = MappingCache()
        unredactor = Unredactor(cache)
        assert unredactor.unredact_value("__RDX_NAME_deadbeef__") == "__RDX_NAME_deadbeef__"

    def test_unknown_text_unchanged(self) -> None:
        cache = MappingCache()
        unredactor = Unredactor(cache)
        assert unredactor.unredact("nothing to undo") == "nothing to undo"


class TestEmptyCache:
    def test_empty_cache_returns_text_unchanged(self) -> None:
        cache = MappingCache()
        unredactor = Unredactor(cache)
        text = "hello __RDX_NAME_abc12345__ world"
        assert unredactor.unredact(text) == text


class TestLongestFirstReplacement:
    def test_no_partial_match_corruption(self) -> None:
        """Longer replacement tokens must be substituted first.

        Without longest-first ordering, replacing "__RDX_A__" before
        "__RDX_AA__" would corrupt the longer token. Longest-first
        ensures "__RDX_AA__" is handled before "__RDX_A__".
        """
        cache = MappingCache()
        cache.get_or_create("r1", "bob", "NAME", "__RDX_A__")
        cache.get_or_create("r2", "carol", "NAME", "__RDX_AA__")
        unredactor = Unredactor(cache)
        text = "user __RDX_AA__ and user __RDX_A__"
        result = unredactor.unredact(text)
        # "__RDX_AA__" (longer) must be replaced first so that it is
        # not partially consumed by the shorter "__RDX_A__" pattern.
        # Auto-tokens (__RDX_*) skip _match_case — originals returned as-is.
        assert result == "user carol and user bob"

    def test_shorter_token_does_not_clobber_longer(self) -> None:
        """If we did NOT sort longest-first, the shorter token would break things."""
        cache = MappingCache()
        cache.get_or_create("r1", "x", "CUSTOM", "AB")
        cache.get_or_create("r2", "y", "CUSTOM", "ABC")
        unredactor = Unredactor(cache)
        # "ABC" must be replaced before "AB" so we get "y" not "xC"
        # Note: _match_case("ABC", "y") returns "Y" because "ABC" is all-upper
        assert unredactor.unredact("ABC") == "Y"


class TestRoundTrip:
    def test_redact_then_unredact_preserves_original(self) -> None:
        """Full round-trip: redact -> unredact should recover the original."""
        cache = MappingCache()
        redactor = Redactor([_name_rule(replacement="peter")], cache)
        original = "hello pablo, how are you pablo?"
        scan_result = redactor.redact(original)

        unredactor = Unredactor(cache)
        restored = unredactor.unredact(scan_result.redacted_text or "")
        assert restored == original

    def test_round_trip_auto_token(self) -> None:
        """Round-trip with auto-generated tokens."""
        cache = MappingCache()
        redactor = Redactor([_name_rule()], cache)
        original = "hello pablo"
        scan_result = redactor.redact(original)

        unredactor = Unredactor(cache)
        restored = unredactor.unredact(scan_result.redacted_text or "")
        assert restored == original

    def test_round_trip_multiple_rules(self) -> None:
        """Round-trip with multiple rules and values."""
        cache = MappingCache()
        rules = [_name_rule(replacement="peter"), _email_rule(replacement="hidden@example.com")]
        redactor = Redactor(rules, cache)
        original = "pablo sent mail to user@corp.io"
        scan_result = redactor.redact(original)
        assert scan_result.redacted_text is not None
        assert "pablo" not in scan_result.redacted_text

        unredactor = Unredactor(cache)
        restored = unredactor.unredact(scan_result.redacted_text)
        assert restored == original


class TestSharedReplacementUnredaction:
    """When multiple originals share one format-preserving replacement,
    the unredactor must always pick the Latin original.

    Format-preserving replacements (e.g. AppStore) are always Latin.
    In code-editing contexts, the Latin original (e.g. RUSTORE) is the
    one that matters — it appears in identifiers, ticket keys, URLs.
    Restoring the Cyrillic variant (РУСТОР) corrupts file edits.
    """

    def test_always_latin_even_in_cyrillic_context(self) -> None:
        """AppStore always -> RUSTORE, even in Cyrillic text."""
        cache = MappingCache()
        cache.get_or_create("rustore", "RUSTORE", "PROJECT", "AppStore")
        cache.get_or_create("rustore", "РУСТОР", "PROJECT", "AppStore")
        unredactor = Unredactor(cache)
        result = unredactor.unredact("Проект AppStore готов к релизу")
        assert "RUSTORE" in result
        assert "РУСТОР" not in result

    def test_always_latin_in_latin_context(self) -> None:
        """AppStore -> RUSTORE in Latin context."""
        cache = MappingCache()
        cache.get_or_create("rustore", "RUSTORE", "PROJECT", "AppStore")
        cache.get_or_create("rustore", "РУСТОР", "PROJECT", "AppStore")
        unredactor = Unredactor(cache)
        result = unredactor.unredact("The AppStore project is ready")
        assert "RUSTORE" in result
        assert "РУСТОР" not in result

    def test_lowercase_always_latin(self) -> None:
        """Lowercase 'appstore' -> 'rustore' (Latin), even in Cyrillic context."""
        cache = MappingCache()
        cache.get_or_create("rustore", "RUSTORE", "PROJECT", "AppStore")
        cache.get_or_create("rustore", "РУСТОР", "PROJECT", "AppStore")
        unredactor = Unredactor(cache)
        result = unredactor.unredact("используй appstore для сборки")
        assert "rustore" in result
        assert "рустор" not in result

    def test_uppercase_always_latin(self) -> None:
        """Uppercase APPSTORE -> RUSTORE, even in Cyrillic context."""
        cache = MappingCache()
        cache.get_or_create("rustore", "RUSTORE", "PROJECT", "AppStore")
        cache.get_or_create("rustore", "РУСТОР", "PROJECT", "AppStore")
        unredactor = Unredactor(cache)
        result = unredactor.unredact("ПРОЕКТ APPSTORE ГОТОВ")
        assert "RUSTORE" in result

    def test_ticket_key_not_corrupted(self) -> None:
        """APPSTORE-103217 -> RUSTORE-103217 (not РУСТОР-103217)."""
        cache = MappingCache()
        cache.get_or_create("rustore", "RUSTORE", "PROJECT", "AppStore")
        cache.get_or_create("rustore", "РУСТОР", "PROJECT", "AppStore")
        unredactor = Unredactor(cache)
        result = unredactor.unredact("Посмотри тикет APPSTORE-103217")
        assert "RUSTORE-103217" in result
        assert "РУСТОР" not in result

    def test_round_trip_redact_unredact_latin(self) -> None:
        """Full round-trip: redact RUSTORE -> APPSTORE -> unredact back to RUSTORE."""
        cache = MappingCache()
        rule = Rule(
            id="rustore",
            pattern=r"(?i)рустор|rustore",
            is_regex=True,
            action="redact",
            replacement="AppStore",
            category="PROJECT",
        )
        redactor = Redactor([rule], cache)
        original = "RUSTORE is a great project"
        scan_result = redactor.redact(original)
        assert scan_result.redacted_text is not None
        assert "RUSTORE" not in scan_result.redacted_text
        # Redactor applies _match_case: RUSTORE is upper -> AppStore becomes APPSTORE
        assert "APPSTORE" in scan_result.redacted_text

        unredactor = Unredactor(cache)
        restored = unredactor.unredact(scan_result.redacted_text)
        assert restored == original

    def test_round_trip_cyrillic_restores_latin_when_both_in_cache(self) -> None:
        """When both RUSTORE and РУСТОР are in cache, unredact always picks RUSTORE.

        Even if the redacted text was originally РУСТОР, the Latin original
        is restored because it's the one used in code, identifiers, and
        ticket keys. Restoring Cyrillic would corrupt file edits.
        """
        cache = MappingCache()
        rule = Rule(
            id="rustore",
            pattern=r"(?i)рустор|rustore",
            is_regex=True,
            action="redact",
            replacement="AppStore",
            category="PROJECT",
        )
        redactor = Redactor([rule], cache)
        # Redact both so both originals are in cache
        redactor.redact("RUSTORE")
        scan_result = redactor.redact("РУСТОР — отличный проект")
        assert scan_result.redacted_text is not None
        assert "РУСТОР" not in scan_result.redacted_text
        assert "APPSTORE" in scan_result.redacted_text

        unredactor = Unredactor(cache)
        restored = unredactor.unredact(scan_result.redacted_text)
        # Latin original is restored, not Cyrillic
        assert "RUSTORE" in restored
