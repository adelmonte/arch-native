"""Find, fetch and inspect PKGBUILDs across the configured tiers."""

import logging
import os
import re
import shutil
import subprocess
import threading

from .state import strip_local_pkgrel_bump
from .util import _fix_ownership, _git, _in_blacklist, ignore_special_files, vercmp

log = logging.getLogger("buildbot")


def _apply_local_patch(pkgname: str, patch_file: str, upstream_dir: str, local_dir: str,
                       build_user: str = "buildbot") -> str:
    """
    Copy upstream_dir to local_dir/_patched/, apply patch_file with patch -p1.
    Raises RuntimeError if the patch does not apply cleanly — this is intentional:
    a broken patch should stop the build loudly, not silently use stale code.
    Returns the path to the patched working directory.
    """
    work_dir = os.path.join(local_dir, "_patched")
    if os.path.exists(work_dir):
        shutil.rmtree(work_dir)
    shutil.copytree(upstream_dir, work_dir, ignore=ignore_special_files)

    dry = subprocess.run(
        ["patch", "--dry-run", "-p1", "--input", patch_file],
        capture_output=True, text=True, cwd=work_dir,
    )
    if dry.returncode != 0:
        raise RuntimeError(
            f"[{pkgname}] local patch no longer applies cleanly — upstream PKGBUILD may "
            f"have changed. Review and update the patch:\n"
            f"  {patch_file}\n"
            f"patch --dry-run output:\n{(dry.stdout + dry.stderr).strip()}"
        )

    subprocess.run(
        ["patch", "-p1", "--input", patch_file],
        check=True, capture_output=True, cwd=work_dir,
    )
    _fix_ownership(work_dir, build_user)
    log.info("[%s] applied local patch from %s", pkgname, os.path.basename(patch_file))
    return work_dir


def _quick_pkgver(pkgbuild_dir: str) -> str:
    """Grep pkgver/pkgrel/epoch from a PKGBUILD for cheap tier comparison.

    Returns a vercmp-compatible 'epoch:pkgver-pkgrel' string, or '' for VCS
    packages whose pkgver= line is a placeholder updated by pkgver().
    """
    pkgbuild = os.path.join(pkgbuild_dir, "PKGBUILD")
    pkgver = pkgrel = epoch = ""
    try:
        with open(pkgbuild) as f:
            for line in f:
                line = line.strip()
                m = re.match(r"^pkgver=(['\"]?)(\S+)\1", line)
                if m:
                    pkgver = m.group(2)
                    continue
                m = re.match(r"^pkgrel=(['\"]?)(\S+)\1", line)
                if m:
                    pkgrel = m.group(2)
                    continue
                m = re.match(r"^epoch=(['\"]?)(\S+)\1", line)
                if m:
                    epoch = m.group(2)
    except OSError:
        pass
    if not pkgver or not pkgrel:
        return ""
    return f"{epoch}:{pkgver}-{pkgrel}" if epoch else f"{pkgver}-{pkgrel}"


# Default tier sources used when no tier_sources config is provided.
# Each entry: {"type": "clone"|"monorepo"|"pkgctl", ...}
_DEFAULT_TIER_SOURCES: dict = {
    "artix":   {"type": "clone", "url": "https://gitea.artixlinux.org/packages/{pkgname}.git"},
    "cachyos": {"type": "monorepo"},
    "arch":    {"type": "pkgctl"},
}

_PKGCTL_URL = "https://gitlab.archlinux.org/archlinux/packaging/packages/{pkgname}.git"

# Serializes clones and pulls between the build loop and the upstream-check
# thread, which otherwise race on the same per-package checkouts.
git_lock = threading.Lock()

_monorepo_cache: dict = {}


def _monorepo_dirs(tier_base: str) -> dict:
    """{dirname: path} of every PKGBUILD directory in a monorepo checkout.

    Walking a large monorepo once per package dominated the upstream check, so
    the walk is cached until git next touches the checkout.
    """
    try:
        stamp = os.stat(os.path.join(tier_base, ".git", "index")).st_mtime
    except OSError:
        stamp = None
    cached = _monorepo_cache.get(tier_base)
    if stamp is not None and cached and cached[0] == stamp:
        return cached[1]
    dirs = {}
    for root, subdirs, files in os.walk(tier_base):
        subdirs[:] = [d for d in subdirs if d != ".git"]
        if "PKGBUILD" in files:
            dirs.setdefault(os.path.basename(root), root)
    _monorepo_cache[tier_base] = (stamp, dirs)
    return dirs


