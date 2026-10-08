"""The build daemon main loop."""

import logging
import os
import queue as _thread_queue
import shutil
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from .build import build_package, generate_makepkg_conf, import_pgp_keys, prepare_gnupg_home, sign_packages, upgrade_chroot
from .config import load_config
from .pacman import build_pkgbase_map, load_manifest, read_local_packages
from .patches import write_patch_status
from .repo import _prune_cycle, _run_fsck, add_to_repo, stage_packages
from .resolve import check_upstream_updates, is_eligible, parse_srcinfo, resolve_pkgbuild
from .soname import _queue_soname_repairs, _resolve_pending_cascades, _soname_provides_from_pkg, sync_index
from .state import _is_stalled, _queue_lock, _record_failure, _retry_due, acquire_daemon_lock, clear_in_progress, daemon_pid, diff_manifest, get_built_state, inject_always_build, load_failed, load_in_progress, load_pending, prune_stale_queue_entries, save_built_state, save_failed, save_pending, strip_local_pkgrel_bump, update_built_state, write_in_progress, write_metrics
from .util import _fmt_srcinfo_ver, _git, _sanitize_reason, vercmp

log = logging.getLogger("buildbot")


shutdown_flag = False


wake_flag = False


def handle_sigterm(signum, frame):
    global shutdown_flag
    log.info("Received signal %d, finishing current work and shutting down...", signum)
    shutdown_flag = True


def handle_sigusr1(signum, frame):
    """Wake the daemon for an immediate build pass (sent by `buildbot sync`)."""
    global wake_flag
    wake_flag = True
    log.info("Received wake signal — will run an immediate build pass")


def prune_old_build_logs(log_dir: str, retention_days: int):
    """Remove per-package build logs older than retention_days."""
    if retention_days <= 0:
        return 0
    cutoff = time.time() - (retention_days * 86400)
    removed = 0
    for root, dirs, files in os.walk(log_dir):
        rel = os.path.relpath(root, log_dir)
        if rel == ".":
            continue
        for f in files:
            if not f.endswith(".log"):
                continue
            fp = os.path.join(root, f)
            try:
                if os.stat(fp).st_mtime < cutoff:
                    os.remove(fp)
                    removed += 1
            except FileNotFoundError:
                pass
            except Exception as e:
                log.debug("Failed pruning log %s: %s", fp, e)
    return removed


_STAT_KEYS = (
    "attempted", "succeeded", "failed",
    "skipped_previous_failure", "skipped_ineligible", "skipped_missing_keys",
)


def _write_metrics(config: dict, status: str, stats: dict, pending_end: int, **extra):
    write_metrics(config, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        **extra,
        "pending_start": stats.get("pending_start", 0),
        "pending_end": pending_end,
        **{k: stats.get(k, 0) for k in _STAT_KEYS},
    })


