import os

from arch_native.repo import prune_blacklisted_from_repo, prune_uninstalled_from_repo

FILES = ["aom-3.1-1-x86_64.pkg.tar.zst", "aom-docs-3.1-1-x86_64.pkg.tar.zst",
         "aom-debug-3.1-1-x86_64.pkg.tar.zst"]


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for f in FILES:
        (repo / f).write_text("x")
        (repo / (f + ".sig")).write_text("s")
    entry = {"version": "3.1-1", "pkg_files": list(FILES)}
    built = {"aom": dict(entry), "aom-docs": dict(entry), "aom-debug": dict(entry)}
    return repo, built


def test_prune_uninstalled_deletes_only_the_removed_packages_files(tmp_path):
    repo, built = _repo(tmp_path)
    removed = prune_uninstalled_from_repo({"aom"}, built, str(repo / "forge.db.tar.zst"), str(repo))
    assert sorted(removed) == ["aom-debug", "aom-docs"]
    assert sorted(os.listdir(repo)) == [FILES[0], FILES[0] + ".sig"]


def test_prune_blacklisted_keeps_subpackage_of_rebuilt_pkgbase(tmp_path):
    repo, built = _repo(tmp_path)
    assert prune_blacklisted_from_repo(["aom-docs"], built, str(repo / "forge.db.tar.zst"), str(repo)) == []
    assert len(os.listdir(repo)) == 6
