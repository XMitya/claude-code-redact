"""Full CLI entry point for rdx.

Usage:
    rdx setup --proxy [--foreground] [--port N] [--no-unredact]
    rdx setup --hooks [--global]
    rdx setup --off
    rdx setup --show
    rdx init                        (interactive setup wizard)
    rdx hook                        (reads JSON from stdin)
    rdx rewrite COMMAND
    rdx rules edit|validate|list
    rdx secret add|list
    rdx check FILE... [--json]
    rdx cat FILE [-n]
    rdx discover [DIR]
    rdx audit [--follow] [--stats]
    rdx debug [--list] [--diff N]
    rdx <anything-else>             (catch-all: execute with output redaction)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from rdx.core.mappings import MappingCache
from rdx.core.redactor import Redactor
from rdx.core.rules import (
    add_rule,
    get_rules_path,
    load_rules,
    load_rules_file,
    save_rules_file,
    validate_rules_file,
)
from rdx.core.scanner import hash_text
from rdx.core.unredactor import Unredactor
from rdx.detect.patterns import get_builtin_rules

DEFAULT_PROXY_PORT = 8642


# ── Helpers ────────────────────────────────────────────────────────


def _project_dir() -> Path:
    return Path.cwd()


def _build_rules() -> list:
    rules = load_rules()
    rules.extend(get_builtin_rules())
    return rules


# ── proxy ──────────────────────────────────────────────────────────


def cmd_proxy_install_systemd(args: argparse.Namespace) -> int:
    """Install rdx proxy as a systemd user service."""
    port = args.port
    rdx_path = subprocess.run(
        ["which", "rdx"], capture_output=True, text=True
    ).stdout.strip() or str(Path.home() / ".local/bin/rdx")

    service_content = f"""[Unit]
Description=RDX Redaction Proxy
After=network.target

[Service]
Type=simple
ExecStart={rdx_path} proxy start --foreground --port {port}
Restart=on-failure
RestartSec=3
WorkingDirectory=%h

