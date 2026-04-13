"""
buildbot_lib.py — Core library for the personal package buildbot.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("buildbot")



def _load_json_file(path: str, default, label: str):
    """Load JSON file with safe fallback for missing/empty/corrupt content."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as f:
            raw = f.read().strip()
        if not raw:
            return default
        data = json.loads(raw)
        if isinstance(default, dict) and not isinstance(data, dict):
            log.warning("%s is not a JSON object (%s), resetting", label, type(data).__name__)
            return default
        if isinstance(default, list) and not isinstance(data, list):
            log.warning("%s is not a JSON array (%s), resetting", label, type(data).__name__)
            return default
        return data
    except json.JSONDecodeError as e:
        log.warning("%s is invalid JSON (%s), resetting", label, e)
        return default
    except Exception as e:
        log.warning("Failed reading %s (%s), resetting", label, e)
        return default


def _save_json_file(path: str, data):
    """Write JSON atomically with fsync to reduce corruption risk."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

# ---------------------------------------------------------------------------
# Regex patterns (ported from ALHP.GO)
# ---------------------------------------------------------------------------
RE_PKGREL = re.compile(r"(?m)^pkgrel\s*=\s*(.+)$")
RE_LD_ERROR = re.compile(r"(?mi).*collect2: error: ld returned \d+ exit status.*")
RE_RUST_LTO_ERROR = re.compile(
    r"(?m)^error: options `-C (.+)` and `-C lto` are incompatible$"
)
# Patterns that indicate a transient download/network failure rather than a
# compile error.  Builds matching these should be re-queued, not added to
# failed.json.
RE_DOWNLOAD_FAILURE = re.compile(
    r"(?mi)"
    r"Could not download sources"
    r"|Failure while downloading"
    r"|failed to download"
    r"|curl: \(\d+\)"
    r"|Unable to connect to"
    r"|Failed to connect"
    r"|Connection reset by peer"
    r"|429 Too Many Requests"
    r"|SSL certificate problem"
    r"|error: Could not resolve"
    r"|Network is unreachable"
)


# ---------------------------------------------------------------------------
# Manifest handling
# ---------------------------------------------------------------------------
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


def generate_makepkg_conf(config: dict, output_path: str):
    """
    Write a makepkg.conf to output_path based on config values.
    Handles local vs remote mode differences:
      - local: march=native, target-cpu=native in RUSTFLAGS, check enabled
      - remote: explicit march, no target-cpu in RUSTFLAGS, !check
    """
    march = config.get("march", "native")
    mode  = config.get("mode", "remote")
    local = (mode == "local")

    cflags = (
        f"-march={march} -O3 -pipe -fno-plt -fexceptions "
        f"-Wp,-D_FORTIFY_SOURCE=3 -fstack-clash-protection "
        f"-fcf-protection -fno-semantic-interposition "
        # GCC 15 promoted several C legacy patterns to hard errors; demote back
        # to warnings so packages that haven't been updated yet can still build.
        f"-Wno-error=incompatible-pointer-types "
        f"-Wno-error=discarded-qualifiers "
        f"-Wno-error=implicit-function-declaration"
    )

    if local:
        rustflags = "-C opt-level=3 -C target-cpu=native"
        check_flag = "check"
        mode_comment = "local mode — building and running on the same machine"
    else:
        rustflags = "-C opt-level=3"
        check_flag = "!check"
        mode_comment = (
            "remote mode — !check: test suites may SIGILL if march != build host CPU"
        )

    content = f"""\
#!/hint/bash
# makepkg configuration generated by buildbot ({mode_comment})

CARCH="x86_64"
CHOST="x86_64-pc-linux-gnu"

CFLAGS="{cflags}"
CXXFLAGS="$CFLAGS -Wp,-D_GLIBCXX_ASSERTIONS"
LDFLAGS="-Wl,-O1 -Wl,--sort-common -Wl,--as-needed -Wl,-z,relro -Wl,-z,now -Wl,-z,pack-relative-relocs"
LTOFLAGS="-flto=auto -falign-functions=32"
RUSTFLAGS="{rustflags}"
MAKEFLAGS="-j$(nproc)"
BUILDDIR=/tmp/makepkg
SRCDEST=/tmp/makepkg-src
PACKAGER="Buildbot <buildbot@{config.get('repo_name', 'arch-native')}>"

