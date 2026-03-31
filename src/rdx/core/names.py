"""Name variant expansion for person-based redaction rules.

Given a full name like 'John Smith Williams', generates all common
corporate variants (dot-separated, underscore, camelCase, initials,
truncated, etc.) and pairs them with the replacement name's variants.
"""

from __future__ import annotations


def expand_name(full: str) -> list[str]:
    """Generate common corporate variants of a full name.

    Returns lowercase-normalized variants. The caller should compile
    patterns with re.IGNORECASE to catch all case forms.
    """
    parts = full.strip().split()
    if not parts:
        return []
    if len(parts) == 1:
        return [parts[0].lower()]

    first = parts[0].lower()
    all_lasts = [p.lower() for p in parts[1:]]
    first_last = all_lasts[0]
    final_last = all_lasts[-1]

    variants: set[str] = set()

    # --- Full name forms ---
    variants.add(" ".join(parts).lower())                       # john smith williams
    variants.add(".".join(p.lower() for p in parts))            # john.smith.williams
    variants.add("_".join(p.lower() for p in parts))            # john_smith_williams
    variants.add("-".join(p.lower() for p in parts))            # john-smith-williams
    variants.add("".join(p.lower() for p in parts))             # johnsmithwilliams

    # --- Two-part combos (first + each last) ---
    for last in all_lasts:
        variants.add(f"{first}.{last}")                         # john.smith, john.williams
        variants.add(f"{first}_{last}")                         # john_smith
        variants.add(f"{first}-{last}")                         # john-smith
        variants.add(f"{first}{last}")                          # johnsmith

        # Initial + last
        variants.add(f"{first[0]}{last}")                       # jsmith
        variants.add(f"{first[0]}.{last}")                      # j.smith
        variants.add(f"{first[0]}_{last}")                      # j_smith

        # First + initial
        variants.add(f"{first}.{last[0]}")                      # john.s
        variants.add(f"{first}{last[0]}")                       # johns

        # Truncated (8-char limit — old LDAP/Unix)
        variants.add(f"{first}{last}"[:8])                      # johnsmit
        variants.add(f"{first[0]}{last}"[:8])                   # jsmithwi → jsmith

    # --- Multi-initial combos ---
    if len(all_lasts) >= 2:
        # First initial + first last + second last initial
        variants.add(f"{first[0]}{first_last}{final_last[0]}")  # jsmithw
        # All initials + last
        initials = "".join(p[0].lower() for p in parts[:-1])
        variants.add(f"{initials}{final_last}")                 # jswilliams
        variants.add(f"{initials}.{final_last}")                # js.williams

    # --- First name only ---
    variants.add(first)                                          # john

    # Remove empty strings
    variants.discard("")

    return sorted(variants)


def pair_variants(
    original_name: str,
    replacement_name: str,
) -> list[tuple[str, str]]:
    """Generate paired (original_variant, replacement_variant) tuples.

    Both names are expanded, then variants at the same index are paired.
    If the names have different numbers of parts, best-effort pairing is used.
    """
    orig_variants = expand_name(original_name)
    repl_variants = expand_name(replacement_name)

    # Build a mapping by variant "type" (structure)
    # Two variants are the same type if they have the same structure
    # when you replace the name parts with placeholders
    orig_parts = original_name.lower().split()
    repl_parts = replacement_name.lower().split()

    pairs: list[tuple[str, str]] = []

    # Direct structural pairing: apply the same transformation to both names
    pairs_set: set[tuple[str, str]] = set()

    for ov in orig_variants:
        # Figure out what transformation produced this variant
        rv = _apply_same_transform(ov, orig_parts, repl_parts)
        if rv and rv != ov:
            pairs_set.add((ov, rv))

    return sorted(pairs_set)


