"""Small session-scoped permission policy for effect tools."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import config


class Decision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionResult:
    decision: Decision
    reason: str
    user_rejected: bool = False
    remembered: bool = False


_DENIED_COMMANDS = [
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE), "git reset --hard can discard work"),
    (re.compile(r"\bgit\s+clean\b[^\r\n]*\s-[^\s]*f", re.IGNORECASE), "git clean -f can delete untracked files"),
    (re.compile(r"\b(?:shutdown|reboot|poweroff)\b", re.IGNORECASE), "machine power commands are outside the task boundary"),
    (re.compile(r"\b(?:diskpart|mkfs(?:\.[a-z0-9]+)?|format(?:\.com)?)\b", re.IGNORECASE), "disk formatting commands are denied"),
    (
        re.compile(r"\brm\s+-(?=[^\r\n]*r)(?=[^\r\n]*f)[^\r\n]*\s+(?:/|~)(?:\s|$)", re.IGNORECASE),
        "recursive deletion of a system or home root is denied",
    ),
    (
        re.compile(
            r"\bRemove-Item\b(?=[^\r\n]*-Recurse)(?=[^\r\n]*-Force)"
            r"(?=[^\r\n]*(?:[a-z]:\\(?:[\s\"']|$)|\$HOME(?:[\\/\s\"']|$)|~(?:[\\/\s\"']|$)))",
            re.IGNORECASE,
        ),
        "recursive forced deletion of a drive or home root is denied",
    ),
    (
        re.compile(
            r"\b(?:rd|rmdir|del)\b(?=[^\r\n]*/s)(?=[^\r\n]*/q)"
            r"(?=[^\r\n]*[a-z]:\\(?:[\s\"'*]|$))",
            re.IGNORECASE,
        ),
        "recursive forced deletion of a drive root is denied",
    ),
]

_SHELL_CONTROL = re.compile(r"(?:&&|\|\||[|;&<>`\r\n]|\$\()")
_READ_ONLY_COMMANDS = [
    re.compile(
        r"^(?:dir|type|where(?:\.exe)?|findstr|tree|echo|cd|pwd|ls|cat|head|tail|"
        r"grep|rg|find|wc|which|diff|stat|du)(?:\s|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^git(?:\.exe)?\s+(?:status|diff|log|show|rev-parse|ls-files|grep|blame)"
        r"(?:\s|$)",
        re.IGNORECASE,
    ),
    re.compile(r"^git(?:\.exe)?\s+branch\s+--show-current(?:\s|$)", re.IGNORECASE),
    re.compile(
        r"^(?:Get-ChildItem|Get-Content|Get-Item|Get-FileHash|Select-String|"
        r"Resolve-Path|Test-Path)(?:\s|$)",
        re.IGNORECASE,
    ),
]
_COMMON_VERIFIERS = [
    re.compile(
        r"^(?:\"[^\"]*python(?:\.exe)?\"|[^\s\"]*python(?:\d+(?:\.\d+)*)?(?:\.exe)?)"
        r"\s+-m\s+(?:unittest|pytest|ruff|compileall|py_compile)(?:\s|$)",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:pytest|py\.test)(?:\.exe)?(?:\s|$)", re.IGNORECASE),
    re.compile(r"^ruff(?:\.exe)?\s+check(?:\s|$)", re.IGNORECASE),
    re.compile(r"^(?:npm|pnpm|yarn)(?:\.cmd)?\s+(?:test|run\s+(?:test|lint|check|typecheck))(?:\s|$)", re.IGNORECASE),
    re.compile(r"^(?:cargo|go|dotnet)(?:\.exe)?\s+test(?:\s|$)", re.IGNORECASE),
    re.compile(r"^(?:mvn|mvnw|gradle|gradlew)(?:\.cmd|\.bat)?\s+(?:test|check|verify)(?:\s|$)", re.IGNORECASE),
]
_ALWAYS_ASK_COMMANDS = [
    (re.compile(r"\bgit\s+(?:push|commit|merge|rebase|checkout|switch|cherry-pick|tag)\b", re.IGNORECASE), "Git history or remote state may change"),
    (re.compile(r"\b(?:pip|pip3)\s+install\b|\bpython(?:\.exe)?\s+-m\s+pip\s+install\b", re.IGNORECASE), "package installation can execute third-party code"),
    (re.compile(r"\b(?:npm|pnpm|yarn)(?:\.cmd)?\s+(?:install|add|remove|ci)\b", re.IGNORECASE), "dependency installation changes the environment"),
    (re.compile(r"\b(?:rm|del|erase|rmdir|rd|Remove-Item|Move-Item)\b", re.IGNORECASE), "files may be deleted or moved"),
    (re.compile(r"\b(?:curl|wget|Invoke-WebRequest|Invoke-RestMethod|ssh|scp|sftp)\b", re.IGNORECASE), "the command accesses an external system"),
    (re.compile(r"\b(?:docker|kubectl|terraform|ansible|helm)\b", re.IGNORECASE), "the command may affect services or infrastructure"),
    (re.compile(r"\b(?:deploy|publish)\b", re.IGNORECASE), "the command may publish external state"),
    (re.compile(r"^(?:powershell|pwsh)(?:\.exe)?\s+-(?:Command|EncodedCommand)\b|^cmd(?:\.exe)?\s+/c\b", re.IGNORECASE), "nested shells hide the effective command"),
]
_PROTECTED_FILE_NAMES = {
    ".env",
    ".agentignore",
    ".agent-verifier",
    "credentials.json",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
}
_SAFE_ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template"}
_UNSAFE_AUTO_COMMAND_PATH = re.compile(
    r"(?:[a-z]:[\\/]|\\\\|\.\.[\\/]|~[\\/]|\$(?:env:|home\b|\{home\})|"
    r"%[a-z_][a-z0-9_]*%|\$[a-z_][a-z0-9_]*|\$\{[^}]+\}|"
    r"/(?:etc|home|root|users|private)(?:/|\b)|"
    r"(?:^|[\\/\s])(?:\.agent|\.ssh|\.aws|\.kube|\.env(?:\.[^\s\\/]*)?)(?:[\\/\s]|$))",
    re.IGNORECASE,
)


def _has_shell_control(cmd: str) -> bool:
    return bool(_SHELL_CONTROL.search(cmd)) or "--output" in cmd.lower()


def _unsafe_auto_command_path(cmd: str) -> bool:
    return bool(_UNSAFE_AUTO_COMMAND_PATH.search(cmd))


def _matches_any(cmd: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.search(cmd) for pattern in patterns)


def _is_read_only_command(cmd: str) -> bool:
    return (
        not _has_shell_control(cmd)
        and not _unsafe_auto_command_path(cmd)
        and _matches_any(cmd.strip(), _READ_ONLY_COMMANDS)
    )


def _is_common_verifier(cmd: str) -> bool:
    return (
        not _has_shell_control(cmd)
        and not _unsafe_auto_command_path(cmd)
        and _matches_any(cmd.strip(), _COMMON_VERIFIERS)
    )


def _always_ask_reason(cmd: str) -> str | None:
    for pattern, reason in _ALWAYS_ASK_COMMANDS:
        if pattern.search(cmd):
            return reason
    return None


def _protected_edit_reason(path: str) -> str | None:
    parts = [part.lower() for part in path.replace("\\", "/").split("/") if part]
    if not parts:
        return None
    name = parts[-1]
    if ".git" in parts:
        return "Git metadata is a protected path"
    if name == ".agent-verifier":
        return "the user/project final verifier is protected validation policy"
    if name == ".agentignore":
        return "the workspace tracking ignore file is protected runtime policy"
    if name in _PROTECTED_FILE_NAMES or (
        name.startswith(".env.") and name not in _SAFE_ENV_EXAMPLES
    ):
        return "the file may contain credentials or secrets"
    return None


def _command_tokens(cmd: str) -> list[str]:
    try:
        return [token.strip('"\'') for token in shlex.split(cmd, posix=False)]
    except ValueError:
        return []


def _requires_exact_session_approval(cmd: str) -> bool:
    """Identify interpreter entry points whose family can execute arbitrary code."""

    tokens = _command_tokens(cmd)
    if len(tokens) < 2:
        return False
    executable = Path(tokens[0]).name.lower()
    if executable.endswith((".exe", ".cmd")):
        executable = executable.rsplit(".", 1)[0]
    option = tokens[1].lower()
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
        return option in {"-c", "-"}
    if executable in {"bash", "sh", "zsh"}:
        return option == "-c"
    if executable in {"node", "ruby", "perl"}:
        return option == "-e"
    if executable in {"powershell", "pwsh"}:
        return option in {"-command", "-encodedcommand"}
    return executable == "cmd" and option == "/c"


def _command_scope(cmd: str) -> str:
    """Return a small, visible command family for session-scoped approval."""

    tokens = _command_tokens(cmd)
    if not tokens:
        return cmd.strip()
    executable = Path(tokens[0]).name.lower()
    if executable.endswith((".exe", ".cmd")):
        executable = executable.rsplit(".", 1)[0]
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
        executable = "python"
        if len(tokens) >= 3 and tokens[1].lower() == "-m":
            return f"python -m {tokens[2].lower()}"
    if len(tokens) >= 2:
        return f"{executable} {tokens[1].lower()}"
    return executable


@dataclass
class PermissionManager:
    """Resolve effect-tool permissions and remember approvals for one run."""

    allow_clean_edits: bool = False
    allowed_command_scopes: set[str] = field(default_factory=set)
    allowed_exact_commands: set[str] = field(default_factory=set)
    denied_command_scopes: set[str] = field(default_factory=set)
    allowed_dirty_files: set[str] = field(default_factory=set)
    allowed_protected_files: set[str] = field(default_factory=set)
    answerer: Callable[[str], str] | None = field(default=None, repr=False, compare=False)

    def _answer(self, prompt: str) -> str:
        if self.answerer is not None:
            try:
                return self.answerer(prompt).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return ""
        try:
            return input(prompt).strip().lower()
        except EOFError:
            return ""

    @staticmethod
    def _denied_command_reason(cmd: str) -> str | None:
        for pattern, reason in _DENIED_COMMANDS:
            if pattern.search(cmd):
                return reason
        return None

    def decide_edit(self, path: str, *, initially_dirty: bool = False) -> PermissionResult:
        if not config.REQUIRE_CONFIRMATION:
            return PermissionResult(Decision.ALLOW, "confirmation disabled for this run")
        protected_reason = _protected_edit_reason(path)
        dirty_needs_approval = initially_dirty and path not in self.allowed_dirty_files
        protected_needs_approval = (
            protected_reason is not None and path not in self.allowed_protected_files
        )
        if dirty_needs_approval or protected_needs_approval:
            reasons = []
            if dirty_needs_approval:
                reasons.append("it had user changes before this run")
            if protected_needs_approval:
                reasons.append(str(protected_reason))
            return PermissionResult(Decision.ASK, "; ".join(reasons))
        if config.PERMISSION_MODE == "balanced":
            return PermissionResult(
                Decision.ALLOW,
                "balanced mode auto-approves ordinary workspace edits",
            )
        if self.allow_clean_edits:
            return PermissionResult(Decision.ALLOW, "clean-file edits approved for this session")
        return PermissionResult(Decision.ASK, "manual mode requires edit approval")

    def authorize_edit(self, path: str, *, initially_dirty: bool = False) -> PermissionResult:
        decision = self.decide_edit(path, initially_dirty=initially_dirty)
        if decision.decision is not Decision.ASK:
            return decision

        protected_reason = _protected_edit_reason(path)
        special = initially_dirty or protected_reason is not None
        if special:
            answer = self._answer(
                f"Permission required for {path}: {decision.reason}.\n"
                "  [1] Allow this edit once\n"
                "  [2] Allow edits to this file for this session\n"
                "  [3] Deny\n"
                "Choose [1/2/3]: "
            )
            if answer in {"2", "a", "always"}:
                if initially_dirty:
                    self.allowed_dirty_files.add(path)
                if protected_reason is not None:
                    self.allowed_protected_files.add(path)
                return PermissionResult(
                    Decision.ALLOW,
                    "user approved this file-specific exception for the session",
                    remembered=True,
                )
            if answer in {"1", "y", "yes"}:
                return PermissionResult(Decision.ALLOW, "user approved this edit once")
            return PermissionResult(
                Decision.DENY,
                "user rejected a change requiring file-specific approval",
                user_rejected=True,
            )

        answer = self._answer(
            f"Permission required for {path}: {decision.reason}.\n"
            "  [1] Allow this edit once\n"
            "  [2] Allow ordinary edits for this session\n"
            "  [3] Deny\n"
            "Choose [1/2/3]: "
        )
        if answer in {"2", "a", "always"}:
            self.allow_clean_edits = True
            return PermissionResult(
                Decision.ALLOW,
                "user approved clean-file edits for the session",
                remembered=True,
            )
        if answer in {"1", "y", "yes"}:
            return PermissionResult(Decision.ALLOW, "user approved this edit once")
        return PermissionResult(
            Decision.DENY,
            "user rejected this edit",
            user_rejected=True,
        )

    def decide_command(
        self,
        cmd: str,
        *,
        verification: bool = False,
        required_verifier: str | None = None,
    ) -> PermissionResult:
        denied_reason = self._denied_command_reason(cmd)
        if denied_reason:
            return PermissionResult(Decision.DENY, denied_reason)
        scope = _command_scope(cmd)
        if scope in self.denied_command_scopes:
            return PermissionResult(Decision.DENY, f"command family denied for this session: {scope}")
        if not config.REQUIRE_CONFIRMATION:
            return PermissionResult(Decision.ALLOW, "confirmation disabled for this run")
        normalized = cmd.strip()
        critical_reason = _always_ask_reason(normalized)
        if critical_reason:
            return PermissionResult(Decision.ASK, critical_reason)
        if required_verifier and normalized == required_verifier.strip():
            return PermissionResult(Decision.ALLOW, "user-configured final verifier")
        if _is_read_only_command(normalized):
            return PermissionResult(Decision.ALLOW, "recognized read-only command")
        if (
            config.PERMISSION_MODE == "balanced"
            and verification
            and _is_common_verifier(normalized)
        ):
            return PermissionResult(Decision.ALLOW, "recognized local verification command")
        if normalized in self.allowed_exact_commands:
            return PermissionResult(Decision.ALLOW, "exact command approved for this session")
        if scope in self.allowed_command_scopes:
            return PermissionResult(Decision.ALLOW, f"command family approved for this session: {scope}")
        return PermissionResult(Decision.ASK, "unrecognized command requires approval")

    def authorize_command(
        self,
        cmd: str,
        *,
        verification: bool = False,
        required_verifier: str | None = None,
    ) -> PermissionResult:
        decision = self.decide_command(
            cmd,
            verification=verification,
            required_verifier=required_verifier,
        )
        if decision.decision is not Decision.ASK:
            return decision

        normalized = cmd.strip()
        scope = _command_scope(normalized)
        exact_only = _requires_exact_session_approval(normalized)
        critical = _always_ask_reason(normalized) is not None
        if critical:
            answer = self._answer(
                f"High-impact command: {decision.reason}.\n"
                f"\n{normalized}\n\n"
                "  [1] Allow once\n"
                "  [2] Deny once\n"
                f"  [3] Deny this command family for the session: {scope}\n"
                "Choose [1/2/3]: "
            )
            if answer in {"1", "y", "yes"}:
                return PermissionResult(Decision.ALLOW, "user approved this high-impact command once")
            if answer in {"3", "a", "always"}:
                self.denied_command_scopes.add(scope)
                return PermissionResult(
                    Decision.DENY,
                    f"user denied command family for the session: {scope}",
                    user_rejected=True,
                    remembered=True,
                )
            return PermissionResult(
                Decision.DENY,
                "user rejected this high-impact command",
                user_rejected=True,
            )

        remembered_target = (
            f"this exact command for the session: {normalized}"
            if exact_only
            else f"this command family for the session: {scope}"
        )
        answer = self._answer(
            f"Command requires approval: {decision.reason}.\n"
            f"\n{normalized}\n\n"
            "  [1] Allow once\n"
            f"  [2] Allow {remembered_target}\n"
            "  [3] Deny\n"
            "Choose [1/2/3]: "
        )
        if answer in {"2", "a", "always"}:
            if exact_only:
                self.allowed_exact_commands.add(normalized)
            else:
                self.allowed_command_scopes.add(scope)
            return PermissionResult(
                Decision.ALLOW,
                (
                    f"user approved exact command for the session: {normalized}"
                    if exact_only
                    else f"user approved command family for the session: {scope}"
                ),
                remembered=True,
            )
        if answer in {"1", "y", "yes"}:
            return PermissionResult(Decision.ALLOW, "user approved this command once")
        return PermissionResult(
            Decision.DENY,
            "user rejected this command",
            user_rejected=True,
        )
