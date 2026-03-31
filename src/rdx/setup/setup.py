"""Setup commands for configuring rdx with Claude Code.

``rdx setup`` is the single entry point for all configuration.
Each mode cleans up the previous mode before applying the new one.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from rdx.core.rules import PROJECT_RULES_FILE, load_rules
from rdx.detect.patterns import get_builtin_rules

from .rdx_md import write_rdx_md

CLAUDE_MD_FILE = "CLAUDE.md"
RDX_MD_FILE = "RDX.md"
SETTINGS_DIR = ".claude"
SETTINGS_FILE = SETTINGS_DIR + "/settings.json"
PID_FILE = SETTINGS_DIR + "/rdx_proxy.pid"

_CLAUDE_MD_MARKER = "<!-- rdx:include -->"
_CLAUDE_MD_INCLUDE = f'{_CLAUDE_MD_MARKER}\n@import "RDX.md"\n'

DEFAULT_PROXY_PORT = 8642


# ── Deny permissions (shared by all modes) ────────────────────────


def _build_deny_permissions() -> list[str]:
    """Permission deny rules for rdx security."""
    return [
        "Read(path:.redaction_rules)",
        "Read(path:**/.redaction_rules)",
        "Bash(command:cat .redaction_rules*)",
        "Bash(command:cat */.redaction_rules*)",
        "Bash(command:grep * .redaction_rules*)",
        "Bash(command:head * .redaction_rules*)",
        "Bash(command:tail * .redaction_rules*)",
        "Bash(command:less .redaction_rules*)",
        "Read(~/.ssh/id_*)",
        "Read(~/.ssh/ed25519)",
        "Read(~/.ssh/*.pem)",
        "Read(~/.ssh/*.key)",
        "Read(~/.gnupg/**)",
        "Edit(~/.ssh/**)",
        "Edit(~/.gnupg/**)",
        "Read(**/*.pem)",
        "Read(**/*.key)",
        "Read(**/*.pkcs8)",
        "Read(**/*.p12)",
        "Read(**/*.pfx)",
        "Edit(**/*.pem)",
        "Edit(**/*.key)",
        "Edit(**/*.pkcs8)",
    ]


# ── Hooks config builders ─────────────────────────────────────────


def _build_full_hooks() -> dict[str, list]:
    """All hooks for hooks-only mode."""
    cmd = "rdx hook"
    return {
        "PreToolUse": [
            {"matcher": "Read|Write|Edit|Bash|Grep",
             "hooks": [{"type": "command", "command": cmd}]},
        ],
        "PostToolUse": [
            {"matcher": "Bash",
             "hooks": [{"type": "command", "command": cmd}]},
        ],
        "UserPromptSubmit": [
            {"matcher": "",
             "hooks": [{"type": "command", "command": cmd}]},
        ],
    }


def _build_write_only_hooks() -> dict[str, list]:
    """Minimal hooks for proxy --no-unredact mode (un-redact writes only)."""
    cmd = "rdx hook"
    return {
        "PreToolUse": [
            {"matcher": "Write|Edit",
             "hooks": [{"type": "command", "command": cmd}]},
        ],
    }


# ── Settings file management ──────────────────────────────────────


def _read_settings(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _write_settings(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")


def _remove_rdx_hooks(settings: dict[str, Any]) -> None:
    """Remove all rdx hooks from settings, preserving non-rdx hooks."""
    hooks = settings.get("hooks", {})
    for event in list(hooks.keys()):
        entries = hooks[event]
        if isinstance(entries, list):
            hooks[event] = [
                e for e in entries
                if not _is_rdx_hook(e)
            ]
            if not hooks[event]:
                del hooks[event]
    if not hooks:
        settings.pop("hooks", None)


def _is_rdx_hook(entry: dict) -> bool:
    """Check if a hook entry belongs to rdx."""
    hook_list = entry.get("hooks", [])
    for h in hook_list:
        cmd = h.get("command", "")
        if "rdx hook" in cmd or "rdx-hook" in cmd:
            return True
    return False


def _merge_deny_permissions(settings: dict[str, Any]) -> None:
    """Add rdx deny permissions, preserving existing ones."""
    settings.setdefault("permissions", {})
    existing = settings["permissions"].get("deny", [])
    new = _build_deny_permissions()
    merged = list(dict.fromkeys(existing + new))
    settings["permissions"]["deny"] = merged


def _add_hooks(settings: dict[str, Any], hooks: dict[str, list]) -> None:
    """Add hooks to settings, preserving non-rdx hooks."""
    settings.setdefault("hooks", {})
    for event, entries in hooks.items():
        existing = settings["hooks"].get(event, [])
        # Remove old rdx hooks first
        existing = [e for e in existing if not _is_rdx_hook(e)]
        existing.extend(entries)
        settings["hooks"][event] = existing


# ── CLAUDE.md management ──────────────────────────────────────────


def _ensure_claude_md_includes_rdx(project_dir: Path) -> bool:
    claude_md = project_dir / CLAUDE_MD_FILE
    if claude_md.exists():
        content = claude_md.read_text()
        if _CLAUDE_MD_MARKER in content:
            return False
        if content and not content.endswith("\n"):
            content += "\n"
        content += "\n" + _CLAUDE_MD_INCLUDE
        claude_md.write_text(content)
    else:
        claude_md.write_text(_CLAUDE_MD_INCLUDE)
    return True


# ── Proxy process management ──────────────────────────────────────


def _pid_path(project_dir: Path) -> Path:
    return project_dir / PID_FILE


def _stop_proxy(project_dir: Path) -> bool:
    """Stop proxy if running. Returns True if it was running."""
    pid_path = _pid_path(project_dir)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        pid_path.unlink(missing_ok=True)
        return True
    except (OSError, ValueError):
        pid_path.unlink(missing_ok=True)
        return False


def _start_proxy_background(project_dir: Path, port: int) -> int:
    """Start proxy in background. Returns pid."""
    pid_path = _pid_path(project_dir)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "rdx.proxy.server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_path.write_text(str(proc.pid))
    return proc.pid


def _start_proxy_foreground(
    project_dir: Path,
    port: int,
    *,
    no_unredact: bool = False,
    enable_logging: bool = False,
    log_bodies: bool = False,
) -> None:
    """Start proxy in foreground (blocks). Used for debug and systemd."""
    import uvicorn
    from rdx.proxy.server import enable_audit, enable_body_logging, disable_unredact

    if enable_logging:
        enable_audit()
    if log_bodies:
        enable_body_logging()
    if no_unredact:
        disable_unredact()

    pid_path = _pid_path(project_dir)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))
    try:
        uvicorn.run("rdx.proxy.server:app", host="127.0.0.1", port=port, log_level="warning")
    finally:
        pid_path.unlink(missing_ok=True)


def _proxy_status(project_dir: Path) -> tuple[bool, int | None]:
    """Check if proxy is running. Returns (running, pid)."""
    pid_path = _pid_path(project_dir)
    if not pid_path.exists():
        return False, None
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return True, pid
    except (OSError, ValueError):
        pid_path.unlink(missing_ok=True)
        return False, None


# ── Public API ────────────────────────────────────────────────────


def setup_proxy(
    project_dir: Path,
    port: int = DEFAULT_PROXY_PORT,
    *,
    no_unredact: bool = False,
    foreground: bool = False,
    enable_logging: bool = False,
    log_bodies: bool = False,
) -> dict[str, Any]:
    """Set up proxy mode. Cleans up hooks mode first.

    1. Remove all rdx hooks from settings
    2. Add deny permissions
    3. If --no-unredact: add Write/Edit-only hooks
    4. Generate RDX.md + CLAUDE.md
    5. Start proxy
    """
    settings_path = project_dir / SETTINGS_FILE
    settings = _read_settings(settings_path)

    # Clean up: remove all rdx hooks
    _remove_rdx_hooks(settings)

    # Add deny permissions
    _merge_deny_permissions(settings)

    # If no-unredact, add Write/Edit-only hooks
    if no_unredact:
        _add_hooks(settings, _build_write_only_hooks())

    _write_settings(settings_path, settings)

    # Generate RDX.md
    mode = "proxy-no-unredact" if no_unredact else "proxy"
    rdx_md_path = write_rdx_md(project_dir, mode=mode)
    _ensure_claude_md_includes_rdx(project_dir)

    # Stop any running proxy
    _stop_proxy(project_dir)

    result: dict[str, Any] = {
        "mode": mode,
        "port": port,
        "no_unredact": no_unredact,
        "settings_path": str(settings_path),
    }

    if foreground:
        print(f"Proxy starting on http://127.0.0.1:{port}", file=sys.stderr)
        if no_unredact:
            print("--no-unredact: chat stays redacted. Write/Edit hooks un-redact files.", file=sys.stderr)
        print(f"Use: ANTHROPIC_BASE_URL=http://localhost:{port} claude", file=sys.stderr)
        _start_proxy_foreground(
            project_dir, port,
            no_unredact=no_unredact,
            enable_logging=enable_logging,
            log_bodies=log_bodies,
        )
    else:
        if enable_logging or log_bodies:
            print("Logging flags require --foreground.", file=sys.stderr)
            return {**result, "error": "logging requires --foreground"}
        pid = _start_proxy_background(project_dir, port)
        result["pid"] = pid
        print(f"Proxy started on http://127.0.0.1:{port} (pid {pid})")
        print(f"Use: ANTHROPIC_BASE_URL=http://localhost:{port} claude")

    return result


def setup_hooks(
    project_dir: Path,
    global_scope: bool = False,
) -> dict[str, Any]:
    """Set up hooks mode. Cleans up proxy mode first.

    1. Stop proxy if running
    2. Remove old rdx hooks
    3. Add full hooks + deny permissions
    4. Generate RDX.md + CLAUDE.md
    """
    # Stop proxy
    was_running = _stop_proxy(project_dir)

    settings_path = (
        Path.home() / ".claude" / "settings.json"
        if global_scope
        else project_dir / SETTINGS_FILE
    )
    settings = _read_settings(settings_path)

    # Clean up old rdx hooks, then add new ones
    _remove_rdx_hooks(settings)
    _add_hooks(settings, _build_full_hooks())
    _merge_deny_permissions(settings)

    _write_settings(settings_path, settings)

    rdx_md_path = write_rdx_md(project_dir, mode="hooks")
    _ensure_claude_md_includes_rdx(project_dir)

    result: dict[str, Any] = {
        "mode": "hooks",
        "global_scope": global_scope,
        "settings_path": str(settings_path),
        "proxy_was_stopped": was_running,
    }
    if was_running:
        print("Stopped running proxy.")
    print(f"Hooks configured in {settings_path}")
    return result


def setup_off(project_dir: Path) -> dict[str, Any]:
    """Remove all rdx configuration.

    1. Stop proxy
    2. Remove rdx hooks
    3. Keep deny permissions (they're always useful)
    """
    was_running = _stop_proxy(project_dir)

    settings_path = project_dir / SETTINGS_FILE
    settings = _read_settings(settings_path)
    _remove_rdx_hooks(settings)
    _write_settings(settings_path, settings)

    result: dict[str, Any] = {
        "mode": "off",
        "proxy_was_stopped": was_running,
    }
    if was_running:
        print("Stopped proxy.")
    print("rdx disabled. Deny permissions kept.")
    return result


def show_config(project_dir: Path) -> dict[str, Any]:
    """Display current rdx configuration."""
    running, pid = _proxy_status(project_dir)

    settings_path = project_dir / SETTINGS_FILE
    settings = _read_settings(settings_path)

    has_full_hooks = False
    has_write_hooks = False
    hooks = settings.get("hooks", {})
    for event, entries in hooks.items():
        for e in entries:
            if _is_rdx_hook(e):
                matcher = e.get("matcher", "")
                if "Read" in matcher or "Bash" in matcher:
                    has_full_hooks = True
                elif "Write" in matcher:
                    has_write_hooks = True

    if running:
        if has_write_hooks:
            mode = "proxy --no-unredact"
        else:
            mode = "proxy"
    elif has_full_hooks:
        mode = "hooks"
    else:
        mode = "off"

    config: dict[str, Any] = {
        "mode": mode,
        "proxy_running": running,
        "proxy_pid": pid,
        "rules_file": str(project_dir / PROJECT_RULES_FILE),
        "rules_exist": (project_dir / PROJECT_RULES_FILE).exists(),
        "rdx_md_exists": (project_dir / RDX_MD_FILE).exists(),
    }

    user_rules = load_rules(project_dir)
    config["user_rules"] = len(user_rules)
    config["builtin_rules"] = len(get_builtin_rules())

    return config
