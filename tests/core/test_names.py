"""Tests for name variant expansion."""

from rdx.core.names import expand_name, expand_person_to_rules, pair_variants


class TestExpandName:
    def test_two_part_name(self) -> None:
        variants = expand_name("John Smith")
        assert "john.smith" in variants
        assert "john_smith" in variants
        assert "john-smith" in variants
        assert "johnsmith" in variants
        assert "jsmith" in variants
        assert "j.smith" in variants
        assert "john.s" in variants
        assert "john" in variants

    def test_three_part_name(self) -> None:
        variants = expand_name("John Smith Williams")
        assert "john.smith.williams" in variants
        assert "john.smith" in variants
        assert "john.williams" in variants
        assert "jsmith" in variants
        assert "jwilliams" in variants
        assert "jsmithw" in variants
        assert "john_smith_williams" in variants

    def test_single_name(self) -> None:
        variants = expand_name("Rajesh")
        assert variants == ["rajesh"]

    def test_empty(self) -> None:
        assert expand_name("") == []
        assert expand_name("   ") == []

    def test_truncated_variants(self) -> None:
        variants = expand_name("John Smithington")
        # 8-char truncated
        truncated = [v for v in variants if len(v) <= 8]
        assert len(truncated) > 0

    def test_all_lowercase(self) -> None:
        """All generated variants should be lowercase — case is handled by (?i)."""
        variants = expand_name("John Smith")
        for v in variants:
            assert v == v.lower(), f"Variant '{v}' is not lowercase"

    def test_no_duplicates(self) -> None:
        variants = expand_name("John Smith")
        assert len(variants) == len(set(variants))


class TestPairVariants:
    def test_basic_pairing(self) -> None:
        pairs = pair_variants("John Smith", "Jane Doe")
        originals = [p[0] for p in pairs]
        replacements = [p[1] for p in pairs]

        # john.smith → jane.doe
        assert "john.smith" in originals
        idx = originals.index("john.smith")
        assert replacements[idx] == "jane.doe"

    def test_initial_pairing(self) -> None:
        pairs = pair_variants("John Smith", "Jane Doe")
        pair_dict = dict(pairs)
        # jsmith → jdoe
        if "jsmith" in pair_dict:
            assert pair_dict["jsmith"] == "jdoe"

    def test_no_self_pairs(self) -> None:
        """Original and replacement should never be the same."""
        pairs = pair_variants("John Smith", "Jane Doe")
        for orig, repl in pairs:
            assert orig != repl

    def test_three_to_three(self) -> None:
        pairs = pair_variants("John Smith Williams", "Jane Doe Miller")
        pair_dict = dict(pairs)
        assert "john.smith.williams" in pair_dict
        assert pair_dict["john.smith.williams"] == "jane.doe.miller"

    def test_different_length_names(self) -> None:
        """Should handle names with different numbers of parts."""
        pairs = pair_variants("John Smith", "Jane Doe Miller")
        assert len(pairs) > 0  # Should produce some pairs


