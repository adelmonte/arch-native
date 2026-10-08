"""Command-line interface."""

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from contextlib import nullcontext
from datetime import datetime, timezone

from .build import prepare_gnupg_home
from .config import load_config
from .daemon import run_daemon
from .pacman import _manifest_map, build_pkgbase_map, load_manifest, read_local_packages
from .patches import cmd_patch
from .repo import _run_fsck
from .soname import _deferred_names, _find_soname_breakage, _soname_index_path, soname_lib_base, sync_index
from .state import _DEFERRED_STATUSES, daemon_pid, _is_stalled, _queue_item_for, _queue_lock, diff_manifest, get_built_state, load_failed, load_in_progress, load_pending, prune_stale_queue_entries, save_built_state, save_failed, save_pending
from .util import _load_json_file, _load_json_strict, _sanitize_reason

log = logging.getLogger("buildbot")


def _service_active(config: dict) -> bool:
    return daemon_pid(config) > 0


def _wake_daemon(config: dict) -> bool:
    """Signal the running daemon to run an immediate build pass. Returns True on success."""
    pid = daemon_pid(config)
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
        return True
    except (ProcessLookupError, PermissionError) as e:
        log.debug("could not signal daemon (pid %d): %s", pid, e)
        return False


_STOPPED_MSG = "error: the daemon is running — stop it first (e.g. sudo systemctl stop arch-native)"


