"""Queue and build state: built.json, pending.json, failed.json and friends."""

import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .util import _in_blacklist, _load_json_file, _save_json_file, vercmp

log = logging.getLogger("buildbot")


def get_built_state(state_path: str) -> dict:
    """Load built.json -> {pkgname: {version, pkgrel, built_at, pkg_files}}."""
    return _load_json_file(state_path, {}, "built state")


def save_built_state(state_path: str, state: dict):
    """Atomically write built.json."""
    _save_json_file(state_path, state)


# Statuses that mean "don't queue this package for a rebuild right now."
_DEFERRED_STATUSES = frozenset({
    "ineligible",           # arch=any or otherwise not rebuildable
    "pending_upstream",     # PKGBUILD is older than installed; waiting for upstream
    "pending_world_cascade",# built and staged; waiting for soname to land in world repos
})


def strip_local_pkgrel_bump(version: str) -> str:
    """Normalize a dot-bumped pkgrel back to upstream version form.

    A version like '1.2.3-1.1' becomes '1.2.3-1'. The dotted pkgrel comes from
    the client side — the installed package was built by a vendor/helper (e.g.
    CachyOS) at a sub-pkgrel that the server rebuilds at the plain pkgrel. Every
    version comparison normalizes through this so a plain server rebuild isn't
    seen as perpetually behind the dotted installed version.
    """
    if "-" not in version:
        return version

    pkgver, pkgrel = version.rsplit("-", 1)
    if "." not in pkgrel:
        return version

    parts = pkgrel.split(".")
    if len(parts) < 2 or not parts[-1].isdigit():
        return version

    base_pkgrel = ".".join(parts[:-1])
    if not base_pkgrel:
        return version
    return f"{pkgver}-{base_pkgrel}"


def diff_manifest(
    manifest: list,
    built: dict,
    blacklist: list = None,
    pkgbase_map: dict = None,
) -> list[dict]:
    """Return packages needing a build: new or version-changed."""
    blacklist = blacklist or []
    todo = []
    for pkg in manifest:
        name = pkg["name"]

        # Skip AUR/local packages (not in any sync database)
        if pkg.get("repo") == "unknown":
            continue

        # Skip blacklisted packages and split packages whose pkgbase is blacklisted
        if _in_blacklist(name, blacklist):
            continue
        if pkgbase_map:
            pkgbase = pkgbase_map.get(name)
            if pkgbase and pkgbase != name and _in_blacklist(pkgbase, blacklist):
                continue

        if name not in built:
            todo.append({**pkg, "build_reason": "new"})
            continue

        # Normalize the installed version's dotted pkgrel before comparing, the
        # same way every other comparison path does. Without this a package
        # installed at e.g. '1.2.3-1.1' but rebuilt by the server at '1.2.3-1'
        # would be re-queued every cycle forever.
        if vercmp(strip_local_pkgrel_bump(pkg["version"]), built[name]["version"]) > 0:
            todo.append({**pkg, "build_reason": "update"})

    return todo


def inject_always_build(manifest: list, config: dict) -> list:
    """Append virtual manifest entries for config['always_build'] packages.

    These are packages the build server should build and keep in the repo even
    when they are not installed on the client. Injecting them into the in-memory
    manifest each cycle makes the rest of the pipeline treat them as installed:
    diff_manifest builds them, check_upstream_updates rebuilds on upstream bumps,
    and prune_uninstalled_from_repo no longer deletes them (their name is now in
    manifest_names). Because the injection is in memory only, an rsync of the
    real client manifest can't clobber it.

    The sentinel version "0" makes a never-built entry resolve as "new" while a
    built one won't re-trigger diff_manifest (upstream-bump rebuilds are handled
    by check_upstream_updates against the built version). Mutates and returns the
    list. A name already present in the manifest (genuinely installed) is skipped.
    """
    extra = config.get("always_build") or []
    if not extra:
        return manifest
    present = {p["name"] for p in manifest}
    for name in extra:
        if name in present:
            continue
        manifest.append({
            "name": name,
            "version": "0",
            "repo": "always_build",
            "reason": "always_build",
        })
        present.add(name)
    return manifest