def _tier_dir(pkgname: str, tier: str, src: dict, pkgbuilds_dir: str,
              fetch: str, build_user: str) -> str | None:
    """The directory holding pkgname's PKGBUILD in one non-local tier, or None.

    fetch controls the network:
      "full" — clone a missing checkout and pull an existing one (builds)
      "pull" — pull existing checkouts only (the hourly upstream check, which
               must not try a clone of every package from every tier)
      "none" — read what is on disk (status and reporting)
    """
    tier_base = os.path.join(pkgbuilds_dir, tier)
    if src["type"] == "monorepo":
        return _monorepo_dirs(tier_base).get(pkgname)

    pkg_dir = os.path.join(tier_base, pkgname)
    url = (src["url"] if src["type"] == "clone" else _PKGCTL_URL).format(pkgname=pkgname)
    if fetch != "none":
        with git_lock:
            if not os.path.isdir(pkg_dir):
                if fetch == "full":
                    os.makedirs(tier_base, exist_ok=True)
                    r = _git(["clone", "--depth=1", url, pkg_dir], build_user, capture_output=True)
                    if r.returncode != 0:
                        log.debug("[%s] %s: not in this tier (clone failed)", pkgname, tier)
            else:
                _git(["-C", pkg_dir, "checkout", "--", "PKGBUILD"], build_user, capture_output=True)
                r = _git(["-C", pkg_dir, "pull", "--ff-only"], build_user, capture_output=True)
                if r.returncode != 0:
                    log.debug("[%s] %s pull failed: %s", pkgname, tier,
                              r.stderr.decode(errors="replace").strip()[:120])
    for subdir in ("", "trunk"):
        candidate = os.path.join(pkg_dir, subdir) if subdir else pkg_dir
        if os.path.isfile(os.path.join(candidate, "PKGBUILD")):
            return candidate
    return None


def locate_pkgbuild(
    pkgname: str,
    pkgbuilds_dir: str,
    priority: list[str],
    tier_sources: dict,
    version_select: str = "priority",
    fetch: str = "full",
    build_user: str = "buildbot",
) -> tuple[str, str]:
    """(directory, tier) of pkgname's upstream PKGBUILD. "local" is ignored here.

    version_select:
      "priority" — first tier in priority that has the package wins
      "highest"  — every tier is checked and the highest pkgver wins
    Raises FileNotFoundError when no tier has it.
    """
    candidates: list[tuple[str, str]] = []
    for tier in priority:
        src = tier_sources.get(tier)
        if tier == "local" or src is None:
            continue
        found = _tier_dir(pkgname, tier, src, pkgbuilds_dir, fetch, build_user)
        if not found:
            log.debug("[%s] %s: no PKGBUILD found in this tier", pkgname, tier)
            continue
        candidates.append((found, tier))
        if version_select == "priority":
            break

    if not candidates:
        raise FileNotFoundError(f"No PKGBUILD found for {pkgname} in enabled tiers: {priority}")

    best_dir, best_tier = candidates[0]
    best_ver = _quick_pkgver(best_dir)
    for pkgbuild_dir, tier in candidates[1:]:
        ver = _quick_pkgver(pkgbuild_dir)
        if ver and (not best_ver or vercmp(ver, best_ver) > 0):
            best_dir, best_tier, best_ver = pkgbuild_dir, tier, ver
    if best_tier != candidates[0][1]:
        log.info("[%s] resolved PKGBUILD from tier: %s (pkgver %s > %s from %s)",
                 pkgname, best_tier, best_ver, _quick_pkgver(candidates[0][0]), candidates[0][1])
    else:
        log.info("[%s] resolved PKGBUILD from tier: %s", pkgname, best_tier)
    return best_dir, best_tier


def upstream_priority(pkgname: str, config: dict) -> list[str]:
    """The non-local tiers to take pkgname's upstream PKGBUILD from."""
    priority = config["package_tier_overrides"].get(pkgname) or config["repo_priority"]
    return [t for t in priority if t != "local"]


