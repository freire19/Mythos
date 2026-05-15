"""Centralized security validation for Mythos.

Unifies command/pipeline validation previously scattered across:
- alpha/tools/shell_tools.py (_HARD_BLOCKED_PATTERNS, _validate_command)
- alpha/tools/pipeline_tools.py (_SHELL_EXPANSION_RE, _validate_pipeline)

Also provides the shared denylist constants both modules need (#D002).
"""

import re
import shlex

from ._platform import IS_WINDOWS

# ─── Catastrophic shell patterns ─────────────────────────────────────

_HARD_BLOCKED_PATTERNS = [
    # Recursive file deletion
    r"\brm\s+(?:-\S*[rR]\S*|--recursive\b)",
    # Filesystem formatting / wiping
    r"\bmkfs(?:\.[a-z0-9]+)?\b",
    r"\bmke2fs\b",
    r"\bwipefs\b",
    r"\bshred\b",
    # Raw disk writes
    r"\bdd\s+[^\n]*of=/dev/(sd|nvme|hd|xvd|vd|mmcblk)",
    r">\s*/dev/(sd|nvme|hd|xvd|vd|mmcblk)",
    # Fork bomb (case-sensitive — `:` literal)
    r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;?\s*:",
    # su (sudo is handled via pattern matches, not blanket-blocked)
    r"(^|[;&|]\s*)su(\s|$)",
    # Power / halt / reboot
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\binit\s+[0-6]\b",
    r"\btelinit\b",
    r"\bsystemctl\s+(poweroff|reboot|halt|kexec|rescue|emergency|suspend|hibernate)\b",
    # Writes to critical system files
    r">\s*/etc/(passwd|shadow|sudoers|fstab|hosts(\s|$))",
    r"\b(tee|dd)\s+[^|;]*\s/etc/(passwd|shadow|sudoers|fstab)",
    r"\bvisudo\b",
    # chmod on critical system dirs
    r"\bchmod\s+\S+\s+/(etc|usr|boot|bin|sbin|lib|lib64|sys|proc)(\s|/|$)",
    r"\bchmod\s+-R\s+\S+\s+/(\s|$)",
    # chown to root on system paths
    r"\bchown\s+\S*root\S*\s+/(etc|usr|boot|bin|sbin|lib)",
    # Kernel module manipulation
    r"\b(insmod|rmmod)\b",
    r"\bmodprobe\s+-r\b",
    # LVM / crypto destruction
    r"\b(lvremove|vgremove|pvremove)\b",
    r"\bcryptsetup\s+(erase|luksErase|wipeKey|luksRemoveKey)\b",
    # User/group destruction
    r"\b(userdel|groupdel)\b",
    # Interactive disk partitioning on real devices
    r"\b(fdisk|gdisk|cfdisk|sfdisk|parted)\s+/dev/",
    # Firewall flush/reset
    r"\b(iptables|ip6tables|nft)\b\s+(?:.*\s+)?(?:-F|-X|--flush)(?:\s|$)",
    r"\bufw\s+(reset|disable)\b",
    # find with -fprint/-fprintf (sandbox escape)
    r"\bfind\s+.*-fprintf?\s",
    # ─── Windows destructive patterns ───
    r"\b(?:rmdir|rd)\s+(?:/s\b|/q\s+/s\b|/s\s+/q\b)",
    r"\bdel\s+(?:/[fsq]\s*)*?/s\b",
    r"\bRemove-Item\b[^\n]*-Recurse",
    r"\bformat\s+[a-z]:\s",
    r"\bdiskpart\b",
    r"\bshutdown\s+/[rstpgha]",
    r"\b(Stop-Computer|Restart-Computer)\b",
    r"\breg\s+delete\b",
    r"\bRemove-ItemProperty\b",
    r"\bnet\s+user\s+\S+\s+/delete",
    r"\bRemove-LocalUser\b",
]

# Single combined regex (avoids 27+ individual searches per validation call, #D020).
HARD_BLOCKED_RE = re.compile(
    "|".join(f"(?:{p})" for p in _HARD_BLOCKED_PATTERNS), re.IGNORECASE
)

# Backwards compat: code that imports HARD_BLOCKED (e.g. pipeline_tools) still
# works. Each element is a re.Pattern with .search().
HARD_BLOCKED = [re.compile(p, re.IGNORECASE) for p in _HARD_BLOCKED_PATTERNS]

# Shell expansion patterns (command/variable substitution, process substitution).
_SHELL_EXPANSION_RE = re.compile(
    r"\$\("     # command substitution: $(...)
    r"|`"       # backtick substitution
    r"|\$\{"    # variable expansion: ${...}
    r"|\$[A-Za-z_]"  # variable reference: $VAR
    r"|<\(\)"    # process substitution: <(...)
    r"|\$\(\(\("  # arithmetic expansion: $(((...)))
)


# ─── Validation functions ────────────────────────────────────────────

def validate_command(command: str) -> str | None:
    """Return error message if command is destructive, None otherwise.

    Denylist model: only catastrophic patterns (HARD_BLOCKED_RE) are rejected.
    Any other command runs. Approval layer decides user prompting.
    """
    if "\n" in command or "\r" in command:
        return "Comando bloqueado: caracteres de newline não são permitidos"

    if HARD_BLOCKED_RE.search(command):
        return "Comando bloqueado por segurança (padrão destrutivo detectado)"

    # On Windows, cmd.exe parses the command — POSIX shlex breaks backslash
    # paths and doesn't reflect cmd syntax. Newline + HARD_BLOCKED already
    # checked above; nothing more to validate.
    if IS_WINDOWS:
        return None

    segments = command.split("|") if "|" in command else [command]
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        try:
            parts = shlex.split(segment)
            if not parts:
                continue
        except ValueError:
            return "Comando malformado"

    return None


def validate_pipeline(pipeline: str) -> str | None:
    """Validate a full pipeline string. Returns error message or None.

    Denylist model: catastrophic patterns blocked; everything else runs.
    """
    # Block shell variable/command expansion (injection vector)
    if _SHELL_EXPANSION_RE.search(pipeline):
        return "Pipeline bloqueado: expansão de variáveis/comandos ($(), ``, ${}) não é permitida"

    # Check hard-blocked patterns on the full string (combined regex, #D020)
    if HARD_BLOCKED_RE.search(pipeline):
        return "Pipeline bloqueado por segurança (padrão destrutivo detectado)"

    # Syntactic check per segment (no allowlist; HARD_BLOCKED already gated)
    segments = re.split(r"\s*(?:\|\||&&|;|\|)\s*", pipeline)
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        cmd_part = re.split(r"\s*(?:>>?|2>>?|<)\s*", segment)[0].strip()
        if not cmd_part:
            continue
        try:
            parts = shlex.split(cmd_part)
            if not parts:
                continue
        except ValueError:
            return f"Segmento malformado no pipeline: {segment}"

    return None
