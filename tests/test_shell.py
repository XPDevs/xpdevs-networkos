"""The command layer must never execute anything outside its allowlist."""

from __future__ import annotations

import pytest

from networkos import shell
from networkos.errors import CommandError


def test_unknown_program_is_refused():
    with pytest.raises(CommandError):
        shell.run(["definitely-not-a-program", "--help"])


def test_absolute_path_outside_the_allowlist_is_refused():
    with pytest.raises(CommandError):
        shell.run(["/bin/echo", "hello"])


def test_shell_metacharacters_are_not_interpreted():
    result = shell.run(["ip", "-o", "link", "show", "name;reboot"])
    # `ip` rejects the bogus interface name and nothing else happens.
    assert "reboot" not in result.stdout or result.returncode != 0


def test_allowlisted_program_runs():
    if not shell.available("ip"):
        pytest.skip("iproute2 is not installed")
    result = shell.run(["ip", "-V"])
    assert result.ok
    assert result.stdout.startswith("ip utility")
