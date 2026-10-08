"""Small shared helpers: JSON files, vercmp, git, pacman desc fields, package filenames."""

import fnmatch
import json
import logging
import os
import signal
import subprocess
from pathlib import Path

log = logging.getLogger("buildbot")


def _in_blacklist(name: str, blacklist: list[str]) -> bool:
    """Check if name matches any blacklist entry (exact match or fnmatch pattern)."""
    return any(name == entry or fnmatch.fnmatch(name, entry) for entry in blacklist)


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


def _fix_ownership(path: str, user: str = "buildbot"):
    """Ensure the build user owns the resolved PKGBUILD directory."""
    try:
        import pwd
        pw = pwd.getpwnam(user)
        for root, dirs, files in os.walk(path):
            os.chown(root, pw.pw_uid, pw.pw_gid)
            for f in files:
                os.chown(os.path.join(root, f), pw.pw_uid, pw.pw_gid)
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


_GIT_TIMEOUT = 600


def _git(args: list, build_user: str = "buildbot", **kwargs) -> subprocess.CompletedProcess:
    """Run git as build_user when the daemon runs as root.

    Git 2.35.2+ rejects operations on directories not owned by the current
    user (safe.directory). Since pkgbuilds/ is owned by buildbot but the
    daemon runs as root, every pull silently fails unless we drop privileges.
    """
    # A stalled HTTPS transfer otherwise waits out the kernel's TCP
    # retransmission limit (~15 min); one did, and held up the daemon.
    git = ["git", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60"]
    cmd = (["runuser", "-u", build_user, "--"] if os.getuid() == 0 else []) + git + args
    timeout = kwargs.pop("timeout", _GIT_TIMEOUT)
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    with subprocess.Popen(cmd, start_new_session=True, **kwargs) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # git runs under runuser; kill its whole session, not just runuser
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
            log.warning("git %s timed out after %ds", " ".join(args[:3]), timeout)
            msg = f"timed out after {timeout}s"
            err = (err or "") + msg if kwargs.get("text") else (err or b"") + msg.encode()
            return subprocess.CompletedProcess(cmd, 124, out, err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _pkgname_from_filename(filename: str) -> str:
    """Extract pkgname from a filename like {name}-{ver}-{rel}-{arch}.pkg.tar.zst"""
    basename = os.path.basename(filename)
    for ext in (".pkg.tar.zst", ".pkg.tar.xz"):
        if basename.endswith(ext):
            basename = basename[:-len(ext)]
            break
    parts = basename.rsplit("-", 3)
    return parts[0] if len(parts) == 4 else basename


def _ver_from_pkg_path(f: "Path") -> str:
    """Extract pkgver-pkgrel from a .pkg.tar.zst filename for version-based sorting."""
    basename = f.name
    for ext in (".pkg.tar.zst", ".pkg.tar.xz"):
        if basename.endswith(ext):
            basename = basename[:-len(ext)]
            break
    parts = basename.rsplit("-", 3)
    return f"{parts[1]}-{parts[2]}" if len(parts) == 4 else ""


def vercmp(a: str, b: str) -> int:
    """Wrap the system vercmp binary. Returns -1, 0, or 1."""
    try:
        result = subprocess.run(
            ["vercmp", a, b], capture_output=True, text=True
        )
        output = result.stdout.strip()
        if not output:
            raise ValueError(f"vercmp produced no output for {a!r} vs {b!r} (exit {result.returncode})")
        return int(output)
    except FileNotFoundError:
        raise RuntimeError("vercmp binary not found — is pacman installed?") from None
    except ValueError as e:
        raise RuntimeError(f"vercmp returned unexpected output: {e}") from None


def _fmt_srcinfo_ver(si: dict) -> str:
    epoch = si.get("epoch", "")
    return f"{epoch}:{si['pkgver']}-{si['pkgrel']}" if epoch else f"{si['pkgver']}-{si['pkgrel']}"


def _load_json_strict(path: str):
    with open(path, "r") as f:
        raw = f.read().strip()
    if not raw:
        raise ValueError("empty file")
    return json.loads(raw)


def _sanitize_reason(reason: str) -> str:
    """Strip GPG noise and truncate to the first meaningful line."""
    if not reason:
        return "unknown"
    skip_prefixes = ("gpg:", "Warning:", "warning:", "Note:")
    for line in reason.splitlines():
        line = line.strip()
        if line and not any(line.startswith(p) for p in skip_prefixes):
            return line[:120]
    return reason.splitlines()[0].strip()[:120]


def _db_field(content: str, field: str) -> str:
    marker = f"%{field}%"
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == marker and i + 1 < len(lines):
            return lines[i + 1].strip()
    return ""
