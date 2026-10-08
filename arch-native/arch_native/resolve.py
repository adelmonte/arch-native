"""Find, fetch and inspect PKGBUILDs across the configured tiers."""

import logging
import os
import re
import shutil
import subprocess

from .state import strip_local_pkgrel_bump
from .util import _fix_ownership, _git, _in_blacklist, ignore_special_files, vercmp

log = logging.getLogger("buildbot")


def _apply_local_patch(pkgname: str, patch_file: str, upstream_dir: str, local_dir: str) -> str:
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
    _fix_ownership(work_dir)
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


def resolve_pkgbuild(
    pkgname: str,
    pkgbuilds_dir: str,
    pkgbase_map: dict = None,
    repo_priority: list[str] = None,
    _tried_pkgbase: bool = False,
    tier_sources: dict = None,
    version_select: str = "priority",
) -> tuple[str, str]:
    """
    Resolve a PKGBUILD from configured tier priority.

    tier_sources maps tier name → source dict:
      {"type": "clone",   "url": "https://host/{pkgname}.git"}
      {"type": "monorepo"}   — walks pkgbuilds/<tier>/ directory tree
      {"type": "pkgctl"}     — uses Arch devtools pkgctl
    Falls back to _DEFAULT_TIER_SOURCES when tier_sources is None.

    version_select controls how multiple matching tiers are resolved:
      "priority" — first tier in repo_priority wins (safe default)
      "highest"  — all tiers are checked; the one with the highest pkgver wins
    """
    sources = tier_sources if tier_sources is not None else _DEFAULT_TIER_SOURCES
    known = {"local"} | set(sources)

    if repo_priority:
        priority = [t for t in repo_priority if t in known]
        if not priority:
            priority = ["local"] + list(sources)
    else:
        priority = ["local"] + list(sources)

    def _try_pkgbase_fallback() -> tuple[str, str] | None:
        if _tried_pkgbase or not pkgbase_map or pkgname not in pkgbase_map:
            return None
        pkgbase = pkgbase_map[pkgname]
        if not pkgbase or pkgbase == pkgname:
            return None
        log.info("[%s] pkgname not found, trying pkgbase: %s", pkgname, pkgbase)
        try:
            return resolve_pkgbuild(pkgbase, pkgbuilds_dir, pkgbase_map, priority, True, tier_sources, version_select)
        except FileNotFoundError:
            return None

    # Local tier always wins immediately if present.
    if "local" in priority:
        local = os.path.join(pkgbuilds_dir, "local", pkgname)
        patch_file = os.path.join(local, f"{pkgname}.patch")
        pkgbuild_file = os.path.join(local, "PKGBUILD")

        if os.path.isfile(patch_file):
            upstream_priority = [t for t in priority if t != "local"]
            try:
                upstream_dir, _ = resolve_pkgbuild(
                    pkgname, pkgbuilds_dir, pkgbase_map, upstream_priority,
                    _tried_pkgbase, tier_sources, version_select,
                )
            except FileNotFoundError:
                raise FileNotFoundError(
                    f"[{pkgname}] local patch exists but no upstream PKGBUILD "
                    f"found in tiers: {upstream_priority}"
                )
            patched_dir = _apply_local_patch(pkgname, patch_file, upstream_dir, local)
            return patched_dir, "local"

        elif os.path.isfile(pkgbuild_file):
            log.warning(
                "[%s] local/ contains a full PKGBUILD copy — consider converting to a "
                ".patch file (buildbot patch create %s). Full copies go stale silently.",
                pkgname, pkgname,
            )
            log.info("[%s] resolved PKGBUILD from tier: local (full copy)", pkgname)
            return local, "local"

    # Collect candidates from all non-local tiers, then return the one with
    # the highest pkgver so a stale tier doesn't shadow a newer one.
    candidates: list[tuple[str, str]] = []  # (pkgbuild_dir, tier)

    for tier in priority:
        if tier == "local":
            continue
        src = sources.get(tier)
        if src is None:
            continue
        kind = src["type"]

        candidates_before = len(candidates)

        if kind == "clone":
            tier_dir = os.path.join(pkgbuilds_dir, tier, pkgname)
            if not os.path.isdir(tier_dir):
                url = src["url"].format(pkgname=pkgname)
                log.debug("[%s] attempting %s clone: %s", pkgname, tier, url)
                result = _git(["clone", "--depth=1", url, tier_dir],
                              capture_output=True, text=True)
                if result.returncode != 0:
                    log.debug("[%s] %s: not in this tier (clone failed)", pkgname, tier)
            else:
                r = _git(["-C", tier_dir, "pull", "--ff-only"], capture_output=True)
                if r.returncode != 0:
                    log.debug("[%s] %s pull failed: %s", pkgname, tier,
                              r.stderr.decode(errors="replace").strip()[:120])
            for subdir in ("", "trunk"):
                candidate = (os.path.join(tier_dir, subdir, "PKGBUILD") if subdir
                             else os.path.join(tier_dir, "PKGBUILD"))
                if os.path.isfile(candidate):
                    resolved = os.path.dirname(candidate)
                    _fix_ownership(resolved)
                    candidates.append((resolved, tier))
                    break

        elif kind == "monorepo":
            monorepo_dir = os.path.join(pkgbuilds_dir, tier)
            for root, dirs, files in os.walk(monorepo_dir):
                dirs[:] = [d for d in dirs if d != ".git"]
                if os.path.basename(root) == pkgname and "PKGBUILD" in files:
                    _fix_ownership(root)
                    candidates.append((root, tier))
                    break

        elif kind == "pkgctl":
            tier_root = os.path.join(pkgbuilds_dir, tier)
            tier_dir = os.path.join(tier_root, pkgname)
            if not os.path.isdir(tier_dir):
                log.debug("[%s] fetching via pkgctl repo clone", pkgname)
                os.makedirs(tier_root, exist_ok=True)
                result = _git(["clone", "--depth=1",
                               f"https://gitlab.archlinux.org/archlinux/packaging/packages/{pkgname}.git",
                               tier_dir],
                              capture_output=True, text=True)
                if result.returncode != 0:
                    log.debug("[%s] %s: not found via pkgctl (clone failed)", pkgname, tier)
            else:
                r = _git(["-C", tier_dir, "pull", "--ff-only"], capture_output=True)
                if r.returncode != 0:
                    log.debug("[%s] arch pull failed: %s", pkgname,
                              r.stderr.decode(errors="replace").strip()[:120])
            if os.path.isfile(os.path.join(tier_dir, "PKGBUILD")):
                _fix_ownership(tier_dir)
                candidates.append((tier_dir, tier))

        if len(candidates) == candidates_before:
            log.debug("[%s] %s: no PKGBUILD found in this tier", pkgname, tier)

        if candidates and version_select == "priority":
            break  # first tier match wins

    if not candidates:
        fallback_result = _try_pkgbase_fallback()
        if fallback_result is not None:
            return fallback_result
        raise FileNotFoundError(f"No PKGBUILD found for {pkgname} in enabled tiers: {priority}")

    if len(candidates) == 1:
        pkgbuild_dir, tier = candidates[0]
        log.info("[%s] resolved PKGBUILD from tier: %s", pkgname, tier)
        return pkgbuild_dir, tier

    # Multiple candidates — pick the one with the highest pkgver.
    best_dir, best_tier = candidates[0]
    best_ver = _quick_pkgver(best_dir)
    for pkgbuild_dir, tier in candidates[1:]:
        ver = _quick_pkgver(pkgbuild_dir)
        if ver and (not best_ver or vercmp(ver, best_ver) > 0):
            best_dir, best_tier = pkgbuild_dir, tier
            best_ver = ver
    if best_tier != candidates[0][1]:
        log.info(
            "[%s] resolved PKGBUILD from tier: %s (pkgver %s > %s from %s)",
            pkgname, best_tier, best_ver, _quick_pkgver(candidates[0][0]), candidates[0][1],
        )
    else:
        log.info("[%s] resolved PKGBUILD from tier: %s", pkgname, best_tier)
    return best_dir, best_tier


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
    default_priority = config.get("repo_priority", ["local", "arch"])
    tier_overrides = config.get("package_tier_overrides", {})
    tier_sources = config.get("tier_sources", {})
    pkgbuilds_dir = config["pkgbuilds_dir"]

    blacklist = config.get("blacklist", [])
    for pkg in manifest:
        if should_stop and should_stop():
            log.info("Upstream check interrupted by shutdown signal")
            break
        name = pkg["name"]
        if pkg.get("repo") == "unknown":
            continue
        if name in blacklist:
            continue
        if name not in built:
            continue

        built_ver = built[name]["version"]
        ver_parts = built_ver.rsplit("-", 1)
        if len(ver_parts) == 2 and "." in ver_parts[1]:
            base_ver = ver_parts[0] + "-" + ver_parts[1].split(".")[0]
        else:
            base_ver = built_ver

        # Honor per-package tier overrides, exactly like the build path does.
        # Otherwise the upstream check can flag an "update" from a tier the build
        # is configured to skip (e.g. python is pinned to arch because the CachyOS
        # PKGBUILD needs a blacklisted dep) — producing an endless rebuild loop.
        repo_priority = tier_overrides.get(name) or default_priority

        version_select = config.get("tier_version_select", "priority")
        best_dir = best_tier = None
        best_quick_ver = ""

        for tier in repo_priority:
            if tier == "local":
                local_dir = os.path.join(pkgbuilds_dir, "local", name)
                if os.path.isfile(os.path.join(local_dir, "PKGBUILD")):
                    best_dir, best_tier = local_dir, "local"
                    break  # local always wins
                continue

            src = tier_sources.get(tier)
            if src is None:
                continue
            kind = src["type"]
            tier_base = os.path.join(pkgbuilds_dir, tier)
            tier_dir = None

            if kind == "clone":
                pkg_dir = os.path.join(tier_base, name)
                if os.path.isdir(pkg_dir) and not skip_pulls:
                    _git(["-C", pkg_dir, "checkout", "--", "PKGBUILD"],
                         config["build_user"], capture_output=True)
                    r = _git(["-C", pkg_dir, "pull", "--ff-only"],
                             config["build_user"], capture_output=True)
                    if r.returncode != 0:
                        log.debug("[%s] %s pull failed: %s", name, tier,
                                  r.stderr.decode(errors="replace").strip()[:120])
                for subdir in ("", "trunk"):
                    candidate = (os.path.join(pkg_dir, subdir, "PKGBUILD") if subdir
                                 else os.path.join(pkg_dir, "PKGBUILD"))
                    if os.path.isfile(candidate):
                        tier_dir = os.path.dirname(candidate)
                        break

            elif kind == "monorepo":
                for root, dirs, files in os.walk(tier_base):
                    dirs[:] = [d for d in dirs if d != ".git"]
                    if os.path.basename(root) == name and "PKGBUILD" in files:
                        tier_dir = root
                        break

            elif kind == "pkgctl":
                pkg_dir = os.path.join(tier_base, name)
                if not skip_pulls:
                    if not os.path.isdir(pkg_dir):
                        # Create clone on demand so version checks aren't permanently blind
                        # to packages whose initial build used a different tier (e.g. artix).
                        url = f"https://gitlab.archlinux.org/archlinux/packaging/packages/{name}.git"
                        r = _git(["clone", "--depth=1", url, pkg_dir],
                                 config["build_user"], capture_output=True)
                        if r.returncode != 0:
                            log.debug("[%s] arch clone failed: %s", name,
                                      r.stderr.decode(errors="replace").strip()[:120])
                    if os.path.isdir(pkg_dir):
                        _git(["-C", pkg_dir, "checkout", "--", "PKGBUILD"],
                             config["build_user"], capture_output=True)
                        r = _git(["-C", pkg_dir, "pull", "--ff-only"],
                                 config["build_user"], capture_output=True)
                        if r.returncode != 0:
                            log.debug("[%s] arch pull failed: %s", name,
                                      r.stderr.decode(errors="replace").strip()[:120])
                if os.path.isdir(pkg_dir) and os.path.isfile(os.path.join(pkg_dir, "PKGBUILD")):
                    tier_dir = pkg_dir

            if tier_dir:
                if version_select == "priority":
                    best_dir, best_tier = tier_dir, tier
                    break  # first tier match wins
                ver = _quick_pkgver(tier_dir)
                if ver and (not best_quick_ver or vercmp(ver, best_quick_ver) > 0):
                    best_dir, best_tier, best_quick_ver = tier_dir, tier, ver

        # Cheap version read — just grep pkgver/pkgrel/epoch from the PKGBUILD.
        # parse_srcinfo (runs makepkg --printsrcinfo per package) would be more
        # accurate for complex expressions, but costs ~0.5 s/pkg × 800 pkgs ≈ 7 min.
        # _quick_pkgver reads the file directly in <1 ms and is accurate for the
        # static literal assignments used in almost all PKGBUILDs.  VCS packages
        # (pkgver() function) return "" and are silently skipped.
        upstream_ver = None
        if best_dir:
            upstream_ver = _quick_pkgver(best_dir) or None
            # Reject bash variable placeholders ($var / ${var}) that _quick_pkgver
            # might capture from unusual PKGBUILDs.
            if upstream_ver and ("$" in upstream_ver or "{" in upstream_ver):
                upstream_ver = None

        if upstream_ver:
            normalized_upstream = strip_local_pkgrel_bump(upstream_ver)

            # Packages staged but awaiting world cascade: handled by _resolve_pending_cascades.
            if built[name].get("status") == "pending_world_cascade":
                continue

            # Packages deferred because PKGBUILD was stale: rebuild once it catches up.
            # Use >= so we trigger when PKGBUILD reaches the installed version, not only
            # when it exceeds it (the normal > check would never fire in that case).
            if built[name].get("status") == "pending_upstream":
                if vercmp(normalized_upstream, base_ver) >= 0:
                    log.info("[%s] PKGBUILD caught up (%s >= installed %s) — queuing rebuild", name, normalized_upstream, base_ver)
                    updates.append({**pkg, "build_reason": "update"})
                continue

            if vercmp(upstream_ver, base_ver) > 0 and vercmp(normalized_upstream, base_ver) == 0:
                log.debug("[%s] ignoring local pkgrel bump in source tree: %s", name, upstream_ver)
            elif vercmp(normalized_upstream, base_ver) > 0:
                log.info("[%s] upstream update detected: %s -> %s", name, base_ver, normalized_upstream)
                updates.append({**pkg, "build_reason": "update"})

    return updates