[Install]
WantedBy=default.target
"""
    service_dir = Path.home() / ".config/systemd/user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "rdx-proxy.service"
    service_file.write_text(service_content)

    subprocess.run(["systemctl", "--user", "daemon-reload"])
    subprocess.run(["systemctl", "--user", "enable", "rdx-proxy"])
    subprocess.run(["systemctl", "--user", "start", "rdx-proxy"])

    print(f"Installed rdx-proxy.service (port {port})")
    print(f"  Service file: {service_file}")
    print(f"  Status:  systemctl --user status rdx-proxy")
    print(f"  Logs:    journalctl --user -u rdx-proxy -f")
    print(f"  Stop:    systemctl --user stop rdx-proxy")
    print(f"  Remove:  systemctl --user disable rdx-proxy && rm {service_file}")
    return 0


# ── init ───────────────────────────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> int:
    """Run the interactive init wizard."""
    from rdx.init import run_init
    return run_init(
        project_dir=_project_dir(),
        non_interactive=args.non_interactive,
    )


# ── setup ──────────────────────────────────────────────────────────


def cmd_setup(args: argparse.Namespace) -> int:
    """Configure Claude Code integration — single entry point."""
    from rdx.setup.setup import setup_proxy, setup_hooks, setup_off, show_config

    project_dir = _project_dir()

    if getattr(args, "show", False):
        config = show_config(project_dir)
        for k, v in config.items():
            print(f"  {k:25s} {v}")
        return 0

    if getattr(args, "off", False):
        setup_off(project_dir)
        return 0

    if getattr(args, "proxy", False):
        setup_proxy(
            project_dir,
            port=getattr(args, "port", DEFAULT_PROXY_PORT),
            no_unredact=getattr(args, "no_unredact", False),
            foreground=getattr(args, "foreground", False),
            enable_logging=getattr(args, "dangerously_enable_logging", False),
            log_bodies=getattr(args, "dangerously_log_full_bodies", False),
        )
        return 0

    if getattr(args, "hooks", False):
        setup_hooks(project_dir, global_scope=getattr(args, "global_", False))
        return 0

    print("Specify --proxy, --hooks, --off, or --show")
    return 1


def _setup_hooks() -> int:
    """Called by init wizard."""
    print("Hooks setup: add to .claude/settings.json:")
    print(json.dumps({
        "hooks": {
            "PreToolUse": [{"command": "rdx hook"}],
            "PostToolUse": [{"command": "rdx hook"}],
            "UserPromptSubmit": [{"command": "rdx hook"}],
        }
    }, indent=2))
    return 0


# ── hook ───────────────────────────────────────────────────────────


def cmd_hook(args: argparse.Namespace) -> int:
    """Run as a Claude Code hook (reads JSON from stdin)."""
    from rdx.hooks.hook import run_hook
    return run_hook()


# ── rewrite ────────────────────────────────────────────────────────


def cmd_rewrite(args: argparse.Namespace) -> int:
    """Un-redact and rewrite a command for rdx proxy execution."""
    command = " ".join(args.command)
    if not command:
        return 0

    cache = MappingCache()
    unredactor = Unredactor(cache)
    from rdx.hooks.rewrite import rewrite_command
    result = rewrite_command(command, unredactor)
    print(result if result is not None else command)
    return 0


# ── rules ──────────────────────────────────────────────────────────


def cmd_rules_edit(args: argparse.Namespace) -> int:
    """Open rules file in $EDITOR, then validate."""
    path = get_rules_path(global_=args.global_)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("rules: []\n")

    editor = os.environ.get("EDITOR", "vi")
    ret = subprocess.call([editor, str(path)])
    if ret != 0:
        return ret

    errors = validate_rules_file(path)
    if errors:
        print("Validation errors:")
        for err in errors:
            print(f"  - {err}")
        return 1
    print("Rules valid.")
    return 0


def cmd_rules_validate(args: argparse.Namespace) -> int:
    """Validate the rules file."""
    path = get_rules_path(global_=args.global_)
    if not path.exists():
        print(f"No rules file at {path}")
        return 0

    errors = validate_rules_file(path)
    if errors:
        for err in errors:
            print(f"  - {err}")
        return 1
    rules = load_rules_file(path)
    print(f"Valid: {len(rules)} rule(s)")
    return 0


def cmd_rules_list(args: argparse.Namespace) -> int:
    """List all rules."""
    if args.global_:
        rules = load_rules_file(get_rules_path(global_=True))
    else:
        rules = load_rules()
    if not rules:
        print("No rules configured.")
        return 0

    for r in rules:
        action = r.action.upper()
        target = f" target={r.target}" if r.target != "both" else ""
        tool = f" tool={r.tool}" if r.tool else ""
        hashed = " [hashed]" if r.hashed else ""
        pattern = r.pattern[:40] + "..." if r.pattern and len(r.pattern) > 40 else (r.pattern or "")
        desc = f" — {r.description}" if r.description else ""
        print(f"  {r.id:30s} {action:6s} {r.category:8s} {pattern}{hashed}{target}{tool}{desc}")
    return 0


# ── secret ─────────────────────────────────────────────────────────


def cmd_secret_add(args: argparse.Namespace) -> int:
    """Add a hashed secret rule."""
    rule_id = args.id
    secret = os.environ.get("REDACT_SECRET", "")
    if not secret:
        print("Enter the secret value (will be hashed, not stored):")
        secret = sys.stdin.readline().strip()
    if not secret:
        print("No secret provided.")
        return 1

    hashed = hash_text(secret)
    add_rule(
        rule_id,
        hashed,
        action=args.action,
        category=args.category,
        description=args.description or f"Hashed secret {rule_id}",
        global_=args.global_,
    )
    # Mark the rule as hashed by reloading and patching
    path = get_rules_path(global_=args.global_)
    rules = load_rules_file(path)
    for r in rules:
        if r.id == rule_id:
            r.hashed = True
            r.hash_extractor = args.extractor
            break
    save_rules_file(path, rules)

    print(f"Secret rule '{rule_id}' added (SHA-256: {hashed[:16]}...)")
    return 0


def cmd_secret_list(args: argparse.Namespace) -> int:
    """List hashed secret rules."""
    if args.global_:
        rules = load_rules_file(get_rules_path(global_=True))
    else:
        rules = load_rules()
    hashed = [r for r in rules if r.hashed]
    if not hashed:
        print("No hashed secret rules.")
        return 0
    for r in hashed:
        print(f"  {r.id:30s} {r.category:8s} {r.description}")
    return 0


# ── check ──────────────────────────────────────────────────────────


def _line_col(text: str, offset: int) -> tuple[int, int]:
    """Convert character offset to (line, col) — both 0-based."""
    line = text.count("\n", 0, offset)
    last_nl = text.rfind("\n", 0, offset)
    col = offset if last_nl == -1 else offset - last_nl - 1
    return line, col


def cmd_check(args: argparse.Namespace) -> int:
    """Scan files or stdin for secrets."""
    rules = _build_rules()
    cache = MappingCache()
    redactor = Redactor(rules, cache)
    found = 0
    use_json = getattr(args, "json", False)
    json_matches: list[dict] = []

    def _scan(text: str, source: str) -> None:
        nonlocal found
        result = redactor.redact(text, target="both")
        if not result.matches:
            return
        found += len(result.matches)
        for m in result.matches:
            line, col = _line_col(text, m.start)
            if use_json:
                replacement = cache.get_or_create(
                    m.rule.id, m.text, m.rule.category, m.rule.replacement
                )
                json_matches.append({
                    "file": source, "line": line, "col": col,
                    "start": m.start, "end": m.end,
                    "original": m.text, "replacement": replacement,
                    "rule_id": m.rule.id, "category": m.rule.category,
                    "description": m.rule.description, "action": m.rule.action,
                })
            elif not args.quiet:
                print(f"  {source}:{line + 1}: [{m.rule.id}] {m.rule.description}")

    if args.stdin:
        _scan(sys.stdin.read(), "stdin")
    else:
        for file_path in args.files:
            p = Path(file_path)
            if not p.exists():
                print(f"  {file_path}: not found", file=sys.stderr)
                continue
            try:
                text = p.read_text()
            except (OSError, UnicodeDecodeError) as e:
                print(f"  {file_path}: {e}", file=sys.stderr)
                continue
            _scan(text, file_path)

    if use_json:
        json.dump({"matches": json_matches, "total": found}, sys.stdout, indent=2)
        print()
        return 1 if found else 0

    if found:
        print(f"\n{found} issue(s) found.")
        return 1
    if not args.quiet:
        print("No issues found.")
    return 0


# ── cat ───────────────────────────────────────────────────────────


def cmd_cat(args: argparse.Namespace) -> int:
    """Print file(s) with redactions applied — like cat but redacted."""
    rules = _build_rules()
    cache = MappingCache()
    redactor = Redactor(rules, cache)

    for file_path in args.files:
        p = Path(file_path)
        if not p.exists():
            print(f"rdx cat: {file_path}: No such file", file=sys.stderr)
            continue
        try:
            text = p.read_text()
        except (OSError, UnicodeDecodeError) as e:
            print(f"rdx cat: {file_path}: {e}", file=sys.stderr)
            continue

        result = redactor.redact(text, target="both")
        output = result.redacted_text if result.redacted_text else text

        if args.number:
            for i, line in enumerate(output.splitlines(), 1):
                print(f"{i:6d}\t{line}")
        else:
            print(output, end="")

        if result.matches and not args.quiet:
            print(f"\n# rdx: {len(result.matches)} redaction(s) applied", file=sys.stderr)

    return 0


# ── debug ──────────────────────────────────────────────────────────


def _diff_json(
    orig: object, red: object, path: str = ""
) -> list[tuple[str, str, str]]:
    """Recursively find string values that differ between two JSON structures.

    Returns list of (json_path, original_value, redacted_value).
    """
    diffs: list[tuple[str, str, str]] = []
    if isinstance(orig, dict) and isinstance(red, dict):
        for key in orig:
            if key in red:
                diffs.extend(_diff_json(orig[key], red[key], f"{path}.{key}" if path else key))
    elif isinstance(orig, list) and isinstance(red, list):
        for i, (o, r) in enumerate(zip(orig, red)):
            diffs.extend(_diff_json(o, r, f"{path}[{i}]"))
    elif isinstance(orig, str) and isinstance(red, str):
        if orig != red:
            diffs.append((path, orig, red))
    return diffs


def cmd_debug(args: argparse.Namespace) -> int:
    """Read and compare rdx debug body dumps."""
    debug_dir = Path(args.dir) if args.dir else Path.cwd() / ".claude" / "rdx_debug"
    if not debug_dir.exists():
        print(f"No debug directory at {debug_dir}")
        print("Start the proxy with --dangerously-log-full-bodies to generate debug files.")
        return 1

    files = sorted(debug_dir.glob("*.json"))
    if not files:
        print("No debug files found.")
        return 0

    # Group by request ID
    requests: dict[str, dict[str, Path]] = {}
    for f in files:
        parts = f.stem.split("_")  # HHMMSS_r0001_1_original_request
        if len(parts) >= 3:
            req_id = parts[1]  # r0001
            label = "_".join(parts[2:])  # 1_original_request
            requests.setdefault(req_id, {})[label] = f

    if args.list:
        for req_id, labels in requests.items():
            print(f"\n  {req_id}:")
            for label, path in sorted(labels.items()):
                size = path.stat().st_size
                print(f"    {label:35s} {size:>8,d} bytes")
        return 0

    if args.diff:
        # Show diff between original and redacted for a specific request
        req_id = args.diff
        if req_id not in requests:
            # Try with r prefix
            req_id = f"r{args.diff.zfill(4)}"
        if req_id not in requests:
            print(f"Request {args.diff} not found. Use --list to see available requests.")
            return 1

        labels = requests[req_id]
        orig_key = next((k for k in labels if "original" in k), None)
        red_key = next((k for k in labels if "redacted" in k and "un" not in k), None)

        if not orig_key or not red_key:
            print(f"Missing original or redacted file for {req_id}")
            return 1

        orig = json.loads(labels[orig_key].read_text())
        red = json.loads(labels[red_key].read_text())

        # Walk the JSON and find all string values that differ
        diffs = _diff_json(orig, red)
        if not diffs:
            print(f"  {req_id}: no differences (nothing was redacted)")
        else:
            print(f"  {req_id}: {len(diffs)} value(s) changed\n")
            for path, orig_val, red_val in diffs:
                print(f"  [{path}]")
                # Unescape \n and show line-by-line diff
                orig_lines = orig_val.replace("\\n", "\n").splitlines()
                red_lines = red_val.replace("\\n", "\n").splitlines()
                for i, (o, r) in enumerate(zip(orig_lines, red_lines)):
                    if o != r:
                        print(f"    L{i + 1}:")
                        print(f"      - {o[:150]}")
                        print(f"      + {r[:150]}")
                print()
        return 0

    # Default: show summary
    print(f"Debug directory: {debug_dir}")
    print(f"Requests: {len(requests)}\n")
    for req_id, labels in requests.items():
        orig_size = next((labels[k].stat().st_size for k in labels if "original" in k), 0)
        red_size = next((labels[k].stat().st_size for k in labels if "redacted" in k and "un" not in k), 0)
        has_response = any("response" in k for k in labels)
        print(f"  {req_id}: {len(labels)} files | request: {orig_size:,d}→{red_size:,d} bytes | response: {'yes' if has_response else 'no'}")
    return 0


# ── audit ──────────────────────────────────────────────────────────


def cmd_audit(args: argparse.Namespace) -> int:
    """View or manage the audit log."""
    from rdx.audit.logger import AuditLogger

    logger = AuditLogger()

    if args.clear:
        count = logger.clear()
        print(f"Cleared {count} audit entries.")
        return 0

    if getattr(args, "follow", False):
        print("Following audit log (Ctrl+C to stop)...")
        try:
            logger.follow()
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    if args.stats:
        stats = logger.get_stats()
        if stats.get("total", 0) == 0:
            print("No audit entries.")
            return 0
        for key, val in sorted(stats.items()):
            print(f"  {key:30s} {val}")
        return 0

    entries = logger.get_recent(args.tail)
    if not entries:
        print("No audit entries.")
        return 0

    for entry in entries:
        ts = entry.timestamp[:19]
        tool = f" [{entry.tool}]" if entry.tool else ""
        rules = f" rules={','.join(entry.rule_ids)}" if entry.rule_ids else ""
        count = f" count={entry.count}" if entry.count else ""
        detail = f" {entry.detail}" if entry.detail else ""
        print(f"  {ts} {entry.event:10s} {entry.direction:10s}{tool}{rules}{count}{detail}")
    return 0


# ── discover ──────────────────────────────────────────────────────


def cmd_discover(args: argparse.Namespace) -> int:
    """Scan a directory for secrets and suggest redaction rules."""
    from rdx.discover import discover, interactive_add, print_report

    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"Not a directory: {directory}", file=sys.stderr)
        return 1

    report = discover(
        directory,
        use_presidio=args.presidio,
        quiet=args.quiet,
    )
    print_report(report, quiet=args.quiet)

    if args.add and report.findings:
        return interactive_add(report, directory)

    return 1 if report.findings else 0


# ── shadow ─────────────────────────────────────────────────────────


def cmd_shadow_clean(args: argparse.Namespace) -> int:
    """Remove all shadow files."""
    from rdx.hooks.shadow import clean_shadows
    count = clean_shadows(_project_dir())
    print(f"Removed {count} shadow file(s).")
    return 0


# ── catch-all ──────────────────────────────────────────────────────


def cmd_catchall(args: argparse.Namespace) -> int:
    """Execute an arbitrary command with stdout/stderr redaction."""
    command = args.rest
    if not command:
        return 0

    rules = _build_rules()
    cache = MappingCache()
    redactor = Redactor(rules, cache)

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print(f"rdx: command not found: {command[0]}", file=sys.stderr)
        return 127

    if result.stdout:
        scan = redactor.redact(result.stdout, target="tool")
        sys.stdout.write(scan.redacted_text or result.stdout)
    if result.stderr:
        scan = redactor.redact(result.stderr, target="tool")
        sys.stderr.write(scan.redacted_text or result.stderr)

    return result.returncode


# ── Parser ─────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rdx",
        description="Redaction proxy for AI coding tools",
    )
    subparsers = parser.add_subparsers(dest="command")

    # init
    init_p = subparsers.add_parser("init", help="Interactive setup wizard")
    init_p.add_argument("--non-interactive", action="store_true", help="Read JSON config from stdin")
    init_p.set_defaults(func=cmd_init)

    # setup — single entry point for all configuration
    setup_p = subparsers.add_parser("setup", help="Configure rdx mode (proxy/hooks/off)")
    mode_group = setup_p.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--proxy", action="store_true", help="Proxy mode: intercept all API traffic")
    mode_group.add_argument("--hooks", action="store_true", help="Hooks mode: per-tool redaction")
    mode_group.add_argument("--off", action="store_true", help="Disable rdx (keep deny permissions)")
    mode_group.add_argument("--show", action="store_true", help="Show current configuration")
    # Proxy options
    setup_p.add_argument("--port", type=int, default=8642, help="Proxy port (default: 8642)")
    setup_p.add_argument("--foreground", action="store_true", help="Run proxy in foreground")
    setup_p.add_argument("--no-unredact", action="store_true", help="Chat stays redacted, Write/Edit hooks un-redact files")
    setup_p.add_argument("--dangerously-enable-logging", action="store_true", help="Enable audit logging (foreground only)")
    setup_p.add_argument("--dangerously-log-full-bodies", action="store_true", help="Dump full API bodies (foreground only)")
    # Hooks options
    setup_p.add_argument("--global", dest="global_", action="store_true", help="Configure hooks globally")
    setup_p.set_defaults(func=cmd_setup)

    # hook
    hook_p = subparsers.add_parser("hook", help="Run as Claude Code hook (stdin JSON)")
    hook_p.set_defaults(func=cmd_hook)

    # rewrite
    rewrite_p = subparsers.add_parser("rewrite", help="Rewrite command for rdx proxy")
    rewrite_p.add_argument("command", nargs=argparse.REMAINDER, help="Command to rewrite")
    rewrite_p.set_defaults(func=cmd_rewrite)

    # rules
    rules_p = subparsers.add_parser("rules", help="Manage redaction rules")
    rules_sub = rules_p.add_subparsers(dest="rules_command")

    edit_p = rules_sub.add_parser("edit", help="Edit rules in $EDITOR")
    edit_p.add_argument("--global", dest="global_", action="store_true")
    edit_p.set_defaults(func=cmd_rules_edit)

    validate_p = rules_sub.add_parser("validate", help="Validate rules file")
    validate_p.add_argument("--global", dest="global_", action="store_true")
    validate_p.set_defaults(func=cmd_rules_validate)

    list_p = rules_sub.add_parser("list", help="List all rules")
    list_p.add_argument("--global", dest="global_", action="store_true")
    list_p.set_defaults(func=cmd_rules_list)

    # secret
    secret_p = subparsers.add_parser("secret", help="Manage hashed secrets")
    secret_sub = secret_p.add_subparsers(dest="secret_command")

    add_p = secret_sub.add_parser("add", help="Add a hashed secret rule")
    add_p.add_argument("--id", required=True, help="Rule ID")
    add_p.add_argument("--action", default="redact", help="Action (default: redact)")
    add_p.add_argument("--category", default="KEY", help="Category (default: KEY)")
    add_p.add_argument("--description", default="", help="Description")
    add_p.add_argument("--extractor", default=None, help="Hash extractor regex")
    add_p.add_argument("--global", dest="global_", action="store_true")
    add_p.set_defaults(func=cmd_secret_add)

    slist_p = secret_sub.add_parser("list", help="List hashed secret rules")
    slist_p.add_argument("--global", dest="global_", action="store_true")
    slist_p.set_defaults(func=cmd_secret_list)

    # check
    check_p = subparsers.add_parser("check", help="Scan files for secrets")
    check_p.add_argument("files", nargs="*", help="Files to scan")
    check_p.add_argument("--stdin", action="store_true", help="Read from stdin")
    check_p.add_argument("-q", "--quiet", action="store_true", help="Quiet mode")
    check_p.add_argument("--json", action="store_true", help="JSON output for tooling")
    check_p.set_defaults(func=cmd_check)

    # cat
    cat_p = subparsers.add_parser("cat", help="Print file with redactions applied")
    cat_p.add_argument("files", nargs="+", help="Files to print")
    cat_p.add_argument("-n", "--number", action="store_true", help="Number output lines")
    cat_p.add_argument("-q", "--quiet", action="store_true", help="Suppress redaction count")
    cat_p.set_defaults(func=cmd_cat)

    # audit
    # debug
    debug_p = subparsers.add_parser("debug", help="Read proxy debug body dumps")
    debug_p.add_argument("--list", action="store_true", help="List all debug files")
    debug_p.add_argument("--diff", type=str, metavar="REQ_ID", help="Show diff between original and redacted (e.g., --diff 1)")
    debug_p.add_argument("--dir", type=str, help="Debug directory (default: .claude/rdx_debug/)")
    debug_p.set_defaults(func=cmd_debug)

    # audit
    audit_p = subparsers.add_parser("audit", help="View audit log")
    audit_p.add_argument("--stats", action="store_true", help="Show aggregate stats")
    audit_p.add_argument("--clear", action="store_true", help="Clear the audit log")
    audit_p.add_argument("--tail", type=int, default=50, help="Number of recent entries (default 50)")
    audit_p.add_argument("-f", "--follow", action="store_true", help="Follow log in real-time (like tail -f)")
    audit_p.set_defaults(func=cmd_audit)

    # discover
    discover_p = subparsers.add_parser("discover", help="Scan directory for secrets and suggest rules")
    discover_p.add_argument("directory", nargs="?", default=".", help="Directory to scan (default: .)")
    discover_p.add_argument("--add", action="store_true", help="Interactively add findings as rules")
    discover_p.add_argument("--presidio", action="store_true", help="Enable Presidio NLP detection")
    discover_p.add_argument("-q", "--quiet", action="store_true", help="Quiet mode")
    discover_p.set_defaults(func=cmd_discover)

    # shadow
    shadow_p = subparsers.add_parser("shadow", help="Manage shadow files")
    shadow_sub = shadow_p.add_subparsers(dest="shadow_command")

    clean_p = shadow_sub.add_parser("clean", help="Remove all shadow files")
    clean_p.set_defaults(func=cmd_shadow_clean)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()

    # Catch-all: if first arg is not a known subcommand, treat as command to run
    known = {"setup", "init", "hook", "rewrite", "rules", "secret", "check", "cat", "debug", "audit", "discover", "shadow"}
    if argv is None:
        argv = sys.argv[1:]

    if argv and argv[0] not in known and argv[0] not in ("-h", "--help"):
        ns = argparse.Namespace(rest=argv)
        return cmd_catchall(ns)

    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
