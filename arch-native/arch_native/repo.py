"""Publish to and prune the pacman repo; fsck."""

import functools
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .pacman import _read_forge_db
from .resolve import git_lock
from .state import _DEFERRED_STATUSES, get_built_state, save_built_state
from .util import _in_blacklist, _pkgname_from_filename, _ver_from_pkg_path, vercmp

log = logging.getLogger("buildbot")


def _prune_stale_versions(repo_dir: str, newly_added: list[str], keep: int = 1) -> list[str]:
    """For each newly added pkg, keep the `keep` highest-version files for the same pkgname,
    delete the rest (and their .sig siblings). Sorts by package version via vercmp."""
    new_pkgnames = {_pkgname_from_filename(f) for f in newly_added}

    pruned = []
    for pkgname in new_pkgnames:
        files = list(
            f for f in Path(repo_dir).glob("*.pkg.tar.zst")
            if _pkgname_from_filename(f.name) == pkgname
        )
        files.sort(
            key=functools.cmp_to_key(
                lambda a, b: vercmp(_ver_from_pkg_path(a) or "0", _ver_from_pkg_path(b) or "0")
            ),
            reverse=True,
        )
        for f in files[keep:]:
            f.unlink()
            sig = Path(str(f) + ".sig")
            if sig.exists():
                sig.unlink()
            pruned.append(f.name)
            log.info("Pruned stale: %s", f.name)
    return pruned


def add_to_repo(pkg_files: list[str], repo_db_path: str, repo_dir: str,
                autoprune: bool = True, autoprune_keep: int = 1):
    """
    Move packages + sigs to repo dir, run repo-add, then optionally prune older
    versions of the same pkgname so the repo dir doesn't accumulate orphans.
    Flags: -v (verbose) -p (prevent downgrade)
    """
    moved = []
    for f in pkg_files:
        dest = os.path.join(repo_dir, os.path.basename(f))
        shutil.move(f, dest)
        moved.append(dest)
        sig = f + ".sig"
        if os.path.exists(sig):
            shutil.move(sig, os.path.join(repo_dir, os.path.basename(sig)))

    cmd = ["repo-add", "-v", "-p", repo_db_path] + moved
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 and result.returncode != 1:
        raise RuntimeError(f"repo-add failed: {result.stderr}")
    log.info("repo-add: added %d package(s)", len(moved))

    if autoprune:
        _prune_stale_versions(repo_dir, moved, keep=autoprune_keep)
    return moved


def stage_packages(pkg_files: list[str], repo_dir: str) -> list[str]:
    """Move packages + sigs to repo_dir without adding to the repo DB.
    Used when a library soname bump must wait for world repos to catch up.
    Returns list of staged filenames (basenames only)."""
    staged = []
    for f in pkg_files:
        dest = os.path.join(repo_dir, os.path.basename(f))
        shutil.move(f, dest)
        staged.append(os.path.basename(dest))
        sig = f + ".sig"
        if os.path.exists(sig):
            shutil.move(sig, os.path.join(repo_dir, os.path.basename(sig)))
    return staged


