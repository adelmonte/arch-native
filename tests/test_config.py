def test_defaults_and_tiers(config, tmp_path):
    from arch_native.config import load_config
    assert config["blacklist"] == ["gcc", "ttf-*"]
    assert config["repo_priority"] == ["local", "arch"]
    assert config["tier_sources"] == {"arch": {"type": "pkgctl"}}

    conf = tmp_path / "c2.conf"
    conf.write_text("""\
[arch-native]
repo_priority = local, Artix, myfork, nosrc, arch, arch
myfork_source = clone https://example.com/{pkgname}.git
opt_level = bogus
[package_tiers]
python = local,arch
[package_timeouts]
firefox = 28800
bad = x
""")
    c = load_config(str(conf))
    assert c["repo_priority"] == ["local", "artix", "myfork", "nosrc", "arch"]
    assert c["tier_sources"]["myfork"] == {"type": "clone", "url": "https://example.com/{pkgname}.git"}
    assert "nosrc" not in c["tier_sources"]
    assert c["opt_level"] == "3"
    assert c["package_tier_overrides"] == {"python": ["local", "arch"]}
    assert c["package_timeouts"] == {"firefox": 28800}
