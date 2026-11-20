"""Hardware and operating-system identification."""

from __future__ import annotations

import os
import platform
import re
import time
from typing import Any

from .. import PRODUCT_NAME, __version__, shell, util
from . import resources


def _read_first(paths: tuple[str, ...]) -> str | None:
    for path in paths:
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        text = raw.replace(b"\x00", b"").decode("utf-8", "replace").strip()
        if text:
            return text
    return None


def model() -> dict[str, Any]:
    """Raspberry Pi model, with an honest description on other hardware."""
    raw = _read_first(
        (
            "/proc/device-tree/model",
            "/sys/firmware/devicetree/base/model",
            "/sys/class/dmi/id/product_name",
        )
    )
    serial = _read_first(("/proc/device-tree/serial-number", "/sys/firmware/devicetree/base/serial-number"))
    if raw:
        is_pi = "raspberry pi" in raw.lower()
        return {
            "name": raw,
            "is_raspberry_pi": is_pi,
            "serial": serial if is_pi else None,
            "detected_from": "device-tree",
        }
    return {
        "name": f"Generic {platform.machine()} system",
        "is_raspberry_pi": False,
        "serial": None,
        "detected_from": "fallback",
    }


def os_release() -> dict[str, Any]:
    data: dict[str, str] = {}
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as handle:
            for line in handle:
                if "=" in line:
                    key, value = line.rstrip("\n").split("=", 1)
                    data[key] = value.strip().strip('"')
    except OSError:
        pass
    return {
        "name": data.get("PRETTY_NAME") or data.get("NAME") or "Unknown Linux",
        "id": data.get("ID", ""),
        "version": data.get("VERSION_ID", ""),
        "codename": data.get("VERSION_CODENAME", ""),
    }


def kernel() -> dict[str, Any]:
    uname = os.uname()
    return {
        "release": uname.release,
        "version": uname.version,
        "arch": uname.machine,
        "hostname": uname.nodename,
        "python": platform.python_version(),
    }


def firmware() -> dict[str, Any]:
    """Bootloader/firmware version on Raspberry Pi hardware."""
    if not shell.available("vcgencmd"):
        return {"available": False, "version": None, "throttled": None}
    result = shell.run(["vcgencmd", "version"], timeout=4)
    throttled = shell.run(["vcgencmd", "get_throttled"], timeout=4)
    flags = None
    if throttled.ok and "=" in throttled.stdout:
        raw = throttled.stdout.strip().split("=", 1)[1]
        try:
            value = int(raw, 16)
            flags = {
                "raw": raw,
                "undervoltage_now": bool(value & 0x1),
                "freq_capped_now": bool(value & 0x2),
                "throttled_now": bool(value & 0x4),
                "soft_temp_limit_now": bool(value & 0x8),
                "undervoltage_occurred": bool(value & 0x10000),
                "throttled_occurred": bool(value & 0x40000),
            }
        except ValueError:
            flags = None
    first_line = result.stdout.strip().splitlines()[0] if result.ok and result.stdout.strip() else None
    return {"available": True, "version": first_line, "throttled": flags}


def time_status() -> dict[str, Any]:
    return {
        "epoch": util.epoch_now(),
        "utc": util.iso(time.time()),
        "timezone": time.strftime("%Z"),
    }


def summary() -> dict[str, Any]:
    """Everything the System page and the Overview header need."""
    hw = model()
    memory = resources.memory()
    return {
        "product": {"name": PRODUCT_NAME, "version": __version__},
        "model": hw,
        "os": os_release(),
        "kernel": kernel(),
        "firmware": firmware(),
        "memory": memory,
        "storage": resources.storage("/"),
        "temperature": {**resources.temperature(), "state": resources.thermal_state(resources.temperature().get("celsius"))},
        "uptime": resources.uptime(),
        "load": resources.load_average(),
        "footprint": resources.product_footprint(),
        "time": time_status(),
        "cpu_count": os.cpu_count() or 1,
    }


MEMORY_TOTAL_RE = re.compile(r"^MemTotal:\s+(\d+)", re.MULTILINE)
