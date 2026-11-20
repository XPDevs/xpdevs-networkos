"""CPU, memory, storage, temperature and traffic readings from the kernel.

Everything here is read from /proc and /sys: no third-party agent, no polling
daemon.  Rates are derived from the delta between two calls, so an idle Pi does
no work at all when nobody is looking at the dashboard.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .. import util

_LOCK = threading.Lock()
_CPU_PREV: dict[str, tuple[float, int]] = {}
_TRAFFIC_PREV: dict[str, tuple[float, int, int]] = {}
_HISTORY: deque[dict[str, Any]] = deque(maxlen=60)


# --------------------------------------------------------------------------- #
# CPU
# --------------------------------------------------------------------------- #
def cpu_times() -> dict[str, int]:
    """Read aggregate CPU jiffies from /proc/stat."""
    totals = {"user": 0, "nice": 0, "system": 0, "idle": 0, "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
    try:
        with open("/proc/stat", "r", encoding="utf-8") as handle:
            first = handle.readline().split()
    except OSError:
        return totals
    if not first or first[0] != "cpu":
        return totals
    keys = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"]
    for key, raw in zip(keys, first[1:]):
        try:
            totals[key] = int(raw)
        except ValueError:
            totals[key] = 0
    return totals


def cpu_usage() -> dict[str, Any]:
    """Percentage CPU utilisation since the previous call (real measurement)."""
    now = time.monotonic()
    times = cpu_times()
    total = sum(times.values())
    idle = times["idle"] + times["iowait"]
    cores = os.cpu_count() or 1
    with _LOCK:
        prev = _CPU_PREV.get("all")
        _CPU_PREV["all"] = (now, total, idle)  # type: ignore[assignment]
    if not prev:
        return {"percent": None, "window": 0.0, "cores": cores, "load": load_average()}
    prev_ts, prev_total, prev_idle = prev[0], prev[1], prev[2]  # type: ignore[misc]
    window = max(0.001, now - prev_ts)
    total_delta = total - prev_total
    idle_delta = idle - prev_idle
    percent = None
    if total_delta > 0:
        percent = round(util.clamp(100.0 * (1.0 - idle_delta / total_delta), 0.0, 100.0), 1)
    return {"percent": percent, "window": round(window, 2), "cores": cores, "load": load_average()}


def load_average() -> dict[str, float]:
    try:
        one, five, fifteen = os.getloadavg()
        return {"1m": round(one, 2), "5m": round(five, 2), "15m": round(fifteen, 2)}
    except OSError:
        return {"1m": 0.0, "5m": 0.0, "15m": 0.0}


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #
def memory() -> dict[str, Any]:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    try:
                        info[parts[0][:-1]] = int(parts[1]) * 1024
                    except ValueError:
                        continue
    except OSError:
        return {"total": 0, "used": 0, "available": 0, "percent": 0, "buffers": 0, "cached": 0, "swap_total": 0, "swap_used": 0}

    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable") or info.get("MemFree", 0)
    buffers = info.get("Buffers", 0)
    cached = info.get("Cached", 0)
    used = max(0, total - available)
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    return {
        "total": total,
        "used": used,
        "available": available,
        "buffers": buffers,
        "cached": cached,
        "percent": util.percent(used, total),
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - swap_free),
    }


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def storage(path: str = "/") -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"total": 0, "used": 0, "free": 0, "percent": 0}
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": util.percent(usage.used, usage.total),
    }


def product_footprint() -> dict[str, Any]:
    """Size of the installed product plus its database (disk budget check)."""
    total = 0
    for root in ("/opt/xpdevs-networkos", "/var/lib/xpdevs-networkos"):
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                try:
                    total += os.stat(os.path.join(dirpath, name)).st_size
                except OSError:
                    continue
    return {"bytes": total, "human": util.human_bytes(total)}


# --------------------------------------------------------------------------- #
# Temperature
# --------------------------------------------------------------------------- #
_THERMAL_CANDIDATES = (
    "/sys/class/thermal/thermal_zone0/temp",
    "/sys/class/hwmon/hwmon0/temp1_input",
    "/sys/devices/virtual/thermal/thermal_zone0/temp",
)


def temperature() -> dict[str, Any]:
    for path in _THERMAL_CANDIDATES:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        celsius = value / 1000.0 if value > 1000 else float(value)
        if -40 <= celsius <= 150:
            return {"celsius": round(celsius, 1), "source": path, "available": True}
    return {"celsius": None, "source": None, "available": False}


def thermal_state(celsius: float | None) -> str:
    if celsius is None:
        return "unknown"
    if celsius >= 80:
        return "critical"
    if celsius >= 70:
        return "hot"
    if celsius >= 60:
        return "warm"
    return "normal"


# --------------------------------------------------------------------------- #
# Uptime / boot
# --------------------------------------------------------------------------- #
def uptime() -> dict[str, Any]:
    seconds = 0.0
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            seconds = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return {"seconds": int(seconds), "human": util.format_duration(seconds), "booted_at": util.iso(time.time() - seconds)}


# --------------------------------------------------------------------------- #
# Network counters and live traffic
# --------------------------------------------------------------------------- #
def interface_counters() -> dict[str, dict[str, int]]:
    counters: dict[str, dict[str, int]] = {}
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as handle:
            lines = handle.readlines()[2:]
    except OSError:
        return counters
    for line in lines:
        if ":" not in line:
            continue
        name, data = line.split(":", 1)
        fields = data.split()
        if len(fields) < 16:
            continue
        try:
            counters[name.strip()] = {
                "rx_bytes": int(fields[0]),
                "rx_packets": int(fields[1]),
                "rx_errors": int(fields[2]),
                "rx_dropped": int(fields[3]),
                "tx_bytes": int(fields[8]),
                "tx_packets": int(fields[9]),
                "tx_errors": int(fields[10]),
                "tx_dropped": int(fields[11]),
            }
        except (ValueError, IndexError):
            continue
    return counters


def traffic(interfaces: list[str], *, history: bool = True) -> dict[str, Any]:
    """Bytes/second and cumulative totals per interface."""
    now = time.monotonic()
    counters = interface_counters()
    result: dict[str, Any] = {}
    with _LOCK:
        for name in interfaces:
            data = counters.get(name)
            if not data:
                result[name] = {
                    "available": False,
                    "rx_bytes": 0,
                    "tx_bytes": 0,
                    "rx_rate": None,
                    "tx_rate": None,
                }
                continue
            previous = _TRAFFIC_PREV.get(name)
            rx_rate = tx_rate = None
            if previous:
                prev_ts, prev_rx, prev_tx = previous
                elapsed = max(0.001, now - prev_ts)
                rx_rate = max(0, int((data["rx_bytes"] - prev_rx) / elapsed))
                tx_rate = max(0, int((data["tx_bytes"] - prev_tx) / elapsed))
            _TRAFFIC_PREV[name] = (now, data["rx_bytes"], data["tx_bytes"])
            result[name] = {
                "available": True,
                "rx_bytes": data["rx_bytes"],
                "tx_bytes": data["tx_bytes"],
                "rx_rate": rx_rate,
                "tx_rate": tx_rate,
                "rx_errors": data["rx_errors"],
                "tx_errors": data["tx_errors"],
            }
        if history:
            total_rx = sum(entry.get("rx_rate") or 0 for entry in result.values())
            total_tx = sum(entry.get("tx_rate") or 0 for entry in result.values())
            _HISTORY.append({"ts": time.time(), "rx": total_rx, "tx": total_tx})
    return result


def traffic_history() -> list[dict[str, Any]]:
    with _LOCK:
        return list(_HISTORY)


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #
def snapshot(interfaces: list[str]) -> dict[str, Any]:
    cpu = cpu_usage()
    mem = memory()
    disk = storage("/")
    temp = temperature()
    return {
        "cpu": cpu,
        "memory": mem,
        "storage": disk,
        "temperature": {**temp, "state": thermal_state(temp.get("celsius"))},
        "uptime": uptime(),
        "traffic": traffic(interfaces),
        "history": traffic_history(),
        "load": cpu["load"],
    }
