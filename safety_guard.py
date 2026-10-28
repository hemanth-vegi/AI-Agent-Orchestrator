import re
import subprocess
from pathlib import Path
from typing import Tuple, Optional

BLOCKED_PATTERNS = [
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-[a-zA-Z]*f\b",
    r"\b(rm|rmdir|del)\b.*(/s|/q|-rf|-r)",
    r"\bpip\s+install\s+.*(--upgrade|--force-reinstall)\b",
    r"\b(format|diskpart|mkfs)\b",
    r"\bDrop\s+database\b",
]

SAFE_WHITELIST = [
    r"^python\s+[\w\./\\-]+\.py.*",
    r"^pytest.*",
    r"^git\s+(status|diff|log|branch|show)",
    r"^node\s+[\w\./\\-]+\.js.*",
    r"^dir\b",
    r"^ls\b",
]

def check_command_safety(cmd: str) -> Tuple[str, str]:
    """
    Evaluates shell command safety.
    Returns:
        ("BLOCK", reason) -> Dangerous command halted immediately.
        ("ALLOW", reason) -> Whitelisted safe command auto-approved.
        ("ASK", reason)   -> Unknown command prompts user for manual confirmation.
    """
    cmd_clean = (cmd or "").strip()
    if not cmd_clean:
        return "ALLOW", "Empty command"

    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, cmd_clean, re.IGNORECASE):
            return "BLOCK", f"Security violation: Command matched blocked pattern '{pattern}'"

    for pattern in SAFE_WHITELIST:
        if re.search(pattern, cmd_clean, re.IGNORECASE):
            return "ALLOW", "Whitelisted safe command"

    return "ASK", "Unrecognized command requires interactive user confirmation"


def create_safety_checkpoint(repo_path: str = ".") -> Optional[str]:
    """Takes a hidden git snapshot commit before an agent touches files."""
    try:
        res = subprocess.run(
            ["git", "stash", "create"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        commit = res.stdout.strip()
        return commit if commit else None
    except Exception:
        return None


def rollback_checkpoint(checkpoint_hash: str, repo_path: str = ".") -> bool:
    """Restores the workspace to the exact state before the agent made changes."""
    try:
        subprocess.run(
            ["git", "stash", "apply", checkpoint_hash],
            cwd=repo_path,
            capture_output=True,
            check=True
        )
        return True
    except Exception:
        return False
