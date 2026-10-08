"""Local PKGBUILD patches: create, check, ack, publish health."""

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from .pacman import _installed_names
from .resolve import locate_pkgbuild, upstream_priority
from .util import ignore_special_files

log = logging.getLogger("buildbot")


def cmd_patch(args, config: dict) -> int:
    subcmd = getattr(args, "patch_cmd", None)
    if subcmd == "create":
        return _cmd_patch_create(args, config)
    if subcmd == "show":
        return _cmd_patch_show(args, config)
    if subcmd == "check":
        return _cmd_patch_check(args, config)
    if args.patch_cmd == "ack":
        return _cmd_patch_ack(args, config)
    if subcmd == "status":
        return _cmd_patch_status(args, config)
    print("error: specify a subcommand: create, show, check, status")
    return 2


def _locate_upstream(pkgname: str, config: dict, fetch: str = "full") -> tuple[str, str]:
    """(directory, tier) of the upstream PKGBUILD a patch for pkgname applies to."""
    return locate_pkgbuild(
        pkgname, config["pkgbuilds_dir"], upstream_priority(pkgname, config),
        config["tier_sources"], config.get("tier_version_select", "priority"),
        fetch, config["build_user"],
    )


def _cmd_patch_create(args, config: dict) -> int:
    """Interactively create a local patch by editing the upstream PKGBUILD."""
    import shutil as _shutil
    pkgname = args.pkgname
    pkgbuilds_dir = config["pkgbuilds_dir"]
    local_dir = os.path.join(pkgbuilds_dir, "local", pkgname)
    patch_file = os.path.join(local_dir, f"{pkgname}.patch")

    if os.path.isfile(patch_file) and not args.force:
        print(f"error: patch already exists: {patch_file}")
        print(f"  edit it directly, or use --force to overwrite from the current upstream")
        return 1

    if os.path.isfile(os.path.join(local_dir, "PKGBUILD")):
        print(f"warning: {local_dir} already contains a full PKGBUILD copy.")
        print(f"  after creating the patch, remove the PKGBUILD file to avoid confusion.")

    try:
        upstream_dir, tier = _locate_upstream(pkgname, config)
    except FileNotFoundError as e:
        print(f"error: {e}")
        return 1

    print(f"  upstream tier: {tier}  ({upstream_dir})")

    with tempfile.TemporaryDirectory(prefix=f"arch-native-patch-{pkgname}-") as tmpdir:
        work_dir = os.path.join(tmpdir, "work")
        _shutil.copytree(upstream_dir, work_dir, ignore=ignore_special_files)
        orig = os.path.join(tmpdir, "PKGBUILD.orig")
        _shutil.copy2(os.path.join(work_dir, "PKGBUILD"), orig)

        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
        print(f"  opening {editor} ...")
        ret = subprocess.run([editor, os.path.join(work_dir, "PKGBUILD")])
        if ret.returncode != 0:
            print("editor exited non-zero, aborting")
            return 1

        diff = subprocess.run(
            ["diff", "-u", "--label", "a/PKGBUILD", "--label", "b/PKGBUILD",
             orig, os.path.join(work_dir, "PKGBUILD")],
            capture_output=True, text=True,
        )
        if diff.returncode == 0:
            print("no changes made")
            return 0
        if diff.returncode != 1:
            print(f"diff error: {diff.stderr}")
            return 1

        # Record the upstream version this patch was written against so
        # patch check can flag when upstream has moved past it.
        metadata = ""
        try:
            orig_content = open(orig).read()
            pv = re.search(r'^pkgver\s*=\s*(\S+)', orig_content, re.MULTILINE)
            pr = re.search(r'^pkgrel\s*=\s*(\S+)', orig_content, re.MULTILINE)
            if pv and pr:
                metadata = f"# arch-native-patch: pkgver={pv.group(1)} pkgrel={pr.group(1)}\n"
        except OSError:
            pass

        os.makedirs(local_dir, exist_ok=True)
        with open(patch_file, "w") as f:
            f.write(metadata + diff.stdout)

    print(f"  saved: {patch_file}")
    return 0


def _cmd_patch_show(args, config: dict) -> int:
    pkgname = args.pkgname
    patch_file = os.path.join(config["pkgbuilds_dir"], "local", pkgname, f"{pkgname}.patch")
    if not os.path.isfile(patch_file):
        print(f"no local patch for {pkgname!r}  (looked in {patch_file})")
        return 1
    with open(patch_file) as f:
        sys.stdout.write(f.read())
    return 0


