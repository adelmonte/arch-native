import os
import shutil
import subprocess

import pytest

from arch_native.cli import _ensure_signing_key


@pytest.mark.skipif(not shutil.which("gpg") or os.getuid() == 0, reason="needs gpg, non-root")
def test_signing_key_created_once_and_exported(config):
    os.makedirs(config["gnupg_home"], mode=0o700)
    try:
        fpr = _ensure_signing_key(config)
        assert len(fpr) == 40
        asc = os.path.join(config["repo_dir"], "buildbot-public.asc")
        assert open(asc).read().startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
        assert _ensure_signing_key(config) == fpr
    finally:
        subprocess.run(["gpgconf", "--homedir", config["gnupg_home"], "--kill", "all"])