def cmd_status(args, config: dict) -> int:
    import fnmatch
    # Colors — match `buildbot --help`
    try:
        from _colorize import can_colorize, get_theme
        if can_colorize():
            ap = get_theme().argparse
            HEAD, LBL, R = ap.heading, ap.label, ap.reset
            BOLD, DIM = "\033[1m", "\033[2m"
            GRN, RED  = "\033[32m", "\033[31m"
        else:
            HEAD = LBL = R = BOLD = DIM = GRN = RED = ""
    except ImportError:
        HEAD = LBL = R = BOLD = DIM = GRN = RED = ""

    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)

    def _age(s):
        if s < 60:    return f"{s}s"
        if s < 3600:  return f"{s // 60}m"
        if s < 86400: return f"{s // 3600}h{(s % 3600) // 60:02d}m"
        return f"{s // 86400}d{(s % 86400) // 3600:02d}h"

    # ─── gather ─────────────────────────────────────────────────────
    pending     = load_pending(config["pending_path"])
    failed_map  = load_failed(config["failed_path"])
    built       = get_built_state(config["state_path"])
    in_progress = load_in_progress(config)
    active      = _service_active(config)
    timeout     = int(config.get("build_timeout") or 0)

    metrics = {}
    metrics_mtime = None
    if os.path.exists(config["metrics_path"]):
        try: metrics_mtime = os.stat(config["metrics_path"]).st_mtime
        except Exception: pass
        try:
            with open(config["metrics_path"]) as f:
                metrics = json.load(f)
        except Exception:
            pass

    # Published patch-health summary (written by the daemon / `buildbot patch status`).
    # Read-only here — recomputing is expensive, so we just surface what's on disk.
    patch_summary = {}
    _patch_path = os.path.join(config["repo_dir"], "patch-status.json")
    if os.path.exists(_patch_path):
        try:
            with open(_patch_path) as f:
                patch_summary = json.load(f)
        except Exception:
            pass

    building = in_progress.get("name") if in_progress else None
    elapsed = 0
    if in_progress and in_progress.get("started_at"):
        try:
            started = datetime.fromisoformat(in_progress["started_at"])
            elapsed = int((now - started).total_seconds())
        except Exception:
            pass

    stale_build  = bool(building and not active)
    timed_out    = bool(building and timeout and elapsed > timeout)

    new_pkgs     = [p for p in pending if p.get("build_reason") == "new"]
    update_pkgs  = [p for p in pending if p.get("build_reason") == "update"]
    soname_pkgs  = [p for p in pending if p.get("build_reason") == "soname"]
    retry_pkgs   = [p for p in pending if p.get("build_reason") not in ("new", "update", "soname")]
    next_pkg     = pending[0]["name"] if pending else None

    recent = []
    for n, e in built.items():
        if not (isinstance(e, dict) and e.get("built_at")):
            continue
        if e.get("status") in _DEFERRED_STATUSES:
            continue
        try:
            ts = datetime.fromisoformat(e["built_at"])
            recent.append((n, e.get("version", "?"), int((now - ts).total_seconds())))
        except Exception:
            continue
    recent.sort(key=lambda x: x[2])

    installed_total = rebuilt_count = blacklisted_count = ineligible_count = cascade_count = 0
    blacklist = config.get("blacklist", [])
    def _bl(name):
        return any(name == e or fnmatch.fnmatch(name, e) for e in blacklist)
    try:
        manifest = (read_local_packages() if config.get("mode") == "local"
                    else load_manifest(config["manifest_path"]))
        installed_total = len(manifest)
        rebuilt_count = sum(1 for p in manifest if p["name"] in built and built[p["name"]].get("status") not in _DEFERRED_STATUSES)
        blacklisted_count = sum(1 for p in manifest if _bl(p["name"]))
        ineligible_count = sum(1 for p in manifest if p["name"] in built and built[p["name"]].get("status") == "ineligible")
        cascade_count = sum(1 for p in manifest if p["name"] in built and built[p["name"]].get("status") == "pending_world_cascade")
    except Exception:
        pass

    repo_name = os.path.basename(config["repo_db"]).split(".db")[0]
    repo_size = ""
    try:
        r = subprocess.run(["du", "-sh", config["repo_dir"]], capture_output=True, text=True)
        if r.returncode == 0:
            repo_size = r.stdout.split()[0]
    except Exception:
        pass

    cycle_str = ""
    if active and metrics_mtime:
        rem = int(config.get("poll_interval", 300) - (now.timestamp() - metrics_mtime))
        cycle_str = f"in {_age(rem)}" if rem > 0 else "running now"

    # ─── render ─────────────────────────────────────────────────────
    def section(title, count=None):
        suffix = f"  {DIM}{count}{R}" if count is not None else ""
        print(f"{HEAD}{title}{R}{suffix}")

    def row(k, v, w=10):
        print(f"  {LBL}{k:<{w}}{R}  {v}")

    # Header
    dot = f"{GRN}●{R}" if active else f"{RED}●{R}"
    state = "active" if active else f"{RED}inactive{R}"
    print(f"{BOLD}arch-native{R}  {dot} {state}")
    print()

    # Building
    if stale_build:
        section("Building")
        row("status", f"{RED}stale — daemon not running{R}")
        row("package", building)
        row("started", f"{_age(elapsed)} ago")
    elif timed_out:
        section("Building")
        row("package", f"{RED}{building}{R}")
        row("elapsed", f"{RED}{_age(elapsed)}  ⚠ exceeded build_timeout ({_age(timeout)}){R}")
    elif building:
        section("Building")
        row("package", f"{BOLD}{building}{R}")
        row("elapsed", _age(elapsed) if elapsed else "just started")
    elif active and metrics.get("status") not in ("sleeping", None):
        # Daemon is mid-cycle (upgrading chroot / checking upstream) but not yet building.
        section("Building", f"{DIM}working — preparing cycle{R}")
    else:
        section("Building", f"{DIM}idle{R}")
    print()

    # Queue — counts plus a short preview of what's up next (folds in `buildbot queue`)
    section("Queue", f"{len(pending)} pending")
    if pending:
        parts = [f"{len(new_pkgs)} new", f"{len(update_pkgs)} updates"]
        if retry_pkgs:
            parts.append(f"{len(retry_pkgs)} retries")
        if soname_pkgs:
            parts.append(f"{len(soname_pkgs)} soname repairs")
        row("breakdown", DIM + " · ".join(parts) + R)
        preview = pending[:5]
        nw = max(len(p["name"]) for p in preview)
        for i, p in enumerate(preview):
            mark = f"{BOLD}▸{R}" if i == 0 else " "
            reason = p.get("build_reason", "?")
            print(f"  {mark} {p['name']:<{nw}}  {DIM}{p.get('version', '?')}  {reason}{R}")
        if len(pending) > len(preview):
            print(f"  {DIM}  +{len(pending) - len(preview)} more — run: buildbot queue{R}")
    print()

    # Recently built
    if recent:
        section("Recently built")
        top = recent[:5]
        nw  = max(len(n) for n, _, _ in top)
        vw  = max(len(v) for _, v, _ in top)
        for n, v, age in top:
            print(f"  {n:<{nw}}  {DIM}{v:<{vw}}{R}  {DIM}{_age(age)} ago{R}")
        print()

    # Failed — split into stalled (needs human attention) and active.
    # A package can be published-ok AND have a failed rebuild attempt; annotate
    # those so the Failed list doesn't read as "broken/missing" (matches `why`).
    def _published_note(name):
        e = built.get(name)
        if (isinstance(e, dict) and e.get("version") and e.get("built_at")
                and e.get("status") not in _DEFERRED_STATUSES):
            return f"  {DIM}(repo has {e['version']}){R}"
        return ""

    if failed_map:
        stalled_rows = [(n, r) for n, r in failed_map.items() if _is_stalled(r, config)]
        active_rows  = [(n, r) for n, r in failed_map.items() if not _is_stalled(r, config)]

        if stalled_rows:
            section(f"Stalled  {DIM}needs attention{R}", str(len(stalled_rows)))
            stalled_rows.sort(key=lambda kv: kv[1].get("timestamp", ""), reverse=True)
            nw = max((len(n) for n, _ in stalled_rows), default=0)
            SHOW = 5
            for n, rec in stalled_rows[:SHOW]:
                age = ""
                ts = rec.get("first_failed_at") or rec.get("timestamp", "")
                if ts:
                    try:
                        age = f"{_age(int((now - datetime.fromisoformat(ts)).total_seconds()))} ago"
                    except Exception: pass
                retries = rec.get("retries", 0)
                reason = _sanitize_reason(rec.get("reason", "unknown"))[:50]
                print(f"  {RED}{n:<{nw}}{R}  {DIM}{age:<10}{R}  {retries}x  {reason}{_published_note(n)}")
            if len(stalled_rows) > SHOW:
                print(f"  {DIM}+{len(stalled_rows) - SHOW} more — run: buildbot failed{R}")
            print()

        if active_rows:
            section("Failed", str(len(active_rows)))
            rows = sorted(active_rows, key=lambda kv: kv[1].get("timestamp", ""), reverse=True)
            SHOW = 5
            nw = max((len(n) for n, _ in rows[:SHOW]), default=0)
            for n, rec in rows[:SHOW]:
                age = ""
                ts = rec.get("timestamp", "")
                if ts:
                    try:
                        age = f"{_age(int((now - datetime.fromisoformat(ts)).total_seconds()))} ago"
                    except Exception: pass
                reason = _sanitize_reason(rec.get("reason", "unknown"))[:60]
                print(f"  {RED}{n:<{nw}}{R}  {DIM}{age:<10}{R}  {reason}{_published_note(n)}")
            if len(rows) > SHOW:
                print(f"  {DIM}+{len(rows) - SHOW} more — run: buildbot failed{R}")
            print()

    # Repo / scan
    section(f"Repo  {DIM}{repo_name}{R}")
    if installed_total:
        pct = rebuilt_count / installed_total * 100
        row("rebuilt", f"{rebuilt_count} / {installed_total}  ({pct:.0f}%)")
        if cascade_count:
            # Show how long each has been staged so a long stall is visible rather
            # than an opaque "pending N". A cascade can wait indefinitely if a world
            # repo never finishes migrating reverse-deps off the old soname.
            staged = []
            for n, e in built.items():
                if isinstance(e, dict) and e.get("status") == "pending_world_cascade" and e.get("built_at"):
                    try:
                        staged.append((n, int((now - datetime.fromisoformat(e["built_at"])).total_seconds())))
                    except Exception:
                        pass
            staged.sort(key=lambda x: -x[1])
            detail = ", ".join(f"{n} {_age(a)}" for n, a in staged[:4])
            oldest = staged[0][1] if staged else 0
            warn = RED if oldest > 14 * 86400 else DIM
            row("pending", f"{cascade_count}  {warn}(staged, awaiting world soname migration: {detail}){R}")
        if blacklisted_count:
            row("blacklisted", f"{blacklisted_count} / {installed_total}  ({blacklisted_count / installed_total * 100:.0f}%)  {DIM}(see /etc/arch-native.conf){R}")
        if ineligible_count:
            reasons = {}
            for p in manifest:
                e = built.get(p["name"])
                if e and e.get("status") == "ineligible":
                    r = e.get("reason") or "unknown"
                    reasons[r] = reasons.get(r, 0) + 1
            why = " · ".join(f"{n} {r}" for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]))
            row("ineligible", f"{ineligible_count} / {installed_total}  ({ineligible_count / installed_total * 100:.0f}%)  {DIM}({why}){R}")
        always_build_count = sum(1 for n in config.get("always_build", []) if n in built)
        if always_build_count:
            row("always-build", f"{always_build_count}  {DIM}(built + kept, not installed){R}")
    if patch_summary.get("total"):
        # Always show all four counts (including zeros) so this matches
        # `buildbot patch status`; highlight review/fail red only when non-zero.
        review, fail = patch_summary.get("review", 0), patch_summary.get("fail", 0)
        bits = [
            f"{patch_summary.get('ok', 0)} ok",
            (f"{RED}{review} review{R}" if review else f"{review} review"),
            (f"{RED}{fail} fail{R}" if fail else f"{fail} fail"),
            f"{patch_summary.get('orphaned', 0)} orphaned",
        ]
        row("patches", f"{patch_summary['total']}  {DIM}({R}" + f"{DIM} · {R}".join(bits) + f"{DIM}){R}")
    if repo_size:
        row("size", repo_size)
    if cycle_str:
        row("next cycle", cycle_str)

    return 0


