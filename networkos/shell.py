"""Allowlisted process execution.

Nothing in XPDevs NetworkOS ever passes user input to a shell.  Every external
program is addressed by an absolute path from a fixed table and invoked with an
argument vector, so shell metacharacters in request data have no meaning.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

from . import paths
from .errors import CommandError

#: Logical name -> candidate absolute paths, in preference order.
BINARIES: dict[str, tuple[str, ...]] = {
    "ip": ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip", "/bin/ip"),
    "nft": ("/usr/sbin/nft", "/sbin/nft", "/usr/bin/nft", "/bin/nft"),
    "iw": ("/usr/sbin/iw", "/sbin/iw", "/usr/bin/iw"),
    "hostapd": ("/usr/sbin/hostapd", "/sbin/hostapd"),
    "dnsmasq": ("/usr/sbin/dnsmasq", "/sbin/dnsmasq"),
    "systemctl": ("/usr/bin/systemctl", "/bin/systemctl"),
    "journalctl": ("/usr/bin/journalctl", "/bin/journalctl"),
    "ping": ("/usr/bin/ping", "/bin/ping"),
    "curl": ("/usr/bin/curl", "/usr/local/bin/curl"),
    "reboot": ("/usr/sbin/reboot", "/sbin/reboot", "/usr/bin/reboot"),
    "shutdown": ("/usr/sbin/shutdown", "/sbin/shutdown", "/usr/bin/shutdown"),
    "pihole": ("/usr/local/bin/pihole", "/usr/bin/pihole"),
    "pihole-ftl": ("/usr/bin/pihole-FTL", "/usr/sbin/pihole-FTL"),
    "vcgencmd": ("/usr/bin/vcgencmd", "/opt/vc/bin/vcgencmd"),
    "apt-get": ("/usr/bin/apt-get", "/usr/local/bin/apt-get"),
    "tar": ("/usr/bin/tar", "/bin/tar"),
    "chpasswd": ("/usr/sbin/chpasswd", "/usr/bin/chpasswd"),
    "sudo": ("/usr/bin/sudo", "/bin/sudo"),
    "python3": ("/usr/bin/python3", "/usr/local/bin/python3"),
    "ip6tables": ("/usr/sbin/ip6tables", "/sbin/ip6tables"),
    "iptables": ("/usr/sbin/iptables", "/sbin/iptables"),
}

_SAFE_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "LANG": "C",
    "HOME": "/root" if os.geteuid() == 0 else "/tmp",
    "TERM": "dumb",
}


def _environment() -> dict[str, str]:
    """Minimal environment; NETWORKOS_* and PYTHONPATH pass through for tests."""
    env = dict(_SAFE_ENV)
    for key, value in os.environ.items():
        if key.startswith("NETWORKOS_") or key == "PYTHONPATH":
            env[key] = value
    return env


@dataclass
class Result:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    command: str = ""
    duration_ms: int = 0

    def raise_for_status(self, message: str) -> "Result":
        if not self.ok:
            raise CommandError(
                message,
                returncode=self.returncode,
                stderr=(self.stderr or self.stdout).strip()[:500],
                command=self.command,
            )
        return self


def binary(name: str) -> str | None:
    """Resolve an allowlisted program, honouring PATH placement changes."""
    for candidate in BINARIES.get(name, ()):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which(name)
    if found and os.path.isabs(found):
        return found
    return None


def available(name: str) -> bool:
    return binary(name) is not None


def run(
    argv: Sequence[str],
    *,
    timeout: float = 10.0,
    input_text: str | None = None,
    check: bool = False,
    cwd: str = "/",
) -> Result:
    """Execute an allowlisted program without a shell."""
    if not argv:
        raise ValueError("empty command")
    program = str(argv[0])
    if not os.path.isabs(program):  # never let a caller smuggle in a bare name
        resolved = binary(program)
        if not resolved:
            raise CommandError(f"Required program '{program}' is not installed.", command=program)
        program = resolved
    else:
        allowed = {path for paths_ in BINARIES.values() for path in paths_}
        # The privileged helper is a product-owned path outside the binary
        # allowlist. Without this exception ctl() refused to run whenever the
        # caller was already root (for example the boot-time net unit, or any
        # process started by the installer), which surfaced as a 500 on the
        # dashboard overview page.
        product_paths = {str(paths.CTL_PATH), str(paths.INSTALL_ROOT)}
        if (
            program not in allowed
            and not program.startswith(str(paths.INSTALL_ROOT))
            and program not in product_paths
        ):
            raise CommandError("Refusing to execute a program outside the allowlist.", command=program)

    started = time.monotonic()
    try:
        completed = subprocess.run(
            [program, *[str(arg) for arg in argv[1:]]],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_environment(),
            cwd=cwd,
            input=input_text,
            shell=False,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        # The allowlist resolved a path that no longer exists (uninstalled
        # package, or a PATH that differs from the caller's). Report it as a
        # missing program instead of leaking "No such file or directory".
        raise CommandError(f"Required program '{program}' could not be found.", command=program) from exc
    except subprocess.TimeoutExpired:
        raise CommandError("The operation timed out.", command=os.path.basename(program))
    except (OSError, ValueError) as exc:
        raise CommandError(f"Could not run {os.path.basename(program)}: {exc}", command=os.path.basename(program))

    result = Result(
        ok=completed.returncode == 0,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        command=" ".join([os.path.basename(program), *[str(a) for a in argv[1:]]])[:300],
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    if check and not result.ok:
        result.raise_for_status("The command failed.")
    return result


# --------------------------------------------------------------------------- #
# Privileged helper
# --------------------------------------------------------------------------- #
def ctl_argv() -> list[str]:
    """Return the argument vector prefix for the privileged CLI."""
    if os.environ.get("NETWORKOS_DEV_CTL") == "1":
        python = binary("python3") or "/usr/bin/python3"
        return [python, "-m", "networkos.cli"]
    if os.geteuid() == 0:
        return [str(paths.CTL_PATH)]
    sudo = binary("sudo") or "/usr/bin/sudo"
    return [sudo, "-n", str(paths.CTL_PATH)]


def ctl(*args: str, timeout: float = 90.0, input_text: str | None = None) -> Result:
    """Run a privileged NetworkOS command through the sudo allowlist."""
    argv = [*ctl_argv(), *args]
    result = run(argv, timeout=timeout, input_text=input_text)
    if not result.ok and "password is required" in result.stderr.lower():
        raise CommandError(
            "The dashboard service is not authorised to perform that action. "
            "Re-run the installer to repair the sudo policy.",
            returncode=result.returncode,
            command="networkosctl " + " ".join(args),
        )
    return result


def is_root() -> bool:
    return os.geteuid() == 0


def command_available(names: Iterable[str]) -> dict[str, bool]:
    return {name: available(name) for name in names}
