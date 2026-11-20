"""XPDevs NetworkOS - lightweight network operating system for the Raspberry Pi.

Backend package. Target hardware: Raspberry Pi 3 Model B (1 GB RAM),
Raspberry Pi OS Lite 64-bit.
"""

from pathlib import Path

VENDOR = "XPDevs"
VENDOR_URL = "https://xpdevs.github.io/"
PRODUCT_NAME = "XPDevs NetworkOS"
PRODUCT_SHORT = "NetworkOS"
PRODUCT_TAGLINE = "Wireless routing, DHCP and network-wide ad blocking for the Raspberry Pi"
PRODUCT_DESCRIPTION = (
    "A lightweight, self-hosted network operating system that turns a Raspberry Pi "
    "into a configurable wireless router, access point and network-wide advertisement blocker."
)

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def _read_version() -> str:
    try:
        text = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "1.0.0"
    return text or "1.0.0"


__version__ = _read_version()
USER_AGENT = f"{PRODUCT_NAME}/{__version__} ({VENDOR_URL})"