def cmd_doctor(args, config: dict) -> int:
    checks = []

    def add(name, ok, detail):
        checks.append((name, ok, detail))

    pid = daemon_pid(config)
    add("service", pid > 0, f"running (pid {pid})" if pid else "not running")

    # JSON files
    for path, expected, label in [
        (config["pending_path"], list, "pending"),
        (config["failed_path"], dict, "failed"),
        (config["state_path"], dict, "built_state"),
    ]:
        if not os.path.exists(path):
            add(f"json:{label}", False, "missing")
            continue
        try:
            obj = _load_json_strict(path)
            add(f"json:{label}", isinstance(obj, expected), f"{type(obj).__name__}")
        except Exception as e:
            add(f"json:{label}", False, str(e))

    if config.get("mode") == "local":
        add("manifest", True, "local mode — reads pacman DB directly")
    else:
        add("manifest", os.path.exists(config["manifest_path"]), config["manifest_path"])
    add("chroot_root", os.path.isdir(config["chroot_root"]), config["chroot_root"])
    add("repo_dir", os.path.isdir(config["repo_dir"]), config["repo_dir"])

    # GNUPG mode
    if os.path.isdir(config["gnupg_home"]):
        mode = os.stat(config["gnupg_home"]).st_mode & 0o777
        add("gnupg_mode", mode == 0o700, oct(mode))
    else:
        add("gnupg_mode", False, "missing")

    # Chroot pacman keyring
    chroot_root = config.get("chroot_root", "")
    if os.path.isdir(chroot_root):
        result = subprocess.run(
            ["arch-nspawn", chroot_root, "pacman-key", "--list-keys"],
            capture_output=True, text=True,
        )
        keyring_ok = result.returncode == 0 and "pub" in result.stdout
        add("chroot_keyring", keyring_ok, "populated" if keyring_ok else "empty or uninitialized")
    else:
        add("chroot_keyring", False, f"chroot not found: {chroot_root}")

    # Stuck world-cascade packages: staged but never published. A long stall
    # usually means a distro repo never finished migrating off the old soname.
    try:
        _b = get_built_state(config["state_path"])
        _now = datetime.now(timezone.utc)
        stuck = []
        for n, e in _b.items():
            if isinstance(e, dict) and e.get("status") == "pending_world_cascade" and e.get("built_at"):
                try:
                    days = int((_now - datetime.fromisoformat(e["built_at"])).total_seconds()) // 86400
                    stuck.append((n, days))
                except Exception:
                    pass
        if stuck:
            stuck.sort(key=lambda x: -x[1])
            detail = ", ".join(f"{n} {d}d" for n, d in stuck[:4])
            add("cascades", stuck[0][1] <= 14, f"{len(stuck)} staged ({detail}) — see: buildbot why <pkg>")
        else:
            add("cascades", True, "none staged")
    except Exception as e:
        add("cascades", False, str(e))

    # Packages in the repo linking a soname the repo no longer provides. Reads
    # the cached index only — no scanning, so doctor stays fast.
    try:
        _idx = _load_json_file(_soname_index_path(config), {}, "soname index")
        if not _idx:
            add("sonames", True, "index not built yet")
        else:
            _broken = _find_soname_breakage(_idx, _deferred_names(config))
            _ahead = {n: k["ahead"] for n, k in _broken.items() if k["ahead"]}
            _stale = [n for n, k in _broken.items() if k["stale"] and not k["ahead"]]
            if not _broken:
                add("sonames", True, f"{len(_idx)} packages consistent")
            elif _ahead:
                _libs = sorted({soname_lib_base(x) for v in _ahead.values() for x in v})
                add("sonames", False,
                    f"{len(_ahead)} package(s) need a newer {', '.join(_libs[:3])} "
                    f"than forge builds — rebuild that library first")
            else:
                # A package whose repair stamp still matches its current file
                # was already rebuilt against this same gap and did not come
                # out fixed, so no rebuild is pending for it. Saying otherwise
                # would leave this line permanently red and unread.
                _tried = [n for n in _stale
                          if (_idx[n].get("repair") or {}).get("stamp")
                          == _idx[n].get("stamp")]
                _queued = [n for n in _stale if n not in _tried]
                _parts = []
                if _queued:
                    _parts.append(f"{len(_queued)} queued for rebuild")
                if _tried:
                    _parts.append(
                        f"{len(_tried)} not fixable by rebuild "
                        f"({', '.join(sorted(_tried)[:3])}) — see: buildbot why <pkg>")
                add("sonames", not _queued and bool(_tried),
                    f"{len(_stale)} package(s) link a stale soname: " + "; ".join(_parts))
    except Exception as e:
        add("sonames", False, str(e))

    ok_all = True
    for name, ok, detail in checks:
        print(f"{'OK' if ok else 'FAIL':4} {name:14} {detail}")
        if not ok:
            ok_all = False
    return 0 if ok_all else 1