def _queue_from_manifest(config: dict, manifest: list, built: dict, pkgbase_map: dict) -> None:
    """Auto-retry stalled packages, diff manifest, and populate the build queue."""
    with _queue_lock(config):
        stall_auto_retry_days = config.get("stall_auto_retry_days", 3)
        if stall_auto_retry_days > 0:
            _failed_all = load_failed(config["failed_path"])
            _now_utc = datetime.now(timezone.utc)
            auto_retried = []
            for _pkgname, _rec in list(_failed_all.items()):
                if not _is_stalled(_rec, config):
                    continue
                _ts = _rec.get("timestamp") or _rec.get("first_failed_at", "")
                if not _ts:
                    continue
                try:
                    _last = datetime.fromisoformat(_ts)
                    if (_now_utc - _last).days >= stall_auto_retry_days:
                        del _failed_all[_pkgname]
                        auto_retried.append(_pkgname)
                except Exception:
                    pass
            if auto_retried:
                save_failed(config["failed_path"], _failed_all)
                log.info("Auto-retried %d stalled package(s): %s",
                         len(auto_retried), ", ".join(sorted(auto_retried)))

        new_pkgs = diff_manifest(manifest, built, config["blacklist"], pkgbase_map)
        if not new_pkgs:
            log.info("No new or updated packages to build")
            return

        pending = load_pending(config["pending_path"])
        failed_now = load_failed(config["failed_path"])
        pending_names = {p["name"] for p in pending}
        queued = queued_new = queued_update = skipped_existing = skipped_failed = skipped_stalled = 0
        for pkg in new_pkgs:
            if pkg["name"] in pending_names:
                skipped_existing += 1
                continue
            failed_rec = failed_now.get(pkg["name"], {})
            if failed_rec.get("version") == pkg["version"]:
                if not _retry_due(failed_rec, config):
                    skipped_failed += 1
                    continue
                log.info("[%s] failure backoff elapsed (%s, %d prior) — retrying",
                         pkg["name"], failed_rec.get("error_type", "build"),
                         failed_rec.get("retries", 0))
            if _is_stalled(failed_rec, config):
                skipped_stalled += 1
                continue
            pkg["queued_at"] = datetime.now(timezone.utc).isoformat()
            pending.append(pkg)
            queued += 1
            if pkg["build_reason"] == "new":
                queued_new += 1
            elif pkg["build_reason"] == "update":
                queued_update += 1
        save_pending(config["pending_path"], pending)
    log.info(
        "Manifest queue update: queued=%d (new=%d update=%d), "
        "skipped_existing=%d, skipped_failed=%d, skipped_stalled=%d",
        queued, queued_new, queued_update, skipped_existing, skipped_failed, skipped_stalled,
    )
    if skipped_stalled:
        log.warning(
            "%d package(s) stalled (>=%d failures or >=%d days) — run: buildbot failed",
            skipped_stalled, config.get("failed_stall_retries", 5), config.get("failed_stall_days", 7),
        )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def _setup_logging(config: dict, debug: bool):
    level = logging.DEBUG if debug else getattr(logging, config["log_level"])
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    os.makedirs(config["log_dir"], exist_ok=True)
    fh = logging.FileHandler(os.path.join(config["log_dir"], "buildbot.log"))
    fh.setFormatter(formatter)
    # stdout goes to the journal / init system log
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    log.setLevel(level)
    log.addHandler(fh)
    log.addHandler(sh)


def _deploy_chroot_files(config: dict):
    """Copy distro-specific files into the base chroot (once at startup)."""
    chroot_root = config["chroot_root"]
    if not os.path.isdir(chroot_root):
        return
    # artix-meson wrapper — only needed for Artix
    if config["distro"] == "artix":
        for src in (
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artix-meson"),
            "/usr/share/arch-native/artix-meson",
        ):
            if os.path.isfile(src):
                dest = os.path.join(chroot_root, "usr/local/bin/artix-meson")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                os.chmod(dest, 0o755)
                log.info("Deployed artix-meson into chroot")
                break

    # chroot pacman.conf — only redeploy if explicitly configured; `buildbot
    # init` handles the initial copy.
    pacman_conf_src = config.get("chroot_pacman_conf", "")
    if pacman_conf_src:
        if os.path.isfile(pacman_conf_src):
            shutil.copy2(pacman_conf_src, os.path.join(chroot_root, "etc/pacman.conf"))
            log.info("Deployed pacman.conf into chroot from %s", pacman_conf_src)
        else:
            log.warning("chroot_pacman_conf not found: %s", pacman_conf_src)


def _recover_interrupted(config: dict):
    """Put a build the previous run was killed during back at the queue head."""
    recovered = load_in_progress(config)
    if not recovered:
        return
    # A very old record means the build hung before the crash rather than
    # being cut short by a clean restart.
    started_at_str = recovered.get("started_at")
    if started_at_str:
        try:
            started_at = datetime.fromisoformat(started_at_str)
            age_secs = (datetime.now(timezone.utc) - started_at).total_seconds()
            timeout = config.get("build_timeout") or 0
            stale_threshold = timeout * 2 if timeout else 86400
            if age_secs > stale_threshold:
                log.warning(
                    "in_progress record for %s is %.1fh old (started_at=%s) — "
                    "may be from a hung build; re-queuing anyway",
                    recovered.get("name"), age_secs / 3600, started_at_str,
                )
        except Exception:
            pass
    with _queue_lock(config):
        pending = load_pending(config["pending_path"])
        if recovered.get("name") not in {p.get("name") for p in pending}:
            pending.insert(0, recovered)
            save_pending(config["pending_path"], pending)
            log.warning("Recovered interrupted package back to queue: %s", recovered.get("name"))
    clear_in_progress(config)