def prune_blacklisted_from_repo(
    blacklist: list[str],
    built: dict,
    repo_db_path: str,
    repo_dir: str,
) -> list[str]:
    """Remove blacklisted packages from the repo db and delete their files.

    Returns the list of package names removed.
    """
    # Split packages share the full pkg_files list across all subpackage entries,
    # so a blacklisted name sharing files with a non-blacklisted entry is a
    # subpackage of a pkgbase we do rebuild. Those must stay in the db: the sibling
    # we publish carries a hard `libfoo=<pkgver>` dep that only our own build can
    # satisfy once forge's pkgver runs ahead of the world repo — dropping libudev
    # left udev 261.2-1 needing libudev=261.2, which world's libudev 261-2 can't
    # fill. Blacklisting only means "never resolve this as its own pkgbase".
    sibling_files = set()
    for name in built:
        if not _in_blacklist(name, blacklist):
            sibling_files.update(built[name].get("pkg_files", []))

    to_remove = []
    for name in built:
        if not _in_blacklist(name, blacklist):
            continue
        if built[name].get("status") == "ineligible":
            continue
        if any(f in sibling_files for f in built[name].get("pkg_files", [])):
            log.debug("Keeping blacklisted %s: subpackage of a rebuilt pkgbase", name)
            continue
        to_remove.append(name)
    if not to_remove:
        return []

    result = subprocess.run(
        ["repo-remove", repo_db_path] + to_remove,
        capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        log.warning("repo-remove returned %d: %s", result.returncode, result.stderr)

    # Split packages share the full pkg_files list across all subpackage entries.
    # Only delete a file if no non-blacklisted entry still references it — otherwise
    # removing a blacklisted subpackage (e.g. libudev) would delete the main
    # package file (udev-*.pkg.tar.zst) that the non-blacklisted udev entry also lists.
    protected = set()
    for name in built:
        if name not in to_remove:
            for fname in built[name].get("pkg_files", []):
                protected.add(fname)

    for name in to_remove:
        pkg_files = built[name].get("pkg_files", [])
        for fname in pkg_files:
            if fname in protected:
                continue
            for path in [os.path.join(repo_dir, fname),
                         os.path.join(repo_dir, fname + ".sig")]:
                if os.path.exists(path):
                    os.remove(path)

    log.info("Removed %d blacklisted package(s) from repo: %s", len(to_remove), ", ".join(to_remove))
    return to_remove


def prune_uninstalled_from_repo(
    manifest_names: set,
    built: dict,
    repo_db_path: str,
    repo_dir: str,
) -> list[str]:
    """Remove packages that are no longer in the client manifest from the repo.

    Returns the list of package names removed.
    """
    to_remove = [
        name for name in built
        if name not in manifest_names
        and built[name].get("status") != "ineligible"
    ]
    if not to_remove:
        return []

    result = subprocess.run(
        ["repo-remove", repo_db_path] + to_remove,
        capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        log.warning("repo-remove returned %d: %s", result.returncode, result.stderr)

    # Split packages share the full pkg_files list across all subpackage entries.
    # Only delete a file if no installed package still references it — otherwise
    # removing an uninstalled subpackage (e.g. aom-docs) would delete the main
    # package file (aom-3.x.pkg.tar.zst) that the installed aom entry also lists.
    protected = set()
    for name in built:
        if name in manifest_names:
            for fname in built[name].get("pkg_files", []):
                protected.add(fname)

    for name in to_remove:
        pkg_files = built[name].get("pkg_files", [])
        for fname in pkg_files:
            if fname in protected:
                continue
            for path in [os.path.join(repo_dir, fname),
                         os.path.join(repo_dir, fname + ".sig")]:
                if os.path.exists(path):
                    os.remove(path)

    log.info("Removed %d uninstalled package(s) from repo: %s", len(to_remove), ", ".join(to_remove))
    return to_remove


def prune_stale_pkgbuild_clones(
    pkgbuilds_dir: str,
    tier_sources: dict,
    current_names: set[str],
) -> list[str]:
    """Delete per-package PKGBUILD clone dirs for packages no longer in use.

    Only applies to clone-type and pkgctl-type tiers (one directory per package).
    Monorepo tiers (a single git checkout covering all packages) are not touched.

    current_names: set of package names to keep (manifest names currently
    installed on the desktop).
    """
    import shutil
    removed = []
    for tier, src in tier_sources.items():
        if src.get("type") not in ("clone", "pkgctl"):
            continue
        tier_dir = os.path.join(pkgbuilds_dir, tier)
        if not os.path.isdir(tier_dir):
            continue
        for entry in os.listdir(tier_dir):
            full = os.path.join(tier_dir, entry)
            if not os.path.isdir(full):
                continue
            if entry not in current_names:
                try:
                    shutil.rmtree(full)
                    removed.append(f"{tier}/{entry}")
                except Exception as e:
                    log.warning("Failed to remove stale PKGBUILD clone %s: %s", full, e)
    if removed:
        log.info("Removed %d stale PKGBUILD clone(s): %s", len(removed), ", ".join(removed))
    return removed


def _run_fsck(config: dict, dry_run: bool = False, verbose: bool = False) -> tuple[int, int]:
    """
    Check and repair consistency between built.json, the forge repo DB, and
    physical .pkg.tar.zst files. Returns (issues_found, issues_repaired).

    Repair table:
      file ✓  db ✓  built ✓  → ok
      file ✓  db ✓  built ✗  → reanimate built.json from DB metadata
      file ✓  db ✗  built ✓  → reanimate: repo-add the file
      file ✓  db ✗  built ✗  → orphan file: delete
      file ✗  db ✓  built ✓  → broken: repo-remove + clear built.json
      file ✗  db ✓  built ✗  → broken: repo-remove
      file ✗  db ✗  built ✓  → broken: clear built.json entry

    Covers the common SIGKILL race: build finished, files copied to repo_dir,
    but repo-add or the built.json write didn't complete before the kill.
    """
    def _ver_from_fname(fname: str) -> str:
        """Extract pkgver-pkgrel from a package filename."""
        for ext in (".pkg.tar.zst", ".pkg.tar.xz"):
            if fname.endswith(ext):
                fname = fname[:-len(ext)]
                break
        parts = fname.rsplit("-", 3)
        return f"{parts[1]}-{parts[2]}" if len(parts) == 4 else ""

    built = get_built_state(config["state_path"])
    repo_db = config["repo_db"]
    repo_dir = config["repo_dir"]

    db_pkgs = _read_forge_db(repo_db)

    physical: dict[str, list[str]] = {}
    if os.path.isdir(repo_dir):
        for fname in os.listdir(repo_dir):
            if fname.endswith(".pkg.tar.zst"):
                pn = _pkgname_from_filename(fname)
                physical.setdefault(pn, []).append(fname)

    staged_names = {n for n, r in built.items() if r.get("status") == "pending_world_cascade"}
    built_pkgs = {n: r for n, r in built.items() if r.get("status") not in _DEFERRED_STATUSES}
    all_names = (set(db_pkgs) | set(physical) | set(built_pkgs)) - staged_names

    # Collect repairs as (action, name, extra)
    repairs = []

    # Duplicate DB entries — same package name, multiple versions recorded
    for name, db_rec in db_pkgs.items():
        if db_rec.get("dup_versions"):
            repairs.append(("remove_dup_db", name, []))

    for name in sorted(all_names):
        in_db    = name in db_pkgs
        in_files = bool(physical.get(name))
        in_built = name in built_pkgs

        if in_db and in_files and in_built:
            # Check for pkgrel drift: a higher-version file exists in the repo dir
            # than what the DB (and built.json) record. Happens when the DB is
            # repaired/reset but the newer pkg file survived on disk.
            db_ver = db_pkgs[name].get("version", "")
            best_file = None
            best_ver = db_ver
            for fname in physical.get(name, []):
                fver = _ver_from_fname(fname)
                if fver and vercmp(fver, best_ver) > 0:
                    best_ver = fver
                    best_file = fname
            if best_file:
                repairs.append(("fix_drift", name, [best_file]))
            else:
                if verbose:
                    print(f"  ok      {name}")
            continue
        elif in_files and in_db and not in_built:
            repairs.append(("reanimate_built", name, physical[name]))
        elif in_files and not in_db and in_built:
            repairs.append(("reanimate_db", name, physical[name]))
        elif in_files and not in_db and not in_built:
            repairs.append(("delete_orphan", name, physical[name]))
        elif not in_files and in_db and in_built:
            repairs.append(("remove_db_clear_built", name, []))
        elif not in_files and in_db and not in_built:
            repairs.append(("remove_db", name, []))
        elif not in_files and not in_db and in_built:
            repairs.append(("clear_built", name, []))

    if not repairs:
        print("fsck: all consistent")
        return 0, 0

    label = {
        "reanimate_built":    "reanimate built.json",
        "reanimate_db":       "reanimate db",
        "delete_orphan":      "delete orphan file",
        "remove_db_clear_built": "remove db + clear built",
        "remove_db":          "remove db",
        "clear_built":        "clear built.json",
        "remove_dup_db":      "remove duplicate db entry",
        "fix_drift":          "fix pkgrel drift",
    }
    nw = max(len(n) for _, n, _ in repairs)
    for action, name, _ in repairs:
        print(f"  {label[action]:<22}  {name}")

    if dry_run:
        print(f"\nfsck: {len(repairs)} issue(s) — re-run without --dry-run to repair")
        return len(repairs), 0

    repaired = 0
    for action, name, files in repairs:
        try:
            if action == "reanimate_db":
                paths = [os.path.join(repo_dir, f) for f in files]
                r = subprocess.run(["repo-add", "-p", repo_db] + paths, capture_output=True)
                if r.returncode in (0, 1):
                    repaired += 1
                    log.info("fsck: repo-add %s", name)
                else:
                    log.warning("fsck: repo-add %s failed: %s", name, r.stderr.decode())

            elif action == "reanimate_built":
                db_rec = db_pkgs[name]
                built[name] = {
                    "version": db_rec["version"],
                    "built_at": datetime.now(timezone.utc).isoformat(),
                    "pkg_files": files,
                }
                save_built_state(config["state_path"], built)
                repaired += 1
                log.info("fsck: reanimated built.json for %s", name)

            elif action == "delete_orphan":
                for fname in files:
                    fpath = os.path.join(repo_dir, fname)
                    try:
                        os.remove(fpath)
                    except FileNotFoundError:
                        pass
                    sig = fpath + ".sig"
                    if os.path.exists(sig):
                        os.remove(sig)
                repaired += 1
                log.info("fsck: deleted orphan files for %s", name)

            elif action in ("remove_db_clear_built", "remove_db"):
                r = subprocess.run(["repo-remove", repo_db, name], capture_output=True)
                if r.returncode in (0, 1):
                    if action == "remove_db_clear_built" and name in built:
                        del built[name]
                        save_built_state(config["state_path"], built)
                    repaired += 1
                    log.info("fsck: repo-remove %s", name)
                else:
                    log.warning("fsck: repo-remove %s failed: %s", name, r.stderr.decode())

            elif action == "clear_built":
                if name in built:
                    del built[name]
                    save_built_state(config["state_path"], built)
                repaired += 1
                log.info("fsck: cleared built.json entry for %s", name)

            elif action == "remove_dup_db":
                # repo-remove clears ALL versions for this name; re-add from disk
                r = subprocess.run(["repo-remove", repo_db, name], capture_output=True)
                if r.returncode in (0, 1):
                    pkg_file = None
                    for fname in physical.get(name, []):
                        fp = os.path.join(repo_dir, fname)
                        if os.path.exists(fp):
                            pkg_file = fp
                            break
                    if pkg_file:
                        subprocess.run(["repo-add", "-p", repo_db, pkg_file], capture_output=True)
                    repaired += 1
                    log.info("fsck: deduped DB for %s, re-added %s", name, pkg_file)
                else:
                    log.warning("fsck: repo-remove %s failed: %s", name, r.stderr.decode())

            elif action == "fix_drift":
                # DB has an older pkgrel than the highest-version file on disk;
                # re-add the newer file so the DB and built.json stay in sync.
                old_ver = db_pkgs[name].get("version", "?")
                fpath = os.path.join(repo_dir, files[0])
                r = subprocess.run(["repo-add", "-p", repo_db, fpath], capture_output=True)
                if r.returncode in (0, 1):
                    new_ver = _ver_from_fname(files[0])
                    if new_ver and name in built:
                        built[name]["version"] = new_ver
                        built[name]["pkg_files"] = [files[0]]
                        save_built_state(config["state_path"], built)
                    repaired += 1
                    log.info("fsck: fixed pkgrel drift for %s (%s → %s)", name, old_ver, new_ver)
                else:
                    log.warning("fsck: fix_drift repo-add %s failed: %s", name, r.stderr.decode())

        except Exception as e:
            log.warning("fsck: repair failed for %s (%s): %s", name, action, e)

    print(f"\nfsck: repaired {repaired}/{len(repairs)} issue(s)")
    return len(repairs), repaired


def _prune_cycle(config: dict, manifest: list, manifest_names: set, built: dict) -> dict:
    """Run all autoprune operations for one cycle. Returns the updated built dict."""
    if config.get("autoprune_blacklisted", True):
        removed = prune_blacklisted_from_repo(
            config["blacklist"], built,
            config["repo_db"], config["repo_dir"],
        )
        if removed:
            for name in removed:
                built.pop(name, None)
            save_built_state(config["state_path"], built)

    if config.get("autoprune_uninstalled", True):
        removed = prune_uninstalled_from_repo(
            manifest_names, built,
            config["repo_db"], config["repo_dir"],
        )
        if removed:
            for name in removed:
                built.pop(name, None)
            save_built_state(config["state_path"], built)

    if config.get("autoprune_pkgbuild_clones", True) and config.get("tier_sources"):
        with git_lock:
            prune_stale_pkgbuild_clones(config["pkgbuilds_dir"], config["tier_sources"], manifest_names)

    return built