def cmd_queue_show(args, config: dict) -> int:
    pending = load_pending(config["pending_path"])
    limit = max(1, args.limit) if args.limit else len(pending)
    visible = pending[:limit]
    print(f"pending  {len(pending)}\n")
    name_w    = max((len(p.get("name",    "?")) for p in visible), default=20)
    version_w = max((len(p.get("version", "?")) for p in visible), default=12)
    for idx, pkg in enumerate(visible, 1):
        print(f"  {idx:<4} {pkg.get('name','?'):<{name_w}}  {pkg.get('version','?'):<{version_w}}  {pkg.get('build_reason','?')}")
    if limit < len(pending):
        print(f"  ... {len(pending) - limit} more")
    return 0


def cmd_sync(args, config: dict) -> int:
    daemon_active = _service_active(config)

    manifest = (read_local_packages() if config.get("mode") == "local"
                else load_manifest(config["manifest_path"]))
    manifest_names = {p["name"] for p in manifest}
    built = get_built_state(config["state_path"])
    pkgbase_map = build_pkgbase_map()
    needed = diff_manifest(manifest, built, config["blacklist"], pkgbase_map)

    # A running daemon re-derives the queue itself on its next pass, so just
    # nudge it to start one now.
    if daemon_active and not args.dry_run and not args.reset:
        need_count = len(needed)
        summary = f"{need_count} package(s) need building" if need_count else "queue already up to date"
        if _wake_daemon(config):
            print(f"{summary} — building now (follow: journalctl -fu arch-native)")
        else:
            print(f"{summary} — daemon will pick them up within one poll cycle")
        return 0

    lock_ctx = _queue_lock(config) if not args.dry_run else nullcontext()
    with lock_ctx:
        # Prune stale entries for packages no longer installed
        if not args.dry_run:
            pp, pf = prune_stale_queue_entries(config, manifest_names)
            if pp or pf:
                print(f"pruned stale entries: {pp} pending, {pf} failed removed")

        existing = [] if args.reset else load_pending(config["pending_path"])
        existing_names = {p.get("name") for p in existing}

        added = 0
        for pkg in needed:
            if pkg["name"] not in existing_names:
                existing.append(pkg)
                existing_names.add(pkg["name"])
                added += 1

        if not args.dry_run:
            save_pending(config["pending_path"], existing)

    action = "reset and rebuilt" if args.reset else "updated"
    suffix = "  (dry run)" if args.dry_run else ""
    print(f"queue {action}: {added} added, {len(existing)} total{suffix}")
    if not args.dry_run:
        if daemon_active and _wake_daemon(config):
            print("building now (follow: journalctl -fu arch-native)")
        elif not daemon_active:
            print("start the service to build: sudo systemctl start arch-native")
    return 0


def cmd_queue_retry_failed(args, config: dict) -> int:
    targets_from_args = getattr(args, "packages", [])
    if not args.all and not targets_from_args:
        print("error: specify package name(s) or --all")
        return 2

    manifest_map = _manifest_map(config)

    # Build set of manifest package names for filtering (--all only)
    manifest_names = set(manifest_map.keys())

    lock_ctx = _queue_lock(config) if not args.dry_run else nullcontext()
    with lock_ctx:
        pending = load_pending(config["pending_path"])
        failed = load_failed(config["failed_path"])

        pending_names = {p.get("name") for p in pending}
        targets = set(failed.keys()) if args.all else set(targets_from_args)

        moved = 0
        removed_failed = 0
        skipped_not_installed = 0
        force_rebuild_names = []
        for name in sorted(targets):
            rec = failed.get(name)
            if not rec:
                # Not a failed package — force-rebuild if explicitly named.
                # --all only touches failed packages.
                if args.all:
                    continue
                if name not in manifest_names:
                    skipped_not_installed += 1
                    continue
                if name not in pending_names:
                    q = _queue_item_for(name, manifest_map)
                    q["build_reason"] = "rebuild"
                    pending.append(q)
                    pending_names.add(name)
                    moved += 1
                    force_rebuild_names.append(name)
                continue
            # --all: skip packages not in the current manifest (stale failed entries)
            # Explicit names are always retried regardless.
            if args.all and name not in manifest_names:
                skipped_not_installed += 1
                continue
            if name not in pending_names:
                pending.append(_queue_item_for(name, manifest_map, rec.get("version", "unknown")))
                pending_names.add(name)
                moved += 1
            del failed[name]
            removed_failed += 1

        if not args.dry_run:
            save_pending(config["pending_path"], pending)
            save_failed(config["failed_path"], failed)
            if force_rebuild_names:
                _built = get_built_state(config["state_path"])
                for n in force_rebuild_names:
                    _built.pop(n, None)
                save_built_state(config["state_path"], _built)

    parts = [f"retry: moved {moved} to pending"]
    if skipped_not_installed:
        parts.append(f"skipped {skipped_not_installed} not in manifest")
    print("  ".join(parts) + ("  (dry run)" if args.dry_run else ""))
    return 0


def cmd_failed_list(args, config: dict) -> int:
    failed = load_failed(config["failed_path"])
    rows = sorted(
        failed.items(),
        key=lambda kv: kv[1].get("timestamp", ""),
        reverse=True,
    )
    limit = max(1, args.limit) if args.limit else len(rows)
    print(f"failed  {len(failed)}\n")
    for name, rec in rows[:limit]:
        retries = rec.get("retries", 0)
        retry_str = f"{retries}x"
        ts = rec.get("timestamp", "")[:10]
        reason = _sanitize_reason(rec.get("reason", "unknown"))
        failed_tier = rec.get("failed_tier", "")
        tier_note = f"  [skipping tier: {failed_tier}]" if failed_tier else ""
        log_path = rec.get("log_path", "")
        # Fall back to finding the latest log if stored path is gone
        if not log_path or not os.path.isfile(log_path):
            log_dir_pkg = os.path.join(config["log_dir"], name)
            if os.path.isdir(log_dir_pkg):
                logs = sorted((f for f in os.listdir(log_dir_pkg) if f.endswith(".log")), reverse=True)
                if logs:
                    log_path = os.path.join(log_dir_pkg, logs[0])
        print(f"  {name:<28} {rec.get('version','?'):<20} {retry_str:<5} {ts}  {reason}{tier_note}")
        if log_path:
            print(f"  {'':28} {log_path}")
    if limit < len(rows):
        print(f"  ... {len(rows) - limit} more")
    return 0


