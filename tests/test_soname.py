from arch_native.soname import soname_lib_base, _soname_version, _find_soname_breakage


def test_soname_parts():
    assert soname_lib_base("libfoo.so.2.1.0") == "libfoo.so"
    assert _soname_version("libjxl.so.0.12") == "0.12"
    assert _soname_version("libfoo.so") == ""


def test_find_soname_breakage():
    index = {
        "absl": {"provides": ["libabsl_base.so.2608"], "needs": []},
        "grpc": {"provides": [], "needs": ["libabsl_base.so.2605", "libc.so.6"]},
        "proto": {"provides": [], "needs": ["libabsl_base.so.2701"]},
        "fine": {"provides": [], "needs": ["libabsl_base.so.2608"]},
        "parked": {"provides": [], "needs": ["libabsl_base.so.2605"]},
    }
    broken = _find_soname_breakage(index, deferred={"parked"})
    assert broken == {
        "grpc": {"stale": ["libabsl_base.so.2605"], "ahead": []},
        "proto": {"stale": [], "ahead": ["libabsl_base.so.2701"]},
    }


def _sync_db(path, entries):
    import io
    import tarfile
    with tarfile.open(path, "w:gz") as tf:
        for i, (provides, depends) in enumerate(entries):
            body = f"%NAME%\npkg{i}\n\n%PROVIDES%\n" + "\n".join(provides) + \
                   "\n\n%DEPENDS%\n" + "\n".join(depends) + "\n"
            data = body.encode()
            info = tarfile.TarInfo(f"pkg{i}-1-1/desc")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_sync_index(tmp_path):
    from arch_native.soname import SyncIndex
    _sync_db(tmp_path / "extra.db", [
        (["libnettle.so=9-64"], []),
        (["libnettle.so=8-64"], []),                 # compat package
        ([], ["libnettle.so=8-64", "libfoo.so=2-64"]),
        ([], ["libfoo.so=1-64"]),
    ])
    _sync_db(tmp_path / "forge.db", [(["libfoo.so=2-64"], [])])
    idx = SyncIndex("forge", str(tmp_path))
    assert idx.has_soname("libnettle.so=9-64")
    assert not idx.has_soname("libfoo.so=2-64")       # only forge provides it
    assert idx.has_lib("libnettle.so") and not idx.has_lib("libfoo.so")
    # the old nettle soname is still provided by the compat package
    assert idx.soname_ready("libnettle.so=9-64")
    assert idx.depends_on_old_soname("libfoo.so", "libfoo.so=2-64")
    assert not SyncIndex("forge", str(tmp_path / "missing")).has_lib("x.so")