def _list_local_patches(pkgbuilds_dir: str):
    """Return the list of pkgnames with a local patch, or None if no local/ dir."""
    local_root = os.path.join(pkgbuilds_dir, "local")
    if not os.path.isdir(local_root):
        return None
    return [
        d for d in os.listdir(local_root)
        if not d.startswith("_")
        and os.path.isfile(os.path.join(local_root, d, f"{d}.patch"))
    ]


_PATCH_HEADER_RE = re.compile(
    r'^# arch-native-patch: pkgver=(\S+) pkgrel=(\S+)(?:\s+policy=(\S+))?')


def _read_patch_header(patch_file: str) -> tuple:
    """(pkgver, pkgrel, policy) from a patch's leading comment, or Nones.

    policy is 'permanent' for a patch that fixes this build setup rather than
    an upstream defect; anything else (including absent) is treated as a
    transient workaround that upstream may one day make unnecessary.
    """
    try:
        with open(patch_file) as pf:
            for line in pf:
                if not line.startswith("#"):
                    break
                m = _PATCH_HEADER_RE.match(line)
                if m:
                    return m.group(1), m.group(2), m.group(3)
    except OSError:
        pass
    return None, None, None


def _write_patch_header(patch_file: str, pkgver: str, pkgrel: str,
                        policy: str = None) -> bool:
    """Rewrite the patch's stamp in place, leaving the diff byte-identical."""
    try:
        with open(patch_file) as pf:
            lines = pf.readlines()
    except OSError as e:
        print(f"error: cannot read {patch_file}: {e}")
        return False

    stamp = f"# arch-native-patch: pkgver={pkgver} pkgrel={pkgrel}"
    if policy:
        stamp += f" policy={policy}"
    stamp += "\n"

    at = next((i for i, line in enumerate(lines)
               if line.startswith("#") and _PATCH_HEADER_RE.match(line)), None)
    if at is None:
        lines.insert(0, stamp)
    else:
        lines[at] = stamp

    tmp = patch_file + ".tmp"
    try:
        with open(tmp, "w") as pf:
            pf.writelines(lines)
        os.replace(tmp, patch_file)
    except OSError as e:
        print(f"error: cannot write {patch_file}: {e}")
        return False
    return True


def _upstream_pkgver_for(pkgname: str, config: dict) -> tuple:
    """(pkgver, pkgrel, tier) read statically from the resolved upstream PKGBUILD."""
    upstream_dir, tier = _locate_upstream(pkgname, config)
    content = open(os.path.join(upstream_dir, "PKGBUILD")).read()
    pv = re.search(r'^pkgver\s*=\s*(\S+)', content, re.MULTILINE)
    pr = re.search(r'^pkgrel\s*=\s*(\S+)', content, re.MULTILINE)
    return (pv.group(1) if pv else None, pr.group(1) if pr else None, tier)


def _cmd_patch_ack(args, config: dict) -> int:
    """Re-stamp reviewed patches as seen at the current upstream version.

    Without this there is no way to clear 'review'. The status compares a
    patch against the version it was written at, forever, so one upstream bump
    flags a patch permanently and the review list only ever grows. Acking says
    "I looked at this against today's upstream", which makes review mean
    "drifted since you last looked" — a short, actionable list.
    """
    pkgbuilds_dir = config["pkgbuilds_dir"]
    if args.all:
        installed = _installed_names(config)
        names = [n for n in (_list_local_patches(pkgbuilds_dir) or [])
                 if _check_one_patch(n, config, installed, no_clone=True)[0] == "review"]
        if not names:
            print("no patches in review")
            return 0
    else:
        names = [args.pkgname]

    policy = "permanent" if args.permanent else None
    acked = 0
    for name in names:
        patch_file = os.path.join(pkgbuilds_dir, "local", name, f"{name}.patch")
        if not os.path.isfile(patch_file):
            print(f"  {name}: no patch file")
            continue
        try:
            pkgver, pkgrel, tier = _upstream_pkgver_for(name, config)
        except FileNotFoundError as e:
            print(f"  {name}: {e}")
            continue
        if not pkgver:
            print(f"  {name}: could not read upstream pkgver")
            continue
        # Preserve an existing policy unless this call sets one.
        _ov, _or, existing = _read_patch_header(patch_file)
        if _write_patch_header(patch_file, pkgver, pkgrel, policy or existing):
            note = f" policy={policy or existing}" if (policy or existing) else ""
            print(f"  {name}: stamped {pkgver}-{pkgrel} ({tier}){note}")
            acked += 1

    print(f"\nacked {acked} patch(es)")
    if acked:
        write_patch_status(config)
    return 0