def cmd_built(args, config: dict) -> int:
    built = get_built_state(config["state_path"])
    rows = sorted(
        ((k, v) for k, v in built.items() if v.get("status") not in _DEFERRED_STATUSES),
        key=lambda kv: kv[1].get("built_at", ""),
        reverse=True,
    )
    limit = args.limit if args.limit else len(rows)
    print(f"built  {len(rows)}\n")
    for name, rec in rows[:limit]:
        ts = rec.get("built_at", "")[:10]
        ver = rec.get("version", "?")
        flags = " [pgp-skipped]" if rec.get("pgp_skipped") else ""
        # Explain dot-notation pkgrel to the user
        pkgrel = ver.rsplit("-", 1)[-1] if "-" in ver else ""
        rel_note = " *" if "." in pkgrel else ""
        print(f"  {name:<30} {ver:<24}{ts}{flags}{rel_note}")
    if limit < len(rows):
        print(f"  ... {len(rows) - limit} more")
    if any("." in rec.get("version","").rsplit("-",1)[-1] for _, rec in rows[:limit]):
        print("\n  * pkgrel x.N = locally bumped rebuild (upstream pkgrel unchanged)")
    return 0


def cmd_why(args, config: dict) -> int:
    """Explain in plain English why a package is in its current state."""
    pkg = args.package
    built  = get_built_state(config["state_path"])
    failed = load_failed(config["failed_path"])
    pending = load_pending(config["pending_path"])

    # Try to read manifest for installed version
    try:
        if config.get("mode") == "local":
            manifest = read_local_packages()
        else:
            manifest = load_manifest(config["manifest_path"])
        installed = next((p for p in manifest if p["name"] == pkg), None)
    except Exception:
        installed = None

    built_rec   = built.get(pkg)
    failed_rec  = failed.get(pkg)
    pending_rec = next((p for p in pending if p["name"] == pkg), None)
    status      = built_rec.get("status", "ok") if built_rec else None

    installed_ver = installed["version"] if installed else None
    forge_ver     = built_rec.get("version") if built_rec else None

    def _age_str(iso: str) -> str:
        try:
            ts = datetime.fromisoformat(iso)
            secs = int((datetime.now(timezone.utc) - ts).total_seconds())
            if secs < 3600:  return f"{secs // 60}m ago"
            if secs < 86400: return f"{secs // 3600}h ago"
            return f"{secs // 86400}d ago"
        except Exception:
            return ""

    print(f"\n  {pkg}\n")

    if pkg in config.get("always_build", []) and not installed:
        print(f"  Note      always_build — built and kept in forge though not installed\n")

    if pending_rec:
        reason = pending_rec.get("build_reason", "?")
        queued_at = _age_str(pending_rec.get("queued_at", ""))
        print(f"  Status    queued for build ({reason})  {queued_at}")
        if forge_ver:
            print(f"  In forge  {forge_ver}")
        if installed_ver:
            print(f"  Installed {installed_ver}")
        print()
        return 0

    if status == "ok" or (built_rec and status not in _DEFERRED_STATUSES):
        built_at = _age_str(built_rec.get("built_at", ""))
        print(f"  Status    ok — in forge repo")
        print(f"  Version   {forge_ver}  (built {built_at})")
        if installed_ver:
            print(f"  Installed {installed_ver}")
        # A package can be published-ok AND have a newer failed rebuild attempt —
        # both are true. Surface the failed attempt instead of hiding it behind "ok".
        if failed_rec:
            fv = failed_rec.get("version", "?")
            fts = _age_str(failed_rec.get("timestamp", ""))
            freason = _sanitize_reason(failed_rec.get("reason", "unknown"))
            fretries = failed_rec.get("retries", 0)
            print(f"  Last try  {fv} failed {fts} — {freason} ({fretries}x)")
            flog = failed_rec.get("log_path", "")
            if flog:
                print(f"  Log       {flog}")
            print()
            print(f"  The repo copy ({forge_ver}) is fine; only the rebuild to {fv} failed.")
            print(f"  Retry: buildbot retry {pkg}   ·   Dismiss the failure: buildbot clear {pkg}")
        print()
        return 0

    if status == "pending_upstream":
        print(f"  Status    waiting for upstream PKGBUILD to catch up")
        print(f"  Forge had {forge_ver}")
        if installed_ver:
            print(f"  Installed {installed_ver}")
        print()
        print("  The best available PKGBUILD is older than what's installed. Forge will")
        print("  rebuild automatically once the PKGBUILD reaches the installed version.")
        print()
        return 0

    if status == "pending_world_cascade":
        staged_age = _age_str(built_rec.get("built_at", ""))
        print(f"  Status    built, waiting for library soname update in distro repos")
        print(f"  Version   {forge_ver}  (staged {staged_age})")
        # Diagnose each awaited soname: not-yet-provided vs provided-but-a-world
        # reverse-dep still needs the old one (the conservative second gate).
        world = sync_index(config.get("repo_name", ""))
        blocking = []
        for s in built_rec.get("cascade_sonames", []):
            lib = s.split("=", 1)[0]
            if not world.has_soname(s):
                state = "not in distro repos yet"
                blocking.append(s)
            elif world.depends_on_old_soname(lib, s):
                state = "provided, but a distro package still depends on the old soname"
                blocking.append(s)
            else:
                state = "ready"
            print(f"  Soname    {s} — {state}")
        print()
        if blocking:
            print("  It releases automatically once every distro repo has migrated off the old")
            print("  soname. Note: if the distro already ships this version (e.g. via CachyOS),")
            print("  your client already gets it from there, so the staged copy is harmless to")
            print("  leave waiting — it is not blocking your client from having the package.")
        else:
            print("  All sonames are ready — it should publish on the next cycle.")
        print()
        return 0

    if status == "ineligible":
        reason = built_rec.get("reason")
        print(f"  Status    not rebuilt — {reason or 'ineligible'}")
        print(f"  Version   {forge_ver or '—'}")
        print()
        if reason == "arch=any":
            print("  This package is architecture-independent (arch=any) and doesn't benefit")
            print("  from native CPU optimisation, so forge skips it.")
        elif reason == "haskell":
            print("  Haskell packages are locked to the exact GHC they were built with, so")
            print("  forge leaves them to the distro.")
        elif reason:
            print("  Its pkgbase is blacklisted in /etc/arch-native.conf.")
        else:
            print("  Recorded before reasons were kept; it is re-checked on the next cycle.")
        print()
        return 0

    if failed_rec:
        retries = failed_rec.get("retries", 0)
        reason  = failed_rec.get("reason", "unknown")
        failed_tier = failed_rec.get("failed_tier", "")
        ts = _age_str(failed_rec.get("timestamp", ""))
        log_path = failed_rec.get("log_path", "")
        print(f"  Status    failed  ({retries} attempt{'s' if retries != 1 else ''})")
        if installed_ver:
            print(f"  Installed {installed_ver}")
        print(f"  Reason    {reason}")
        if failed_tier:
            print(f"  Tier skip [{failed_tier} skipped due to earlier dep/source error]")
        if ts:
            print(f"  Last try  {ts}")
        if log_path:
            print(f"  Log       {log_path}")
        print()
        print(f"  Re-queue: buildbot retry {pkg}")
        print(f"  Dismiss:  buildbot clear {pkg}")
        print()
        return 0

    if installed and not built_rec:
        print(f"  Status    never attempted")
        print(f"  Installed {installed_ver}")
        print()
        print("  This package is installed but forge has never tried to build it.")
        print("  It will be queued on the next build cycle.")
        print()
        return 0

    print(f"  {pkg!r} is not installed and has no forge build record.")
    print()
    return 1