class TestExpandPersonToRules:
    def test_basic_person(self) -> None:
        person = {"name": "John Smith", "replacement": "Jane Doe"}
        rules = expand_person_to_rules("dev", person)
        assert len(rules) > 0
        # All rules should have pattern and replacement
        for r in rules:
            assert "pattern" in r
            assert "replacement" in r
            assert r["id"].startswith("dev-")

    def test_with_nicknames(self) -> None:
        person = {
            "name": "John Smith",
            "replacement": "Jane Doe",
            "nicknames": ["Johnny", "JS"],
            "replacement_nicknames": ["JD", "JD2"],
        }
        rules = expand_person_to_rules("dev", person)
        nick_rules = [r for r in rules if "-nick-" in r["id"]]
        assert len(nick_rules) == 2
        assert nick_rules[0]["replacement"] == "JD"

    def test_with_usernames(self) -> None:
        person = {
            "name": "John Smith",
            "replacement": "Jane Doe",
            "usernames": ["jsmith01", "john.s"],
            "replacement_usernames": ["jdoe01", "jane.d"],
        }
        rules = expand_person_to_rules("dev", person)
        user_rules = [r for r in rules if "-user-" in r["id"]]
        assert len(user_rules) == 2
        assert user_rules[0]["replacement"] == "jdoe01"

    def test_with_emails(self) -> None:
        person = {
            "name": "John Smith",
            "replacement": "Jane Doe",
            "emails": ["john.smith@corp.com"],
            "replacement_emails": ["jane.doe@newcorp.com"],
        }
        rules = expand_person_to_rules("dev", person)
        email_rules = [r for r in rules if "-email-" in r["id"]]
        assert len(email_rules) == 1
        assert email_rules[0]["category"] == "EMAIL"
        assert email_rules[0]["replacement"] == "jane.doe@newcorp.com"

    def test_case_insensitive_patterns(self) -> None:
        """Name variant patterns should include (?i) for case insensitivity."""
        person = {"name": "John Smith", "replacement": "Jane Doe"}
        rules = expand_person_to_rules("dev", person)
        name_rules = [r for r in rules if "-name-" in r["id"]]
        for r in name_rules:
            assert r["pattern"].startswith("(?i)"), f"Pattern missing (?i): {r['pattern']}"

    def test_empty_person(self) -> None:
        assert expand_person_to_rules("dev", {}) == []
        assert expand_person_to_rules("dev", {"name": "John"}) == []
        assert expand_person_to_rules("dev", {"replacement": "Jane"}) == []

    def test_auto_replacement_usernames(self) -> None:
        """When replacement_usernames not provided, auto-generate from replacement name."""
        person = {
            "name": "John Smith",
            "replacement": "Jane Doe",
            "usernames": ["jsmith"],
        }
        rules = expand_person_to_rules("dev", person)
        user_rules = [r for r in rules if "-user-" in r["id"]]
        assert len(user_rules) == 1
        # Should get auto-generated fallback
        assert user_rules[0]["replacement"] == "user1"


class TestRulesIntegration:
    def test_person_rules_load_from_yaml(self, tmp_path) -> None:
        """Person blocks in YAML should expand into multiple rules."""
        from rdx.core.rules import load_rules_file

        rules_file = tmp_path / ".redaction_rules"
        rules_file.write_text("""
rules:
  - id: dev-lead
    category: NAME
    person:
      name: 'John Smith'
      replacement: 'Jane Doe'
      nicknames: ['Johnny']
      replacement_nicknames: ['JD']
      usernames: ['jsmith01']
      replacement_usernames: ['jdoe01']
      emails: ['john@corp.com']
      replacement_emails: ['jane@corp.com']
""")
        rules = load_rules_file(rules_file)
        assert len(rules) > 5  # Should expand to many rules

        # Check all rule IDs start with dev-lead
        for r in rules:
            assert r.id.startswith("dev-lead-")

        # Check we got name, nick, user, email variants
        ids = [r.id for r in rules]
        assert any("nick" in i for i in ids)
        assert any("user" in i for i in ids)
        assert any("email" in i for i in ids)
        assert any("name" in i for i in ids)

    def test_mixed_person_and_regular_rules(self, tmp_path) -> None:
        """Person and regular rules can coexist."""
        from rdx.core.rules import load_rules_file

        rules_file = tmp_path / ".redaction_rules"
        rules_file.write_text("""
rules:
  - id: dev-lead
    category: NAME
    person:
      name: 'John Smith'
      replacement: 'Jane Doe'

  - id: company
    pattern: 'CorpName'
    replacement: 'FakeCorp'
    category: PROJECT
    is_regex: false
""")
        rules = load_rules_file(rules_file)
        # Should have expanded person rules + the regular rule
        regular = [r for r in rules if r.id == "company"]
        assert len(regular) == 1
        assert regular[0].replacement == "FakeCorp"

        person = [r for r in rules if r.id.startswith("dev-lead-")]
        assert len(person) > 3