def _check_one_patch(pkgname: str, config: dict, installed=None, no_clone: bool = False) -> tuple:
    """Check a single local patch against current upstream.

    Returns (status, detail, tier) where status is one of:
      ok, review, fail, orphaned, not_found, no_patch_file.

    If no_clone=True, only use pre-existing tier clones — do not trigger any
    git clone or pkgctl fetch.  Patches whose upstream has never been cloned
    are reported as not_found.  Use this for patch status / reporting commands
    that must not make network calls.
    """
    import shutil as _shutil
    pkgbuilds_dir = config["pkgbuilds_dir"]
    patch_file = os.path.join(pkgbuilds_dir, "local", pkgname, f"{pkgname}.patch")
    if not os.path.isfile(patch_file):
        return ("no_patch_file", "no patch file", None)
    if installed is not None and pkgname not in installed:
        return ("orphaned", "not installed", None)


    try:
        upstream_dir, tier = _locate_upstream(pkgname, config, "none" if no_clone else "full")
    except FileNotFoundError:
        return ("not_found", "upstream not found", None)

    # Read metadata written by patch create / patch ack, if present.
    patch_pkgver, patch_pkgrel, policy = _read_patch_header(patch_file)

    # Read current upstream pkgver/pkgrel for version drift detection.
    # Static text read — not a shell eval — so packages with a computed pkgver
    # (git/VCS, variable substitution) compare the literal expression. patch
    # create reads it the same way, so they stay consistent: such packages get
    # pkgrel-drift detection only, never a false "review". Static versions
    # (the common case) are compared exactly.
    upstream_pkgver = upstream_pkgrel = None
    try:
        content = open(os.path.join(upstream_dir, "PKGBUILD")).read()
        pv = re.search(r'^pkgver\s*=\s*(\S+)', content, re.MULTILINE)
        pr = re.search(r'^pkgrel\s*=\s*(\S+)', content, re.MULTILINE)
        if pv:
            upstream_pkgver = pv.group(1)
        if pr:
            upstream_pkgrel = pr.group(1)
    except OSError:
        pass

    with tempfile.TemporaryDirectory(prefix=f"arch-native-check-{pkgname}-") as tmpdir:
        work_dir = os.path.join(tmpdir, "work")
        _shutil.copytree(upstream_dir, work_dir, ignore=ignore_special_files)
        dry = subprocess.run(
            ["patch", "--dry-run", "-p1", "--input", patch_file],
            capture_output=True, text=True, cwd=work_dir,
        )

    if dry.returncode == 0:
        drifted = (patch_pkgver and upstream_pkgver
                   and (patch_pkgver != upstream_pkgver
                        or patch_pkgrel != upstream_pkgrel))
        if not drifted:
            return ("ok", f"tier: {tier}", tier)
        written = f"{patch_pkgver}-{patch_pkgrel}"
        current = f"{upstream_pkgver}-{upstream_pkgrel}"
        # A patch marked permanent fixes something about this build setup, not
        # an upstream defect — a cross-march host, the Artix libexec layout, a
        # broken LTO build. Upstream releasing a new version cannot retire it,
        # so version drift is not a reason to look at it again. It is still
        # checked for applying, which is the failure that would matter.
        if policy == "permanent":
            return ("ok", f"tier: {tier} (permanent)", tier)
        # Same pkgver, higher pkgrel: the upstream sources are byte-identical
        # and only the packaging moved. The patch still applies, so whatever
        # the packager changed did not touch the lines this patch depends on,
        # and the need for it cannot have changed either.
        if patch_pkgver == upstream_pkgver:
            return ("ok", f"tier: {tier} (pkgrel {patch_pkgrel} to {upstream_pkgrel})", tier)
        return ("review", f"written for {written}, upstream now {current}", tier)

    # The patch does not apply. If it reverse-applies, the PKGBUILD already
    # contains what the patch adds — upstream adopted the same fix and the
    # patch is now dead weight rather than broken. mupdf failed this way for
    # weeks reporting only "checking file PKGBUILD".
    with tempfile.TemporaryDirectory(prefix=f"arch-native-rev-{pkgname}-") as tmpdir:
        work_dir = os.path.join(tmpdir, "work")
        _shutil.copytree(upstream_dir, work_dir, ignore=ignore_special_files)
        rev = subprocess.run(
            ["patch", "-R", "--dry-run", "-p1", "--input", patch_file],
            capture_output=True, text=True, cwd=work_dir,
        )
    if rev.returncode == 0:
        return ("obsolete", "upstream already contains this change", tier)

    first_err = next(
        (l for l in (dry.stdout + dry.stderr).splitlines() if l.strip()),
        "patch failed"
    )
    return ("fail", first_err, tier)