def cmd_logs(args, config: dict) -> int:
    log_dir = os.path.join(config["log_dir"], args.package)
    if not os.path.isdir(log_dir):
        print(f"no logs found for {args.package!r}")
        return 1
    logs = sorted(
        (f for f in os.listdir(log_dir) if f.endswith(".log")),
        reverse=True,
    )
    if not logs:
        print(f"no logs found for {args.package!r}")
        return 1
    log_path = os.path.join(log_dir, logs[0])
    if args.follow:
        os.execlp("tail", "tail", "-f", log_path)
    else:
        with open(log_path, "r", errors="replace") as f:
            sys.stdout.write(f.read())
    return 0


def cmd_failed_clear(args, config: dict) -> int:
    targets_from_args = getattr(args, "packages", [])
    if not args.all and not targets_from_args:
        print("error: specify package name(s) or --all")
        return 2

    lock_ctx = _queue_lock(config) if not args.dry_run else nullcontext()
    with lock_ctx:
        failed = load_failed(config["failed_path"])
        if args.all:
            removed = len(failed)
            new_failed = {}
        else:
            targets = set(targets_from_args)
            removed = sum(1 for n in targets if n in failed)
            new_failed = {k: v for k, v in failed.items() if k not in targets}

        if not args.dry_run:
            save_failed(config["failed_path"], new_failed)

    print(f"cleared {removed} from failed" + ("  (dry run)" if args.dry_run else ""))
    return 0


def cmd_fsck(args, config: dict) -> int:
    """
    Verify and repair consistency between built.json, the forge repo DB,
    and physical package files. Repairs SIGKILL-race divergences automatically.
    Requires the service to be stopped (unless --force is given).
    """
    if not getattr(args, "force", False) and _service_active(config):
        print(_STOPPED_MSG)
        return 2
    dry_run = getattr(args, "dry_run", False)
    verbose = getattr(args, "verbose", False)
    found, _ = _run_fsck(config, dry_run=dry_run, verbose=verbose)
    return 1 if (dry_run and found) else 0


_DEVTOOLS_PACMAN_CONF = "/usr/share/devtools/pacman.conf.d/extra.conf"
_CACHYOS_KEY = "882DCFE48E2051D48E2562ABF3B607488DB35A47"


def _default_chroot_pacman_conf(distro: str) -> str:
    """The build chroot's pacman.conf when chroot_pacman_conf is unset."""
    if distro == "artix":
        candidates = ["/etc/arch-native/chroot-pacman.conf",
                      "/usr/share/arch-native/chroot-pacman.conf"]
    else:
        candidates = ["/etc/arch-native/chroot-pacman.conf", _DEVTOOLS_PACMAN_CONF]
    return next((c for c in candidates if os.path.isfile(c)), "")


def _gpg_cmd(config: dict) -> list[str]:
    cmd = ["gpg", "--homedir", config["gnupg_home"], "--batch"]
    if os.getuid() == 0:
        cmd = ["runuser", "-u", config["build_user"], "--"] + cmd
    return cmd


