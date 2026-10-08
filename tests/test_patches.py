from arch_native.patches import _read_patch_header, _write_patch_header


def test_patch_header_roundtrip(tmp_path):
    p = tmp_path / "x.patch"
    diff = "--- a/PKGBUILD\n+++ b/PKGBUILD\n@@ -1 +1 @@\n-a\n+b\n"
    p.write_text(diff)
    assert _read_patch_header(str(p)) == (None, None, None)
    assert _write_patch_header(str(p), "1.0", "2")
    assert _read_patch_header(str(p)) == ("1.0", "2", None)
    assert _write_patch_header(str(p), "1.1", "1", "permanent")
    assert _read_patch_header(str(p)) == ("1.1", "1", "permanent")
    assert p.read_text().endswith(diff)