def _clean_stale_chroots(config: dict):
    """Remove leftovers of a killed run: the root chroot's pacman lock and build-* copies."""
    chroot_dir = config["chroot_dir"]
    # Left behind when the daemon is SIGKILL'd while arch-nspawn is
    # mid-upgrade; without cleanup every later build hangs on the lock.
    dblck = os.path.join(config["chroot_root"], "var", "lib", "pacman", "db.lck")
    if os.path.exists(dblck):
        try:
            os.remove(dblck)
            log.warning("Removed stale pacman lock from root chroot (previous run was interrupted mid-upgrade)")
        except OSError as e:
            log.warning("Could not remove stale root chroot lock %s: %s", dblck, e)

    try:
        for entry in os.listdir(chroot_dir):
            if not entry.startswith("build-"):
                continue
            path = os.path.join(chroot_dir, entry)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
                log.warning("Removed stale build chroot: %s", path)
            except OSError as e:
                log.warning("Could not remove stale build chroot %s: %s", path, e)
    except OSError as e:
        log.warning("Stale build chroot scan failed: %s", e)


# ---------------------------------------------------------------------------
# Queue refresh
# ---------------------------------------------------------------------------
def _refresh_manifest(config: dict, manifest: list, last_mtime: float) -> tuple[list, float]:
    """Return the current package list and the manifest mtime it was read at."""
    if config["mode"] == "local":
        # The pacman DB is cheap to read, so take it fresh every cycle; a
        # version-only upgrade must reach diff_manifest too.
        new_manifest = read_local_packages()
        if {(p["name"], p["version"]) for p in new_manifest} != {(p["name"], p["version"]) for p in manifest}:
            log.info("Local package list updated: %d packages", len(new_manifest))
        return new_manifest, last_mtime

    manifest_path = config["manifest_path"]
    if not os.path.exists(manifest_path):
        log.debug("No manifest file at %s", manifest_path)
        return manifest, last_mtime
    mtime = os.stat(manifest_path).st_mtime
    if mtime == last_mtime:
        return manifest, last_mtime
    log.info("Manifest change detected (mtime: %s)", datetime.fromtimestamp(mtime))
    new_manifest = load_manifest(manifest_path)
    if not new_manifest:
        log.warning("Manifest at %s loaded empty — may be mid-write, will retry", manifest_path)
        return manifest, last_mtime
    log.info("Loaded manifest with %d packages", len(new_manifest))
    return new_manifest, mtime


def _refresh_queue(config: dict, manifest: list, pkgbase_map: dict):
    # always_build packages ride along as virtual entries so they build and are
    # exempt from uninstalled-pruning. Injected into a copy so change detection
    # on the raw manifest is untouched.
    effective = inject_always_build(list(manifest), config)
    names = {p["name"] for p in effective}

    pp, pf = prune_stale_queue_entries(config, names)
    if pp or pf:
        log.info("Pruned stale queue entries: %d pending, %d failed removed", pp, pf)

    with _queue_lock(config):
        built = get_built_state(config["state_path"])
        built = _prune_cycle(config, effective, names, built)
        _queue_from_manifest(config, effective, built, pkgbase_map)

    # After the manifest queue, so a package needing both a version bump and
    # a soname repair is queued once.
    try:
        _queue_soname_repairs(config, {p["name"]: p for p in effective})
    except Exception as e:
        log.error("Error checking soname consistency: %s", e)