def update_built_state(
    state: dict, pkg: dict, new_version: str, pkg_files: list[str],
    all_pkgnames: list[str] = None,
    pgp_skipped: bool = False,
) -> dict:
    """Record a successful build in the state dict.
    If all_pkgnames is provided (split packages), records all subpackages."""
    ver_parts = new_version.split("-")
    pkgrel = ver_parts[-1] if len(ver_parts) >= 2 else ""

    entry = {
        "version": new_version,
        "pkgrel": pkgrel,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "pkg_files": [os.path.basename(f) for f in pkg_files],
    }
    if pgp_skipped:
        entry["pgp_skipped"] = True

    state[pkg["name"]] = entry

    # Record all subpackages from the same pkgbase so they aren't re-queued
    if all_pkgnames:
        for subpkg in all_pkgnames:
            if subpkg != pkg["name"]:
                state[subpkg] = entry.copy()

    return state


def load_failed(failed_path: str) -> dict:
    """Load failed.json -> {pkgname: {version, reason, timestamp, retries}}."""
    return _load_json_file(failed_path, {}, "failed queue")


def save_failed(failed_path: str, failed: dict):
    """Atomically write failed.json."""
    _save_json_file(failed_path, failed)


def load_pending(pending_path: str) -> list[dict]:
    """Load pending.json."""
    return _load_json_file(pending_path, [], "pending queue")


def save_pending(pending_path: str, pending: list[dict]):
    """Atomically write pending.json."""
    _save_json_file(pending_path, pending)


def prune_stale_queue_entries(config: dict, manifest_names: set) -> tuple[int, int]:
    """
    Remove pending and failed entries for packages no longer in the manifest.
    Returns (pruned_pending, pruned_failed).
    """
    with _queue_lock(config):
        pending = load_pending(config["pending_path"])
        new_pending = [p for p in pending if p.get("name") in manifest_names]
        pruned_pending = len(pending) - len(new_pending)

        failed = load_failed(config["failed_path"])
        new_failed = {k: v for k, v in failed.items() if k in manifest_names}
        pruned_failed = len(failed) - len(new_failed)

        if pruned_pending:
            save_pending(config["pending_path"], new_pending)
        if pruned_failed:
            save_failed(config["failed_path"], new_failed)

    return pruned_pending, pruned_failed


def write_metrics(config: dict, payload: dict):
    """Write lightweight runtime metrics for external monitoring."""
    tmp = config["metrics_path"] + ".tmp"
    os.makedirs(os.path.dirname(config["metrics_path"]), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, config["metrics_path"])


def write_in_progress(config: dict, pkg: dict):
    """Persist currently active package to recover from abrupt restarts."""
    path = config["in_progress_path"]
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = dict(pkg)
    payload["started_at"] = datetime.now(timezone.utc).isoformat()
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_in_progress(config: dict):
    path = config["in_progress_path"]
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            raw = f.read().strip()
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or "name" not in data:
            return None
        return data
    except Exception:
        return None


def clear_in_progress(config: dict):
    try:
        os.remove(config["in_progress_path"])
    except FileNotFoundError:
        pass


_lock_depth = 0


@contextmanager
def _queue_lock(config: dict, timeout: int = 60):
    """Hold the lock over built.json, pending.json and failed.json.

    Every read-modify-write of those files, by the daemon or the CLI, happens
    inside this lock, so `buildbot retry` and friends are safe while the daemon
    runs. Nests within a process: a function that takes it may call another
    that does. Never hold it across a build or a network fetch.
    """
    global _lock_depth
    if _lock_depth:
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
        return

    lock_path = os.path.join(os.path.dirname(config["state_path"]), "queue.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "w") as lockf:
        start = time.time()
        while True:
            try:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() - start > timeout:
                    raise RuntimeError("timed out waiting for queue lock")
                time.sleep(0.1)
        _lock_depth = 1
        try:
            yield
        finally:
            _lock_depth = 0
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def _daemon_lock_path(config: dict) -> str:
    return os.path.join(os.path.dirname(config["state_path"]), "daemon.pid")


def acquire_daemon_lock(config: dict):
    """Claim the daemon slot for this process and record its PID.

    Returns the open lock file, which must stay open for the daemon's lifetime,
    or None if another daemon already holds it. The lock is what the CLI checks
    to tell whether the daemon is running, on any init system.
    """
    path = _daemon_lock_path(config)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()}\n")
    f.flush()
    return f