def _ensure_signing_key(config: dict) -> str:
    """Create the repo signing key if there is none and export its public half.

    Returns the key fingerprint, or "" if gpg failed.
    """
    gpg = _gpg_cmd(config)

    def fingerprint():
        r = subprocess.run(gpg + ["--list-secret-keys", "--with-colons"],
                           capture_output=True, text=True)
        fprs = [l.split(":")[9] for l in r.stdout.splitlines() if l.startswith("fpr:")]
        return fprs[0] if fprs else ""

    fpr = fingerprint()
    if fpr:
        print(f"  skip signing key exists: {fpr}")
    else:
        params = ("%no-protection\nKey-Type: EdDSA\nKey-Curve: ed25519\n"
                  "Name-Real: arch-native\nName-Email: arch-native@localhost\n"
                  "Expire-Date: 0\n%commit\n")
        r = subprocess.run(gpg + ["--gen-key"], input=params, capture_output=True, text=True)
        fpr = fingerprint()
        if r.returncode != 0 or not fpr:
            print(f"  error: signing key generation failed: {r.stderr.strip()[:200]}")
            return ""
        print(f"  ok  signing key created: {fpr}")

    out = os.path.join(config["repo_dir"], "buildbot-public.asc")
    r = subprocess.run(gpg + ["--export", "--armor", fpr], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout:
        with open(out, "w") as f:
            f.write(r.stdout)
        print(f"  ok  public key exported: {out}")
    else:
        print(f"  warning: could not export the public key: {r.stderr.strip()[:200]}")
    return fpr


def cmd_init(args, config: dict) -> int:
    """
    Bootstrap a new arch-native installation:
      1. Create /var/lib/arch-native directory layout
      2. Create the makechrootpkg chroot with mkarchroot
      3. Initialize the chroot's pacman keyring
      4. Set up the build user's GPG homedir and signing key
    Safe to re-run — skips steps that are already done.
    """
    chroot_root = config["chroot_root"]
    gnupg_home  = config["gnupg_home"]
    build_user  = config["build_user"]
    distro      = config["distro"]

    # 1. Create directory layout
    for d in [config["chroot_dir"], config["repo_dir"], config["log_dir"], gnupg_home,
              config["pkgbuilds_dir"], config["makepkg_configs_dir"],
              os.path.dirname(config["manifest_path"])]:
        os.makedirs(d, exist_ok=True)
        print(f"  dir  {d}")

    # 2. Create chroot with mkarchroot if it doesn't exist
    pacman_conf = config.get("chroot_pacman_conf") or _default_chroot_pacman_conf(distro)
    if os.path.isdir(chroot_root):
        print(f"  skip chroot already exists: {chroot_root}")
    else:
        if not pacman_conf or not os.path.isfile(pacman_conf):
            print("error: no pacman.conf for the build chroot")
            print("  set chroot_pacman_conf in /etc/arch-native.conf, e.g.:")
            print(f"    chroot_pacman_conf = {_DEVTOOLS_PACMAN_CONF}")
            return 1
        print(f"  creating chroot at {chroot_root} from {pacman_conf} ...")
        result = subprocess.run(
            ["mkarchroot", "-C", pacman_conf, chroot_root, "base-devel"],
            text=True,
        )
        if result.returncode != 0:
            print("error: mkarchroot failed")
            return 1
        print("  chroot created")

    # 3. Initialize chroot pacman keyring
    print("  initializing chroot pacman keyring ...")
    keyring_cmds = [["pacman-key", "--init"], ["pacman-key", "--populate"]]
    chroot_conf = os.path.join(chroot_root, "etc/pacman.conf")
    try:
        uses_cachyos = "[cachyos" in open(chroot_conf).read()
    except OSError:
        uses_cachyos = False
    if uses_cachyos:
        keyring_cmds += [["pacman-key", "--recv-keys", _CACHYOS_KEY],
                         ["pacman-key", "--lsign-key", _CACHYOS_KEY]]
    for cmd in keyring_cmds:
        result = subprocess.run(["arch-nspawn", chroot_root] + cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  warning: {' '.join(cmd)} returned {result.returncode}")
            print(f"    {result.stderr.strip()[:200]}")
        else:
            print(f"  ok  {' '.join(cmd)}")

    # 4. Build user GPG homedir and the repo signing key
    prepare_gnupg_home(gnupg_home, build_user)
    print(f"  gnupg homedir ready: {gnupg_home}")
    fpr = _ensure_signing_key(config)

    # 5. Artix-specific: deploy artix-meson
    if distro == "artix":
        for src in [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artix-meson"),
            "/usr/share/arch-native/artix-meson",
        ]:
            if os.path.isfile(src):
                dest = os.path.join(chroot_root, "usr/local/bin/artix-meson")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                os.chmod(dest, 0o755)
                print("  deployed artix-meson into chroot")
                break

    print("\narch-native init complete. Next:")
    print("  1. Check the blacklist in /etc/arch-native.conf")
    print("  2. Start the daemon: sudo systemctl enable --now arch-native")
    print("  3. On each client, add the repo to pacman.conf and trust the key:")
    print(f"       sudo pacman-key --add buildbot-public.asc && sudo pacman-key --lsign-key {fpr or 'arch-native@localhost'}")
    return 0 if fpr else 1


def run_cli(args, config: dict) -> int:
    dispatch = {
        "status":  cmd_status,
        "doctor":  cmd_doctor,
        "built":   cmd_built,
        "why":     cmd_why,
        "logs":    cmd_logs,
        "queue":   cmd_queue_show,
        "failed":  cmd_failed_list,
        "retry":   cmd_queue_retry_failed,
        "clear":   cmd_failed_clear,
        "sync":    cmd_sync,
        "fsck":    cmd_fsck,
        "init":    cmd_init,
        "patch":   cmd_patch,
    }
    fn = dispatch.get(args.command)
    if fn:
        return fn(args, config)
    print("error: unknown command")
    return 2


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Hide the auto-generated subcommand list; we supply our own table."""
    def _format_action(self, action):
        if hasattr(action, '_name_parser_map'):
            return ""
        return super()._format_action(action)


def main():
    try:
        from _colorize import can_colorize, get_theme
        _t = get_theme().argparse if can_colorize() else None
    except ImportError:
        _t = None
    _b = (lambda s: f"{_t.heading}{s}{_t.reset}") if _t else (lambda s: s)

    _DESC = f"""\
{_b("commands:")}
    buildbot status
    buildbot doctor
    buildbot built    [-n N]
    buildbot why      <PKG>
    buildbot logs     <PKG> [-f]
    buildbot queue    [-n N]
    buildbot failed   [-n N]
    buildbot retry    <PKG> [--all] [--dry-run]
    buildbot clear    <PKG> [--all] [--dry-run]
    buildbot sync     [--reset] [--dry-run]
    buildbot fsck     [--dry-run] [-v]
    buildbot init
    buildbot patch    {{create|show|check|status}} <PKG>"""

    parser = argparse.ArgumentParser(
        prog="buildbot",
        usage="buildbot <command> [...]",
        description=_DESC,
        epilog="use 'buildbot <command> --help' for available options",
        formatter_class=_HelpFormatter,
    )
    parser.add_argument(
        "--config",
        default="/etc/arch-native.conf",
        metavar="FILE",
        help="config file  (default: /etc/arch-native.conf)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="verbose logging  (daemon only)",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("status",
        formatter_class=_HelpFormatter,
        description="Show service state, the package currently building, queue counts, "
                    "and stats from the most recent poll cycle.")
    sub.add_parser("doctor",
        formatter_class=_HelpFormatter,
        description="Verify that all required paths exist, JSON state files are valid, "
                    "the GPG home has correct permissions (0700), and the chroot pacman "
                    "keyring is initialised.  Safe to run while the service is active.")

    p_built = sub.add_parser("built",
        usage="buildbot built [-n N]",
        formatter_class=_HelpFormatter,
        description="List packages successfully rebuilt by arch-native, newest first.")
    p_built.add_argument("-n", type=int, default=0, dest="limit", metavar="N",
        help="max entries to show  (default: all)")

    p_why = sub.add_parser("why",
        usage="buildbot why <PKG>",
        formatter_class=_HelpFormatter,
        description="Explain in plain English why PKG is in its current state — "
                    "ok, failed, deferred to distro, waiting for upstream, etc.")
    p_why.add_argument("package", metavar="PKG")

    p_logs = sub.add_parser("logs",
        usage="buildbot logs <PKG> [-f]",
        formatter_class=_HelpFormatter,
        description="Print the latest build log for PKG.  "
                    "Use -f to follow in real time.")
    p_logs.add_argument("package", metavar="PKG")
    p_logs.add_argument("-f", "--follow", action="store_true",
        help="follow the log in real time  (like tail -f)")

    p_queue = sub.add_parser("queue",
        usage="buildbot queue [-n N]",
        formatter_class=_HelpFormatter,
        description="List the pending build queue.")
    p_queue.add_argument("-n", type=int, default=25, dest="limit", metavar="N",
        help="max entries to show  (default: 25)")

    p_failed = sub.add_parser("failed",
        usage="buildbot failed [-n N]",
        formatter_class=_HelpFormatter,
        description="List failed builds with failure reason and retry count.")
    p_failed.add_argument("-n", type=int, default=0, dest="limit", metavar="N",
        help="max entries to show  (default: all)")

    p_retry = sub.add_parser("retry",
        usage="buildbot retry <PKG> [--all] [--dry-run]",
        formatter_class=_HelpFormatter,
        description="Re-queue failed package(s) for another build attempt.  "
                    "If a package is not in the failed list it is force-queued for "
                    "a fresh rebuild (its built.json entry is cleared first).")
    p_retry.add_argument("packages", nargs="*", metavar="PKG")
    p_retry.add_argument("--all", action="store_true",
        help="re-queue every failed package")
    p_retry.add_argument("--dry-run", action="store_true",
        help="show what would be re-queued without writing")

    p_clear = sub.add_parser("clear",
        usage="buildbot clear <PKG> [--all] [--dry-run]",
        formatter_class=_HelpFormatter,
        description="Remove package(s) from the failed list without re-queuing them.")
    p_clear.add_argument("packages", nargs="*", metavar="PKG")
    p_clear.add_argument("--all", action="store_true",
        help="clear the entire failed list")
    p_clear.add_argument("--dry-run", action="store_true",
        help="show what would be cleared without writing")

    p_sync = sub.add_parser("sync",
        usage="buildbot sync [--reset] [--dry-run]",
        formatter_class=_HelpFormatter,
        description="Diff the installed package list against built.json and queue any "
                    "new or updated packages.  When the daemon is running it is signalled "
                    "to build immediately; when stopped, the queue is written for the next "
                    "start.")
    p_sync.add_argument("--reset", action="store_true",
        help="clear the existing queue first, then rebuild from scratch")
    p_sync.add_argument("--dry-run", action="store_true",
        help="show what would be queued without writing")

    p_fsck = sub.add_parser("fsck",
        usage="buildbot fsck [--dry-run] [-v]",
        formatter_class=_HelpFormatter,
        description="Check and repair consistency between built.json, the forge repo DB, "
                    "and physical .pkg.tar.zst files.  Fixes divergences caused by SIGKILL "
                    "during a build cycle.  Requires the service to be stopped first.")
    p_fsck.add_argument("--dry-run", action="store_true",
        help="report issues without making any changes")
    p_fsck.add_argument("-v", "--verbose", action="store_true",
        help="also print OK entries")
    p_fsck.add_argument("--force", action="store_true",
        help="run even if the service is active (unsafe)")

    sub.add_parser("init",
        formatter_class=_HelpFormatter,
        description="Initialise a new arch-native installation: create the directory "
                    "layout under /var/lib/arch-native/, build the clean devtools chroot, "
                    "initialise the pacman keyring, and create the repo signing key "
                    "(exported to the repo as buildbot-public.asc).  "
                    "Safe to re-run — skips steps already complete.")

    p_patch = sub.add_parser("patch",
        usage="buildbot patch {create|show|check|ack|status} <PKG>",
        formatter_class=_HelpFormatter,
        description="Manage local PKGBUILD patches stored in "
                    "/var/lib/arch-native/pkgbuilds/local/.")
    patch_sub = p_patch.add_subparsers(dest="patch_cmd", metavar="SUBCOMMAND")

    p_pc = patch_sub.add_parser("create",
        usage="buildbot patch create [--force] <PKG>",
        formatter_class=_HelpFormatter,
        description="Open the upstream PKGBUILD for PKG in $EDITOR and save the diff "
                    "as a local patch.")
    p_pc.add_argument("pkgname", metavar="PKG")
    p_pc.add_argument("--force", action="store_true",
        help="overwrite an existing patch with a fresh copy of upstream")

    p_ps = patch_sub.add_parser("show",
        usage="buildbot patch show <PKG>",
        formatter_class=_HelpFormatter,
        description="Print the local patch for PKG.")
    p_ps.add_argument("pkgname", metavar="PKG")

    p_pck = patch_sub.add_parser("check",
        usage="buildbot patch check <PKG>  |  buildbot patch check --all",
        formatter_class=_HelpFormatter,
        description="Verify that the local patch for PKG (or all patches with --all) "
                    "still applies cleanly against the current upstream PKGBUILD.")
    p_pck_group = p_pck.add_mutually_exclusive_group(required=True)
    p_pck_group.add_argument("pkgname", nargs="?", metavar="PKG")
    p_pck_group.add_argument("--all", action="store_true",
        help="check every local patch")

    p_pa = patch_sub.add_parser("ack",
        usage="buildbot patch ack [--permanent] <PKG>  |  buildbot patch ack [--permanent] --all",
        formatter_class=_HelpFormatter,
        description="Record that a patch has been reviewed against the current upstream "
                    "PKGBUILD, clearing its 'review' flag without touching the diff. "
                    "With --permanent the patch is never flagged for version drift again "
                    "— for patches that fix this build setup (cross-march host, Artix "
                    "libexec layout, a broken LTO build) rather than an upstream defect.")
    p_pa_group = p_pa.add_mutually_exclusive_group(required=True)
    p_pa_group.add_argument("pkgname", nargs="?", metavar="PKG")
    p_pa_group.add_argument("--all", action="store_true",
        help="ack every patch currently in review")
    p_pa.add_argument("--permanent", action="store_true",
        help="exempt from version-drift review for good; still checked for applying")

    patch_sub.add_parser("status",
        usage="buildbot patch status",
        formatter_class=_HelpFormatter,
        description="Recompute patch health and publish patch-status.json into "
                    "the repo dir for clients (native-sync) to read.")

    args = parser.parse_args()
    if args.command:
        config = load_config(args.config)
        rc = run_cli(args, config)
        raise SystemExit(rc)

    run_daemon(args.config, args.debug)
