"""Set up MS-DIAL Interactive for Claude Code and Claude Desktop, one explained step at a time.

    python scripts/setup_claude.py              explain each step and ask before changing anything
    python scripts/setup_claude.py --dry-run    show the plan; change nothing
    python scripts/setup_claude.py --yes        answer yes to every question

On Windows, double-click "Setup MS-DIAL for Claude.cmd" in the folder above this one.

The steps:
  1. Python: 3.10 or newer.
  2. The MCP dependency: "<this python> -m pip install -e .[mcp]" from this checkout, unless the
     mcp and psutil packages are already there.
  3. Claude Code: register the msdial-interactive MCP server at user scope with the claude CLI. An
     existing registration is shown, and replaced only on your yes.
  4. Claude Desktop: merge an msdial-interactive entry into claude_desktop_config.json's
     mcpServers, after a timestamped backup; every other key is left as it is.
  5. Skills: copy every skill under skills/ into Claude Code's skills folder (~/.claude/skills).
     A differing copy already there is backed up first, outside that folder.
  6. Skill ZIPs for Claude Desktop: dist/<name>.skill.zip, to upload under Customize > Skills.
  7. MS-DIAL Console: find one and say whether it is new enough for laboratory lipidomics.
  8. A summary and a first prompt to try.

Only the standard library is used, nothing needs administrator rights, and nothing is uploaded.
Read-only queries (reading configuration files, the Console's own --help) also run in a dry run;
nothing is installed, registered, copied or written. Values of environment variables in any
configuration are never printed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import filecmp
import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent.parent
MCP_SCRIPT = ROOT / "scripts" / "msdial-interactive-mcp.py"
PACKAGER = ROOT / "scripts" / "package-agent-skill.py"
SKILLS = ROOT / "skills"
DIST = ROOT / "dist"
TUTORIAL = ROOT / "docs" / "tutorials" / "lab-lipidomics.md"
SERVER_NAME = "msdial-interactive"
MINIMUM_PYTHON = (3, 10)
# What pyproject.toml's [mcp] extra asks for: mcp[cli]>=1.28,<3 and psutil>=6,<8.
DEPENDENCIES = {"mcp": ((1, 28), (3,)), "psutil": ((6,), (8,))}
CONSOLE_RELEASES_URL = "https://github.com/systemsomicslab/MsdialWorkbench/releases"
MSDIAL_SOURCE_URL = "https://github.com/systemsomicslab/MsdialWorkbench"
# PLACEHOLDER: the official Console prerelease with MsdialWorkbench#826 and #830 is not published yet.
# Replace both with the release tag once it exists, and say so in docs/tutorials/lab-lipidomics.md.
CONSOLE_PRERELEASE_TAG = ""
CONSOLE_PRERELEASE_LABEL = "the MS-DIAL Console prerelease 5.5.2610xx or later"
NORMALIZE_OPTION = "--peak-ids-only"
FIRST_PROMPT = (
    "Check that MS-DIAL Interactive is ready for laboratory lipidomics: start its backend, list the "
    "MS-DIAL Consoles you can find and say whether one is new enough. Do not run any analysis yet."
)


# ---------------------------------------------------------------------------------------------
# What the steps read and how they talk
# ---------------------------------------------------------------------------------------------


@dataclass
class Context:
    """Everything a step reads from the computer, so that a test can give it another one."""

    root: Path = ROOT
    python: str = sys.executable
    home: Path = field(default_factory=Path.home)
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    system: str = field(default_factory=platform.system)
    dry_run: bool = False
    assume_yes: bool = False
    desktop: bool | None = None  # True: configure even without a config file; False: skip.
    claude_code: bool = True
    install: bool = True
    console_paths: list[str] = field(default_factory=list)
    # Drive roots searched for an MSDIAL* Console folder; None: C:\ and D:\ on Windows, none elsewhere.
    drive_roots: list[Path] | None = None
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    which: Callable[[str], str | None] = shutil.which
    ask_input: Callable[[str], str] = input
    interactive: bool = field(default_factory=lambda: bool(getattr(sys.stdin, "isatty", lambda: False)()))
    out: Callable[[str], None] = print
    now: Callable[[], dt.datetime] = dt.datetime.now

    @property
    def windows(self) -> bool:
        return self.system == "Windows"

    def say(self, text: str = "") -> None:
        self.out(text)

    def ask(self, question: str, default: bool) -> bool:
        """A yes or no from the person; --yes answers yes, and a dry run asks nothing."""
        hint = "[Y/n]" if default else "[y/N]"
        if self.dry_run:
            self.say(f"  (dry run) would ask: {question} {hint}")
            return False
        if self.assume_yes:
            self.say(f"  {question} {hint} yes (--yes)")
            return True
        if not self.interactive:
            self.say(f"  {question} {hint} no (no terminal to answer in; run again with --yes to accept)")
            return False
        while True:
            try:
                answer = self.ask_input(f"  {question} {hint} ").strip().casefold()
            except EOFError:
                return False
            if not answer:
                return default
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self.say("  Please answer y or n.")

    def command_text(self, command: list[str]) -> str:
        if self.windows:
            return subprocess.list2cmdline(command)
        return " ".join(_shell_quote(part) for part in command)

    def timestamp(self) -> str:
        return self.now().strftime("%Y%m%d-%H%M%S")


def _shell_quote(text: str) -> str:
    if text and all(character.isalnum() or character in "@%+=:,./-_[]" for character in text):
        return text
    return "'" + text.replace("'", "'\"'\"'") + "'"


@dataclass
class StepResult:
    step: str
    status: str  # ok, done, unchanged, skipped, planned, failed, attention
    detail: str = ""


def _same_path(left: str, right: str, windows: bool) -> bool:
    if windows:
        return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))
    return os.path.normpath(left) == os.path.normpath(right)


def _server_args(ctx: Context) -> list[str]:
    return [str(ctx.root / "scripts" / MCP_SCRIPT.name)]


def _describe_entry(entry: dict[str, Any]) -> list[str]:
    """An MCP server entry as lines to show: command and args, environment variable names only."""
    lines = [f"    command: {entry.get('command', '')}"]
    arguments = entry.get("args") or []
    lines.append(f"    args:    {' '.join(str(item) for item in arguments) if arguments else '(none)'}")
    env = entry.get("env")
    if isinstance(env, dict) and env:
        lines.append(f"    env:     {', '.join(sorted(env))} (values not shown)")
    for key in sorted(set(entry) - {"command", "args", "env", "type"}):
        lines.append(f"    {key}: (set)")
    return lines


def _entry_matches(entry: Any, command: str, arguments: list[str], windows: bool) -> bool:
    if not isinstance(entry, dict):
        return False
    existing = [str(item) for item in entry.get("args") or []]
    return (
        _same_path(str(entry.get("command", "")), command, windows)
        and len(existing) == len(arguments)
        and all(_same_path(left, right, windows) for left, right in zip(existing, arguments))
    )


# ---------------------------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------------------------


def check_python(ctx: Context, version: tuple[int, ...] | None = None) -> StepResult:
    version = tuple(version or sys.version_info[:3])
    text = ".".join(str(part) for part in version)
    ctx.say(f"Python {text} at {ctx.python}.")
    if version[:2] < MINIMUM_PYTHON:
        needed = ".".join(str(part) for part in MINIMUM_PYTHON)
        ctx.say(f"  MS-DIAL Interactive needs Python {needed} or newer. Install it from https://www.python.org/downloads/")
        ctx.say("  (on Windows, the python.org installer; no administrator rights are needed for a per-user install),")
        ctx.say("  then run this setup again with that Python.")
        return StepResult("Python", "failed", f"{text} is older than {needed}")
    return StepResult("Python", "ok", text)


# ---------------------------------------------------------------------------------------------
# 2. The MCP dependency
# ---------------------------------------------------------------------------------------------


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in text.split("."):
        digits = "".join(character for character in piece if character.isdigit())
        if not digits:
            break
        parts.append(int(digits))
        if not piece.isdigit():
            break
    return tuple(parts)


def dependency_status(version: Callable[[str], str] = importlib.metadata.version) -> dict[str, dict[str, Any]]:
    """Each MCP dependency: the installed version, and whether it is in the range pyproject.toml asks."""
    status: dict[str, dict[str, Any]] = {}
    for name, (lowest, below) in DEPENDENCIES.items():
        try:
            installed = version(name)
        except importlib.metadata.PackageNotFoundError:
            status[name] = {"installed": None, "ok": False}
            continue
        number = _version_tuple(installed)
        status[name] = {"installed": installed, "ok": lowest <= number < below}
    return status


def _site_packages_writable(ctx: Context) -> bool:
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return True  # a virtual environment
    target = sysconfig.get_paths().get("purelib", "")
    return bool(target) and os.access(target, os.W_OK)


def install_dependencies(ctx: Context, status: dict[str, dict[str, Any]] | None = None) -> StepResult:
    ctx.say("The MCP server needs the 'mcp' and 'psutil' packages (pyproject.toml's [mcp] extra).")
    status = status if status is not None else dependency_status()
    for name, item in status.items():
        ctx.say(f"  {name}: {item['installed'] or 'not installed'}{'' if item['ok'] or not item['installed'] else ' (outside the range needed)'}")
    if all(item["ok"] for item in status.values()):
        ctx.say("  Already installed; nothing to do.")
        return StepResult("MCP dependency", "unchanged", ", ".join(f"{name} {item['installed']}" for name, item in status.items()))
    command = [ctx.python, "-m", "pip", "install", "-e", ".[mcp]"]
    if not _site_packages_writable(ctx):
        command.insert(4, "--user")
    if not ctx.install:
        ctx.say("  Skipped (--skip-install). Install it later with:")
        ctx.say(f"    cd {ctx.root}")
        ctx.say(f"    {ctx.command_text(command)}")
        return StepResult("MCP dependency", "skipped", "--skip-install")
    ctx.say(f"  This runs, in {ctx.root}:")
    ctx.say(f"    {ctx.command_text(command)}")
    if ctx.dry_run:
        ctx.ask("Install the MCP dependency now?", True)
        return StepResult("MCP dependency", "planned", ctx.command_text(command))
    if not ctx.ask("Install the MCP dependency now?", True):
        return StepResult("MCP dependency", "skipped", "declined; the MCP server cannot start without it")
    try:
        result = ctx.run(command, cwd=str(ctx.root))
    except OSError as error:
        ctx.say(f"  pip could not be started: {error}")
        return StepResult("MCP dependency", "failed", str(error))
    if result.returncode != 0:
        ctx.say(f"  pip exited with code {result.returncode}. Read its message above.")
        ctx.say("  If pip is missing, run: " + ctx.command_text([ctx.python, "-m", "ensurepip", "--upgrade"]))
        ctx.say("  If this Python refuses packages (an 'externally managed' environment), make a virtual")
        ctx.say("  environment, e.g. 'python3 -m venv .venv', and run this setup with its Python.")
        return StepResult("MCP dependency", "failed", f"pip exit code {result.returncode}")
    return StepResult("MCP dependency", "done", "installed")


# ---------------------------------------------------------------------------------------------
# 3. Claude Code
# ---------------------------------------------------------------------------------------------


def claude_code_config_dir(ctx: Context) -> Path:
    configured = ctx.env.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(configured).expanduser() if configured else ctx.home / ".claude"


def claude_code_config_file(ctx: Context) -> Path:
    """Where Claude Code keeps user-scope MCP servers: ~/.claude.json, or .claude.json in CLAUDE_CONFIG_DIR."""
    configured = ctx.env.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(configured).expanduser() if configured else ctx.home) / ".claude.json"


def existing_claude_code_entry(ctx: Context) -> tuple[str, Any]:
    """('absent' | 'present' | 'unreadable', the user-scope msdial-interactive entry)."""
    path = claude_code_config_file(ctx)
    if not path.is_file():
        return "absent", None
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "unreadable", None
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    if isinstance(servers, dict) and SERVER_NAME in servers:
        return "present", servers[SERVER_NAME]
    return "absent", None


def register_claude_code(ctx: Context) -> StepResult:
    ctx.say("Claude Code finds MCP servers registered with 'claude mcp add'; user scope makes this one")
    ctx.say("available in every folder you open Claude Code in.")
    if not ctx.claude_code:
        return StepResult("Claude Code MCP", "skipped", "--no-claude-code")
    claude = ctx.which("claude")
    arguments = _server_args(ctx)
    add = ["claude", "mcp", "add", "--scope", "user", SERVER_NAME, "--", ctx.python, *arguments]
    if not claude:
        ctx.say("  The 'claude' command is not on PATH, so Claude Code is skipped. After installing Claude Code")
        ctx.say("  (https://claude.com/claude-code), run this setup again, or register it yourself:")
        ctx.say(f"    {ctx.command_text(add)}")
        return StepResult("Claude Code MCP", "skipped", "claude CLI not found")
    state, entry = existing_claude_code_entry(ctx)
    replace = False
    if state == "present":
        if _entry_matches(entry, ctx.python, arguments, ctx.windows):
            ctx.say(f"  '{SERVER_NAME}' is already registered at user scope for this Python and this checkout; nothing to do.")
            return StepResult("Claude Code MCP", "unchanged", "already registered")
        ctx.say(f"  '{SERVER_NAME}' is already registered at user scope, differently:")
        for line in _describe_entry(entry if isinstance(entry, dict) else {}):
            ctx.say(line)
        ctx.say("  This setup would register:")
        for line in _describe_entry({"command": ctx.python, "args": arguments}):
            ctx.say(line)
        if isinstance(entry, dict) and entry.get("env"):
            ctx.say("  Its environment variables are not carried over; add them again with 'claude mcp add -e' if needed.")
        replace = True
    elif state == "unreadable":
        ctx.say(f"  {claude_code_config_file(ctx)} could not be read, so an existing registration cannot be shown.")
    commands = []
    if replace:
        commands.append([claude, "mcp", "remove", "--scope", "user", SERVER_NAME])
    commands.append([claude, *add[1:]])
    ctx.say("  This runs:")
    for command in commands:
        ctx.say(f"    {ctx.command_text(['claude', *command[1:]])}")
    question = "Replace the existing registration?" if replace else f"Register '{SERVER_NAME}' with Claude Code?"
    if ctx.dry_run:
        ctx.ask(question, not replace)
        return StepResult("Claude Code MCP", "planned", "replace" if replace else "register")
    if not ctx.ask(question, not replace):
        return StepResult("Claude Code MCP", "skipped", "declined" + ("; the existing registration is kept" if replace else ""))
    for command in commands:
        try:
            result = ctx.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        except OSError as error:
            ctx.say(f"  'claude' could not be started: {error}")
            return StepResult("Claude Code MCP", "failed", str(error))
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if output:
            for line in output.splitlines()[:10]:
                ctx.say(f"    {line}")
        if result.returncode != 0:
            ctx.say(f"  'claude {' '.join(command[1:3])}' exited with code {result.returncode}.")
            return StepResult("Claude Code MCP", "failed", f"claude exit code {result.returncode}")
    return StepResult("Claude Code MCP", "done", "replaced" if replace else "registered")


# ---------------------------------------------------------------------------------------------
# 4. Claude Desktop
# ---------------------------------------------------------------------------------------------


def desktop_config_candidates(ctx: Context) -> list[Path]:
    """Where Claude Desktop keeps claude_desktop_config.json on this system, in the order to prefer."""
    name = "claude_desktop_config.json"
    if ctx.windows:
        candidates: list[Path] = []
        local = ctx.env.get("LOCALAPPDATA", "")
        if local:
            # The Microsoft Store (MSIX) installation reads its own copy of the roaming folder first.
            packages = Path(local) / "Packages"
            if packages.is_dir():
                candidates.extend(sorted(packages.glob(f"Claude_*/LocalCache/Roaming/Claude/{name}")))
        roaming = ctx.env.get("APPDATA", "")
        if roaming:
            candidates.append(Path(roaming) / "Claude" / name)
        return candidates
    if ctx.system == "Darwin":
        return [ctx.home / "Library" / "Application Support" / "Claude" / name]
    base = ctx.env.get("XDG_CONFIG_HOME", "") or str(ctx.home / ".config")
    return [Path(base) / "Claude" / name]


def desktop_config_files(ctx: Context) -> tuple[list[Path], Path | None]:
    """(the existing config files, one per distinct file; where a new one would go)."""
    candidates = desktop_config_candidates(ctx)
    existing: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        if any(_same_file(path, other) for other in existing):
            continue
        existing.append(path)
    default = candidates[-1] if candidates else None
    return existing, default


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def merge_desktop_entry(config: Any, command: str, arguments: list[str]) -> dict[str, Any]:
    """The config with mcpServers.msdial-interactive set; every other key kept, in its order."""
    if not isinstance(config, dict):
        raise ValueError("the file does not hold a JSON object")
    merged = dict(config)
    servers = merged.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("its 'mcpServers' is not a JSON object")
    servers = dict(servers)
    servers[SERVER_NAME] = {"command": command, "args": list(arguments)}
    merged["mcpServers"] = servers
    return merged


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def configure_desktop(ctx: Context) -> list[StepResult]:
    ctx.say("Claude Desktop reads MCP servers from claude_desktop_config.json ('mcpServers').")
    if ctx.desktop is False:
        return [StepResult("Claude Desktop MCP", "skipped", "--no-desktop")]
    existing, default = desktop_config_files(ctx)
    arguments = _server_args(ctx)
    targets = list(existing)
    if not targets:
        if ctx.desktop is not True or default is None:
            ctx.say("  No Claude Desktop configuration was found, so Claude Desktop is skipped.")
            ctx.say("  If you use Claude Desktop, start it once, or run this setup again with --desktop.")
            return [StepResult("Claude Desktop MCP", "skipped", "no claude_desktop_config.json")]
        targets = [default]
    results = []
    for path in targets:
        results.append(_configure_desktop_file(ctx, path, arguments))
    return results


def _configure_desktop_file(ctx: Context, path: Path, arguments: list[str]) -> StepResult:
    label = "Claude Desktop MCP"
    ctx.say(f"  Configuration: {path}")
    config: Any = {}
    if path.is_file():
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            ctx.say(f"  It cannot be read as JSON ({error}); it is left as it is. Fix it, or add the entry by hand.")
            return StepResult(label, "failed", f"{path.name} is not valid JSON")
    try:
        merged = merge_desktop_entry(config, ctx.python, arguments)
    except ValueError as error:
        ctx.say(f"  It is left as it is: {error}.")
        return StepResult(label, "failed", str(error))
    servers = config.get("mcpServers") if isinstance(config.get("mcpServers"), dict) else {}
    others = [name for name in servers if name != SERVER_NAME]
    entry = servers.get(SERVER_NAME)
    if entry is not None and _entry_matches(entry, ctx.python, arguments, ctx.windows):
        ctx.say(f"  '{SERVER_NAME}' is already there for this Python and this checkout; nothing to do.")
        return StepResult(label, "unchanged", str(path))
    replace = entry is not None
    if replace:
        ctx.say(f"  '{SERVER_NAME}' is already there, differently:")
        for line in _describe_entry(entry if isinstance(entry, dict) else {}):
            ctx.say(line)
        if isinstance(entry, dict) and entry.get("env"):
            ctx.say("  Its environment variables are not carried over; the backup keeps them.")
    ctx.say(f"  This setup would {'set' if replace else 'add'}:")
    for line in _describe_entry({"command": ctx.python, "args": arguments}):
        ctx.say(line)
    kept = [key for key in config if key != "mcpServers"] if isinstance(config, dict) else []
    ctx.say(
        f"  Kept as they are: {len(others)} other MCP server(s)"
        + (f" and the keys {', '.join(kept)}" if kept else "")
        + "."
    )
    backup = path.with_name(f"{path.name}.backup-{ctx.timestamp()}") if path.is_file() else None
    if backup:
        ctx.say(f"  A backup is written first: {backup.name}")
    question = f"Replace the '{SERVER_NAME}' entry?" if replace else f"Add '{SERVER_NAME}' to Claude Desktop?"
    if ctx.dry_run:
        ctx.ask(question, not replace)
        return StepResult(label, "planned", str(path))
    if not ctx.ask(question, not replace):
        return StepResult(label, "skipped", "declined")
    try:
        if backup:
            shutil.copy2(path, backup)
        _write_json(path, merged)
    except OSError as error:
        ctx.say(f"  It could not be written: {error}")
        return StepResult(label, "failed", str(error))
    ctx.say("  Written. Quit Claude Desktop completely (also from the system tray or menu bar) and start it again.")
    return StepResult(label, "done", str(path) + (f" (backup {backup.name})" if backup else ""))


# ---------------------------------------------------------------------------------------------
# 5. Skills for Claude Code
# ---------------------------------------------------------------------------------------------


def _is_link(path: Path) -> bool:
    """A symbolic link or, on Windows, a junction: replacing one must never write through it."""
    if path.is_symlink():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _link_target(path: Path) -> str:
    try:
        target = os.readlink(path)
    except (OSError, ValueError):
        return "(target unreadable)"
    # A Windows junction reads back with the extended-length prefix \\?\ in front of its path.
    prefix = "\\\\?\\"
    return target[len(prefix):] if target.startswith(prefix) else target


def _remove_link(path: Path) -> None:
    """Remove the link itself; the folder it points at is not touched."""
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name == "__pycache__" or name.endswith((".pyc", ".pyo"))}


def _relative_files(folder: Path) -> list[str]:
    return sorted(
        path.relative_to(folder).as_posix()
        for path in folder.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    )


def same_tree(left: Path, right: Path) -> bool:
    files = _relative_files(left)
    if files != _relative_files(right):
        return False
    return all(filecmp.cmp(left / name, right / name, shallow=False) for name in files)


def install_skills(ctx: Context) -> list[StepResult]:
    skills_dir = claude_code_config_dir(ctx) / "skills"
    backups_dir = claude_code_config_dir(ctx) / "skill-backups"
    ctx.say(f"Claude Code loads skills from {skills_dir}. Backups go to {backups_dir}, outside it,")
    ctx.say("so that Claude Code never loads a backup as a second copy of the skill.")
    if not ctx.claude_code:
        return [StepResult("Claude Code skills", "skipped", "--no-claude-code")]
    names = sorted(path.name for path in (ctx.root / "skills").iterdir() if (path / "SKILL.md").is_file())
    results = []
    for name in names:
        source = ctx.root / "skills" / name
        target = skills_dir / name
        label = f"Skill {name}"
        ctx.say(f"  {name}:")
        link = _is_link(target)
        if link:
            pointed = _link_target(target)
            if target.exists() and _same_file(target, source):
                ctx.say(f"    already a link to this checkout's copy ({pointed}); nothing to do.")
                results.append(StepResult(label, "unchanged", "linked to this checkout"))
                continue
            ctx.say(f"    {target} is a link to {pointed}.")
            ctx.say("    Replacing it removes only the link (what it points at is not touched) and copies this checkout's skill.")
            question = "Replace the link with a copy?"
            default = False
        elif target.exists():
            if target.is_dir() and same_tree(source, target):
                ctx.say("    already installed and identical; nothing to do.")
                results.append(StepResult(label, "unchanged", str(target)))
                continue
            ctx.say(f"    {target} exists and differs from this checkout's copy.")
            question = "Replace it (the existing copy is backed up first)?"
            default = False
        else:
            ctx.say(f"    copies {source} to {target}")
            question = f"Install the skill {name}?"
            default = True
        backup = backups_dir / f"{name}-{ctx.timestamp()}" if (link or target.exists()) else None
        if backup:
            ctx.say(f"    backup: {backup}")
        if ctx.dry_run:
            ctx.ask(question, default)
            results.append(StepResult(label, "planned", str(target)))
            continue
        if not ctx.ask(question, default):
            results.append(StepResult(label, "skipped", "declined"))
            continue
        try:
            if link:
                backup.mkdir(parents=True, exist_ok=False)
                (backup / "LINK_TARGET.txt").write_text(
                    f"{target} was a link to:\n{_link_target(target)}\n", encoding="utf-8"
                )
                _remove_link(target)
            elif target.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(backup))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, ignore=_ignore)
        except OSError as error:
            ctx.say(f"    It could not be installed: {error}")
            results.append(StepResult(label, "failed", str(error)))
            continue
        results.append(StepResult(label, "done", str(target) + (f" (backup {backup})" if backup else "")))
    return results


# ---------------------------------------------------------------------------------------------
# 6. Skill ZIPs for Claude Desktop
# ---------------------------------------------------------------------------------------------


def _packager() -> Any:
    spec = importlib.util.spec_from_file_location("package_agent_skill", PACKAGER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def build_skill_zips(ctx: Context) -> StepResult:
    packager = _packager()
    names = packager.skill_names(ctx.root / "skills")
    out_dir = ctx.root / "dist"
    ctx.say("Claude Desktop takes a skill as a ZIP uploaded under Customize > Skills (+, Upload a skill).")
    for name in names:
        ctx.say(f"  {out_dir / (name + packager.ZIP_SUFFIX)}")
    if ctx.dry_run:
        ctx.ask("Build the skill ZIPs?", True)
        return StepResult("Skill ZIPs", "planned", str(out_dir))
    if not ctx.ask("Build the skill ZIPs?", True):
        return StepResult("Skill ZIPs", "skipped", "declined")
    try:
        written = packager.package_skills(names, out_dir, ctx.root / "skills")
    except (OSError, ValueError) as error:
        ctx.say(f"  They could not be built: {error}")
        return StepResult("Skill ZIPs", "failed", str(error))
    ctx.say("  In Claude Desktop, upload each one: Customize > Skills > + > Upload a skill, then turn it on.")
    return StepResult("Skill ZIPs", "done", ", ".join(path.name for path in written))


# ---------------------------------------------------------------------------------------------
# 7. MS-DIAL Console
# ---------------------------------------------------------------------------------------------


def _saved_console(ctx: Context) -> str:
    """The Console path Interactive's settings hold, where they are readable."""
    if ctx.windows:
        base = Path(ctx.env.get("LOCALAPPDATA", "") or ctx.home / "AppData" / "Local") / "MSDIALInteractive"
    elif ctx.system == "Darwin":
        base = ctx.home / "Library" / "Application Support" / "MSDIALInteractive"
    else:
        base = Path(ctx.env.get("XDG_CONFIG_HOME", "") or ctx.home / ".config") / "msdial-interactive"
    path = base / "settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    return str(value.get("console_path", "") or "").strip() if isinstance(value, dict) else ""