def _start_upstream_check(config: dict, manifest: list, results: _thread_queue.Queue):
    """Kick off the upstream PKGBUILD check in a thread. Returns (thread, built snapshot)."""
    # Monorepos are pulled here in the main thread, before the build loop, so
    # no build reads a tree mid-pull.
    try:
        for tier, src in config["tier_sources"].items():
            if src["type"] != "monorepo":
                continue
            monorepo_dir = os.path.join(config["pkgbuilds_dir"], tier)
            if os.path.isdir(os.path.join(monorepo_dir, ".git")):
                r = _git(["-C", monorepo_dir, "pull", "--ff-only"],
                         config["build_user"], capture_output=True)
                if r.returncode != 0:
                    log.warning("monorepo pull failed for %s tier: %s",
                                tier, r.stderr.decode(errors="replace").strip()[:200])
    except Exception as e:
        log.error("Error pulling monorepos: %s", e)

    try:
        write_patch_status(config)
    except Exception as e:
        log.warning("Patch status write failed: %s", e)

    snapshot = get_built_state(config["state_path"])
    thread_manifest = inject_always_build(list(manifest), config)

    def worker():
        try:
            results.put(check_upstream_updates(thread_manifest, snapshot, config,
                                               should_stop=lambda: shutdown_flag,
                                               skip_pulls=True))
        except Exception as exc:
            log.error("Upstream check thread error: %s", exc)
            results.put([])

    thread = threading.Thread(target=worker, daemon=True, name="upstream-check")
    thread.start()
    return thread, snapshot


def _queue_upstream_updates(config: dict, updates: list, snapshot: dict):
    log.info("Upstream check completed: %d update(s) found", len(updates))
    if updates:
        with _queue_lock(config):
            pending = load_pending(config["pending_path"])
            built_now = get_built_state(config["state_path"])
            failed_now = load_failed(config["failed_path"])
            pending_names = {p["name"] for p in pending}
            queued = skipped_existing = skipped_failed = skipped_stalled = skipped_already_built = 0
            for pkg in updates:
                pname = pkg["name"]
                if pname in pending_names:
                    skipped_existing += 1
                    continue
                failed_rec = failed_now.get(pname, {})
                if failed_rec.get("version") == pkg["version"]:
                    if not _retry_due(failed_rec, config):
                        skipped_failed += 1
                        continue
                    log.info("[%s] failure backoff elapsed (%s, %d prior) — retrying",
                             pname, failed_rec.get("error_type", "build"),
                             failed_rec.get("retries", 0))
                if _is_stalled(failed_rec, config):
                    skipped_stalled += 1
                    continue
                # Rebuilt at a newer version since the thread started
                old_ver = snapshot.get(pname, {}).get("version", "")
                cur_ver = built_now.get(pname, {}).get("version", "")
                if cur_ver and cur_ver != old_ver:
                    skipped_already_built += 1
                    continue
                pkg["queued_at"] = datetime.now(timezone.utc).isoformat()
                pending.append(pkg)
                queued += 1
            save_pending(config["pending_path"], pending)
        log.info(
            "Upstream update queue: queued=%d, skipped_existing=%d, skipped_failed=%d, skipped_stalled=%d, skipped_already_built=%d",
            queued, skipped_existing, skipped_failed, skipped_stalled, skipped_already_built,
        )
    try:
        _resolve_pending_cascades(config)
    except Exception as e:
        log.error("Error resolving pending cascades: %s", e)


# ---------------------------------------------------------------------------
# Building one package
# ---------------------------------------------------------------------------
def _set_status(config: dict, name: str, version: str, status: str, **extra):
    """Record a non-build outcome so the package isn't re-queued at this version."""
    with _queue_lock(config):
        built = get_built_state(config["state_path"])
        built[name] = {
            "version": version,
            "status": status,
            "built_at": datetime.now(timezone.utc).isoformat(),
            **extra,
        }
        save_built_state(config["state_path"], built)


def _cascade_sonames(pkg_files: list, config: dict) -> set:
    """Sonames this build introduces that the distro repos haven't migrated to yet.

    Publishing such a build would break distro reverse-deps still linking the
    old soname, so it is staged until they catch up.
    """
    new_sonames = set()
    for pf in pkg_files:
        new_sonames |= _soname_provides_from_pkg(pf)
    world = sync_index(config["repo_name"])
    blocking = set()
    for soname in new_sonames:
        if world.soname_ready(soname):
            continue  # distro fully migrated to the new soname
        if world.has_lib(soname.split("=", 1)[0]):
            blocking.add(soname)
    return blocking


