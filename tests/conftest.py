import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "arch-native"))


@pytest.fixture
def config(tmp_path):
    """A config dict pointing every path into tmp_path."""
    from arch_native.config import load_config

    conf = tmp_path / "arch-native.conf"
    conf.write_text(f"""\
[arch-native]
mode = remote
distro = arch
chroot_dir = {tmp_path}/chroots
chroot_root = {tmp_path}/chroots/root
repo_dir = {tmp_path}/repo
pkgbuilds_dir = {tmp_path}/pkgbuilds
makepkg_configs_dir = {tmp_path}/mk
manifest_path = {tmp_path}/client.json
gnupg_home = {tmp_path}/gnupg
state_path = {tmp_path}/built.json
pending_path = {tmp_path}/pending.json
failed_path = {tmp_path}/failed.json
log_dir = {tmp_path}/logs
metrics_path = {tmp_path}/metrics.json
in_progress_path = {tmp_path}/in_progress.json
blacklist = gcc,ttf-*
""")
    for d in ("repo", "pkgbuilds", "logs"):
        (tmp_path / d).mkdir()
    return load_config(str(conf))