def console_candidates(ctx: Context) -> list[tuple[str, Path]]:
    """(where it was found, the Console) for every Console found, the most explicit first."""
    found: list[tuple[str, Path]] = []
    for text in ctx.console_paths:
        found.append(("--console", Path(text).expanduser()))
    saved = _saved_console(ctx)
    if saved:
        found.append(("Interactive's saved setting", Path(saved)))
    environment = ctx.env.get("MSDIAL_CONSOLE_PATH", "").strip()
    if environment:
        found.append(("MSDIAL_CONSOLE_PATH", Path(environment)))
    for name in ("MSDIALCUI.exe", "MSDIALCUI"):
        on_path = ctx.which(name)
        if on_path:
            found.append(("PATH", Path(on_path)))
    bases = [ctx.root.parent, ctx.home, ctx.home / "Downloads", ctx.home / "Desktop", ctx.home / "Documents"]
    if ctx.drive_roots is not None:
        bases.extend(ctx.drive_roots)
    elif ctx.windows:
        bases.extend(Path(f"{drive}:\\") for drive in "CD")
    patterns = (
        "MSDIAL*/MSDIALCUI.exe", "MSDIAL*/MSDIALCUI.dll", "MSDIAL*/*/MSDIALCUI.exe", "MSDIAL*/*/MSDIALCUI.dll",
        "MsdialWorkbench*/tests/MSDIAL5/MsdialCoreTestApp/bin/Release/net48/MSDIALCUI.exe",
        "MsdialWorkbench*/tests/MSDIAL5/MsdialCoreTestApp/bin/Release/net8/MSDIALCUI.dll",
    )
    for base in dict.fromkeys(bases):
        try:
            if not base.is_dir():
                continue
            for pattern in patterns:
                found.extend(("common folder", path) for path in sorted(base.glob(pattern)))
        except OSError:
            continue
    unique: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for source, path in found:
        if not path.is_file():
            continue
        key = os.path.normcase(str(path.resolve()))
        if key in seen:
            continue
        seen.add(key)
        unique.append((source, path))
    return unique


