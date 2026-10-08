from arch_native.build import generate_makepkg_conf, _write_nolto_conf


def _conf(tmp_path, **kw):
    out = tmp_path / "makepkg.conf"
    generate_makepkg_conf({"repo_name": "forge", **kw}, str(out))
    return out.read_text()


def test_remote_conf(tmp_path):
    text = _conf(tmp_path, march="znver4", mode="remote", opt_level="fast")
    assert 'CFLAGS="-march=znver4 -Ofast ' in text
    assert 'RUSTFLAGS="-C opt-level=3"' in text
    assert "!check" in text
    assert 'PACKAGER="Buildbot <buildbot@forge>"' in text


def test_local_conf_and_lto_off(tmp_path):
    text = _conf(tmp_path, march="native", mode="local", lto=False)
    assert "target-cpu=native" in text and " check " in text
    assert 'LTOFLAGS=""' in text and "!lto)" in text


def test_nolto_copy(tmp_path):
    _conf(tmp_path, march="native", mode="remote")
    text = open(_write_nolto_conf(str(tmp_path / "makepkg.conf"))).read()
    assert 'LTOFLAGS=""' in text and "!lto)" in text
