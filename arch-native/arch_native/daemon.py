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
from .soname import _queue_soname_repairs, _resolve_pending_cascades, _soname_provides_from_pkg, _world_depends_on_old_soname, _world_has_lib, _world_has_soname
from .state import _is_stalled, _record_failure, _retry_due, clear_in_progress, diff_manifest, get_built_state, inject_always_build, load_failed, load_in_progress, load_pending, prune_stale_queue_entries, save_built_state, save_failed, save_pending, strip_local_pkgrel_bump, update_built_state, write_in_progress, write_metrics
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


def _queue_from_manifest(config: dict, manifest: list, built: dict, pkgbase_map: dict) -> None:
    """Auto-retry stalled packages, diff manifest, and populate the build queue."""
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


def run_daemon(config_path: str, debug: bool):
    global shutdown_flag, wake_flag

    config = load_config(config_path)

    # Logging setup
    level = logging.DEBUG if debug else getattr(logging, config["log_level"])
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # File handler
    os.makedirs(config["log_dir"], exist_ok=True)
    fh = logging.FileHandler(os.path.join(config["log_dir"], "buildbot.log"))
    fh.setFormatter(formatter)

    # Stdout handler (for journalctl)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)

    log.setLevel(level)
    log.addHandler(fh)
    log.addHandler(sh)

    # Signal handlers
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)
    signal.signal(signal.SIGUSR1, handle_sigusr1)

    log.info(
        "Buildbot starting (march=%s, mode=%s, distro=%s, poll=%ds, build_user=%s, repo_priority=%s)",
        config["march"], config["mode"], config["distro"], config["poll_interval"],
        config["build_user"], ",".join(config["repo_priority"]),
    )

    prepare_gnupg_home(config["gnupg_home"], config["build_user"])

    # Generate makepkg.conf from current config (handles march=native, local !check, etc.)
    makepkg_conf_name = f"makepkg.{config['march'].replace('=','')}.conf"
    makepkg_conf_path = os.path.join(config["makepkg_configs_dir"], makepkg_conf_name)
    generate_makepkg_conf(config, makepkg_conf_path)
    config["_makepkg_conf"] = makepkg_conf_path

    # Deploy distro-specific files into the chroot root (once at startup).
    chroot_root = config["chroot_root"]
    if os.path.isdir(chroot_root):
        # artix-meson wrapper — only needed for Artix
        if config["distro"] == "artix":
            for artix_meson_src in (
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "artix-meson"),
                "/usr/share/arch-native/artix-meson",
            ):
                if os.path.isfile(artix_meson_src):
                    dest = os.path.join(chroot_root, "usr/local/bin/artix-meson")
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    import shutil as _shutil
                    _shutil.copy2(artix_meson_src, dest)
                    os.chmod(dest, 0o755)
                    log.info("Deployed artix-meson into chroot")
                    break

        # chroot pacman.conf — only redeploy if explicitly configured.
        # The install/deploy script handles the initial copy; this path is for
        # users who configure a custom chroot_pacman_conf in buildbot.conf.
        pacman_conf_src = config.get("chroot_pacman_conf", "")
        if pacman_conf_src:
            if os.path.isfile(pacman_conf_src):
                dest = os.path.join(chroot_root, "etc/pacman.conf")
                import shutil as _shutil
                _shutil.copy2(pacman_conf_src, dest)
                log.info("Deployed pacman.conf into chroot from %s", pacman_conf_src)
            else:
                log.warning("chroot_pacman_conf not found: %s", pacman_conf_src)
    write_metrics(config, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "starting",
        "pending_start": 0,
        "pending_end": 0,
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped_previous_failure": 0,
        "skipped_ineligible": 0,
        "skipped_missing_keys": 0,
    })

    recovered = load_in_progress(config)
    if recovered:
        # Warn if the in-progress record is suspiciously old (indicates a very
        # long-running or hung build before the crash, not a clean restart).
        started_at_str = recovered.get("started_at")
        if started_at_str:
            try:
                from datetime import timezone as _tz
                started_at = datetime.fromisoformat(started_at_str)
                age_secs = (datetime.now(_tz.utc) - started_at).total_seconds()
                timeout = config.get("build_timeout") or 0
                stale_threshold = timeout * 2 if timeout else 86400  # 24h if timeout disabled
                if age_secs > stale_threshold:
                    log.warning(
                        "in_progress record for %s is %.1fh old (started_at=%s) — "
                        "may be from a hung build; re-queuing anyway",
                        recovered.get("name"), age_secs / 3600, started_at_str,
                    )
            except Exception:
                pass
        pending = load_pending(config["pending_path"])
        pending_names = {p.get("name") for p in pending}
        if recovered.get("name") not in pending_names:
            pending.insert(0, recovered)
            save_pending(config["pending_path"], pending)
            log.warning("Recovered interrupted package back to queue: %s", recovered.get("name"))
        clear_in_progress(config)

    last_manifest_mtime = 0
    last_upstream_check = 0
    manifest = []
    _upstream_thread = None
    _upstream_results = _thread_queue.Queue()
    _upstream_built_snapshot = {}

    # Remove stale pacman lock from the root chroot. This file is left behind
    # when the daemon is SIGKILL'd while arch-nspawn is mid-upgrade; without
    # cleanup every subsequent build hangs indefinitely waiting on the lock.
    _chroot_dir = config["chroot_dir"]
    _root_dblck = os.path.join(_chroot_dir, "root", "var", "lib", "pacman", "db.lck")
    if os.path.exists(_root_dblck):
        try:
            os.remove(_root_dblck)
            log.warning("Removed stale pacman lock from root chroot (previous run was interrupted mid-upgrade)")
        except OSError as e:
            log.warning("Could not remove stale root chroot lock %s: %s", _root_dblck, e)

    # Remove leftover ephemeral build chroots and their directory locks.
    # Any build-* dirs present at startup are from a killed previous run.
    try:
        for _entry in os.listdir(_chroot_dir):
            _path = os.path.join(_chroot_dir, _entry)
            if _entry.startswith("build-"):
                try:
                    if os.path.isdir(_path):
                        shutil.rmtree(_path, ignore_errors=True)
                    else:
                        os.remove(_path)
                    log.warning("Removed stale build chroot: %s", _path)
                except OSError as e:
                    log.warning("Could not remove stale build chroot %s: %s", _path, e)
    except OSError as e:
        log.warning("Stale build chroot scan failed: %s", e)

    # Auto-fsck on startup: repair any built.json / repo DB / file divergences
    # caused by SIGKILL during a previous build cycle.
    log.info("Running startup fsck...")
    try:
        found, repaired = _run_fsck(config)
        if found:
            log.info("Startup fsck: repaired %d/%d issue(s)", repaired, found)
        else:
            log.info("Startup fsck: all consistent")
    except Exception as e:
        log.warning("Startup fsck failed: %s", e)

    # Build pkgname->pkgbase map for split package resolution
    log.info("Building pkgbase map from sync databases...")
    pkgbase_map = build_pkgbase_map()

    # Publish initial patch health so clients have data before the first
    # upstream check; refreshed once per upstream_check_interval below.
    try:
        write_patch_status(config)
    except Exception as e:
        log.warning("Initial patch status write failed: %s", e)

    while not shutdown_flag:
        cycle_start = time.time()

        # A wake signal (from `buildbot sync`) requests a fast pass: skip the
        # slow per-cycle work (chroot upgrade, upstream check) and go straight to
        # building whatever the manifest/queue needs. The next natural cycle is full.
        fast_pass = wake_flag
        wake_flag = False

        # ----- Step 1: Chroot upgrade -----
        if fast_pass:
            log.info("Fast build pass — skipping chroot upgrade and upstream check")
        else:
            log.info("Upgrading chroot...")
            if not upgrade_chroot(config["chroot_root"], config.get("chroot_extra_packages", [])):
                log.warning("Chroot upgrade did not complete — builds will use existing chroot state")

        if shutdown_flag:
            break

        # ----- Step 2: Manifest check -----
        try:
            if config["mode"] == "local":
                # Local mode: read pacman DB directly every cycle
                new_manifest = read_local_packages()
                manifest_changed = (len(new_manifest) != len(manifest) or
                    {p["name"] for p in new_manifest} != {p["name"] for p in manifest})
                if manifest_changed:
                    manifest = new_manifest
                    log.info("Local package list updated: %d packages", len(manifest))
            else:
                # Remote mode: watch manifest file for changes
                manifest_path = config["manifest_path"]
                if os.path.exists(manifest_path):
                    current_mtime = os.stat(manifest_path).st_mtime
                    if current_mtime != last_manifest_mtime:
                        log.info("Manifest change detected (mtime: %s)", datetime.fromtimestamp(current_mtime))
                        new_manifest = load_manifest(manifest_path)
                        if new_manifest:
                            last_manifest_mtime = current_mtime
                            manifest = new_manifest
                            log.info("Loaded manifest with %d packages", len(manifest))
                        else:
                            log.warning("Manifest at %s loaded empty — may be mid-write, will retry", manifest_path)
                else:
                    log.debug("No manifest file at %s", manifest_path)

            if manifest:
                # Add always_build packages as virtual entries so they build and
                # are exempt from uninstalled-pruning. Inject into a copy so the
                # raw client manifest used for change detection above is untouched.
                effective_manifest = inject_always_build(list(manifest), config)
                manifest_names = {p["name"] for p in effective_manifest}

                # Prune stale pending/failed entries for uninstalled packages
                pp, pf = prune_stale_queue_entries(config, manifest_names)
                if pp or pf:
                    log.info("Pruned stale queue entries: %d pending, %d failed removed", pp, pf)

                built = get_built_state(config["state_path"])
                built = _prune_cycle(config, effective_manifest, manifest_names, built)
                _queue_from_manifest(config, effective_manifest, built, pkgbase_map)

                # Rebuild anything the repo's own library churn has stranded.
                # Runs after the manifest queue so a package needing both a
                # version bump and a soname repair is queued once.
                try:
                    _queue_soname_repairs(
                        config, {p["name"]: p for p in effective_manifest})
                except Exception as e:
                    log.error("Error checking soname consistency: %s", e)
        except Exception as e:
            log.error("Error processing manifest: %s", e)

        if shutdown_flag:
            break

        # ----- Step 3: Upstream update check (async) -----
        now = time.time()
        if not fast_pass and manifest and (now - last_upstream_check) >= config["upstream_check_interval"]:
            if _upstream_thread is None or not _upstream_thread.is_alive():
                log.info("Starting async upstream update check...")
                last_upstream_check = now
                # Pull monorepos synchronously in the main thread to avoid racing
                # with per-package git writes in the build loop (clone/pkgctl tiers).
                try:
                    for tier, src in config["tier_sources"].items():
                        if src["type"] == "monorepo":
                            monorepo_dir = os.path.join(config["pkgbuilds_dir"], tier)
                            if os.path.isdir(os.path.join(monorepo_dir, ".git")):
                                r = _git(["-C", monorepo_dir, "pull", "--ff-only"],
                                         config["build_user"], capture_output=True)
                                if r.returncode != 0:
                                    log.warning(
                                        "monorepo pull failed for %s tier: %s",
                                        tier, r.stderr.decode(errors="replace").strip()[:200],
                                    )
                except Exception as e:
                    log.error("Error pulling monorepos: %s", e)

                # Refresh published patch health now that PKGBUILDs were pulled.
                # Runs in the main thread before the build loop starts, so there
                # is no concurrent git writer racing the patch dry-runs.
                try:
                    write_patch_status(config)
                except Exception as e:
                    log.warning("Patch status write failed: %s", e)

                _upstream_built_snapshot = get_built_state(config["state_path"])
                _thread_manifest = inject_always_build(list(manifest), config)

                def _upstream_worker(m, b, cfg, results_q, stop_fn):
                    try:
                        updates = check_upstream_updates(m, b, cfg, should_stop=stop_fn, skip_pulls=True)
                        results_q.put(updates)
                    except Exception as exc:
                        log.error("Upstream check thread error: %s", exc)
                        results_q.put([])

                _upstream_thread = threading.Thread(
                    target=_upstream_worker,
                    args=(_thread_manifest, _upstream_built_snapshot, config,
                          _upstream_results, lambda: shutdown_flag),
                    daemon=True,
                    name="upstream-check",
                )
                _upstream_thread.start()

        # Drain upstream results from completed thread before building
        if not _upstream_results.empty():
            try:
                updates = _upstream_results.get_nowait()
                log.info("Upstream check completed: %d update(s) found", len(updates))
                if updates:
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
                        # Skip if already built at a newer version since the thread started
                        old_ver = _upstream_built_snapshot.get(pname, {}).get("version", "")
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
            except _thread_queue.Empty:
                pass
            except Exception as e:
                log.error("Error draining upstream results: %s", e)

        if shutdown_flag:
            break

        # ----- Step 4: Queue processing -----
        pending = load_pending(config["pending_path"])
        failed = load_failed(config["failed_path"])
        cycle_stats = {
            "pending_start": len(pending),
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped_previous_failure": 0,
            "skipped_ineligible": 0,
            "skipped_missing_keys": 0,
        }
        writes_since_prune = 0
        write_metrics(config, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "processing",
            "pending_start": cycle_stats["pending_start"],
            "pending_end": len(pending),
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped_previous_failure": 0,
            "skipped_ineligible": 0,
            "skipped_missing_keys": 0,
        })

        while pending and not shutdown_flag:
            pkg = pending.pop(0)
            save_pending(config["pending_path"], pending)
            write_in_progress(config, pkg)
            name = pkg["name"]

            # Skip if currently failed at this version and still inside its
            # backoff. This guard also stops a package that failed earlier in
            # this same drain from being retried immediately, since the
            # shortest backoff is an hour.
            if (name in failed and failed[name].get("version") == pkg["version"]
                    and not _retry_due(failed[name], config)):
                cycle_stats["skipped_previous_failure"] += 1
                log.info("[%s] skipping (previously failed at %s)", name, pkg["version"])
                clear_in_progress(config)
                continue

            build_start = time.time()
            cycle_stats["attempted"] += 1
            log.info("[%s] === BUILD STARTING ===", name)
            try:
                # a. Resolve PKGBUILD (with pkgbase fallback for split packages)
                pkg_priority = config["package_tier_overrides"].get(name) or config["repo_priority"]
                # If this package previously failed with a tier-specific dep or source
                # error, skip that tier and try the next one in priority order.
                _failed_tier = (failed.get(name) or {}).get("failed_tier")
                if _failed_tier and _failed_tier in pkg_priority and len(pkg_priority) > 1:
                    pkg_priority = [t for t in pkg_priority if t != _failed_tier]
                    log.info("[%s] skipping failed tier '%s' (dep/source error) — trying: %s",
                             name, _failed_tier, pkg_priority)
                pkgbuild_dir, tier = resolve_pkgbuild(
                    name,
                    config["pkgbuilds_dir"],
                    pkgbase_map,
                    pkg_priority,
                    tier_sources=config["tier_sources"],
                    version_select=config["tier_version_select"],
                )
                log.info("[%s] PKGBUILD tier: %s (%s)", name, tier, pkgbuild_dir)

                # b. Parse .SRCINFO
                srcinfo = parse_srcinfo(pkgbuild_dir, config["build_user"])
                log.debug("[%s] parsed srcinfo: %s-%s (arch: %s)", name, srcinfo["pkgver"], srcinfo["pkgrel"], srcinfo["arch"])

                # c. Eligibility check
                eligible, reason = is_eligible(pkg, srcinfo, config["blacklist"])
                if not eligible:
                    cycle_stats["skipped_ineligible"] += 1
                    log.info("[%s] not eligible: %s", name, reason)
                    # Record in built state so this package isn't re-queued next cycle
                    _built = get_built_state(config["state_path"])
                    _built[name] = {
                        "version": pkg["version"],
                        "status": "ineligible",
                        "built_at": datetime.now(timezone.utc).isoformat(),
                    }
                    save_built_state(config["state_path"], _built)
                    clear_in_progress(config)
                    continue

                # c2. Skip if PKGBUILD version is older than what's already installed.
                # Use _fmt_srcinfo_ver to include epoch so epoch packages compare correctly.
                # Strip the installed version's dotted pkgrel (.1 suffix) before comparing.
                # That sub-pkgrel comes from the client side (the installed package was built
                # by a vendor/helper at a dotted rel the server rebuilds at the plain rel);
                # the PKGBUILD never carries it, so comparing raw would permanently defer.
                pkgbuild_ver = _fmt_srcinfo_ver(srcinfo)
                _manifest_base_ver = strip_local_pkgrel_bump(pkg["version"])
                if vercmp(pkgbuild_ver, _manifest_base_ver) < 0:
                    log.debug("[%s] PKGBUILD %s is older than installed %s — deferring until upstream catches up", name, pkgbuild_ver, pkg["version"])
                    _built = get_built_state(config["state_path"])
                    _built[name] = {
                        "version": pkg["version"],
                        "status": "pending_upstream",
                        "built_at": datetime.now(timezone.utc).isoformat(),
                    }
                    save_built_state(config["state_path"], _built)
                    clear_in_progress(config)
                    continue

                log.debug("[%s] eligible", name)

                # d. Import PGP keys
                pgp_skipped = False
                if srcinfo["validpgpkeys"]:
                    log.info("[%s] importing %d PGP key(s)", name, len(srcinfo["validpgpkeys"]))
                    missing_keys = import_pgp_keys(srcinfo["validpgpkeys"], config["gnupg_home"], config["build_user"])
                    if missing_keys:
                        if config.get("skip_pgp_on_import_failure"):
                            log.warning(
                                "[%s] PGP keys not found on any keyserver (%s); "
                                "skip_pgp_on_import_failure=true — will build with --skippgpcheck",
                                name, ",".join(missing_keys),
                            )
                            pgp_skipped = True
                        else:
                            cycle_stats["skipped_missing_keys"] += 1
                            reason = "missing PGP keys: " + ",".join(missing_keys)
                            log.error("[%s] %s", name, reason)
                            _record_failure(failed, name, pkg.get("version", "unknown"),
                                            reason, "missing_keys", config["failed_path"])
                            continue
                else:
                    log.info("[%s] no PGP keys required", name)

                # e. Build
                new_version = _fmt_srcinfo_ver(srcinfo)
                log.info("[%s] build starting...", name)
                success, pkg_files, failure_type = build_package(pkg, pkgbuild_dir, config, skippgpcheck=pgp_skipped)

                if success:
                    # g. Success path
                    duration = time.time() - build_start

                    sign_packages(pkg_files, config["gnupg_home"], config["build_user"])
                    log.info("[%s] signed %d package(s)", name, len(pkg_files))

                    # Detect soname bumps that would break world reverse-deps.
                    # If this package introduces a new soname that world repos don't
                    # yet provide, stage the files without publishing and wait for
                    # world to complete its cascade rebuild first.
                    new_sonames = set()
                    for pf in pkg_files:
                        new_sonames |= _soname_provides_from_pkg(pf)
                    cascade_sonames = set()
                    if new_sonames:
                        repo_name = config["repo_name"]
                        for soname in new_sonames:
                            lib_base = soname.split("=", 1)[0]
                            world_has_new = _world_has_soname(soname, repo_name)
                            if world_has_new and not _world_depends_on_old_soname(lib_base, soname, repo_name):
                                continue  # world fully migrated to new soname
                            if _world_has_lib(lib_base, repo_name):
                                cascade_sonames.add(soname)

                    if cascade_sonames:
                        # Stage: move to repo_dir but skip repo-add until world is ready
                        staged = stage_packages(pkg_files, config["repo_dir"])
                        log.info("[%s] soname bump detected (%s) — staged, awaiting world cascade",
                                 name, ", ".join(sorted(cascade_sonames)))

                        built = get_built_state(config["state_path"])
                        all_subpkgs = srcinfo.get("packages", [])
                        ver_parts = new_version.split("-")
                        pkgrel = ver_parts[-1] if len(ver_parts) >= 2 else ""
                        cascade_entry = {
                            "status": "pending_world_cascade",
                            "version": new_version,
                            "pkgrel": pkgrel,
                            "built_at": datetime.now(timezone.utc).isoformat(),
                            "pkg_files": staged,
                            "cascade_sonames": sorted(cascade_sonames),
                        }
                        if pgp_skipped:
                            cascade_entry["pgp_skipped"] = True
                        built[name] = cascade_entry
                        if all_subpkgs:
                            for subpkg in all_subpkgs:
                                built[subpkg] = cascade_entry.copy()
                        save_built_state(config["state_path"], built)

                    else:
                        moved = add_to_repo(
                            pkg_files, config["repo_db"], config["repo_dir"],
                            autoprune=config["autoprune"],
                            autoprune_keep=config["autoprune_keep"],
                        )
                        log.info("[%s] added to repo", name)

                        built = get_built_state(config["state_path"])
                        all_subpkgs = srcinfo.get("packages", [])
                        built = update_built_state(built, pkg, new_version, moved, all_pkgnames=all_subpkgs, pgp_skipped=pgp_skipped)
                        save_built_state(config["state_path"], built)

                    # Remove from failed if it was there (including subpackages)
                    names_to_clear = set(all_subpkgs) | {name}
                    for n in names_to_clear:
                        if n in failed:
                            del failed[n]
                    save_failed(config["failed_path"], failed)

                    # Remove sibling subpackages from pending queue
                    if len(all_subpkgs) > 1:
                        built_names = set(all_subpkgs)
                        pending = [p for p in pending if p["name"] not in built_names]
                        save_pending(config["pending_path"], pending)
                        log.info("[%s] cleared %d sibling subpackages from queue", name, len(built_names) - 1)

                    cycle_stats["succeeded"] += 1
                    pgp_note = " [PGP SKIPPED — no keyserver]" if pgp_skipped else ""
                    log.info(
                        "[%s] build successful in %ds (tier: %s)%s",
                        name,
                        int(duration),
                        tier,
                        pgp_note,
                    )
                else:
                    # h. Failure path
                    duration = time.time() - build_start

                    if failure_type == "download":
                        # Transient network failure — re-queue instead of failing
                        retry_count = pkg.get("download_retries", 0) + 1
                        limit = config.get("download_retry_limit", 3)
                        if retry_count <= limit:
                            requeue = dict(pkg)
                            requeue["download_retries"] = retry_count
                            requeue["queued_at"] = datetime.now(timezone.utc).isoformat()
                            pending.append(requeue)
                            save_pending(config["pending_path"], pending)
                            cycle_stats["failed"] += 1
                            log.warning(
                                "[%s] download failed after %ds — re-queued (attempt %d/%d)",
                                name, int(duration), retry_count, limit,
                            )
                        else:
                            _record_failure(failed, name, pkg["version"],
                                            f"download failed after {limit} attempts",
                                            "download", config["failed_path"])
                            cycle_stats["failed"] += 1
                            log.error(
                                "[%s] download FAILED %d times — giving up (tier: %s)",
                                name, limit, tier,
                            )
                        continue

                    # Dep/source failure — tier-specific, record the failing tier so
                    # the next attempt skips it and tries the next one in priority order.
                    if failure_type and (failure_type.startswith("dep:") or failure_type == "missing_source"):
                        if failure_type.startswith("dep:"):
                            _dep = failure_type[4:]
                            _reason = f"missing makedep: {_dep}"
                            _etype = "dep"
                        else:
                            _reason = "source file missing from PKGBUILD directory"
                            _etype = "missing_source"
                        _record_failure(failed, name, pkg["version"], _reason, _etype,
                                        config["failed_path"], failed_tier=tier)
                        cycle_stats["failed"] += 1
                        log.error("[%s] build FAILED after %ds — %s (tier: %s, will retry on next tier)",
                                  name, int(duration), _reason, tier)
                        continue

                    # Compile/link/etc failure — pull last non-empty line from log as reason
                    build_reason = "build failed"
                    log_path_for_record = None
                    log_dir_pkg = os.path.join(config["log_dir"], name)
                    if os.path.isdir(log_dir_pkg):
                        logs = sorted(
                            (f for f in os.listdir(log_dir_pkg) if f.endswith(".log")),
                            reverse=True,
                        )
                        if logs:
                            log_path_for_record = os.path.join(log_dir_pkg, logs[0])
                            try:
                                with open(log_path_for_record, "r", errors="replace") as lf:
                                    lines = [l.rstrip() for l in lf if l.strip()]
                                # Skip transient "Build failed, check /chroot/path" lines
                                meaningful = [l for l in lines if not l.startswith("==> ERROR: Build failed, check ")]
                                if meaningful:
                                    build_reason = _sanitize_reason(meaningful[-1])
                                elif lines:
                                    build_reason = _sanitize_reason(lines[-1])
                            except Exception:
                                pass
                    if failure_type == "timeout":
                        build_reason = f"build timed out after {config.get('build_timeout', 0)}s"
                    _record_failure(failed, name, pkg["version"], build_reason,
                                    failure_type or "build", config["failed_path"],
                                    log_path=log_path_for_record)
                    cycle_stats["failed"] += 1
                    log.error(
                        "[%s] build FAILED after %ds (tier: %s)",
                        name,
                        int(duration),
                        tier,
                    )

            except FileNotFoundError as e:
                cycle_stats["failed"] += 1
                log.error("[%s] PKGBUILD not found: %s", name, e)
                _record_failure(failed, name, pkg.get("version", "unknown"),
                                _sanitize_reason(str(e)), "pkgbuild_not_found",
                                config["failed_path"])
            except Exception as e:
                cycle_stats["failed"] += 1
                log.error("[%s] unexpected error: %s", name, e, exc_info=True)
                _record_failure(failed, name, pkg.get("version", "unknown"),
                                _sanitize_reason(str(e)), "build",
                                config["failed_path"])
            finally:
                clear_in_progress(config)

            # Reload pending in case it was modified externally
            pending = load_pending(config["pending_path"])
            writes_since_prune += 1
            if writes_since_prune >= 20:
                removed_now = prune_old_build_logs(config["log_dir"], config["log_retention_days"])
                if removed_now:
                    log.info("Pruned %d old build log(s) during queue processing", removed_now)
                writes_since_prune = 0

            write_metrics(config, {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "processing",
                "pending_start": cycle_stats["pending_start"],
                "pending_end": len(pending),
                "attempted": cycle_stats.get("attempted", 0),
                "succeeded": cycle_stats.get("succeeded", 0),
                "failed": cycle_stats.get("failed", 0),
                "skipped_previous_failure": cycle_stats.get("skipped_previous_failure", 0),
                "skipped_ineligible": cycle_stats.get("skipped_ineligible", 0),
                "skipped_missing_keys": cycle_stats.get("skipped_missing_keys", 0),
            })

        if shutdown_flag:
            break

        # ----- Step 5: Sleep -----
        elapsed = time.time() - cycle_start
        sleep_time = max(0, config["poll_interval"] - elapsed)

        pending_end = len(load_pending(config["pending_path"]))
        metrics = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "sleeping",
            "cycle_seconds": int(elapsed),
            "sleep_seconds": int(sleep_time),
            "pending_start": cycle_stats.get("pending_start", 0),
            "pending_end": pending_end,
            "attempted": cycle_stats.get("attempted", 0),
            "succeeded": cycle_stats.get("succeeded", 0),
            "failed": cycle_stats.get("failed", 0),
            "skipped_previous_failure": cycle_stats.get("skipped_previous_failure", 0),
            "skipped_ineligible": cycle_stats.get("skipped_ineligible", 0),
            "skipped_missing_keys": cycle_stats.get("skipped_missing_keys", 0),
        }
        write_metrics(config, metrics)

        removed_logs = prune_old_build_logs(config["log_dir"], config["log_retention_days"])
        log.info(
            "Cycle complete: pending %d -> %d, attempted=%d ok=%d failed=%d skipped(prev=%d ineligible=%d missing_keys=%d), pruned_logs=%d, sleeping %ds",
            metrics["pending_start"],
            metrics["pending_end"],
            metrics["attempted"],
            metrics["succeeded"],
            metrics["failed"],
            metrics["skipped_previous_failure"],
            metrics["skipped_ineligible"],
            metrics["skipped_missing_keys"],
            removed_logs,
            int(sleep_time),
        )

        # Interruptible sleep — a wake signal cuts it short for an immediate pass.
        sleep_end = time.time() + sleep_time
        while time.time() < sleep_end and not shutdown_flag and not wake_flag:
            time.sleep(1)

    log.info("Buildbot shutting down cleanly.")
