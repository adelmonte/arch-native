from arch_native.util import (
    _in_blacklist, _pkgname_from_filename, _ver_from_pkg_path, _sanitize_reason,
    _load_json_file, _save_json_file, vercmp,
)
from pathlib import Path


def test_pkgname_and_version_from_filename():
    f = "lib32-foo-bar-1:2.3.4-1.1-x86_64.pkg.tar.zst"
    assert _pkgname_from_filename(f) == "lib32-foo-bar"
    assert _ver_from_pkg_path(Path(f)) == "1:2.3.4-1.1"


def test_blacklist_globs():
    bl = ["gcc", "ttf-*", "*-bin"]
    assert _in_blacklist("gcc", bl)
    assert _in_blacklist("ttf-dejavu", bl)
    assert _in_blacklist("yay-bin", bl)
    assert not _in_blacklist("gcc-libs", bl)


def test_vercmp():
    assert vercmp("1.0-1", "1.0-1") == 0
    assert vercmp("1.0-2", "1.0-1") == 1
    assert vercmp("1:0.1-1", "9.9-1") == 1


def test_sanitize_reason_skips_gpg_noise():
    assert _sanitize_reason("gpg: keybox created\nreal error here") == "real error here"
    assert _sanitize_reason("") == "unknown"


def test_json_roundtrip_and_corrupt_fallback(tmp_path):
    p = tmp_path / "x.json"
    _save_json_file(str(p), {"a": 1})
    assert _load_json_file(str(p), {}, "x") == {"a": 1}
    p.write_text("{not json")
    assert _load_json_file(str(p), {}, "x") == {}
    p.write_text("[1]")
    assert _load_json_file(str(p), {}, "x") == {}
