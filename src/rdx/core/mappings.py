"""In-memory bidirectional mapping cache for redaction tokens."""

import hashlib
import threading

from .models import Category, Redaction


class MappingCache:
    """In-memory bidirectional mapping cache for redaction tokens.

    No file persistence -- everything lives in process memory.
    Forward map: (rule_id, original) -> replacement
    Reverse map: replacement -> list[Redaction]

    Multiple originals may share the same format-preserving replacement
    (e.g. rule ``(?i)рустор|rustore`` → ``AppStore`` matches both
    ``RUSTORE`` and ``РУСТОР``).  The reverse map stores **all** originals
    so that :meth:`_pick_best_original` can choose the right one during
    un-redaction.

    Thread-safe: all mutations are protected by a lock so the proxy
    can handle concurrent requests safely.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Forward: (rule_id, original) -> replacement
        self._forward: dict[tuple[str, str], str] = {}
        # Reverse: replacement -> list of Redaction entries
        # (multiple originals may share the same replacement)
        self._reverse: dict[str, list[Redaction]] = {}

    def get_or_create(
        self,
        rule_id: str,
        original: str,
        category: Category,
        replacement: str | None = None,
    ) -> str:
        """Get existing mapping or create new one.

        If replacement is provided (format-preserving), use it.
        If None, generate deterministic __RDX_ token.
        """
        key = (rule_id, original)
        with self._lock:
            if key in self._forward:
                return self._forward[key]

            if replacement is None:
                replacement = self._generate_token(original, category)

            self._forward[key] = replacement

            # Append to reverse map (avoid duplicates for same original)
            entries = self._reverse.setdefault(replacement, [])
            if not any(e.original == original for e in entries):
                entries.append(
                    Redaction(
                        original=original,
                        replacement=replacement,
                        rule_id=rule_id,
                        category=category,
                    )
                )
            return replacement

    @staticmethod
    def _generate_token(original: str, category: Category) -> str:
        """Generate a deterministic redaction token from original text and category."""
        h = hashlib.sha256(original.encode()).hexdigest()[:8]
        return f"__RDX_{category}_{h}__"

    @staticmethod
    def _pick_best_original(entries: list[Redaction]) -> str:
        """Pick the best original from multiple entries sharing one replacement.

        Prefers Latin/ASCII originals — format-preserving replacements are
        always Latin (e.g. ``AppStore``), so a Latin original matches the
        script of the text found during un-redaction.  This prevents
        cross-script corruption such as restoring ``РУСТОР`` (Cyrillic)
        when the original was ``RUSTORE`` (Latin).

        If no Latin original exists, returns the first entry.
        """
        for entry in entries:
            if entry.original.isascii():
                return entry.original
        return entries[0].original

    def unredact(self, token: str) -> str | None:
        """Look up original value for a token. Returns None if not found."""
        with self._lock:
            entries = self._reverse.get(token)
            if not entries:
                return None
            return self._pick_best_original(entries)

    def get_reverse_map(self) -> dict[str, str]:
        """Get reverse map {replacement: best_original} for bulk un-redaction.

        When multiple originals share the same replacement, the Latin-
        preferred original is returned (see :meth:`_pick_best_original`).
        Used for audit logging and statistics where a single representative
        original is sufficient.
        """
        with self._lock:
            return {
                k: self._pick_best_original(v)
                for k, v in self._reverse.items()
            }

    def get_reverse_map_all(self) -> dict[str, list[str]]:
        """Get reverse map {replacement: [all_originals]} for bulk un-redaction.

        Returns ALL originals that share a replacement, so the unredactor
        can pick the right one based on the script of the surrounding text.
        """
        with self._lock:
            return {
                k: [e.original for e in v]
                for k, v in self._reverse.items()
            }

    def get_all_originals(self) -> set[str]:
        """Get all original values across all replacements.

        Used for prompt-safety checks where *any* original value — not just
        the preferred one — must be detected.
        """
        with self._lock:
            return {
                e.original
                for entries in self._reverse.values()
                for e in entries
            }

    def get_all_redactions(self) -> list[Redaction]:
        """Get all redaction entries for audit/display."""
        with self._lock:
            result: list[Redaction] = []
            for entries in self._reverse.values():
                result.extend(entries)
            return result

    def clear(self) -> None:
        """Clear all mappings."""
        with self._lock:
            self._forward.clear()
            self._reverse.clear()

    def stats(self) -> dict[str, int]:
        """Return mapping statistics."""
        with self._lock:
            return {
                "mappings": len(self._forward),
                "rules": len(
                    {
                        r.rule_id
                        for entries in self._reverse.values()
                        for r in entries
                    }
                ),
            }
