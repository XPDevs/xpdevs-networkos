"""Configuration snapshots used for rollback.

A backup is a gzipped tar containing ``config.toml`` and the rendered
``generated/`` directory.  Restores are path-validated so a crafted archive
name can never write outside the configuration directory.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import tarfile
import time
from pathlib import Path
from typing import Any

from . import __version__, paths

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9-]{1,24}\.tar\.gz$")
ALLOWED_MEMBERS = ("config.toml", "manifest.json")
ALLOWED_PREFIX = "generated/"
KEEP = 10


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def create(label: str = "auto") -> str | None:
    """Snapshot the current configuration. Returns the backup name."""
    clean = re.sub(r"[^a-z0-9-]", "-", str(label).lower())[:24] or "auto"
    paths.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{_timestamp()}-{clean}.tar.gz"
    target = paths.BACKUP_DIR / name
    manifest = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "label": clean,
        "version": __version__,
    }
    try:
        with tarfile.open(target, "w:gz") as archive:
            if paths.CONFIG_FILE.exists():
                archive.add(paths.CONFIG_FILE, arcname="config.toml")
            if paths.GENERATED_DIR.exists():
                archive.add(paths.GENERATED_DIR, arcname="generated")
            payload = json.dumps(manifest, indent=2).encode("utf-8")
            info = tarfile.TarInfo("manifest.json")
            info.size = len(payload)
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(payload))
        os.chmod(target, 0o600)
    except (OSError, tarfile.TarError) as exc:
        log.error("could not create configuration backup: %s", exc)
        return None
    prune()
    return name


def list_backups() -> list[dict[str, Any]]:
    if not paths.BACKUP_DIR.exists():
        return []
    items: list[dict[str, Any]] = []
    for entry in sorted(paths.BACKUP_DIR.glob("*.tar.gz"), reverse=True):
        try:
            stat = entry.stat()
        except OSError:
            continue
        label = entry.name.split("-", 2)[-1].removesuffix(".tar.gz")
        items.append(
            {
                "name": entry.name,
                "label": label,
                "size": stat.st_size,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
            }
        )
    return items


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    safe: list[tarfile.TarInfo] = []
    for member in archive.getmembers():
        name = member.name.lstrip("./")
        if member.issym() or member.islnk() or not member.isfile():
            continue
        if name in ALLOWED_MEMBERS or (name.startswith(ALLOWED_PREFIX) and ".." not in name):
            safe.append(member)
    return safe


def restore(name: str) -> dict[str, Any]:
    """Restore a snapshot. Raises ValueError on an invalid or unsafe archive."""
    if not NAME_RE.match(name):
        raise ValueError("Invalid backup name.")
    source = paths.BACKUP_DIR / name
    if not source.exists():
        raise ValueError("That backup does not exist.")
    paths.GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    restored: list[str] = []
    with tarfile.open(source, "r:gz") as archive:
        members = _safe_members(archive)
        for member in members:
            cleaned = member.name.lstrip("./")
            if cleaned == "config.toml":
                destination = paths.CONFIG_FILE
            else:
                destination = paths.ETC_DIR / cleaned
            destination.parent.mkdir(parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            data = extracted.read()
            temp = destination.with_suffix(destination.suffix + ".restore")
            temp.write_bytes(data)
            os.replace(temp, str(destination))
            restored.append(cleaned)
    if "config.toml" not in restored:
        raise ValueError("The backup does not contain a configuration file.")
    try:
        from . import settings

        settings.invalidate_cache()
    except Exception:  # pragma: no cover
        pass
    return {"name": name, "restored": restored}


def prune(keep: int = KEEP) -> None:
    if not paths.BACKUP_DIR.exists():
        return
    entries = sorted(paths.BACKUP_DIR.glob("*.tar.gz"))
    for entry in entries[: max(0, len(entries) - keep)]:
        try:
            entry.unlink()
        except OSError:
            pass


def latest() -> str | None:
    items = list_backups()
    return items[0]["name"] if items else None


def path_of(name: str) -> Path | None:
    if not NAME_RE.match(name):
        return None
    candidate = paths.BACKUP_DIR / name
    return candidate if candidate.exists() else None