def daemon_pid(config: dict) -> int:
    """PID of the running daemon, or 0 if none holds the daemon lock."""
    try:
        f = open(_daemon_lock_path(config))
    except OSError:
        return 0
    with f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pid = f.read().strip()
            return int(pid) if pid.isdigit() else 0
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return 0


def _queue_item_for(name: str, manifest_map: dict, fallback_version: str = "unknown") -> dict:
    if name in manifest_map:
        pkg = dict(manifest_map[name])
    else:
        pkg = {"name": name, "version": fallback_version, "repo": "unknown", "reason": "unknown"}
    pkg["build_reason"] = pkg.get("build_reason", "retry")
    return pkg


def _is_stalled(rec: dict, config: dict) -> bool:
    """True if a failed record has exceeded the retry or age stall thresholds."""
    stall_retries = config.get("failed_stall_retries", 5)
    if stall_retries > 0 and rec.get("retries", 0) >= stall_retries:
        return True
    stall_days = config.get("failed_stall_days", 7)
    if stall_days > 0:
        ts = rec.get("first_failed_at") or rec.get("timestamp", "")
        if ts:
            try:
                first = datetime.fromisoformat(ts)
                if (datetime.now(timezone.utc) - first).days >= stall_days:
                    return True
            except Exception:
                pass
    return False


# Minimum wait before a failed package may build again, indexed by how many
# times it has already failed. A mirror hiccup or a hung build clears on its
# own, so those start short; a compile failure needs a human or an upstream
# change, so it starts long. The last entry repeats until failed_stall_retries
# trips and the stall / auto-retry path takes over.
_RETRY_BACKOFF_HOURS = {
    "download":       [1, 3, 8, 24],
    "timeout":        [1, 3, 8, 24],
    "dep":            [1, 6, 24],
    "missing_source": [1, 6, 24],
}


_RETRY_BACKOFF_DEFAULT = [6, 24, 48]


def _retry_due(rec: dict, config: dict) -> bool:
    """True if a failed record has waited out its backoff and may build again.

    Both queue paths skip a package whose failed version matches the version
    they are about to queue. Without a backoff that skip never expires on its
    own: the record only clears once it goes stalled at failed_stall_days and
    the auto-retry path picks it up, so a single transient download error
    parked a package for a week and a recorded failed_tier never got its
    next-tier attempt.
    """
    if not rec:
        return False
    ts = rec.get("timestamp") or rec.get("first_failed_at")
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(ts)
    except ValueError:
        return False
    schedule = _RETRY_BACKOFF_HOURS.get(rec.get("error_type"), _RETRY_BACKOFF_DEFAULT)
    idx = min(max(rec.get("retries", 1), 1) - 1, len(schedule) - 1)
    return (datetime.now(timezone.utc) - last) >= timedelta(hours=schedule[idx])


def _record_failure(config: dict, name: str, version: str, reason: str,
                    error_type: str, failed_tier: str = None,
                    log_path: str = None) -> None:
    with _queue_lock(config):
        failed = load_failed(config["failed_path"])
        prior = failed.get(name, {})
        _now = datetime.now(timezone.utc).isoformat()
        rec = {
            "version": version,
            "reason": reason,
            "timestamp": _now,
            "first_failed_at": prior.get("first_failed_at", _now),
            "retries": prior.get("retries", 0) + 1,
            "error_type": error_type,
        }
        if failed_tier is not None:
            rec["failed_tier"] = failed_tier
        if log_path is not None:
            rec["log_path"] = log_path
        failed[name] = rec
        save_failed(config["failed_path"], failed)
