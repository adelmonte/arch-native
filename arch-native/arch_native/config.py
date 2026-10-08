"""Load /etc/arch-native.conf into a plain dict."""

import configparser
import logging

log = logging.getLogger("buildbot")


def load_config(path: str) -> dict:
    cp = configparser.ConfigParser(inline_comment_prefixes=("#",))
    cp.read(path)

    # Accept either [arch-native] or legacy [buildbot] section name
    if "arch-native" in cp:
        sec = cp["arch-native"]
    elif "buildbot" in cp:
        sec = cp["buildbot"]
    else:
        raise KeyError(f"Config file {path!r} must have an [arch-native] section")

    DATA = "/var/lib/arch-native"
    cfg = {}
    cfg["repo_name"] = sec.get("repo_name", "forge")
    repo_name = cfg["repo_name"]
    cfg["march"] = sec.get("march", "native")
    cfg["chroot_dir"] = sec.get("chroot_dir", f"{DATA}/chroots")
    cfg["chroot_root"] = sec.get("chroot_root", f"{DATA}/chroots/root")
    cfg["repo_dir"] = sec.get("repo_dir", f"{DATA}/repo")
    cfg["repo_db"] = sec.get("repo_db", f"{DATA}/repo/{repo_name}.db.tar.zst")
    cfg["pkgbuilds_dir"] = sec.get("pkgbuilds_dir", f"{DATA}/pkgbuilds")
    cfg["makepkg_configs_dir"] = sec.get("makepkg_configs_dir", f"{DATA}/makepkg-configs")
    cfg["manifest_path"] = sec.get("manifest_path", f"{DATA}/manifests/client.json")
    cfg["gnupg_home"] = sec.get("gnupg_home", f"{DATA}/gnupg")
    cfg["build_user"] = sec.get("build_user", "buildbot")
    cfg["state_path"] = sec.get("state_path", f"{DATA}/built.json")
    cfg["pending_path"] = sec.get("pending_path", f"{DATA}/pending.json")
    cfg["failed_path"] = sec.get("failed_path", f"{DATA}/failed.json")
    cfg["log_dir"] = sec.get("log_dir", f"{DATA}/logs")
    cfg["metrics_path"] = sec.get("metrics_path", f"{DATA}/metrics.json")
    cfg["in_progress_path"] = sec.get("in_progress_path", f"{DATA}/in_progress.json")
    cfg["log_retention_days"] = sec.getint("log_retention_days", 7)
    cfg["poll_interval"] = sec.getint("poll_interval", 300)
    cfg["skip_pgp_on_import_failure"] = sec.getboolean("skip_pgp_on_import_failure", False)
    cfg["upstream_check_interval"] = sec.getint("upstream_check_interval", 3600)
    cfg["build_timeout"] = sec.getint("build_timeout", 14400)
    cfg["download_retry_limit"] = sec.getint("download_retry_limit", 3)
    cfg["autoprune"] = sec.getboolean("autoprune", True)
    cfg["autoprune_keep"] = max(1, sec.getint("autoprune_keep", 1))
    cfg["autoprune_blacklisted"] = sec.getboolean("autoprune_blacklisted", True)
    cfg["autoprune_uninstalled"] = sec.getboolean("autoprune_uninstalled", True)
    cfg["autoprune_pkgbuild_clones"] = sec.getboolean("autoprune_pkgbuild_clones", True)
    cfg["stall_auto_retry_days"] = sec.getint("stall_auto_retry_days", 3)
    cfg["log_level"] = sec.get("log_level", "INFO")
    cfg["failed_stall_retries"] = sec.getint("failed_stall_retries", 5)
    cfg["failed_stall_days"] = sec.getint("failed_stall_days", 7)
    cfg["extra_cflags"] = sec.get("extra_cflags", "")

    # Build optimization knobs. Each is optional; empty means "use the built-in
    # default" (see DEFAULT_* in build.py), so the generated makepkg.conf is
    # unchanged unless the user sets one.
    opt_level = sec.get("opt_level", "3").strip()
    if opt_level not in ("0", "1", "2", "3", "s", "g", "fast"):
        log.warning("Unknown opt_level '%s', defaulting to '3'", opt_level)
        opt_level = "3"
    cfg["opt_level"] = opt_level
    cfg["lto"] = sec.getboolean("lto", True)
    cfg["ltoflags"] = sec.get("ltoflags", "")
    cfg["cflags_base"] = sec.get("cflags_base", "")
    cfg["ldflags"] = sec.get("ldflags", "")

    raw_vs = sec.get("tier_version_select", "priority").strip().lower()
    if raw_vs not in ("priority", "highest"):
        log.warning("Unknown tier_version_select '%s', defaulting to 'priority'", raw_vs)
        raw_vs = "priority"
    cfg["tier_version_select"] = raw_vs

    # Mode: local = build on the same machine you run the packages on.
    # remote = dedicated build server.
    mode = sec.get("mode", "local").strip().lower()
    if mode not in ("local", "remote"):
        log.warning("Unknown mode '%s', defaulting to 'local'", mode)
        mode = "local"
    cfg["mode"] = mode

    # In local mode default march to native if not explicitly set
    if mode == "local" and not sec.get("march"):
        cfg["march"] = "native"

    # Distro: controls Artix-specific chroot fixups.
    # "artix" installs libelogind/elogind/libudev and deploys artix-meson.
    # "arch" skips all of that for a clean Arch chroot.
    distro = sec.get("distro", "arch").strip().lower()
    if distro not in ("artix", "arch"):
        log.warning("Unknown distro '%s', defaulting to 'arch'", distro)
        distro = "arch"
    cfg["distro"] = distro

    # chroot_extra_packages: additional packages installed in the chroot each cycle.
    # Defaults to Artix-specific set when distro=artix; empty for arch.
    # Override in config to add/remove packages without changing distro setting.
    extra_raw = sec.get("chroot_extra_packages", None)
    if extra_raw is not None:
        cfg["chroot_extra_packages"] = [s.strip() for s in extra_raw.split(",") if s.strip()]
    elif distro == "artix":
        cfg["chroot_extra_packages"] = ["libelogind", "libudev", "elogind"]
    else:
        cfg["chroot_extra_packages"] = []

    # pacman.conf for the build chroot. Empty lets `buildbot init` pick one by
    # distro: the bundled Artix config, or devtools' extra.conf for Arch.
    cfg["chroot_pacman_conf"] = sec.get("chroot_pacman_conf", "")

    repo_priority_str = sec.get("repo_priority", "local,arch")
    repo_priority = []
    for raw in repo_priority_str.split(","):
        tier = raw.strip().lower()
        if tier and tier not in repo_priority:
            repo_priority.append(tier)
    if not repo_priority:
        repo_priority = ["local", "arch"]
    cfg["repo_priority"] = repo_priority

    # Built-in source defaults — used when a tier has no explicit <name>_source entry.
    _builtin = {
        "artix":   "clone https://gitea.artixlinux.org/packages/{pkgname}.git",
        "cachyos": "monorepo",
        "arch":    "pkgctl",
    }
    tier_sources = {}
    for tier in repo_priority:
        if tier == "local":
            continue
        raw_src = sec.get(f"{tier}_source", _builtin.get(tier))
        if raw_src is None:
            log.warning(
                "Tier '%s' has no source configured (add '%s_source' to config) — skipping",
                tier, tier,
            )
            continue
        parts = raw_src.strip().split(None, 1)
        kind = parts[0].lower()
        if kind == "clone":
            if len(parts) < 2:
                log.warning("Tier '%s': clone source requires a URL — skipping", tier)
                continue
            tier_sources[tier] = {"type": "clone", "url": parts[1]}
        elif kind in ("monorepo", "pkgctl"):
            tier_sources[tier] = {"type": kind}
        else:
            log.warning(
                "Tier '%s': unknown source type '%s' (expected: clone <url>, monorepo, pkgctl) — skipping",
                tier, kind,
            )
    cfg["tier_sources"] = tier_sources

    pkg_tier_overrides = {}
    if cp.has_section("package_tiers"):
        for pkgname, tiers_raw in cp.items("package_tiers"):
            tiers = [t.strip().lower() for t in tiers_raw.split(",") if t.strip()]
            if tiers:
                pkg_tier_overrides[pkgname] = tiers
    cfg["package_tier_overrides"] = pkg_tier_overrides

    pkg_timeouts = {}
    if cp.has_section("package_timeouts"):
        for pkgname, val in cp.items("package_timeouts"):
            try:
                pkg_timeouts[pkgname.strip()] = int(val.strip())
            except ValueError:
                log.warning("Ignoring invalid package_timeouts entry: %s = %s", pkgname, val)
    cfg["package_timeouts"] = pkg_timeouts

    blacklist_str = sec.get("blacklist", "gcc,glibc,coreutils,linux-api-headers")
    cfg["blacklist"] = [s.strip() for s in blacklist_str.split(",") if s.strip()]

    lto_blacklist_str = sec.get("lto_blacklist", "")
    cfg["lto_blacklist"] = [
        s.strip() for s in lto_blacklist_str.split(",") if s.strip()
    ]

    # Packages to build and keep in the repo even when they are not installed
    # on the client (not in the manifest). Injected as virtual manifest entries
    # each cycle — see inject_always_build. Plain names only, no wildcards.
    always_build_str = sec.get("always_build", "")
    cfg["always_build"] = [
        s.strip() for s in always_build_str.split(",") if s.strip()
    ]

    return cfg