BUILDENV=(!distcc color !ccache {check_flag} !sign)
OPTIONS=(strip docs !libtool !staticlibs emptydirs zipman purge debug lto)

DLAGENTS=("file::/usr/bin/curl -qgC - -o %o %u"
          "ftp::/usr/bin/curl -qfC - --ftp-pasv --retry 3 --retry-delay 3 -o %o %u"
          "http::/usr/bin/curl -qb "" -fLC - --retry 3 --retry-delay 3 -o %o %u"
          "https::/usr/bin/curl -qb "" -fLC - --retry 3 --retry-delay 3 -o %o %u"
          "rsync::/usr/bin/rsync --no-motd -z %u %o"
          "scp::/usr/bin/scp -C %u %o")
VCSCLIENTS=("bzr::breezy"
            "fossil::fossil"
            "git::git"
            "hg::mercurial"
            "svn::subversion")

INTEGRITY_CHECK=(sha256)

COMPRESSGZ=(gzip -c -f -n)
COMPRESSBZ2=(bzip2 -c -f)
COMPRESSXZ=(xz -c -z -)
COMPRESSZST=(zstd -c -T0 -9 -)
COMPRESSLRZ=(lrzip -q)
COMPRESSLZO=(lzop -q)
COMPRESSLZ4=(lz4 -q)
COMPRESSLZ=(lzip -c -f)
PKGEXT=".pkg.tar.zst"
SRCEXT=".src.tar.gz"

STRIP_BINARIES="--strip-all"
STRIP_SHARED="--strip-unneeded"
STRIP_STATIC="--strip-debug"