def _publish(pkg: dict, srcinfo: dict, pkg_files: list, config: dict, pgp_skipped: bool):
    """Sign a successful build and publish it, or stage it behind a soname cascade."""
    name = pkg["name"]
    new_version = _fmt_srcinfo_ver(srcinfo)
    all_subpkgs = srcinfo.get("packages", [])

    sign_packages(pkg_files, config["gnupg_home"], config["build_user"])
    log.info("[%s] signed %d package(s)", name, len(pkg_files))

    cascade = _cascade_sonames(pkg_files, config)
    if cascade:
        staged = stage_packages(pkg_files, config["repo_dir"])
        log.info("[%s] soname bump detected (%s) — staged, awaiting world cascade",
                 name, ", ".join(sorted(cascade)))
        entry = {
            "status": "pending_world_cascade",
            "version": new_version,
            "pkgrel": new_version.rsplit("-", 1)[-1] if "-" in new_version else "",
            "built_at": datetime.now(timezone.utc).isoformat(),
            "pkg_files": staged,
            "cascade_sonames": sorted(cascade),
        }
        if pgp_skipped:
            entry["pgp_skipped"] = True
        with _queue_lock(config):
            built = get_built_state(config["state_path"])
            built[name] = entry
            for subpkg in all_subpkgs:
                built[subpkg] = entry.copy()
            save_built_state(config["state_path"], built)
    else:
        moved = add_to_repo(
            pkg_files, config["repo_db"], config["repo_dir"],
            autoprune=config["autoprune"],
            autoprune_keep=config["autoprune_keep"],
        )
        log.info("[%s] added to repo", name)
        with _queue_lock(config):
            built = get_built_state(config["state_path"])
            built = update_built_state(built, pkg, new_version, moved,
                                       all_pkgnames=all_subpkgs, pgp_skipped=pgp_skipped)
            save_built_state(config["state_path"], built)

    with _queue_lock(config):
        failed = load_failed(config["failed_path"])
        cleared = (set(all_subpkgs) | {name}) & set(failed)
        if cleared:
            for n in cleared:
                del failed[n]
            save_failed(config["failed_path"], failed)
        # Sibling subpackages were built by this same PKGBUILD
        if len(all_subpkgs) > 1:
            siblings = set(all_subpkgs)
            pending = load_pending(config["pending_path"])
            remaining = [p for p in pending if p["name"] not in siblings]
            if len(remaining) != len(pending):
                save_pending(config["pending_path"], remaining)
                log.info("[%s] cleared %d sibling subpackage(s) from queue",
                         name, len(pending) - len(remaining))


def _failure_reason_from_log(config: dict, name: str) -> tuple[str, str | None]:
    """(last meaningful log line, log path) for a failed build."""
    log_dir_pkg = os.path.join(config["log_dir"], name)
    if not os.path.isdir(log_dir_pkg):
        return "build failed", None
    logs = sorted((f for f in os.listdir(log_dir_pkg) if f.endswith(".log")), reverse=True)
    if not logs:
        return "build failed", None
    log_path = os.path.join(log_dir_pkg, logs[0])
    try:
        with open(log_path, "r", errors="replace") as lf:
            lines = [l.rstrip() for l in lf if l.strip()]
    except OSError:
        return "build failed", log_path
    # "Build failed, check /chroot/path" only points at a chroot that is gone
    meaningful = [l for l in lines if not l.startswith("==> ERROR: Build failed, check ")]
    if meaningful:
        return _sanitize_reason(meaningful[-1]), log_path
    if lines:
        return _sanitize_reason(lines[-1]), log_path
    return "build failed", log_path


