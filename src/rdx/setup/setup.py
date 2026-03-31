"""Setup commands for configuring rdx with Claude Code."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rdx.core.rules import PROJECT_RULES_FILE, load_rules
from rdx.detect.patterns import get_builtin_rules

from .rdx_md import generate_rdx_md, write_rdx_md

CLAUDE_MD_FILE = "CLAUDE.md"
RDX_MD_FILE = "RDX.md"
SETTINGS_DIR = ".claude"
SETTINGS_FILE = SETTINGS_DIR + "/settings.json"

# Marker used to detect and replace the rdx include line in CLAUDE.md.
_CLAUDE_MD_MARKER = "<!-- rdx:include -->"
_CLAUDE_MD_INCLUDE = f'{_CLAUDE_MD_MARKER}\n@import "RDX.md"\n'

DEFAULT_PROXY_PORT = 8642


def _ensure_claude_md_includes_rdx(project_dir: Path) -> bool:
    """Append the RDX.md import to CLAUDE.md if not already present.

    Returns True if the file was modified, False if already up to date.
    """
    claude_md = project_dir / CLAUDE_MD_FILE
    if claude_md.exists():
        content = claude_md.read_text()
        if _CLAUDE_MD_MARKER in content:
            return False
        # Append with a blank line separator
        if content and not content.endswith("\n"):
            content += "\n"
        content += "\n" + _CLAUDE_MD_INCLUDE
        claude_md.write_text(content)
    else:
        claude_md.write_text(_CLAUDE_MD_INCLUDE)
    return True


def _build_deny_permissions() -> list[str]:
    """Build permission deny rules for rdx security."""
    return [
        # Redaction config — contains the mapping of real→fake values
        "Read(path:.redaction_rules)",
        "Read(path:**/.redaction_rules)",
        "Bash(command:cat .redaction_rules*)",
        "Bash(command:cat */.redaction_rules*)",
        "Bash(command:grep * .redaction_rules*)",
        "Bash(command:head * .redaction_rules*)",
        "Bash(command:tail * .redaction_rules*)",
        "Bash(command:less .redaction_rules*)",
        # SSH keys and credentials
        "Read(~/.ssh/id_*)",
        "Read(~/.ssh/ed25519)",
        "Read(~/.ssh/*.pem)",
        "Read(~/.ssh/*.key)",
        "Read(~/.gnupg/**)",
        "Edit(~/.ssh/**)",
        "Edit(~/.gnupg/**)",
        # Cryptographic material
        "Read(**/*.pem)",
        "Read(**/*.key)",
        "Read(**/*.pkcs8)",
        "Read(**/*.p12)",
        "Read(**/*.pfx)",
        "Edit(**/*.pem)",
        "Edit(**/*.key)",
        "Edit(**/*.pkcs8)",
    ]


def _build_hooks_settings(project_dir: Path) -> dict[str, Any]:
    """Build the hooks + permissions section for .claude/settings.json."""
    hook_command = "rdx hook"
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Read|Write|Edit|Bash|Grep",
                    "hooks": [{"type": "command", "command": hook_command}],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": hook_command}],
                }
            ],
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": hook_command}],
                }
            ],
        },
        "permissions": {
            "deny": _build_deny_permissions(),
        },
    }


def _build_proxy_settings() -> dict[str, Any]:
    """Build permissions for proxy mode (no hooks, just deny rules)."""
    return {
        "permissions": {
            "deny": _build_deny_permissions(),
        },
    }


def _write_settings(path: Path, new_settings: dict[str, Any]) -> None:
    """Merge new_settings into the existing settings file (or create it).

    Hooks are replaced entirely. Permission deny lists are merged
    (new entries added, existing entries preserved).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text())
    else:
        existing = {}

    # Merge permissions.deny (append, don't overwrite)
    if "permissions" in new_settings:
        existing.setdefault("permissions", {})
        new_deny = new_settings["permissions"].get("deny", [])
        existing_deny = existing["permissions"].get("deny", [])
        merged_deny = list(dict.fromkeys(existing_deny + new_deny))  # dedup, preserve order
        existing["permissions"]["deny"] = merged_deny
        # Remove permissions from new_settings so update() doesn't overwrite
        new_settings = {k: v for k, v in new_settings.items() if k != "permissions"}

    existing.update(new_settings)
    path.write_text(json.dumps(existing, indent=2) + "\n")