def resolve_pkgbuild(
    pkgname: str,
    pkgbuilds_dir: str,
    pkgbase_map: dict = None,
    repo_priority: list[str] = None,
    _tried_pkgbase: bool = False,
    tier_sources: dict = None,
    version_select: str = "priority",
    build_user: str = "buildbot",
    fetch: str = "full",
) -> tuple[str, str]:
    """
    Resolve the PKGBUILD to build pkgname from: a local patch applied over
    upstream, a full local PKGBUILD, or the first upstream tier that has it.
    Falls back to the pkgbase for split packages.
    """
    sources = tier_sources if tier_sources is not None else _DEFAULT_TIER_SOURCES
    known = {"local"} | set(sources)
    priority = [t for t in (repo_priority or []) if t in known] or ["local"] + list(sources)

    # Local tier always wins immediately if present.
    if "local" in priority:
        local = os.path.join(pkgbuilds_dir, "local", pkgname)
        patch_file = os.path.join(local, f"{pkgname}.patch")
        if os.path.isfile(patch_file):
            upstream = [t for t in priority if t != "local"]
            try:
                upstream_dir, _ = resolve_pkgbuild(
                    pkgname, pkgbuilds_dir, pkgbase_map, upstream,
                    _tried_pkgbase, tier_sources, version_select, build_user, fetch,
                )
            except FileNotFoundError:
                raise FileNotFoundError(
                    f"[{pkgname}] local patch exists but no upstream PKGBUILD "
                    f"found in tiers: {upstream}"
                )
            return _apply_local_patch(pkgname, patch_file, upstream_dir, local, build_user), "local"
        if os.path.isfile(os.path.join(local, "PKGBUILD")):
            log.warning(
                "[%s] local/ contains a full PKGBUILD copy — consider converting to a "
                ".patch file (buildbot patch create %s). Full copies go stale silently.",
                pkgname, pkgname,
            )
            log.info("[%s] resolved PKGBUILD from tier: local (full copy)", pkgname)
            return local, "local"

    try:
        found, tier = locate_pkgbuild(pkgname, pkgbuilds_dir, priority, sources,
                                      version_select, fetch, build_user)
    except FileNotFoundError:
        pkgbase = (pkgbase_map or {}).get(pkgname)
        if _tried_pkgbase or not pkgbase or pkgbase == pkgname:
            raise
        log.info("[%s] pkgname not found, trying pkgbase: %s", pkgname, pkgbase)
        return resolve_pkgbuild(pkgbase, pkgbuilds_dir, pkgbase_map, priority, True,
                                tier_sources, version_select, build_user, fetch)
    if fetch != "none":
        _fix_ownership(found, build_user)
    return found, tier


def parse_srcinfo(pkgbuild_dir: str, build_user: str = "buildbot") -> dict:
    """Run makepkg --printsrcinfo and parse into a dict."""
    # makepkg refuses to run as root; use runuser if we are root
    env = os.environ.copy()
    if os.getuid() == 0:
        cmd = ["runuser", "-u", build_user, "--", "makepkg", "--printsrcinfo"]
        # nobody needs writable dirs for makepkg checks
        env["BUILDDIR"] = "/tmp"
        env["SRCDEST"] = "/tmp"
        env["PKGDEST"] = "/tmp"
        env["LOGDEST"] = "/tmp"
        env["SRCPKGDEST"] = "/tmp"
    else:
        cmd = ["makepkg", "--printsrcinfo"]
    result = subprocess.run(
        cmd,
        capture_output=True, text=True,
        cwd=pkgbuild_dir,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"makepkg --printsrcinfo failed in {pkgbuild_dir}: {result.stderr}"
        )

    info = {
        "pkgbase": "",
        "pkgver": "",
        "pkgrel": "",
        "epoch": "",
        "arch": [],
        "depends": [],
        "makedepends": [],
        "validpgpkeys": [],
        "packages": [],
    }

    current_pkg = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\w+)\s*=\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()

        if key == "pkgbase":
            info["pkgbase"] = val
        elif key == "pkgname":
            current_pkg = val
            if val not in info["packages"]:
                info["packages"].append(val)
        elif current_pkg is None:
            # Global section (before any pkgname)
            if key == "pkgver":
                info["pkgver"] = val
            elif key == "pkgrel":
                info["pkgrel"] = val
            elif key == "epoch":
                info["epoch"] = val
            elif key == "arch":
                info["arch"].append(val)
            elif key == "depends":
                dep_name = re.split(r"[><=:]", val)[0]
                info["depends"].append(dep_name)
            elif key == "makedepends":
                dep_name = re.split(r"[><=:]", val)[0]
                info["makedepends"].append(dep_name)
            elif key == "validpgpkeys":
                info["validpgpkeys"].append(val)

    return info


