from datetime import datetime, timedelta, timezone

from arch_native.state import (
    strip_local_pkgrel_bump, diff_manifest, inject_always_build, update_built_state,
    _retry_due, _is_stalled, _queue_item_for,
)


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def test_strip_local_pkgrel_bump():
    assert strip_local_pkgrel_bump("1.2.3-1.1") == "1.2.3-1"
    assert strip_local_pkgrel_bump("1.2.3-1") == "1.2.3-1"
    assert strip_local_pkgrel_bump("1.2.3") == "1.2.3"
    assert strip_local_pkgrel_bump("1:1.2-3.2") == "1:1.2-3"


def test_diff_manifest():
    manifest = [
        {"name": "new", "version": "1-1", "repo": "extra"},
        {"name": "same", "version": "1-1.1", "repo": "extra"},
        {"name": "newer", "version": "2-1", "repo": "extra"},
        {"name": "aur", "version": "1-1", "repo": "unknown"},
        {"name": "gcc-libs", "version": "1-1", "repo": "core"},
        {"name": "ttf-x", "version": "1-1", "repo": "extra"},
    ]
    built = {"same": {"version": "1-1"}, "newer": {"version": "1-1"}}
    todo = diff_manifest(manifest, built, ["gcc", "ttf-*"], {"gcc-libs": "gcc"})
    assert {p["name"]: p["build_reason"] for p in todo} == {"new": "new", "newer": "update"}


def test_inject_always_build():
    m = [{"name": "a", "version": "1-1", "repo": "extra", "reason": "explicit"}]
    out = inject_always_build(m, {"always_build": ["a", "b"]})
    assert [p["name"] for p in out] == ["a", "b"]
    assert out[1]["version"] == "0"


def test_update_built_state_records_siblings():
    st = update_built_state({}, {"name": "gcc"}, "15-1", ["/x/gcc-15-1-x86_64.pkg.tar.zst"],
                            all_pkgnames=["gcc", "gcc-libs"], pgp_skipped=True)
    assert st["gcc"]["pkg_files"] == ["gcc-15-1-x86_64.pkg.tar.zst"]
    assert st["gcc-libs"]["version"] == "15-1"
    assert st["gcc"]["pgp_skipped"] and st["gcc"]["pkgrel"] == "1"


def test_retry_backoff():
    assert not _retry_due({}, {})
    assert _retry_due({"timestamp": _ago(hours=2), "retries": 1, "error_type": "download"}, {})
    assert not _retry_due({"timestamp": _ago(hours=2), "retries": 1, "error_type": "build"}, {})
    assert _retry_due({"timestamp": _ago(hours=7), "retries": 1, "error_type": "build"}, {})
    # last schedule entry repeats
    assert not _retry_due({"timestamp": _ago(hours=40), "retries": 9, "error_type": "build"}, {})


def test_is_stalled():
    cfg = {"failed_stall_retries": 5, "failed_stall_days": 7}
    assert _is_stalled({"retries": 5}, cfg)
    assert _is_stalled({"retries": 1, "first_failed_at": _ago(days=8)}, cfg)
    assert not _is_stalled({"retries": 1, "first_failed_at": _ago(days=1)}, cfg)


def test_queue_item_for():
    m = {"a": {"name": "a", "version": "1-1", "repo": "extra", "reason": "explicit"}}
    assert _queue_item_for("a", m)["build_reason"] == "retry"
    assert _queue_item_for("zz", m, "3-1") == {
        "name": "zz", "version": "3-1", "repo": "unknown", "reason": "unknown",
        "build_reason": "retry"}


def test_release_stale_ineligible():
    from arch_native.state import eligibility_fingerprint, release_stale_ineligible
    fp = eligibility_fingerprint({"blacklist": ["gcc"]})
    assert fp == eligibility_fingerprint({"blacklist": ["gcc"]})
    assert fp != eligibility_fingerprint({"blacklist": ["gcc", "llvm"]})
    built = {
        "legacy": {"status": "ineligible"},
        "any": {"status": "ineligible", "reason": "arch=any"},
        "same": {"status": "ineligible", "reason": "haskell", "rules": fp},
        "moved": {"status": "ineligible", "reason": "pkgbase 'gcc' is blacklisted", "rules": "old"},
        "ok": {"version": "1-1"},
    }
    assert sorted(release_stale_ineligible(built, fp)) == ["legacy", "moved"]
    assert set(built) == {"any", "same", "ok"}
