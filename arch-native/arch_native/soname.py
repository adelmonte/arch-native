"""Soname tracking: ELF scans, forge self-consistency, distro cascade gate."""

import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .state import _DEFERRED_STATUSES, _queue_item_for, get_built_state, load_pending, save_built_state, save_pending
from .util import _load_json_file, _pkgname_from_filename, _save_json_file, _ver_from_pkg_path, vercmp

log = logging.getLogger("buildbot")


# Directories that can hold ELF objects. Extracting only these keeps a scan off
# the docs, locale and headers that are most of a package by size.
_ELF_BEARING = ("usr/lib", "usr/lib32", "usr/bin", "usr/sbin", "opt")


def _is_elf(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def elf_sonames_from_pkg(pkg_file: str) -> tuple[set, set]:
    """Read the real soname graph out of a built package.

    Returns (provides, needs): the SONAMEs this package's shared libraries
    declare, and the DT_NEEDED entries its ELF objects reference, both as bare
    library file names ('libfoo.so.1').

    Package metadata cannot answer this. Declaring soname provides and depends
    is optional in a PKGBUILD and most upstreams skip it, so .PKGINFO is empty
    for exactly the libraries that keep drifting here — abseil-cpp declares no
    provides, grpc no depends, yet grpc links libabsl_*.so.2605. Reading the
    binaries is the only way to see the edge.
    """
    provides, needs = set(), set()
    with tempfile.TemporaryDirectory(prefix="arch-native-elf-") as tmp:
        cmd = ["bsdtar", "-xf", pkg_file, "-C", tmp, "--no-same-owner",
               "--no-same-permissions"] + list(_ELF_BEARING)
        r = subprocess.run(cmd, capture_output=True, text=True)
        # A package with none of those paths exits non-zero with nothing
        # extracted; that is a normal result, not a failure.
        if r.returncode != 0 and not os.listdir(tmp):
            return provides, needs

        objects = [
            os.path.join(root, fn)
            for root, _dirs, files in os.walk(tmp)
            for fn in files
            if not os.path.islink(os.path.join(root, fn))
            and _is_elf(os.path.join(root, fn))
        ]
        if not objects:
            return provides, needs

        # readelf takes many files per call and labels each with "File:", so a
        # package costs a handful of processes rather than one per object.
        for i in range(0, len(objects), 200):
            r = subprocess.run(
                ["readelf", "-d"] + objects[i:i + 200],
                capture_output=True, text=True,
            )
            for line in r.stdout.splitlines():
                if "(NEEDED)" in line:
                    m = re.search(r"\[(.+?)\]", line)
                    if m:
                        needs.add(m.group(1))
                elif "(SONAME)" in line:
                    m = re.search(r"\[(.+?)\]", line)
                    if m:
                        provides.add(m.group(1))

    return provides, needs - provides


def soname_lib_base(soname: str) -> str:
    """'libfoo.so.2.1.0' -> 'libfoo.so'. The version-independent library name."""
    idx = soname.find(".so")
    return soname[:idx + 3] if idx != -1 else soname


# Packages scanned per cycle when the index is cold. A scan averages about a
# tenth of a second, so this is roughly half a minute of work against a
# five-minute cycle — enough to cover a full repo in a few cycles without
# holding up the first build after a restart.
_SONAME_SCAN_BUDGET = 250


def _soname_index_path(config: dict) -> str:
    return os.path.join(os.path.dirname(config["state_path"]), "sonames.json")


def _repo_packages(config: dict) -> dict:
    """{pkgname: (version, path, stamp)} for published packages, debug aside.

    stamp fingerprints the file itself, not just its version. forge rebuilds
    the same pkgver-pkgrel constantly — that is the whole point of it — so a
    version string cannot tell a rebuilt package from the one scanned before,
    and keying the index on version alone left it stale in the common case.
    """
    out = {}
    for path in Path(config["repo_dir"]).glob("*.pkg.tar.zst"):
        name = _pkgname_from_filename(path.name)
        if name.endswith("-debug"):
            continue
        ver = _ver_from_pkg_path(path)
        # autoprune normally leaves one file per name; if a prune was cut short
        # the newest is the one the repo DB points at.
        if name in out and vercmp(ver or "0", out[name][0] or "0") <= 0:
            continue
        try:
            st = path.stat()
            stamp = f"{ver}:{int(st.st_mtime)}:{st.st_size}"
        except OSError:
            continue
        out[name] = (ver, str(path), stamp)
    return out


def _refresh_soname_index(config: dict, budget: int = _SONAME_SCAN_BUDGET) -> dict:
    """Bring the soname index in line with the repo, at most `budget` scans.

    Entries are keyed by package name and stamped with the version scanned, so
    a rebuilt package is rescanned and a pruned one is dropped.
    """
    path = _soname_index_path(config)
    index = _load_json_file(path, {}, "soname index")
    current = _repo_packages(config)

    dropped = [n for n in index if n not in current]
    for gone in dropped:
        del index[gone]

    stale = [n for n, (_v, _p, stamp) in current.items()
             if index.get(n, {}).get("stamp") != stamp]
    if not stale:
        if dropped:
            _save_json_file(path, index)
        return index

    log.info("Soname index: %d package(s) to scan", len(stale))
    for name in stale[:budget]:
        ver, pkg_path, stamp = current[name]
        try:
            provides, needs = elf_sonames_from_pkg(pkg_path)
        except Exception as e:
            log.warning("Soname scan failed for %s: %s", name, e)
            continue
        prior = index.get(name, {})
        entry = {"version": ver,
                 "stamp": stamp,
                 "provides": sorted(provides),
                 "needs": sorted(needs)}
        # Carry a repair stamp forward only while the file is byte-for-byte
        # the one that was already rebuilt; any new build deserves a fresh
        # attempt, including a rebuild at the same version.
        if prior.get("stamp") == stamp and "repair" in prior:
            entry["repair"] = prior["repair"]
        index[name] = entry

    if len(stale) > budget:
        log.info("Soname index: %d remaining, continuing next cycle", len(stale) - budget)
    _save_json_file(path, index)
    return index


def _soname_version(soname: str) -> str:
    """'libjxl.so.0.12' -> '0.12'. Empty when the soname carries no version."""
    return soname[len(soname_lib_base(soname)):].lstrip(".")


def _deferred_names(config: dict) -> set:
    """Packages forge is knowingly not rebuilding, from built.json status."""
    try:
        built = get_built_state(config["state_path"])
    except Exception:
        return set()
    return {n for n, e in built.items()
            if isinstance(e, dict) and e.get("status") in _DEFERRED_STATUSES}


def _find_soname_breakage(index: dict, deferred: set = None) -> dict:
    """Packages linking a soname no forge package provides.

    Only libraries forge itself builds are considered. If nothing in the repo
    provides any version of libfoo.so then libfoo.so.1 comes from the distro
    and is not ours to reason about — that is what keeps glibc, systemd and
    every other base library out of the result.

    Each result is {"stale": [...], "ahead": [...]}, which is the difference
    between the two ways a link can dangle:

      stale — forge's library moved on and this package still points at the
              old soname. Rebuilding the package fixes it.
      ahead — this package wants a NEWER soname than forge's copy of the
              library provides, because that library is itself stale or its
              build is failing. Rebuilding this package changes nothing; it
              would link the same missing soname again. The provider has to
              be fixed first.
    """
    provided, by_base = set(), {}
    for entry in index.values():
        for soname in entry.get("provides", []):
            provided.add(soname)
            by_base.setdefault(soname_lib_base(soname), set()).add(soname)

    broken = {}
    for name, entry in index.items():
        # A package forge cannot rebuild right now (arch=any, a PKGBUILD older
        # than what is installed, a staged soname bump) is already reported
        # under that status. Naming it here too would leave this check red for
        # something no rebuild can fix, which is how a health signal stops
        # being read at all.
        if deferred and name in deferred:
            continue
        stale, ahead = [], []
        for soname in sorted(entry.get("needs", [])):
            base = soname_lib_base(soname)
            if soname in provided or base not in by_base:
                continue
            want = _soname_version(soname)
            newer_exists = any(
                vercmp(_soname_version(have) or "0", want or "0") > 0
                for have in by_base[base]
            )
            (stale if newer_exists else ahead).append(soname)
        if stale or ahead:
            broken[name] = {"stale": stale, "ahead": ahead}
    return broken


def _queue_soname_repairs(config: dict, manifest_map: dict) -> int:
    """Rebuild packages left linking a soname that no longer exists in forge.

    Nothing else does this. The world cascade guards the other direction — it
    stops forge from publishing a soname bump the distro repos cannot satisfy —
    but a library forge rebuilds for itself silently strands forge's own
    reverse-dependencies, because a soname change needs no version change to
    happen (abseil-cpp 20260817.0-1 to -2 moved every libabsl from 2605 to
    2608). Those packages install cleanly and then fail to start.
    """
    index = _refresh_soname_index(config)
    broken = _find_soname_breakage(index, _deferred_names(config))
    if not broken:
        return 0

    blocked = {n: v["ahead"] for n, v in broken.items() if v["ahead"]}
    if blocked:
        libs = sorted({soname_lib_base(s) for v in blocked.values() for s in v})
        log.warning(
            "%d package(s) need a newer %s than forge provides — the library "
            "is stale or failing to build, so rebuilding them would not help: %s",
            len(blocked), ", ".join(libs), ", ".join(sorted(blocked)[:8]),
        )

    pending = load_pending(config["pending_path"])
    pending_names = {p.get("name") for p in pending}
    queued = 0
    for name, kinds in sorted(broken.items()):
        # Only rebuild when every dangling link is one a rebuild can fix.
        if kinds["ahead"] or not kinds["stale"]:
            continue
        if name in pending_names:
            continue
        entry = index.get(name, {})
        missing = kinds["stale"]
        prior = entry.get("repair")
        # Already rebuilt against this exact gap and still broken, with the
        # file unchanged since: rebuilding again would only loop. Leave it.
        if prior and prior.get("stamp") == entry.get("stamp") \
                and prior.get("missing") == missing:
            continue
        item = _queue_item_for(name, manifest_map, entry.get("version", "unknown"))
        item["build_reason"] = "soname"
        item["queued_at"] = datetime.now(timezone.utc).isoformat()
        pending.append(item)
        pending_names.add(name)
        entry["repair"] = {"version": entry.get("version"),
                           "stamp": entry.get("stamp"), "missing": missing}
        index[name] = entry
        queued += 1
        log.warning("[%s] links missing soname(s) %s — queued rebuild",
                    name, ", ".join(missing[:3]) + ("..." if len(missing) > 3 else ""))

    if queued:
        save_pending(config["pending_path"], pending)
        _save_json_file(_soname_index_path(config), index)
        log.info("Soname repair: queued %d package(s)", queued)
    return queued


def _soname_provides_from_pkg(pkg_file: str) -> set:
    """Extract soname-style provides (e.g. 'libfoo.so=1-64') from .PKGINFO in a built package."""
    import tarfile as _tarfile
    provides = set()
    try:
        with _tarfile.open(pkg_file) as tf:
            try:
                f = tf.extractfile(".PKGINFO")
            except KeyError:
                return provides
            if f is None:
                return provides
            for line in f.read().decode("utf-8", errors="replace").splitlines():
                if line.startswith("provides = "):
                    val = line[len("provides = "):].strip()
                    if ".so" in val:
                        provides.add(val)
    except Exception as e:
        log.warning("Failed reading soname provides from %s: %s", pkg_file, e)
    return provides


def _world_has_soname(soname: str, repo_name: str) -> bool:
    """Return True if any non-forge pacman sync DB provides the exact soname."""
    import tarfile as _tarfile
    sync_dir = "/var/lib/pacman/sync"
    if not os.path.isdir(sync_dir):
        return True  # can't check — assume safe to publish
    for fname in os.listdir(sync_dir):
        if not fname.endswith(".db") or fname[:-3] == repo_name:
            continue
        db_path = os.path.join(sync_dir, fname)
        try:
            with _tarfile.open(db_path) as tf:
                for m in tf.getmembers():
                    if not m.name.endswith("/desc"):
                        continue
                    f = tf.extractfile(m)
                    if f is None:
                        continue
                    content = f.read().decode("utf-8", errors="replace")
                    in_provides = False
                    for line in content.splitlines():
                        stripped = line.strip()
                        if stripped == "%PROVIDES%":
                            in_provides = True
                            continue
                        if in_provides:
                            if not stripped or stripped.startswith("%"):
                                in_provides = False
                                continue
                            if stripped == soname:
                                return True
        except Exception:
            continue
    return False


def _world_has_lib(lib_base: str, repo_name: str) -> bool:
    """Return True if any non-forge pacman sync DB has any soname for lib_base (e.g. 'libfoo.so').
    Used to distinguish a soname bump (world has old version) from a brand-new library."""
    import tarfile as _tarfile
    sync_dir = "/var/lib/pacman/sync"
    if not os.path.isdir(sync_dir):
        return False
    for fname in os.listdir(sync_dir):
        if not fname.endswith(".db") or fname[:-3] == repo_name:
            continue
        db_path = os.path.join(sync_dir, fname)
        try:
            with _tarfile.open(db_path) as tf:
                for m in tf.getmembers():
                    if not m.name.endswith("/desc"):
                        continue
                    f = tf.extractfile(m)
                    if f is None:
                        continue
                    content = f.read().decode("utf-8", errors="replace")
                    in_provides = False
                    for line in content.splitlines():
                        stripped = line.strip()
                        if stripped == "%PROVIDES%":
                            in_provides = True
                            continue
                        if in_provides:
                            if not stripped or stripped.startswith("%"):
                                in_provides = False
                                continue
                            if stripped.split("=", 1)[0] == lib_base:
                                return True
        except Exception:
            continue
    return False


def _world_depends_on_old_soname(lib_base: str, new_soname: str, repo_name: str) -> bool:
    """Return True if any world repo package still depends on an old soname for lib_base.
    Used as a second gate: even if world *provides* the new soname (e.g. Arch's extra),
    the cascade isn't done until every repo has finished rebuilding its reverse-deps
    (e.g. CachyOS still has chrony linked against the old libnettle.so=8-64)."""
    import tarfile as _tarfile
    sync_dir = "/var/lib/pacman/sync"
    if not os.path.isdir(sync_dir):
        return False
    for fname in os.listdir(sync_dir):
        if not fname.endswith(".db") or fname[:-3] == repo_name:
            continue
        db_path = os.path.join(sync_dir, fname)
        try:
            with _tarfile.open(db_path) as tf:
                for m in tf.getmembers():
                    if not m.name.endswith("/desc"):
                        continue
                    f = tf.extractfile(m)
                    if f is None:
                        continue
                    content = f.read().decode("utf-8", errors="replace")
                    in_depends = False
                    for line in content.splitlines():
                        stripped = line.strip()
                        if stripped == "%DEPENDS%":
                            in_depends = True
                            continue
                        if in_depends:
                            if not stripped or stripped.startswith("%"):
                                in_depends = False
                                continue
                            if stripped.split("=", 1)[0] == lib_base and stripped != new_soname:
                                # Bare soname dep (no version) — not a concrete ABI conflict
                                if "=" not in stripped:
                                    continue
                                # Different ELF class (32-bit vs 64-bit) — not a conflict
                                new_cls = new_soname.rsplit("-", 1)[-1] if "-" in new_soname.split("=", 1)[-1] else ""
                                dep_cls = stripped.rsplit("-", 1)[-1] if "-" in stripped.split("=", 1)[-1] else ""
                                if new_cls and dep_cls and new_cls != dep_cls:
                                    continue
                                # Same ABI version, dep just omits ELF class suffix
                                # (e.g. libalpm.so=16 vs libalpm.so=16-64) — not a conflict
                                new_ver = new_soname.split("=", 1)[1].rsplit("-", 1)[0]
                                dep_ver = stripped.split("=", 1)[1].rsplit("-", 1)[0]
                                if new_ver == dep_ver:
                                    continue
                                # Old soname is covered by a compat package in world
                                # (e.g. nettle3 provides libnettle.so=8-64 while nettle
                                # has moved to =9) — the reverse-dep is already satisfied,
                                # so publishing our build is safe
                                if _world_has_soname(stripped, repo_name):
                                    continue
                                return True
        except Exception:
            continue
    return False


def _resolve_pending_cascades(config: dict):
    """Publish staged packages whose sonames are now available in world repos."""
    built = get_built_state(config["state_path"])
    repo_name = config["repo_name"]

    cascade_pkgs = {n: r for n, r in built.items()
                    if r.get("status") == "pending_world_cascade"}
    if not cascade_pkgs:
        return

    # Group sibling subpackages by their shared file set so we run repo-add once per build
    groups: dict = {}
    for name, rec in cascade_pkgs.items():
        key = frozenset(rec.get("pkg_files", []))
        groups.setdefault(key, []).append(name)

    changed = False
    for files_key, names in groups.items():
        rec = cascade_pkgs[names[0]]
        cascade_sonames = set(rec.get("cascade_sonames", []))

        not_ready = [s for s in cascade_sonames
                     if not _world_has_soname(s, repo_name)
                     or _world_depends_on_old_soname(s.split("=", 1)[0], s, repo_name)]
        if not_ready:
            log.debug("[%s] world cascade still in progress: %s", names[0], ", ".join(not_ready))
            continue

        # World has all sonames — promote staged files to the live repo
        abs_files = [
            os.path.join(config["repo_dir"], f) for f in files_key
            if os.path.exists(os.path.join(config["repo_dir"], f))
        ]
        if not abs_files:
            log.warning("[%s] pending_world_cascade: staged files missing — clearing status", names[0])
        else:
            result = subprocess.run(
                ["repo-add", "-v", "-p", config["repo_db"]] + abs_files,
                capture_output=True, text=True,
            )
            if result.returncode not in (0, 1):
                log.error("[%s] repo-add failed for cascade: %s", names[0], result.stderr)
                continue
            log.info("[%s] world cascade complete — published (%s)",
                     ", ".join(names), ", ".join(cascade_sonames))

        for name in names:
            r = built[name]
            built[name] = {k: v for k, v in r.items()
                           if k not in ("status", "cascade_sonames")}
        changed = True

    if changed:
        save_built_state(config["state_path"], built)
