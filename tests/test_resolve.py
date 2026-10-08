import os

import pytest

from arch_native.resolve import check_upstream_updates, locate_pkgbuild, resolve_pkgbuild

SOURCES = {"cachyos": {"type": "monorepo"}, "arch": {"type": "pkgctl"}}


def _pkgbuild(path, pkgver, pkgrel="1", extra=""):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "PKGBUILD"), "w") as f:
        f.write(f"pkgname=x\npkgver={pkgver}\npkgrel={pkgrel}\n{extra}")


@pytest.fixture
def tiers(tmp_path):
    root = tmp_path / "pkgbuilds"
    _pkgbuild(root / "cachyos" / "group" / "foo", "1.0")
    _pkgbuild(root / "arch" / "foo", "2.0")
    _pkgbuild(root / "arch" / "bar", "3.0")
    _pkgbuild(root / "arch" / "base", "4.0")
    return str(root)


def test_locate_priority_and_highest(tiers):
    d, tier = locate_pkgbuild("foo", tiers, ["local", "cachyos", "arch"], SOURCES, fetch="none")
    assert tier == "cachyos" and d.endswith("cachyos/group/foo")
    _, tier = locate_pkgbuild("foo", tiers, ["cachyos", "arch"], SOURCES, "highest", fetch="none")
    assert tier == "arch"
    _, tier = locate_pkgbuild("bar", tiers, ["cachyos", "arch"], SOURCES, fetch="none")
    assert tier == "arch"
    with pytest.raises(FileNotFoundError):
        locate_pkgbuild("nope", tiers, ["cachyos", "arch"], SOURCES, fetch="none")


def test_resolve_pkgbase_fallback(tiers):
    d, tier = resolve_pkgbuild("base-libs", tiers, {"base-libs": "base"}, ["local", "arch"],
                               tier_sources=SOURCES, fetch="none")
    assert d.endswith("arch/base")


def test_resolve_applies_local_patch(tiers):
    local = os.path.join(tiers, "local", "bar")
    os.makedirs(local)
    with open(os.path.join(local, "bar.patch"), "w") as f:
        f.write("--- a/PKGBUILD\n+++ b/PKGBUILD\n@@ -1,3 +1,3 @@\n pkgname=x\n-pkgver=3.0\n+pkgver=3.1\n pkgrel=1\n")
    d, tier = resolve_pkgbuild("bar", tiers, None, ["local", "arch"], tier_sources=SOURCES, fetch="none")
    assert tier == "local" and d == os.path.join(local, "_patched")
    assert "pkgver=3.1" in open(os.path.join(d, "PKGBUILD")).read()


def test_resolve_full_local_copy(tiers):
    _pkgbuild(os.path.join(tiers, "local", "mine"), "0.1")
    d, tier = resolve_pkgbuild("mine", tiers, None, ["local", "arch"], tier_sources=SOURCES, fetch="none")
    assert tier == "local"


def test_check_upstream_updates(tiers):
    config = {
        "pkgbuilds_dir": tiers, "repo_priority": ["local", "cachyos", "arch"],
        "tier_sources": SOURCES, "tier_version_select": "priority", "build_user": "buildbot",
        "package_tier_overrides": {"foo": ["local", "arch"]},
        "blacklist": ["ba*"],
    }
    manifest = [{"name": n, "version": "0-1", "repo": "extra"} for n in ("foo", "bar", "same")]
    _pkgbuild(os.path.join(tiers, "arch", "same"), "5.0", "1.1")
    built = {
        "foo": {"version": "1.0-1"},     # override pins foo to arch 2.0 → update
        "bar": {"version": "1.0-1"},     # blacklisted by glob
        "same": {"version": "5.0-1"},    # only a local pkgrel bump upstream
    }
    updates = check_upstream_updates(manifest, built, config, skip_pulls=True)
    assert [u["name"] for u in updates] == ["foo"]

    built["foo"] = {"version": "9.0-1", "status": "pending_upstream"}
    assert check_upstream_updates(manifest, built, config, skip_pulls=True) == []
    built["foo"] = {"version": "2.0-1", "status": "pending_upstream"}
    assert [u["name"] for u in check_upstream_updates(manifest, built, config, skip_pulls=True)] == ["foo"]
