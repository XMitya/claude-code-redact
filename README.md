# claude-code-redact

Redaction proxy for AI coding tools — prevent secrets, PII, and NDA material from leaving your machine.

**CLI command:** `rdx`

## The Problem

When using Claude Code, OpenCode, or similar AI coding tools, **everything** is sent to external API servers: file contents, command output, user prompts. This includes API keys, company names under NDA, employee PII, and proprietary code.

Additionally, [prompt injection attacks](https://github.com/gricha/dangerous-skills) can trick the AI into exfiltrating secrets to attacker-controlled servers — with success rates up to 100%.

## How It Works

`rdx` sits between your AI tool and the LLM API. It redacts sensitive data before it leaves your machine, and un-redacts the response so your local tools work normally.

```
                        rdx
                         │
You ← real values ← [un-redact] ← Claude (sees only redacted tokens)
You → real values → [ redact  ] → Anthropic API (receives only redacted tokens)
                         │
                    Your machine
                  (secrets stay here)
```

### Two Replacement Strategies

**Format-preserving** (user-defined): real names → fake names — Claude reasons naturally about the value.

**Auto-token** (discovered PII/secrets): `sk-secret123` → `__RDX_KEY_a1b2c3d4__` — Claude treats it as an opaque placeholder.

### Person Blocks

Define a person once, rdx auto-generates ~30 corporate name variants (dot-separated, underscore, camelCase, initials, truncated) with case-insensitive matching:

```yaml
rules:
  - id: dev-lead
    category: NAME
    person:
      name: 'John Smith Williams'
      replacement: 'Jane Doe Miller'
      nicknames: ['Johnny']
      replacement_nicknames: ['JD']
      usernames: ['jsmith01', 'j.smith']
      replacement_usernames: ['jdoe01', 'j.doe']
      emails: ['john.smith@corp.com']
      replacement_emails: ['jane.doe@newcorp.com']
```

This catches `John Smith Williams`, `JOHN_SMITH`, `jsmith`, `j.smith`, `johnsmithwilliams`, etc. Case is preserved: `JOHN` → `JANE`, `john` → `jane`, `John` → `Jane`.

## Quick Start

```bash
# Install
uv tool install claude-code-redact

# Interactive setup
rdx init

# Or define rules manually
cat > .redaction_rules << 'EOF'
rules:
  - id: dev-lead
    category: NAME
    person:
      name: 'John Smith'
      replacement: 'Jane Doe'
      usernames: ['jsmith']
      replacement_usernames: ['jdoe']

  - id: company
    pattern: '(?i)CorpName'
    replacement: 'FakeCorp'
    category: PROJECT
EOF

# Start the proxy
rdx proxy start --foreground --port 8642

# In another terminal, run Claude Code
ANTHROPIC_BASE_URL=http://localhost:8642 claude
```

## Multi-Project Support

The proxy auto-detects which project each request belongs to by reading the working directory from Claude Code's system prompt. Each project uses its own `.redaction_rules` and its own mapping cache. Projects without rules pass through untouched.

Run one proxy, use it across all your projects:

```bash
rdx proxy start --foreground --port 8642
# All Claude Code sessions using ANTHROPIC_BASE_URL=http://localhost:8642
# automatically get the right rules for their project
```

## Operation Modes

### Proxy Mode (Recommended)

Intercepts all API traffic via `ANTHROPIC_BASE_URL`. Zero coverage gaps.

```bash
rdx proxy start --foreground --port 8642
rdx proxy start                          # Background mode
rdx proxy stop
rdx proxy status
rdx proxy install                        # Install as systemd user service
```

### Proxy + No Un-redact (Awareness Mode)

Chat stays redacted so you see what Claude sees. Writes are un-redacted via hooks so files stay correct:

```bash
rdx proxy start --foreground --port 8642 --no-unredact
rdx setup --hooks   # Required: hooks un-redact Write/Edit content
```

### Hooks Mode (Lightweight)

Claude Code hooks for per-tool redaction. No daemon needed, but can't modify Read/Grep output.

```bash
rdx setup --hooks
```

## Detection Layers

1. **Explicit rules** — Your `.redaction_rules` file with patterns and replacements
2. **Person blocks** — Auto-expand names into ~30 corporate variants with case preservation
3. **Built-in patterns** — 16 regex rules for AWS keys, GitHub tokens, OpenAI keys, JWTs, etc.
4. **Entropy detection** — Flags high-entropy strings (likely random secrets)
5. **Context detection** — Finds secrets by surroundings (`password=`, `api_key:`, `Bearer`, etc.)
6. **NLP discovery** *(optional)* — Microsoft Presidio catches PII you didn't list (names, emails, phone numbers)

```bash
# Install with NLP support
uv tool install "claude-code-redact[nlp]"
```

## Commands

```bash
# Setup
rdx init                         # Interactive setup wizard
rdx setup --proxy                # Configure proxy mode
rdx setup --hooks                # Configure hooks mode

# Proxy
rdx proxy start --foreground     # Start (foreground, required for debug flags)
rdx proxy start                  # Start (background)
rdx proxy stop / status          # Manage proxy
rdx proxy install                # Install as systemd service

# Rules
rdx rules edit                   # Edit rules in $EDITOR
rdx rules validate               # Check syntax
rdx rules list                   # Show all active rules (incl. expanded person blocks)

# Scanning
rdx check FILE...                # Scan files for detectable secrets
rdx check --json FILE...         # JSON output for tooling (VS Code extension)
rdx cat FILE                     # Print file with redactions applied
rdx cat -n FILE                  # With line numbers
rdx discover [DIR]               # Scan project, suggest rules for found secrets

# Secrets
rdx secret add --id NAME         # Add hashed secret (reads from stdin)
rdx secret list                  # List hashed secrets

# Debugging (foreground only)
rdx proxy start --foreground --dangerously-enable-logging    # Audit log
rdx proxy start --foreground --dangerously-log-full-bodies   # Dump full API bodies
rdx audit                        # View audit log
rdx audit --follow               # Tail audit log in real-time
rdx debug                        # Summarize debug body dumps
rdx debug --diff N               # Diff original vs redacted for request #N
```

## Configuration

### `.redaction_rules` format

```yaml
rules:
  # Person block: auto-expands into ~30 variant rules
  - id: developer
    category: NAME
    person:
      name: 'John Smith'
      replacement: 'Jane Doe'
      usernames: ['jsmith']
      replacement_usernames: ['jdoe']
      emails: ['john@corp.com']
      replacement_emails: ['jane@newcorp.com']

  # Simple pattern: case-insensitive with (?i)
  - id: company
    pattern: '(?i)CorpName'
    replacement: 'FakeCorp'
    category: PROJECT

  # Auto-token: no replacement → __RDX_KEY_<hash>__
  - id: api-tokens
    pattern: 'corp-tk-[a-zA-Z0-9]{20,}'
    category: KEY

  # Hashed secret: detects without storing plaintext
  - id: secret-project
    hashed: true
    pattern: 'sha256-hash-here'
    hash_extractor: '\b[A-Z][a-zA-Z]+\b'
    category: PROJECT

  # Block dangerous commands (hooks mode)
  - id: block-no-verify
    pattern: '--no-verify'
    action: block
    tool: Bash
```

### Rule fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `id` | Yes | — | Unique identifier |
| `pattern` | Yes* | — | Regex or literal string. Use `(?i)` for case-insensitive |
| `person` | — | — | Person block (auto-expands, replaces `pattern`) |
| `replacement` | No | auto-token | Format-preserving value, or omit for `__RDX_*__` |
| `category` | No | `CUSTOM` | `NAME`, `EMAIL`, `KEY`, `IP`, `HOST`, `PROJECT`, `PATH`, `CUSTOM` |
| `action` | No | `redact` | `redact`, `block`, or `warn` |
| `is_regex` | No | `true` | Set `false` for literal string matching |
| `target` | No | `both` | `llm`, `tool`, or `both` |
| `tool` | No | all | Restrict to a specific tool (`Bash`, `Read`, etc.) |

## Security Model

- **Zero logging by default.** Audit and body logging require explicit `--dangerously-*` flags in foreground mode only. No env vars.
- **No mapping file on disk.** The reverse map exists only in proxy process memory. Nothing to steal.
- **Per-project isolation.** Different projects get separate mapping caches. No cross-contamination.
- **Deterministic tokens.** Same input always produces the same token (SHA-256). Claude's memory stays coherent across sessions.
- **Case preservation.** `JOHN` → `JANE`, `john` → `jane`, `John` → `Jane` — no accidental case leaks.
- **Defense against exfiltration.** Even if a prompt injection succeeds, exfiltrated data is redacted tokens.

## How Claude Knows About Redaction

`rdx setup` generates an `RDX.md` file appended to your project's `CLAUDE.md`. This tells Claude that redaction is active, shows examples of what redacted values look like, and instructs it to treat them as opaque identifiers.

## VS Code Extension

The `vscode-extension/` directory contains an extension that shows redacted values inline:

- Red highlights on secrets with faded text showing what Claude sees
- Toggle Claude's View (`Ctrl+Shift+R`) for side-by-side comparison
- Hover cards with rule details
- Status bar with redaction count
- Problems panel integration

```bash
cd vscode-extension && npm install && npm run compile
# Then install via VS Code: Developer → Install Extension from Location
```

Requires `rdx` in PATH (`uv tool install claude-code-redact`).

## Status

Core engine, proxy server, hooks mode, CLI, audit log, RDX.md generation, init wizard, discover command, VS Code extension, person block expansion, case preservation, multi-project support, and debug tooling are implemented with 487+ tests passing.

## Acknowledgments

Inspired by [claude-code-redaction-hooks](https://github.com/l-mb/claude-code-redaction-hooks) by Lars Marowsky-Bree — a Claude Code hook-based approach that identified the core limitations in the hook API. `claude-code-redact` extends that work with an API proxy architecture, format-preserving replacements, multi-layer detection (including Microsoft Presidio NLP), person block expansion, and round-trip un-redaction.

## License

Apache-2.0