def _assembly_checks(path: Path) -> dict[str, bool] | None:
    """The two capabilities Interactive reads from the Console assembly, by Interactive's own markers."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from msdial_app.workflow import (  # noqa: PLC0415  (stdlib-only module of this checkout)
            ANNOTATION_CANDIDATES_CONSOLE_MARKERS,
            AUTOMATIC_RT_LOCAL_SUPPORT_CONSOLE_MARKERS,
            console_assembly_path,
        )
    except Exception:  # noqa: BLE001  (a checkout that cannot be imported only loses this detail)
        return None
    try:
        binary = console_assembly_path(path).read_bytes()
    except OSError:
        return None

    def has(marker: str) -> bool:
        return marker.encode("utf-8") in binary or marker.encode("utf-16-le") in binary

    return {
        "the annotation candidate table": all(has(marker) for marker in ANNOTATION_CANDIDATES_CONSOLE_MARKERS),
        "automatic RT correction's local test (MsdialWorkbench#826)": all(
            has(marker) for marker in AUTOMATIC_RT_LOCAL_SUPPORT_CONSOLE_MARKERS
        ),
    }


def probe_console(ctx: Context, path: Path) -> dict[str, Any]:
    """Whether a Console is new enough for laboratory lipidomics; it is only asked for its --help."""
    if path.suffix.casefold() == ".dll":
        dotnet = ctx.which("dotnet")
        if not dotnet:
            return {"ready": False, "missing": ["the dotnet runtime, to start MSDIALCUI.dll"], "help": "not run"}
        command = [dotnet, str(path), "normalize", "--help"]
    else:
        command = [str(path), "normalize", "--help"]
    try:
        result = ctx.run(
            command, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
        )
        help_text = (result.stdout or "") + (result.stderr or "")
        started = True
    except (OSError, subprocess.TimeoutExpired) as error:
        help_text, started = str(error), False
    missing: list[str] = []
    if not started:
        missing.append("a Console that starts (it did not answer normalize --help)")
    elif NORMALIZE_OPTION not in help_text:
        missing.append(f"normalize {NORMALIZE_OPTION} (MsdialWorkbench#830)")
    assembly = _assembly_checks(path)
    if assembly is not None:
        missing.extend(label for label, present in assembly.items() if not present)
    return {
        "ready": not missing,
        "missing": missing,
        "help": "answered" if started else "did not start",
        "assembly_checked": assembly is not None,
    }


def check_console(ctx: Context) -> StepResult:
    ctx.say("Laboratory lipidomics needs an MS-DIAL Console (MSDIALCUI) with 'normalize --peak-ids-only'")
    ctx.say("(MsdialWorkbench#830), the annotation candidate table and the automatic-RT local test (#826).")
    candidates = console_candidates(ctx)
    ready: list[Path] = []
    if not candidates:
        ctx.say("  No MS-DIAL Console was found (Interactive's saved setting, MSDIAL_CONSOLE_PATH, PATH, and")
        ctx.say("  MSDIAL* folders beside this checkout, in your home, Downloads, Desktop and Documents).")
    for source, path in candidates[:8]:
        verdict = probe_console(ctx, path)
        ctx.say(f"  {path}  ({source})")
        if verdict["ready"]:
            ready.append(path)
            ctx.say("    new enough for laboratory lipidomics.")
        else:
            ctx.say(f"    not new enough: it lacks {'; '.join(verdict['missing'])}.")
        if not verdict.get("assembly_checked") and verdict["ready"]:
            ctx.say("    (only its normalize option was checked here; the agent's input check tests the rest.)")
    if ready:
        ctx.say("  When the agent asks which Console runs the analysis, name this one. Setup does not save it")
        ctx.say("  in Interactive's settings: the laboratory workflow names its Console per analysis.")
        return StepResult("MS-DIAL Console", "ok", str(ready[0]))
    tag = CONSOLE_PRERELEASE_TAG or "(tag not published yet)"
    ctx.say(f"  Get {CONSOLE_PRERELEASE_LABEL} {tag}:")
    ctx.say(f"    {CONSOLE_RELEASES_URL}")
    ctx.say("    Unpack its MSDIAL.console ZIP into a folder of your own, e.g. beside this checkout, and run")
    ctx.say("    this setup again (or pass --console <path to MSDIALCUI.exe>).")
    ctx.say("  Until it exists, build the Console from source: clone")
    ctx.say(f"    {MSDIAL_SOURCE_URL}")
    ctx.say("  install the .NET SDK, and ask Claude to use msdial_check_local_console_source and")
    ctx.say("  msdial_build_console_from_local_source on that folder (it shows the build command and asks first).")
    return StepResult("MS-DIAL Console", "attention", "none new enough found")


# ---------------------------------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------------------------------


def summarize(ctx: Context, results: list[StepResult]) -> None:
    ctx.say("")
    ctx.say("Summary" + (" (dry run: nothing was changed)" if ctx.dry_run else ""))
    width = max((len(item.step) for item in results), default=10)
    for item in results:
        ctx.say(f"  {item.step.ljust(width)}  {item.status}{': ' + item.detail if item.detail else ''}")
    ctx.say("")
    ctx.say("Next:")
    ctx.say("  - Claude Code: open a new session (or run /mcp in a running one) and check that")
    ctx.say(f"    '{SERVER_NAME}' is connected.")
    ctx.say("  - Claude Desktop: quit it completely and start it again; upload the skill ZIPs if you have not.")
    ctx.say("  - The tutorial, with the public demo data:")
    ctx.say(f"    {ctx.root / 'docs' / 'tutorials' / TUTORIAL.name}")
    ctx.say("")
    ctx.say("A first prompt to try:")
    ctx.say(f"  {FIRST_PROMPT}")


def run_setup(ctx: Context) -> int:
    results: list[StepResult] = []
    steps = 8

    def header(number: int, title: str) -> None:
        ctx.say("")
        ctx.say(f"Step {number} of {steps}: {title}")

    ctx.say("MS-DIAL Interactive setup for Claude" + (" -- dry run, nothing will be changed" if ctx.dry_run else ""))
    ctx.say(f"Checkout: {ctx.root}")
    header(1, "Python")
    python = check_python(ctx)
    results.append(python)
    if python.status == "failed":
        summarize(ctx, results)
        return 1
    header(2, "The MCP dependency")
    results.append(install_dependencies(ctx))
    header(3, "Claude Code: the MCP server")
    results.append(register_claude_code(ctx))
    header(4, "Claude Desktop: the MCP server")
    results.extend(configure_desktop(ctx))
    header(5, "Claude Code: the skills")
    results.extend(install_skills(ctx))
    header(6, "Claude Desktop: the skill ZIPs")
    results.append(build_skill_zips(ctx))
    header(7, "MS-DIAL Console")
    results.append(check_console(ctx))
    header(8, "Summary")
    summarize(ctx, results)
    return 1 if any(item.status == "failed" for item in results) else 0


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set up MS-DIAL Interactive's MCP server and skills for Claude Code and Claude Desktop."
    )
    parser.add_argument("--dry-run", action="store_true", help="show the plan; change nothing")
    parser.add_argument("--yes", "-y", action="store_true", help="answer yes to every question")
    desktop = parser.add_mutually_exclusive_group()
    desktop.add_argument("--desktop", action="store_true", default=None,
                         help="configure Claude Desktop even if no configuration file exists yet")
    desktop.add_argument("--no-desktop", dest="desktop", action="store_false", help="leave Claude Desktop alone")
    parser.add_argument("--no-claude-code", action="store_true", help="leave Claude Code (MCP and skills) alone")
    parser.add_argument("--skip-install", action="store_true", help="do not run pip")
    parser.add_argument("--console", action="append", default=[], metavar="PATH",
                        help="an MSDIALCUI.exe or MSDIALCUI.dll to check as well")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Loading the packager and Interactive's markers must leave no __pycache__ behind in a dry run.
    sys.dont_write_bytecode = True
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = parse_arguments(argv)
    ctx = Context(
        # Not resolved: a virtual environment's python may be a link to the base interpreter, and
        # resolving it would register the interpreter without the environment's packages.
        python=os.path.abspath(sys.executable),
        dry_run=args.dry_run,
        assume_yes=args.yes,
        desktop=args.desktop,
        claude_code=not args.no_claude_code,
        install=not args.skip_install,
        console_paths=list(args.console),
    )
    try:
        return run_setup(ctx)
    except KeyboardInterrupt:
        ctx.say("")
        ctx.say("Stopped. Steps already done stay done; run the setup again to continue.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