def _handle_build_failure(pkg: dict, tier: str, failure_type: str | None,
                          duration: float, config: dict):
    name = pkg["name"]

    if failure_type == "download":
        # Transient network failure — re-queue instead of failing
        retry_count = pkg.get("download_retries", 0) + 1
        limit = config.get("download_retry_limit", 3)
        if retry_count <= limit:
            requeue = dict(pkg)
            requeue["download_retries"] = retry_count
            requeue["queued_at"] = datetime.now(timezone.utc).isoformat()
            with _queue_lock(config):
                pending = load_pending(config["pending_path"])
                pending.append(requeue)
                save_pending(config["pending_path"], pending)
            log.warning("[%s] download failed after %ds — re-queued (attempt %d/%d)",
                        name, int(duration), retry_count, limit)
        else:
            _record_failure(config, name, pkg["version"],
                            f"download failed after {limit} attempts", "download")
            log.error("[%s] download FAILED %d times — giving up (tier: %s)", name, limit, tier)
        return

    # Dep/source failure — tier-specific. Record the failing tier so the next
    # attempt skips it and tries the next one in priority order.
    if failure_type and (failure_type.startswith("dep:") or failure_type == "missing_source"):
        if failure_type.startswith("dep:"):
            reason, etype = f"missing makedep: {failure_type[4:]}", "dep"
        else:
            reason, etype = "source file missing from PKGBUILD directory", "missing_source"
        _record_failure(config, name, pkg["version"], reason, etype, failed_tier=tier)
        log.error("[%s] build FAILED after %ds — %s (tier: %s, will retry on next tier)",
                  name, int(duration), reason, tier)
        return

    reason, log_path = _failure_reason_from_log(config, name)
    if failure_type == "timeout":
        reason = f"build timed out after {config.get('build_timeout', 0)}s"
    _record_failure(config, name, pkg["version"], reason, failure_type or "build",
                    log_path=log_path)
    log.error("[%s] build FAILED after %ds (tier: %s)", name, int(duration), tier)


def _build_one(pkg: dict, config: dict, pkgbase_map: dict, stats: dict):
    name = pkg["name"]
    failed_rec = load_failed(config["failed_path"]).get(name) or {}

    # Still inside the backoff for a failure at this version. This also stops
    # a package that failed earlier in this same drain from being retried at
    # once, since the shortest backoff is an hour.
    if failed_rec.get("version") == pkg["version"] and not _retry_due(failed_rec, config):
        stats["skipped_previous_failure"] += 1
        log.info("[%s] skipping (previously failed at %s)", name, pkg["version"])
        return

    build_start = time.time()
    stats["attempted"] += 1
    log.info("[%s] === BUILD STARTING ===", name)

    priority = config["package_tier_overrides"].get(name) or config["repo_priority"]
    # A previous dep/source error is specific to the tier it came from
    failed_tier = failed_rec.get("failed_tier")
    if failed_tier and failed_tier in priority and len(priority) > 1:
        priority = [t for t in priority if t != failed_tier]
        log.info("[%s] skipping failed tier '%s' (dep/source error) — trying: %s",
                 name, failed_tier, priority)
    pkgbuild_dir, tier = resolve_pkgbuild(
        name, config["pkgbuilds_dir"], pkgbase_map, priority,
        tier_sources=config["tier_sources"],
        version_select=config["tier_version_select"],
        build_user=config["build_user"],
    )
    log.info("[%s] PKGBUILD tier: %s (%s)", name, tier, pkgbuild_dir)

    srcinfo = parse_srcinfo(pkgbuild_dir, config["build_user"])
    log.debug("[%s] parsed srcinfo: %s-%s (arch: %s)", name, srcinfo["pkgver"], srcinfo["pkgrel"], srcinfo["arch"])

    eligible, reason = is_eligible(pkg, srcinfo, config["blacklist"])
    if not eligible:
        stats["skipped_ineligible"] += 1
        log.info("[%s] not eligible: %s", name, reason)
        _set_status(config, name, pkg["version"], "ineligible", reason=reason)
        return

    # The installed version's dotted pkgrel (a vendor's local rebuild) never
    # appears in a PKGBUILD, so compare without it or this defers forever.
    if vercmp(_fmt_srcinfo_ver(srcinfo), strip_local_pkgrel_bump(pkg["version"])) < 0:
        log.debug("[%s] PKGBUILD %s is older than installed %s — deferring until upstream catches up",
                  name, _fmt_srcinfo_ver(srcinfo), pkg["version"])
        _set_status(config, name, pkg["version"], "pending_upstream")
        return

    pgp_skipped = False
    if srcinfo["validpgpkeys"]:
        log.info("[%s] importing %d PGP key(s)", name, len(srcinfo["validpgpkeys"]))
        missing_keys = import_pgp_keys(srcinfo["validpgpkeys"], config["gnupg_home"], config["build_user"])
        if missing_keys:
            if not config.get("skip_pgp_on_import_failure"):
                stats["skipped_missing_keys"] += 1
                reason = "missing PGP keys: " + ",".join(missing_keys)
                log.error("[%s] %s", name, reason)
                _record_failure(config, name, pkg.get("version", "unknown"), reason, "missing_keys")
                return
            log.warning("[%s] PGP keys not found on any keyserver (%s); "
                        "skip_pgp_on_import_failure=true — will build with --skippgpcheck",
                        name, ",".join(missing_keys))
            pgp_skipped = True
    else:
        log.info("[%s] no PGP keys required", name)

    log.info("[%s] build starting...", name)
    success, pkg_files, failure_type = build_package(pkg, pkgbuild_dir, config, skippgpcheck=pgp_skipped)
    duration = time.time() - build_start

    if not success:
        stats["failed"] += 1
        _handle_build_failure(pkg, tier, failure_type, duration, config)
        return

    _publish(pkg, srcinfo, pkg_files, config, pgp_skipped)
    stats["succeeded"] += 1
    log.info("[%s] build successful in %ds (tier: %s)%s", name, int(duration), tier,
             " [PGP SKIPPED — no keyserver]" if pgp_skipped else "")


