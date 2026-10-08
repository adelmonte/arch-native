"""Read the host pacman DBs and the client manifest."""

import json
import logging
import os

from .util import _db_field, _parse_desc_field

log = logging.getLogger("buildbot")


def load_manifest(path: str) -> list[dict]:
    """Load and validate a JSON package manifest."""
    with open(path, "r") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Manifest must be a JSON array, got {type(data).__name__}")
    required = {"name", "version", "repo", "reason"}
    for i, entry in enumerate(data):
        missing = required - set(entry.keys())
        if missing:
            raise ValueError(f"Manifest entry {i} missing keys: {missing}")
    return data


def read_local_packages(db_path: str = "/var/lib/pacman") -> list[dict]:
    """
    Read installed packages directly from the local pacman database.
    Returns the same [{name, version, repo, reason}] format as load_manifest.
    Used in local mode instead of the rsync'd client manifest.
    """
    import tarfile

    local_db = os.path.join(db_path, "local")
    sync_dir  = os.path.join(db_path, "sync")

    # Build repo map from sync databases: pkgname -> repo name
    repo_map: dict[str, str] = {}
    if os.path.isdir(sync_dir):
        for db_file in os.listdir(sync_dir):
            if not db_file.endswith(".db"):
                continue
            repo_name = db_file[:-3]  # strip .db
            db_full = os.path.join(sync_dir, db_file)
            try:
                with tarfile.open(db_full) as tf:
                    for member in tf.getmembers():
                        if not member.name.endswith("/desc"):
                            continue
                        f = tf.extractfile(member)
                        if f is None:
                            continue
                        content = f.read().decode("utf-8", errors="replace")
                        name = _parse_desc_field(content, "NAME")
                        if name:
                            repo_map[name] = repo_name
            except Exception as e:
                log.debug("Error reading sync DB %s: %s", db_file, e)

    packages = []
    if not os.path.isdir(local_db):
        log.warning("Local pacman DB not found: %s", local_db)
        return packages

    for entry in os.scandir(local_db):
        if not entry.is_dir():
            continue
        desc_path = os.path.join(entry.path, "desc")
        if not os.path.isfile(desc_path):
            continue
        try:
            with open(desc_path, "r", errors="replace") as f:
                content = f.read()
            name    = _parse_desc_field(content, "NAME")
            version = _parse_desc_field(content, "VERSION")
            reason_raw = _parse_desc_field(content, "REASON")
            if not name or not version:
                continue
            # REASON: 0 = explicit, 1 = dependency (field absent = explicit)
            reason = "dependency" if reason_raw == "1" else "explicit"
            repo   = repo_map.get(name, "unknown")
            packages.append({
                "name":    name,
                "version": version,
                "repo":    repo,
                "reason":  reason,
            })
        except Exception as e:
            log.debug("Error reading pacman entry %s: %s", entry.name, e)

    log.info("Read %d installed packages from local pacman DB", len(packages))
    return packages


def build_pkgbase_map() -> dict[str, str]:
    """
    Build pkgname->pkgbase mapping from pacman sync databases.
    Only includes entries where pkgname != pkgbase (i.e. split packages).
    """
    import tarfile

    mapping = {}
    sync_dir = "/var/lib/pacman/sync"
    if not os.path.isdir(sync_dir):
        log.warning("Sync DB dir not found: %s", sync_dir)
        return mapping

    for db_file in os.listdir(sync_dir):
        if not db_file.endswith(".db"):
            continue
        db_path = os.path.join(sync_dir, db_file)
        try:
            with tarfile.open(db_path) as tf:
                for member in tf.getmembers():
                    if not member.name.endswith("/desc"):
                        continue
                    f = tf.extractfile(member)
                    if f is None:
                        continue
                    content = f.read().decode("utf-8", errors="replace")
                    name = _parse_desc_field(content, "NAME")
                    base = _parse_desc_field(content, "BASE")
                    if name and base and name != base:
                        mapping[name] = base
        except Exception as e:
            log.debug("Error parsing sync DB %s: %s", db_file, e)

    log.info("Built pkgbase map: %d split-package entries", len(mapping))
    return mapping


def _manifest_map(config: dict) -> dict:
    manifest = (read_local_packages() if config.get("mode") == "local"
                else load_manifest(config["manifest_path"]))
    return {p["name"]: p for p in manifest}


def _read_forge_db(repo_db_path: str) -> dict:
    """Parse a pacman repo db tarball. Returns {pkgname: {version, filename}}.
    If a name has multiple entries (DB corruption), 'dup_versions' lists the extras."""
    import tarfile
    seen: dict = {}
    if not os.path.exists(repo_db_path):
        return seen
    try:
        with tarfile.open(repo_db_path) as tf:
            for member in tf.getmembers():
                if not member.name.endswith("/desc"):
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode("utf-8", errors="replace")
                name = _db_field(content, "NAME")
                version = _db_field(content, "VERSION")
                filename = _db_field(content, "FILENAME")
                if name:
                    seen.setdefault(name, []).append({"version": version or "", "filename": filename or ""})
    except Exception as e:
        log.warning("Error reading forge DB %s: %s", repo_db_path, e)
        return {}
    result = {}
    for name, entries in seen.items():
        result[name] = entries[-1]
        if len(entries) > 1:
            result[name]["dup_versions"] = entries[:-1]
    return result


def _installed_names(config: dict):
    """Set of installed package names, or None if the manifest is unavailable."""
    try:
        if config.get("mode") == "local":
            manifest = read_local_packages()
        else:
            manifest = load_manifest(config["manifest_path"])
        return {pkg["name"] for pkg in manifest}
    except (FileNotFoundError, ValueError):
        return None