def _apply_same_transform(
    variant: str,
    orig_parts: list[str],
    repl_parts: list[str],
) -> str | None:
    """Given a variant of the original name, produce the equivalent for the replacement."""
    o_first = orig_parts[0]
    o_lasts = orig_parts[1:] if len(orig_parts) > 1 else []
    r_first = repl_parts[0]
    r_lasts = repl_parts[1:] if len(repl_parts) > 1 else []

    # Pad shorter name with empty strings
    max_lasts = max(len(o_lasts), len(r_lasts))
    o_lasts_padded = o_lasts + [""] * (max_lasts - len(o_lasts))
    r_lasts_padded = r_lasts + [""] * (max_lasts - len(r_lasts))

    # Try direct substitution: replace each original part with replacement part
    result = variant
    # Replace longest parts first to avoid partial matches
    replacements = [(o_first, r_first)]
    for ol, rl in zip(o_lasts_padded, r_lasts_padded):
        if ol and rl:
            replacements.append((ol, rl))

    # Sort by length descending
    replacements.sort(key=lambda x: -len(x[0]))

    for orig, repl in replacements:
        if orig in result:
            result = result.replace(orig, repl)
        elif orig[0] in result and len(orig) > 0:
            # Handle initial substitution (j → a if john → alex)
            # Only do this for single-char matches that are clearly initials
            pass

    # Handle initial-based variants
    for ol, rl in [(o_first, r_first)] + list(zip(o_lasts_padded, r_lasts_padded)):
        if not ol or not rl:
            continue
        # If the variant has the initial of the original, replace with replacement initial
        # But only in positions where it's clearly an initial (start, after separator)
        if result == variant:
            # Nothing was replaced by full parts — try initial replacement
            if variant.startswith(ol[0]) and not variant.startswith(ol[:2]):
                result = rl[0] + result[1:]

    if result == variant:
        return None  # No transformation applied
    return result


def expand_person_to_rules(
    rule_id: str,
    person: dict,
    category: str = "NAME",
) -> list[dict]:
    """Expand a person block into individual pattern rules.

    Args:
        rule_id: Base rule ID (e.g., "dev-lead")
        person: Dict with name, replacement, nicknames, usernames, emails, etc.
        category: Rule category

    Returns:
        List of rule dicts ready for _parse_rule()
    """
    rules: list[dict] = []
    name = person.get("name", "")
    replacement = person.get("replacement", "")

    if not name or not replacement:
        return rules

    # 1. Name variant pairs (auto-generated, case-insensitive)
    pairs = pair_variants(name, replacement)
    for i, (orig, repl) in enumerate(pairs):
        rules.append({
            "id": f"{rule_id}-name-{i}",
            "pattern": f"(?i){_escape_for_regex(orig)}",
            "replacement": repl,
            "category": category,
        })

    # 2. Explicit nicknames
    nicknames = person.get("nicknames", [])
    repl_nicknames = person.get("replacement_nicknames", [])
    for i, nick in enumerate(nicknames):
        repl_nick = repl_nicknames[i] if i < len(repl_nicknames) else f"Alias{i + 1}"
        rules.append({
            "id": f"{rule_id}-nick-{i}",
            "pattern": f"(?i)\\b{_escape_for_regex(nick)}\\b",
            "replacement": repl_nick,
            "category": category,
        })

    # 3. Explicit usernames
    usernames = person.get("usernames", [])
    repl_usernames = person.get("replacement_usernames", [])
    for i, uname in enumerate(usernames):
        repl_uname = repl_usernames[i] if i < len(repl_usernames) else f"user{i + 1}"
        rules.append({
            "id": f"{rule_id}-user-{i}",
            "pattern": f"(?i){_escape_for_regex(uname)}",
            "replacement": repl_uname,
            "category": category,
        })

    # 4. Explicit emails
    emails = person.get("emails", [])
    repl_emails = person.get("replacement_emails", [])
    for i, email in enumerate(emails):
        repl_email = repl_emails[i] if i < len(repl_emails) else f"user{i + 1}@example.com"
        rules.append({
            "id": f"{rule_id}-email-{i}",
            "pattern": _escape_for_regex(email),
            "replacement": repl_email,
            "category": "EMAIL",
        })

    return rules


def _escape_for_regex(s: str) -> str:
    """Escape special regex characters except for already-escaped ones."""
    import re
    return re.escape(s)