def _process_queue(config: dict, pkgbase_map: dict) -> dict:
    """Build until the queue is empty or a shutdown is requested."""
    stats = {"pending_start": len(load_pending(config["pending_path"])),
             **{k: 0 for k in _STAT_KEYS}}
    _write_metrics(config, "processing", stats, stats["pending_start"])
    builds_since_prune = 0

    while not shutdown_flag:
        with _queue_lock(config):
            pending = load_pending(config["pending_path"])
            if not pending:
                break
            pkg = pending.pop(0)
            save_pending(config["pending_path"], pending)
        write_in_progress(config, pkg)
        name = pkg["name"]
        try:
            _build_one(pkg, config, pkgbase_map, stats)
        except FileNotFoundError as e:
            stats["failed"] += 1
            log.error("[%s] PKGBUILD not found: %s", name, e)
            _record_failure(config, name, pkg.get("version", "unknown"),
                            _sanitize_reason(str(e)), "pkgbuild_not_found")
        except Exception as e:
            stats["failed"] += 1
            log.error("[%s] unexpected error: %s", name, e, exc_info=True)
            _record_failure(config, name, pkg.get("version", "unknown"),
                            _sanitize_reason(str(e)), "build")
        finally:
            clear_in_progress(config)

        builds_since_prune += 1
        if builds_since_prune >= 20:
            removed = prune_old_build_logs(config["log_dir"], config["log_retention_days"])
            if removed:
                log.info("Pruned %d old build log(s) during queue processing", removed)
            builds_since_prune = 0
        _write_metrics(config, "processing", stats, len(load_pending(config["pending_path"])))

    return stats


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run_daemon(config_path: str, debug: bool):
    global wake_flag

    config = load_config(config_path)
    _setup_logging(config, debug)

    daemon_lock = acquire_daemon_lock(config)
    if daemon_lock is None:
        log.error("Another buildbot daemon is already running (pid %d)", daemon_pid(config))
        sys.exit(1)

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)
    signal.signal(signal.SIGUSR1, handle_sigusr1)

    log.info(
        "Buildbot starting (march=%s, mode=%s, distro=%s, poll=%ds, build_user=%s, repo_priority=%s)",
        config["march"], config["mode"], config["distro"], config["poll_interval"],
        config["build_user"], ",".join(config["repo_priority"]),
    )

    prepare_gnupg_home(config["gnupg_home"], config["build_user"])

    makepkg_conf_name = f"makepkg.{config['march'].replace('=','')}.conf"
    makepkg_conf_path = os.path.join(config["makepkg_configs_dir"], makepkg_conf_name)
    generate_makepkg_conf(config, makepkg_conf_path)
    config["_makepkg_conf"] = makepkg_conf_path

    _deploy_chroot_files(config)
    _write_metrics(config, "starting", {}, 0)
    _recover_interrupted(config)
    _clean_stale_chroots(config)

    # Repair built.json / repo DB / file divergence left by a SIGKILL
    log.info("Running startup fsck...")
    try:
        found, repaired = _run_fsck(config)
        if found:
            log.info("Startup fsck: repaired %d/%d issue(s)", repaired, found)
        else:
            log.info("Startup fsck: all consistent")
    except Exception as e:
        log.warning("Startup fsck failed: %s", e)

    log.info("Building pkgbase map from sync databases...")
    pkgbase_map = build_pkgbase_map()

    # Clients get patch health before the first upstream check
    try:
        write_patch_status(config)
    except Exception as e:
        log.warning("Initial patch status write failed: %s", e)

    manifest: list = []
    last_manifest_mtime = 0.0
    last_upstream_check = 0.0
    upstream_thread = None
    upstream_snapshot: dict = {}
    upstream_results: _thread_queue.Queue = _thread_queue.Queue()

    while not shutdown_flag:
        cycle_start = time.time()

        # A wake signal (from `buildbot sync`) asks for a fast pass: skip the
        # chroot upgrade and upstream check and go straight to building.
        fast_pass = wake_flag
        wake_flag = False

        if fast_pass:
            log.info("Fast build pass — skipping chroot upgrade and upstream check")
        else:
            log.info("Upgrading chroot...")
            if not upgrade_chroot(config["chroot_root"], config.get("chroot_extra_packages", [])):
                log.warning("Chroot upgrade did not complete — builds will use existing chroot state")
        if shutdown_flag:
            break

        try:
            manifest, last_manifest_mtime = _refresh_manifest(config, manifest, last_manifest_mtime)
            if manifest:
                _refresh_queue(config, manifest, pkgbase_map)
        except Exception as e:
            log.error("Error processing manifest: %s", e)
        if shutdown_flag:
            break

        due = (time.time() - last_upstream_check) >= config["upstream_check_interval"]
        if not fast_pass and manifest and due and (upstream_thread is None or not upstream_thread.is_alive()):
            log.info("Starting async upstream update check...")
            last_upstream_check = time.time()
            upstream_thread, upstream_snapshot = _start_upstream_check(config, manifest, upstream_results)

        try:
            updates = upstream_results.get_nowait()
        except _thread_queue.Empty:
            pass
        else:
            try:
                _queue_upstream_updates(config, updates, upstream_snapshot)
            except Exception as e:
                log.error("Error draining upstream results: %s", e)
        if shutdown_flag:
            break

        stats = _process_queue(config, pkgbase_map)
        if shutdown_flag:
            break

        elapsed = time.time() - cycle_start
        sleep_time = max(0, config["poll_interval"] - elapsed)
        pending_end = len(load_pending(config["pending_path"]))
        _write_metrics(config, "sleeping", stats, pending_end,
                       cycle_seconds=int(elapsed), sleep_seconds=int(sleep_time))
        removed_logs = prune_old_build_logs(config["log_dir"], config["log_retention_days"])
        log.info(
            "Cycle complete: pending %d -> %d, attempted=%d ok=%d failed=%d skipped(prev=%d ineligible=%d missing_keys=%d), pruned_logs=%d, sleeping %ds",
            stats["pending_start"], pending_end, stats["attempted"], stats["succeeded"],
            stats["failed"], stats["skipped_previous_failure"], stats["skipped_ineligible"],
            stats["skipped_missing_keys"], removed_logs, int(sleep_time),
        )

        # Interruptible sleep — a wake signal cuts it short for an immediate pass.
        sleep_end = time.time() + sleep_time
        while time.time() < sleep_end and not shutdown_flag and not wake_flag:
            time.sleep(1)

    log.info("Buildbot shutting down cleanly.")