MAN_DIRS=({{usr{{,/local}}{{,/share}},opt/*}}/{{man,info}})
DOC_DIRS=(usr/{{,local/}}{{,share/}}{{doc,gtk-doc}} opt/*/{{doc,gtk-doc}})
PURGE_TARGETS=(usr/{{,share}}/info/dir .packlist *.pod)
DBGSRCDIR="/usr/src/debug"
"""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        f.write(content)
    log.info("Generated makepkg.conf at %s (march=%s, mode=%s)", output_path, march, mode)


# ---------------------------------------------------------------------------
# Built state persistence
# ---------------------------------------------------------------------------
def get_built_state(state_path: str) -> dict:
    """Load built.json -> {pkgname: {version, pkgrel, built_at, pkg_files}}."""
    return _load_json_file(state_path, {}, "built state")


def save_built_state(state_path: str, state: dict):
    """Atomically write built.json."""
    _save_json_file(state_path, state)


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------
def _strip_local_pkgrel_bump(version: str) -> str:
    """Normalize local dot-bumped pkgrel back to upstream version form."""
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
        if name in blacklist:
            continue
        if pkgbase_map:
            pkgbase = pkgbase_map.get(name)
            if pkgbase and pkgbase != name and pkgbase in blacklist:
                continue

        if name not in built:
            todo.append({**pkg, "build_reason": "new"})
            continue

        built_upstream = _strip_local_pkgrel_bump(built[name]["version"])
        manifest_upstream = _strip_local_pkgrel_bump(pkg["version"])
        if vercmp(manifest_upstream, built_upstream) > 0:
            # Upstream version is newer than what we built (ignoring local dot-bumps)
            todo.append({**pkg, "build_reason": "update"})

    return todo
# ---------------------------------------------------------------------------
# PKGBUILD resolution (four-tier)
# ---------------------------------------------------------------------------
def _parse_desc_field(content: str, field: str) -> str | None:
    """Extract a field value from a pacman sync DB desc file."""
    marker = f"%{field}%"
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == marker and i + 1 < len(lines):
            val = lines[i + 1].strip()
            if val:
                return val
    return None


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


def _fix_ownership(path: str):
    """Ensure buildbot user owns the resolved PKGBUILD directory."""
    try:
        import pwd
        buildbot_uid = pwd.getpwnam("buildbot").pw_uid
        buildbot_gid = pwd.getpwnam("buildbot").pw_gid
        for root, dirs, files in os.walk(path):
            os.chown(root, buildbot_uid, buildbot_gid)
            for f in files:
                os.chown(os.path.join(root, f), buildbot_uid, buildbot_gid)
    except (KeyError, PermissionError):
        pass


def ignore_special_files(src: str, names: list[str]) -> set[str]:
    """copytree ignore function — skips named pipes, sockets, and other special files."""
    skip = set()
    for name in names:
        path = os.path.join(src, name)
        try:
            if not (os.path.isfile(path) or os.path.isdir(path) or os.path.islink(path)):
                skip.add(name)
        except OSError:
            pass
    return skip


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


def resolve_pkgbuild(
    pkgname: str,
    pkgbuilds_dir: str,
    pkgbase_map: dict = None,
    repo_priority: list[str] = None,
    _tried_pkgbase: bool = False,
) -> tuple[str, str]:
    """
    Resolve a PKGBUILD from configured tier priority.

    Supported tiers are: local, artix, cachyos, arch.
    """
    default_priority = ["local", "artix", "cachyos", "arch"]
    if repo_priority:
        priority = [tier for tier in repo_priority if tier in default_priority]
        if not priority:
            priority = default_priority
    else:
        priority = default_priority

    def _try_pkgbase_fallback() -> tuple[str, str] | None:
        if _tried_pkgbase or not pkgbase_map or pkgname not in pkgbase_map:
            return None
        pkgbase = pkgbase_map[pkgname]
        if not pkgbase or pkgbase == pkgname:
            return None
        log.info("[%s] pkgname not found, trying pkgbase: %s", pkgname, pkgbase)
        try:
            return resolve_pkgbuild(
                pkgbase,
                pkgbuilds_dir,
                pkgbase_map,
                priority,
                True,
            )
        except FileNotFoundError:
            return None

    for tier in priority:
        if tier == "local":
            local = os.path.join(pkgbuilds_dir, "local", pkgname)
            patch_file = os.path.join(local, f"{pkgname}.patch")
            pkgbuild_file = os.path.join(local, "PKGBUILD")

            if os.path.isfile(patch_file):
                # Patch-based override: resolve upstream then apply.
                upstream_priority = [t for t in priority if t != "local"]
                try:
                    upstream_dir, _ = resolve_pkgbuild(
                        pkgname, pkgbuilds_dir, pkgbase_map, upstream_priority, _tried_pkgbase
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

        elif tier == "artix":
            artix_dir = os.path.join(pkgbuilds_dir, "artix", pkgname)
            if not os.path.isdir(artix_dir):
                url = f"https://gitea.artixlinux.org/packages/{pkgname}.git"
                log.debug("[%s] attempting Artix clone: %s", pkgname, url)
                result = subprocess.run(
                    ["git", "clone", "--depth=1", url, artix_dir],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    log.debug(
                        "[%s] Artix clone failed (expected for non-Artix packages)",
                        pkgname,
                    )
            for subdir in ("", "trunk"):
                if subdir:
                    candidate = os.path.join(artix_dir, subdir, "PKGBUILD")
                else:
                    candidate = os.path.join(artix_dir, "PKGBUILD")
                if os.path.isfile(candidate):
                    resolved = os.path.dirname(candidate)
                    _fix_ownership(resolved)
                    log.info("[%s] resolved PKGBUILD from tier: artix", pkgname)
                    return resolved, "artix"

        elif tier == "cachyos":
            cachyos_dir = os.path.join(pkgbuilds_dir, "cachyos")
            for root, dirs, files in os.walk(cachyos_dir):
                dirs[:] = [d for d in dirs if d != ".git"]
                if os.path.basename(root) == pkgname and "PKGBUILD" in files:
                    _fix_ownership(root)
                    log.info("[%s] resolved PKGBUILD from tier: cachyos", pkgname)
                    return root, "cachyos"

        elif tier == "arch":
            fallback_result = _try_pkgbase_fallback()
            if fallback_result is not None:
                return fallback_result

            arch_root = os.path.join(pkgbuilds_dir, "arch")
            arch_dir = os.path.join(arch_root, pkgname)
            if not os.path.isdir(arch_dir):
                log.debug("[%s] fetching from Arch via pkgctl repo clone", pkgname)
                os.makedirs(arch_root, exist_ok=True)
                result = subprocess.run(
                    ["pkgctl", "repo", "clone", "--protocol=https", pkgname],
                    capture_output=True,
                    text=True,
                    cwd=arch_root,
                )
                if result.returncode != 0:
                    log.error("[%s] pkgctl repo clone failed: %s", pkgname, result.stderr)
            if os.path.isfile(os.path.join(arch_dir, "PKGBUILD")):
                _fix_ownership(arch_dir)
                log.info("[%s] resolved PKGBUILD from tier: arch", pkgname)
                return arch_dir, "arch"

    fallback_result = _try_pkgbase_fallback()
    if fallback_result is not None:
        return fallback_result

    raise FileNotFoundError(f"No PKGBUILD found for {pkgname} in enabled tiers: {priority}")


# ---------------------------------------------------------------------------
# .SRCINFO parsing
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------
def is_eligible(
    pkg: dict, srcinfo: dict, blacklist: list[str]
) -> tuple[bool, str]:
    """Check whether a package should be built."""
    if srcinfo["arch"] == ["any"]:
        return False, "arch=any"

    if pkg["name"] in blacklist:
        return False, "blacklisted"

    pkgbase = srcinfo.get("pkgbase", "")
    if pkgbase and pkgbase != pkg["name"] and pkgbase in blacklist:
        return False, f"pkgbase '{pkgbase}' is blacklisted"

    all_deps = srcinfo.get("depends", []) + srcinfo.get("makedepends", [])
    for dep in all_deps:
        if dep in ("ghc", "haskell-ghc"):
            return False, "haskell"

    return True, ""


# ---------------------------------------------------------------------------
# PGP key import
# ---------------------------------------------------------------------------
def prepare_gnupg_home(gnupg_home: str, build_user: str = "buildbot"):
    """Ensure GNUPGHOME ownership and permissions are compatible with build user."""
    import pwd
    import stat

    os.makedirs(gnupg_home, exist_ok=True)
    try:
        pw = pwd.getpwnam(build_user)
        uid, gid = pw.pw_uid, pw.pw_gid
    except KeyError:
        log.warning("build user '%s' not found; skipping GNUPGHOME ownership prep", build_user)
        return

    for root, dirs, files in os.walk(gnupg_home):
        try:
            os.chown(root, uid, gid)
            os.chmod(root, 0o700)
        except PermissionError:
            pass
        for d in dirs:
            dpath = os.path.join(root, d)
            try:
                os.chown(dpath, uid, gid)
                os.chmod(dpath, 0o700)
            except PermissionError:
                pass
        for f in files:
            fpath = os.path.join(root, f)
            try:
                st = os.lstat(fpath)
            except FileNotFoundError:
                continue
            if stat.S_ISSOCK(st.st_mode):
                continue
            try:
                os.chown(fpath, uid, gid)
                os.chmod(fpath, 0o600)
            except PermissionError:
                pass


def import_pgp_keys(validpgpkeys: list[str], gnupg_home: str, build_user: str = "buildbot") -> list[str]:
    """Import PGP keys as build user with keyserver fallbacks. Returns missing keys."""
    if not validpgpkeys:
        return []

    keyservers = [
        "hkps://keyserver.ubuntu.com",
        "hkp://keyserver.ubuntu.com:80",
        "hkps://keys.openpgp.org",
    ]

    missing = []
    for key in validpgpkeys:
        imported = False
        last_err = ""
        for keyserver in keyservers:
            if os.getuid() == 0:
                cmd = [
                    "runuser", "-u", build_user, "--",
                    "gpg", "--homedir", gnupg_home,
                    "--keyserver", keyserver,
                    "--recv-keys", key,
                ]
            else:
                cmd = [
                    "gpg", "--homedir", gnupg_home,
                    "--keyserver", keyserver,
                    "--recv-keys", key,
                ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
            )
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            if result.returncode == 0 and "No data" not in stderr and "not found" not in stderr.lower():
                log.info("Imported PGP key %s via %s", key, keyserver)
                imported = True
                break
            last_err = (result.stderr or result.stdout or "").strip()
        if not imported:
            log.warning("Failed to import PGP key %s: %s", key, last_err)
            missing.append(key)

    return missing


# ---------------------------------------------------------------------------
# pkgrel bumping (ALHP dot-notation)
# ---------------------------------------------------------------------------
def bump_pkgrel(pkgbuild_path: str, srcinfo: dict) -> str:
    """
    ALHP-style dot-notation pkgrel bump.
    If pkgrel=2     -> becomes 2.1
    If pkgrel=2.3   -> becomes 2.4
    Returns the full new version string (epoch:pkgver-newpkgrel).
    """
    with open(pkgbuild_path, "r") as f:
        content = f.read()

    current_rel = srcinfo["pkgrel"]

    if "." in current_rel:
        parts = current_rel.split(".")
        base = parts[0]
        frac = int(parts[-1])
        new_rel = f"{base}.{frac + 1}"
    else:
        new_rel = f"{current_rel}.1"

    content = RE_PKGREL.sub(f"pkgrel={new_rel}", content)

    with open(pkgbuild_path, "w") as f:
        f.write(content)

    epoch = srcinfo.get("epoch", "")
    pkgver = srcinfo["pkgver"]
    if epoch:
        return f"{epoch}:{pkgver}-{new_rel}"
    return f"{pkgver}-{new_rel}"


def _cleanup_build(chroot_dir: str, chroot_names: list[str], pkgbuild_dir: str, pkgname: str,
                   remove_packages: bool = False):
    """Remove chroot copies and build artifacts after a build.

    remove_packages=True only on failure — on success the caller still needs
    the .pkg.tar.zst files for signing and repo-add.
    """
    for name in chroot_names:
        chroot_path = os.path.join(chroot_dir, name)
        if os.path.isdir(chroot_path):
            log.debug("[%s] removing chroot copy %s", pkgname, name)
            shutil.rmtree(chroot_path, ignore_errors=True)
        lock_path = chroot_path + ".lock"
        if os.path.exists(lock_path):
            try:
                os.remove(lock_path)
            except OSError:
                pass
    # Clean source/build artifacts from PKGBUILD dir
    for subdir in ("src", "pkg"):
        p = os.path.join(pkgbuild_dir, subdir)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    # Only remove package files when the build failed (partial/leftover artifacts).
    # On success, the caller needs them for signing; pre-build cleanup handles
    # leftovers from any previous run.
    if remove_packages:
        for pattern in ("*.pkg.tar.zst", "*.pkg.tar.zst.sig"):
            for f in Path(pkgbuild_dir).glob(pattern):
                try:
                    f.unlink()
                    log.debug("[%s] removed leftover package file %s", pkgname, f.name)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build_package(
    pkg: dict, pkgbuild_dir: str, config: dict, skippgpcheck: bool = False
) -> tuple[bool, list[str], str | None]:
    """
    Build via makechrootpkg with the pantherlake config.
    Returns (success, [pkg_file_paths], failure_type).
    failure_type is None on success, "download" for transient network errors,
    "timeout" for hung builds, or None for compile failures.
    On LTO failure, retries once with LTO disabled.
    skippgpcheck: pass --skippgpcheck to makepkg (source hashes still verified).
    """
    import uuid as _uuid

    chroot_name = "build-" + str(_uuid.uuid4())[:8]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = os.path.join(config["log_dir"], pkg["name"])
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"{timestamp}.log")

    # Clean any leftover package files before building
    for pattern in ("*.pkg.tar.zst", "*.pkg.tar.zst.sig"):
        for f in Path(pkgbuild_dir).glob(pattern):
            try:
                f.unlink()
                log.debug("[%s] cleaned leftover package file before build: %s", pkg["name"], f.name)
            except OSError:
                pass

    # Use the generated conf if available, otherwise fall back to the named file
    makepkg_conf = config.get("_makepkg_conf") or os.path.join(
        config["makepkg_configs_dir"],
        f"makepkg.{config.get('march', 'pantherlake')}.conf",
    )

    if skippgpcheck:
        log.warning("[%s] PGP key import failed — building with --skippgpcheck (source hashes still verified)", pkg["name"])

    def _run_build(conf_path, chroot_id, logpath):
        cmd = [
            "makechrootpkg",
            "-c",
            "-U", config.get("build_user", "buildbot"),
            "-D", config["makepkg_configs_dir"],
            "-l", chroot_id,
            "-r", config["chroot_dir"],
            "--",
            "--config", conf_path,
            "-f",
            "-m",
            "--noprogressbar",
        ]
        if skippgpcheck:
            cmd.append("--skippgpcheck")
        env = os.environ.copy()
        env["GNUPGHOME"] = config["gnupg_home"]
        # CMake 4.x removed compat with cmake_minimum_required < 3.5.
        # This env var lets old packages configure without PKGBUILD patches.
        env["CMAKE_POLICY_VERSION_MINIMUM"] = "3.5"

        log.info("[%s] running: %s", pkg["name"], " ".join(cmd))

        timeout = config.get("build_timeout")
        with open(logpath, "w") as lf:
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=pkgbuild_dir,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    env=env,
                    timeout=timeout,
                )
                return proc.returncode
            except subprocess.TimeoutExpired:
                lf.write(f"\n=== BUILD TIMED OUT after {timeout}s ===\n")
                lf.flush()
                log.error("[%s] build timed out after %ds", pkg["name"], timeout)
                return -2  # sentinel: timeout

    chroots_used = [chroot_name]
    rc = _run_build(makepkg_conf, chroot_name, log_file)

    if rc != 0:
        # Check for LTO errors
        with open(log_file, "r", errors="replace") as lf:
            build_output = lf.read()

        # Check for download failure before LTO retry (download failures are
        # transient — no point retrying with LTO disabled).
        if RE_DOWNLOAD_FAILURE.search(build_output):
            log.warning("[%s] download failure detected (exit %d), see %s", pkg["name"], rc, log_file)
            _cleanup_build(config["chroot_dir"], chroots_used, pkgbuild_dir, pkg["name"],
                           remove_packages=True)
            return False, [], "download"

        if rc == -2:
            # Timeout sentinel — don't bother with LTO retry
            _cleanup_build(config["chroot_dir"], chroots_used, pkgbuild_dir, pkg["name"],
                           remove_packages=True)
            return False, [], "timeout"

        if RE_LD_ERROR.search(build_output) or RE_RUST_LTO_ERROR.search(build_output):
            log.warning(
                "[%s] LTO error detected, retrying with LTO disabled", pkg["name"]
            )
            nolto_conf = makepkg_conf + ".nolto"
            with open(makepkg_conf, "r") as f:
                conf_text = f.read()
            conf_text = re.sub(
                r'^LTOFLAGS=".*"$', 'LTOFLAGS=""', conf_text, flags=re.MULTILINE
            )
            conf_text = re.sub(r"\blto\b", "!lto", conf_text)
            with open(nolto_conf, "w") as f:
                f.write(conf_text)

            timestamp2 = datetime.now().strftime("%Y%m%d-%H%M%S")
            log_file_retry = os.path.join(log_dir, f"{timestamp2}-nolto.log")
            nolto_chroot = chroot_name + "-nolto"
            chroots_used.append(nolto_chroot)
            rc = _run_build(nolto_conf, nolto_chroot, log_file_retry)
            log_file = log_file_retry

            try:
                os.remove(nolto_conf)
            except OSError:
                pass

        if rc != 0:
            log.error("[%s] build failed (exit %d), see %s", pkg["name"], rc, log_file)
            _cleanup_build(config["chroot_dir"], chroots_used, pkgbuild_dir, pkg["name"],
                           remove_packages=True)
            return False, [], None

    # Collect built package files
    pkg_files = [str(p) for p in Path(pkgbuild_dir).glob("*.pkg.tar.zst")]

    if not pkg_files:
        log.error("[%s] build succeeded but no .pkg.tar.zst files found", pkg["name"])
        return False, [], None

    log.info("[%s] build produced %d package(s)", pkg["name"], len(pkg_files))

    # Cleanup chroot copies and build artifacts
    _cleanup_build(config["chroot_dir"], chroots_used, pkgbuild_dir, pkg["name"])

    return True, pkg_files, None


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------
def sign_packages(pkg_files: list[str], gnupg_home: str, build_user: str = "buildbot"):
    """Detach-sign each .pkg.tar.zst file as the build user."""
    for f in pkg_files:
        if os.getuid() == 0:
            cmd = ["runuser", "-u", build_user, "--",
                   "gpg", "--homedir", gnupg_home, "--batch", "--detach-sign", f]
        else:
            cmd = ["gpg", "--homedir", gnupg_home, "--batch", "--detach-sign", f]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to sign {f}: {result.stderr}")
        log.info("Signed %s", os.path.basename(f))


# ---------------------------------------------------------------------------
# Repo management
# ---------------------------------------------------------------------------
def add_to_repo(pkg_files: list[str], repo_db_path: str, repo_dir: str):
    """
    Move packages + sigs to repo dir, then run repo-add.
    Flags: -v (verbose) -p (prevent replace) -n (new only)
    """
    moved = []
    for f in pkg_files:
        dest = os.path.join(repo_dir, os.path.basename(f))
        shutil.move(f, dest)
        moved.append(dest)
        sig = f + ".sig"
        if os.path.exists(sig):
            shutil.move(sig, os.path.join(repo_dir, os.path.basename(sig)))

    cmd = ["repo-add", "-v", "-p", "-n", repo_db_path] + moved
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 and result.returncode != 1:
        raise RuntimeError(f"repo-add failed: {result.stderr}")
    log.info("repo-add: added %d package(s)", len(moved))
    return moved


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Failed queue
# ---------------------------------------------------------------------------
def load_failed(failed_path: str) -> dict:
    """Load failed.json -> {pkgname: {version, reason, timestamp, retries}}."""
    return _load_json_file(failed_path, {}, "failed queue")


def save_failed(failed_path: str, failed: dict):
    """Atomically write failed.json."""
    _save_json_file(failed_path, failed)


# ---------------------------------------------------------------------------
# Pending queue
# ---------------------------------------------------------------------------
def load_pending(pending_path: str) -> list[dict]:
    """Load pending.json."""
    return _load_json_file(pending_path, [], "pending queue")


def save_pending(pending_path: str, pending: list[dict]):
    """Atomically write pending.json."""
    _save_json_file(pending_path, pending)


# ---------------------------------------------------------------------------
# Version comparison (via system vercmp)
# ---------------------------------------------------------------------------
def vercmp(a: str, b: str) -> int:
    """Wrap the system vercmp binary. Returns -1, 0, or 1."""
    result = subprocess.run(
        ["vercmp", a, b], capture_output=True, text=True
    )
    return int(result.stdout.strip())


# ---------------------------------------------------------------------------
# Chroot upgrade
# ---------------------------------------------------------------------------
def upgrade_chroot(chroot_root: str, extra_packages: list[str] = None):
    """
    Upgrade the clean chroot and install any extra packages.

    extra_packages: installed with -Sd --overwrite '*' after the main upgrade.
    For Artix this is ['libelogind', 'libudev', 'elogind']; for Arch it's empty.
    Common build deps (socat, gperf) are always installed.
    """
    log.info("Upgrading chroot at %s", chroot_root)
    result = subprocess.run(
        ["arch-nspawn", chroot_root, "pacman", "-Syu", "--noconfirm"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning("Chroot upgrade warnings: %s", result.stderr.strip())
    else:
        log.info("Chroot upgrade complete")

    if extra_packages:
        result = subprocess.run(
            [
                "arch-nspawn", chroot_root,
                "pacman", "-Sd", "--noconfirm", "--overwrite", "*",
            ] + extra_packages,
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.warning("Extra package install warnings: %s", result.stderr.strip())
        else:
            log.info("Extra packages installed in chroot: %s", " ".join(extra_packages))

    # Always install common build dependencies
    result = subprocess.run(
        [
            "arch-nspawn", chroot_root,
            "pacman", "-S", "--needed", "--noconfirm",
            "socat", "gperf",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning("socat/gperf install warnings: %s", result.stderr.strip())
    else:
        log.info("socat/gperf installed in chroot")

    return True
