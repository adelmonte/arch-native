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
