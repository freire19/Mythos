"""Central security hub (#D002).

Consolidates command/pipeline validation primitives that were scattered
across `shell_tools.py`, `pipeline_tools.py`, and `approval.py`.

Imports from here should be the single source of truth for:
- Hard-blocked command patterns (denylist model)
- Command validation
- Pipeline validation
"""

import re
import shlex

# ─── Hard-blocked command patterns (denylist model) ───
#
# Shell commands that match these patterns are rejected before approval.
# Approval layer decides user prompting for everything else (#D027).
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
    # Fork bomb
    r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;?\s*:",
    # su
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
]

# Combined regex for single-pass matching (#D020).
HARD_BLOCKED_RE = re.compile(
    "|".join(f"(?:{p})" for p in _HARD_BLOCKED_PATTERNS), re.IGNORECASE
)

# Shell expansion detection (pipeline injection vector).
_SHELL_EXPANSION_RE = re.compile(
    r"\$\(|`|\$\{|\$[A-Za-z_]|<\(|\$\(\(|\\"
)


def validate_command(command: str) -> str | None:
    """Return error message if command matches hard-blocked patterns.

    Denylist model: only catastrophic patterns (HARD_BLOCKED_RE) are rejected.
    Any other command runs. Approval layer decides user prompting.
    """
    if "\n" in command or "\r" in command:
        return "Comando bloqueado: caracteres de newline nao sao permitidos"

    if HARD_BLOCKED_RE.search(command):
        return "Comando bloqueado por seguranca (padrao destrutivo detectado)"

    return None


def validate_pipeline(pipeline: str) -> str | None:
    """Validate a full pipeline string. Returns error message or None.

    Denylist model: catastrophic patterns blocked; everything else runs.
    """
    if _SHELL_EXPANSION_RE.search(pipeline):
        return "Pipeline bloqueado: expansao de variaveis/comandos ($(), ``, ${{}}) nao e permitida"

    if HARD_BLOCKED_RE.search(pipeline):
        return "Pipeline bloqueado por seguranca (padrao destrutivo detectado)"

    # Syntactic check per segment
    segments = re.split(r"\s*(?:\|\||&&|;|\|)\s*", pipeline)
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        cmd_part = re.split(r"\s*(?:>>?|2>>?|<)\s*", segment)[0].strip()
        if not cmd_part:
            continue
        try:
            shlex.split(cmd_part)
        except ValueError:
            return f"Pipeline bloqueado: sintaxe invalida no segmento '{cmd_part[:80]}'"

    return None