def write_patch_status(config: dict) -> tuple:
    """Compute patch health and publish a summary JSON into the repo dir.

    Served by nginx alongside the repo DB so clients (native-sync) can show
    patch health at a glance. Best-effort: logs and returns on any error.

    Returns (summary dict, published: bool). published is False when the
    caller lacks write permission to the repo directory.
    """
    pkgnames = _list_local_patches(config["pkgbuilds_dir"]) or []
    installed = _installed_names(config)

    counts = {"ok": 0, "review": 0, "fail": 0, "orphaned": 0,
              "obsolete": 0, "not_found": 0, "no_patch_file": 0}
    patches = []
    for pkgname in sorted(pkgnames):
        status, detail, _tier = _check_one_patch(pkgname, config, installed,
                                                  no_clone=True)
        counts[status] = counts.get(status, 0) + 1
        patches.append({"name": pkgname, "status": status, "detail": detail})

    summary = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "total": len(pkgnames),
        "ok": counts["ok"],
        "review": counts["review"],
        # not_found / no_patch_file are broken patches too — fold into fail.
        "fail": counts["fail"] + counts["not_found"] + counts["no_patch_file"],
        "orphaned": counts["orphaned"],
        # Applies in reverse: upstream took the change, so the patch can go.
        "obsolete": counts["obsolete"],
        "patches": patches,
    }

    out_path = os.path.join(config["repo_dir"], "patch-status.json")
    tmp = out_path + ".tmp"
    try:
        os.makedirs(config["repo_dir"], exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(summary, f, indent=2)
        os.replace(tmp, out_path)
        log.debug("Wrote patch status: %d ok, %d review, %d fail",
                  summary["ok"], summary["review"], summary["fail"])
        return summary, True
    except PermissionError:
        # Expected whenever a human runs `buildbot patch status`: the repo dir is
        # writable by the daemon only. The counts still print, just unpublished.
        log.debug("Not publishing patch status to %s: no write access", out_path)
        return summary, False
    except OSError as e:
        log.warning("Could not write patch status to %s: %s", out_path, e)
        return summary, False


def _cmd_patch_check(args, config: dict) -> int:
    """Verify that local patches still apply against current upstream."""
    pkgbuilds_dir = config["pkgbuilds_dir"]

    if args.all:
        pkgnames = _list_local_patches(pkgbuilds_dir)
        if pkgnames is None:
            print("no local/ patches directory")
            return 0
        if not pkgnames:
            print("no local patches found")
            return 0
        installed = _installed_names(config)
    else:
        pkgnames = [args.pkgname]
        installed = None

    any_failed = False
    for pkgname in sorted(pkgnames):
        status, detail, _tier = _check_one_patch(pkgname, config, installed)
        if status == "ok":
            print(f"  {pkgname:<28} ok  ({detail})")
        elif status == "review":
            print(f"  {pkgname:<28} review  ({detail})")
            any_failed = True
        elif status == "orphaned":
            print(f"  {pkgname:<28} orphaned  ({detail})")
            any_failed = True
        elif status == "obsolete":
            print(f"  {pkgname:<28} obsolete  ({detail})")
            any_failed = True
        elif status == "not_found":
            print(f"  {pkgname:<28} upstream not found")
            any_failed = True
        elif status == "no_patch_file":
            print(f"  {pkgname:<28} NO PATCH FILE")
            any_failed = True
        else:  # fail
            print(f"  {pkgname:<28} FAIL  {detail}")
            any_failed = True

    return 1 if any_failed else 0


def _cmd_patch_status(args, config: dict) -> int:
    """Recompute and publish the patch health summary, then print the counts."""
    summary, published = write_patch_status(config)
    print(f"  total     {summary['total']}")
    print(f"  ok        {summary['ok']}")
    print(f"  review    {summary['review']}")
    print(f"  fail      {summary['fail']}")
    print(f"  orphaned  {summary['orphaned']}")
    if published:
        out_path = os.path.join(config["repo_dir"], "patch-status.json")
        print(f"\n  published: {out_path}")
    return 0