def is_eligible(
    pkg: dict, srcinfo: dict, blacklist: list[str]
) -> tuple[bool, str]:
    """Check whether a package should be built."""
    if srcinfo["arch"] == ["any"]:
        return False, "arch=any"

    if _in_blacklist(pkg["name"], blacklist):
        return False, "blacklisted"

    pkgbase = srcinfo.get("pkgbase", "")
    if pkgbase and pkgbase != pkg["name"] and _in_blacklist(pkgbase, blacklist):
        return False, f"pkgbase '{pkgbase}' is blacklisted"

    all_deps = srcinfo.get("depends", []) + srcinfo.get("makedepends", [])
    for dep in all_deps:
        if dep in ("ghc", "haskell-ghc"):
            return False, "haskell"

    return True, ""


def check_upstream_updates(manifest, built, config, should_stop=None, skip_pulls=False):
    """
    For each built package in the manifest, compare built version against
    current upstream. Returns list of packages needing rebuild.
    should_stop: optional callable; checked between packages so a SIGTERM
    received during the upstream check can interrupt it cleanly.
    """
    updates = []
    pkgbuilds_dir = config["pkgbuilds_dir"]
    fetch = "none" if skip_pulls else "pull"

    for pkg in manifest:
        if should_stop and should_stop():
            log.info("Upstream check interrupted by shutdown signal")
            break
        name = pkg["name"]
        if pkg.get("repo") == "unknown" or name not in built:
            continue
        if _in_blacklist(name, config.get("blacklist", [])):
            continue
        # Staged behind a soname cascade: _resolve_pending_cascades owns these
        if built[name].get("status") == "pending_world_cascade":
            continue

        base_ver = strip_local_pkgrel_bump(built[name]["version"])

        # Same tier selection as the build: a full local copy, otherwise the
        # upstream a patch (if any) applies to, honoring per-package overrides.
        local_dir = os.path.join(pkgbuilds_dir, "local", name)
        priority = config["package_tier_overrides"].get(name) or config["repo_priority"]
        best_dir = None
        if ("local" in priority
                and os.path.isfile(os.path.join(local_dir, "PKGBUILD"))
                and not os.path.isfile(os.path.join(local_dir, f"{name}.patch"))):
            best_dir = local_dir
        else:
            try:
                best_dir, _ = locate_pkgbuild(
                    name, pkgbuilds_dir, upstream_priority(name, config),
                    config.get("tier_sources", {}),
                    config.get("tier_version_select", "priority"),
                    fetch, config["build_user"],
                )
            except FileNotFoundError:
                continue

        # A static read of pkgver/pkgrel/epoch: makepkg --printsrcinfo costs
        # ~0.5 s/pkg, minutes across a full manifest. VCS packages (pkgver()
        # function) read as "" and are skipped.
        upstream_ver = _quick_pkgver(best_dir)
        if not upstream_ver or "$" in upstream_ver or "{" in upstream_ver:
            continue
        normalized_upstream = strip_local_pkgrel_bump(upstream_ver)

        # Deferred because the PKGBUILD was older than installed: rebuild once
        # it reaches the installed version, not only once it passes it.
        if built[name].get("status") == "pending_upstream":
            if vercmp(normalized_upstream, base_ver) >= 0:
                log.info("[%s] PKGBUILD caught up (%s >= installed %s) — queuing rebuild",
                         name, normalized_upstream, base_ver)
                updates.append({**pkg, "build_reason": "update"})
            continue

        if vercmp(normalized_upstream, base_ver) > 0:
            log.info("[%s] upstream update detected: %s -> %s", name, base_ver, normalized_upstream)
            updates.append({**pkg, "build_reason": "update"})
        elif vercmp(upstream_ver, base_ver) > 0:
            log.debug("[%s] ignoring local pkgrel bump in source tree: %s", name, upstream_ver)

    return updates
