import json

import pytest

from arch_native import daemon
from arch_native.state import _queue_lock, load_failed, load_pending, save_failed, save_pending


def _pkg(name, version="1.0-1", reason="new"):
    return {"name": name, "version": version, "repo": "extra", "reason": "explicit",
            "build_reason": reason}


def _srcinfo(pkgver="1.0", pkgrel="1", arch=("x86_64",), packages=None):
    return {"pkgbase": "", "pkgver": pkgver, "pkgrel": pkgrel, "epoch": "",
            "arch": list(arch), "depends": [], "makedepends": [], "validpgpkeys": [],
            "packages": packages or []}


@pytest.fixture
def fake_build(monkeypatch, config):
    """Stub out everything that touches git, chroots or the repo."""
    outcomes, srcinfos, hooks = {}, {}, {}
    published = []

    monkeypatch.setattr(daemon, "resolve_pkgbuild",
                        lambda name, *a, **kw: (f"/fake/{name}", "arch"))
    monkeypatch.setattr(daemon, "parse_srcinfo",
                        lambda d, u: srcinfos.get(d.rsplit("/", 1)[1], _srcinfo()))

    def build_package(pkg, pkgbuild_dir, config, skippgpcheck=False):
        if pkg["name"] in hooks:
            hooks[pkg["name"]]()
        return outcomes.get(pkg["name"], (True, [f"/fake/{pkg['name']}.pkg.tar.zst"], None))

    monkeypatch.setattr(daemon, "build_package", build_package)
    monkeypatch.setattr(daemon, "sign_packages", lambda *a: None)
    monkeypatch.setattr(daemon, "_cascade_sonames", lambda files, cfg: set())

    def add_to_repo(files, *a, **kw):
        published.extend(files)
        return [f.rsplit("/", 1)[1] for f in files]

    monkeypatch.setattr(daemon, "add_to_repo", add_to_repo)
    return {"outcomes": outcomes, "srcinfos": srcinfos, "hooks": hooks, "published": published}


def _built(config):
    return json.load(open(config["state_path"]))


def test_success_records_siblings_and_clears_queue(config, fake_build):
    fake_build["srcinfos"]["llvm"] = _srcinfo(packages=["llvm", "llvm-libs"])
    save_pending(config["pending_path"], [_pkg("llvm"), _pkg("llvm-libs"), _pkg("zlib")])
    save_failed(config["failed_path"], {"llvm-libs": {"version": "0.9-1", "retries": 1}})

    stats = daemon._process_queue(config, {})

    built = _built(config)
    assert set(built) == {"llvm", "llvm-libs", "zlib"}
    assert stats["succeeded"] == 2 and stats["attempted"] == 2
    assert load_failed(config["failed_path"]) == {}
    assert load_pending(config["pending_path"]) == []


def test_ineligible_and_pending_upstream(config, fake_build):
    fake_build["srcinfos"]["docs"] = _srcinfo(arch=("any",))
    fake_build["srcinfos"]["old"] = _srcinfo(pkgver="0.9")
    save_pending(config["pending_path"], [_pkg("docs"), _pkg("old")])

    daemon._process_queue(config, {})

    built = _built(config)
    assert built["docs"]["status"] == "ineligible" and built["docs"]["reason"] == "arch=any"
    assert built["old"]["status"] == "pending_upstream"


def test_failures(config, fake_build):
    fake_build["outcomes"].update({
        "net": (False, [], "download"),
        "dep": (False, [], "dep:libfoo"),
        "cc": (False, [], None),
    })
    save_pending(config["pending_path"], [_pkg("net"), _pkg("dep"), _pkg("cc")])

    stats = daemon._process_queue(config, {})

    failed = load_failed(config["failed_path"])
    assert failed["dep"]["failed_tier"] == "arch" and failed["dep"]["error_type"] == "dep"
    assert failed["cc"]["error_type"] == "build"
    # download failures go back on the queue until download_retry_limit
    assert failed["net"]["reason"] == "download failed after 3 attempts"
    assert stats["failed"] == 2 + 4


def test_skips_package_inside_backoff(config, fake_build):
    from datetime import datetime, timezone
    save_pending(config["pending_path"], [_pkg("cc")])
    save_failed(config["failed_path"], {"cc": {
        "version": "1.0-1", "retries": 1, "error_type": "build",
        "timestamp": datetime.now(timezone.utc).isoformat()}})

    stats = daemon._process_queue(config, {})
    assert stats["skipped_previous_failure"] == 1 and stats["attempted"] == 0


def test_cli_writes_during_a_build_survive(config, fake_build):
    """The daemon used to hold failed.json in memory for a whole drain."""
    save_pending(config["pending_path"], [_pkg("slow")])
    save_failed(config["failed_path"], {"broken": {"version": "1-1", "retries": 3}})

    def cli_retry_mid_build():
        with _queue_lock(config):
            failed = load_failed(config["failed_path"])
            del failed["broken"]
            save_failed(config["failed_path"], failed)
            pending = load_pending(config["pending_path"])
            pending.append(_pkg("broken", "1-1", "retry"))
            save_pending(config["pending_path"], pending)

    fake_build["hooks"]["slow"] = cli_retry_mid_build
    fake_build["outcomes"]["broken"] = (False, [], None)

    daemon._process_queue(config, {})

    failed = load_failed(config["failed_path"])
    # retried, built, failed again: one fresh record, not the stale retries=3
    assert failed["broken"]["retries"] == 1
    assert "slow" in _built(config)