def setup_proxy(project_dir: Path, port: int = DEFAULT_PROXY_PORT) -> dict[str, Any]:
    """Set up rdx in proxy mode.

    - Writes deny permissions for .redaction_rules
    - Generates RDX.md
    - Appends import to CLAUDE.md

    Returns a summary dict.
    """
    settings_path = project_dir / SETTINGS_FILE
    proxy_settings = _build_proxy_settings()
    _write_settings(settings_path, proxy_settings)

    rdx_md_path = write_rdx_md(project_dir, mode="proxy")
    claude_md_modified = _ensure_claude_md_includes_rdx(project_dir)

    return {
        "mode": "proxy",
        "port": port,
        "settings_path": str(settings_path),
        "rdx_md": str(rdx_md_path),
        "claude_md_modified": claude_md_modified,
    }


def setup_hooks(
    project_dir: Path,
    global_scope: bool = False,
) -> dict[str, Any]:
    """Set up rdx in hooks mode.

    - Writes hooks config to .claude/settings.json
    - Generates RDX.md
    - Appends import to CLAUDE.md

    Returns a summary dict.
    """
    # Write hooks settings
    if global_scope:
        settings_path = Path.home() / ".claude" / "settings.json"
    else:
        settings_path = project_dir / SETTINGS_FILE

    hooks_settings = _build_hooks_settings(project_dir)
    _write_settings(settings_path, hooks_settings)

    # Generate RDX.md
    rdx_md_path = write_rdx_md(project_dir, mode="hooks")
    claude_md_modified = _ensure_claude_md_includes_rdx(project_dir)

    return {
        "mode": "hooks",
        "global_scope": global_scope,
        "settings_path": str(settings_path),
        "rdx_md": str(rdx_md_path),
        "claude_md_modified": claude_md_modified,
    }


def show_config(project_dir: Path) -> dict[str, Any]:
    """Display current rdx configuration for the project.

    Returns a dict describing what is configured.
    """
    config: dict[str, Any] = {
        "project_dir": str(project_dir),
        "rules_file": str(project_dir / PROJECT_RULES_FILE),
        "rules_file_exists": (project_dir / PROJECT_RULES_FILE).exists(),
        "rdx_md_exists": (project_dir / RDX_MD_FILE).exists(),
        "claude_md_exists": (project_dir / CLAUDE_MD_FILE).exists(),
        "claude_md_has_rdx_import": False,
        "hooks_configured": False,
        "settings_path": str(project_dir / SETTINGS_FILE),
    }

    # Check CLAUDE.md for rdx import
    claude_md = project_dir / CLAUDE_MD_FILE
    if claude_md.exists():
        content = claude_md.read_text()
        config["claude_md_has_rdx_import"] = _CLAUDE_MD_MARKER in content

    # Check hooks settings
    settings_path = project_dir / SETTINGS_FILE
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
            config["hooks_configured"] = "hooks" in settings
        except json.JSONDecodeError:
            config["hooks_configured"] = False

    # Rule counts
    user_rules = load_rules(project_dir)
    builtin_rules = get_builtin_rules()
    config["user_rule_count"] = len(user_rules)
    config["builtin_rule_count"] = len(builtin_rules)

    # Categories
    all_rules = list(user_rules)
    seen_ids = {r.id for r in user_rules}
    for r in builtin_rules:
        if r.id not in seen_ids:
            all_rules.append(r)
    categories = sorted({r.category for r in all_rules})
    config["active_categories"] = categories

    return config
