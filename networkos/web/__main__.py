"""Entry point for ``python -m networkos.web`` (used by the systemd unit)."""

from __future__ import annotations

from . import serve

if __name__ == "__main__":
    serve()
