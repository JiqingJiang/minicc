"""Process-level sandbox execution adapter (macOS Seatbelt sandbox-exec + fallback process group)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from src.harness.memory.store import state_root


@dataclass(frozen=True)
class SandboxPolicy:
    workspace: Path
    allow_network: bool = False
    extra_writable: tuple[Path, ...] = ()


@lru_cache(maxsize=1)
def sandbox_available() -> bool:
    """Return True only when macOS sandbox-exec exists and is not blocked by an outer sandbox."""
    if sys.platform != "darwin":
        return False
    sb_bin = shutil.which("sandbox-exec")
    if sb_bin is None:
        return False
    try:
        probe = subprocess.run(
            [sb_bin, "-p", "(version 1)(allow default)", "/usr/bin/true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=False,
        )
        return probe.returncode == 0
    except OSError:
        return False


def _escape_sbpl(path: str) -> str:
    return path.replace("\\", "\\\\").replace('"', '\\"')


def build_seatbelt_profile(policy: SandboxPolicy) -> str:
    """Build a macOS Seatbelt profile restricting file writes to the workspace, state_root, and /tmp."""
    writable: set[str] = {
        str(policy.workspace.resolve()),
        str(state_root().expanduser().resolve()),
        "/tmp",
        "/private/tmp",
        "/private/var/tmp",
        "/private/var/folders",
        str(Path(tempfile.gettempdir()).resolve()),
    }
    for extra in policy.extra_writable:
        try:
            writable.add(str(extra.expanduser().resolve()))
        except OSError:
            continue

    subpaths = "\n  ".join(f'(subpath "{_escape_sbpl(item)}")' for item in sorted(writable))
    network_rule = "(allow network*)" if policy.allow_network else '(deny network*)\n(allow network* (remote unix-socket))'
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        f"{network_rule}\n"
        "(allow file-write*\n"
        '  (literal "/dev/null")\n'
        '  (literal "/dev/zero")\n'
        '  (literal "/dev/tty")\n'
        '  (literal "/dev/ptmx")\n'
        '  (regex #"^/dev/ttys[0-9]+$")\n'
        f"  {subpaths}\n"
        ")\n"
    )


def wrap_sandbox_command(
    shell: str, command: str, policy: SandboxPolicy, *, unsandboxed: bool = False
) -> tuple[list[str], bool]:
    """Return (argv, is_sandboxed) for subprocess.Popen."""
    base_cmd = [shell, "--noprofile", "--norc", "-c", command]
    if unsandboxed or not sandbox_available():
        return base_cmd, False
    sb_bin = shutil.which("sandbox-exec")
    if sb_bin is None:
        return base_cmd, False
    profile = build_seatbelt_profile(policy)
    return [sb_bin, "-p", profile, *base_cmd], True
