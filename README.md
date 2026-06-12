# arch-native

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Rebuilds your installed packages from source with CPU-optimized compiler flags,
signs them, and publishes them as a local pacman repo.

Works on any pacman-based distro.

---

## Contents

- [Packages](#packages)
- [Modes](#modes)
- [Dependencies](#dependencies)
- [Installation](#installation)
  - [On the build server](#on-the-build-server)
  - [On the desktop](#on-the-desktop)
  - [Set up pkglist-export (remote mode)](#set-up-pkglist-export-remote-mode)
- [native-sync](#native-sync)
- [Configuration](#configuration)
  - [Configuration reference](#configuration-reference)
  - [What gets built: blacklist & eligibility](#what-gets-built-blacklist--eligibility)
- [Local PKGBUILD patches](#local-pkgbuild-patches)
- [How it works](#how-it-works)
- [buildbot CLI reference](#buildbot-cli-reference)
- [License](#license)

---

## Packages

| Package | Installs | Purpose |
|---|---|---|
| `arch-native` | `/usr/bin/buildbot` | Build daemon and CLI |
| `arch-native-client` | `/usr/bin/native-sync`, `/usr/bin/pkglist-export` | Client tools (both modes) |

Build from source with `makepkg -si` in each directory.

**Names used throughout:** **buildbot** is the server binary (daemon + CLI).
**native-sync** is the client upgrade tool. **forge** is the example
`repo_name` — substitute your own wherever it appears.

---

## Modes

**Local mode** — the daemon runs on the machine that uses the packages, reading
the local pacman DB directly (no manifest or rsync). `-march=native` plus
`target-cpu=native` in RUSTFLAGS, and test suites run. Best for a single machine
fast enough to build its own packages.

**Remote mode** — the daemon runs on a dedicated build server; a pacman hook on
the desktop rsyncs its installed-package list there after each transaction, and
the server builds, signs, and publishes for the desktop to pull. Use it to
offload builds to a separate machine. The build server ideally shares the
target's microarchitecture, so test suites and full Rust optimization run. If it
is older than your desktop it can still cross-build — C/C++ gets the full
`-march=<target>` — but test suites and full Rust optimization are disabled,
because those run on the build server and would SIGILL on the unsupported ISA
(see [Build host / target CPU
mismatch](#build-host--target-cpu-mismatch-remote-mode)).

---

## Dependencies

Both packages declare these via pacman, so they install automatically — listed
here for reference.

**`arch-native`** (build server):

| Dependency | Used for |
|---|---|
| `python` | `buildbot` daemon and CLI (pure Python) |
| `devtools` | `mkarchroot`, `arch-nspawn`, `makechrootpkg`, `pkgctl` |
| `pacman` | `pacman-key`, DB reads, `repo-add` |
| `gnupg` | package signing and PGP key import |
| `rsync` | manifest sync in remote mode (receives the list from the desktop) |
| `git` | cloning and pulling upstream PKGBUILD repos |

**`arch-native-client`** (desktop): `python` and `rsync`.

---

## Installation

**Local mode** — every step runs on the same machine; skip the pkglist-export
setup at the end.

**Remote mode** — steps 1–6 run on the build server; step 7 onward runs on the
desktop that will use the packages.

## On the build server

### 1. Install the package

```bash
yay -S arch-native          # or: cd arch-native && makepkg -si
```

### 2. Edit the config

```bash
$EDITOR /etc/arch-native.conf
```

Local mode (build and use on the same machine):

```ini
[arch-native]
repo_name = myrepo     # pacman DB name
march = native         # let gcc optimize for this CPU
mode = local
distro = arch          # or "artix"
```

Remote mode (build server cross-compiling for your desktop CPU):

```ini
[arch-native]
repo_name = myrepo
march = znver4         # explicit target, e.g. znver4, skylake, pantherlake
mode = remote
distro = arch
```

> **⚠ Set your blacklist now, before the daemon ever runs.** With no blacklist
> the daemon will try to rebuild toolchain packages (`gcc`, `glibc`, `binutils`,
> …) with custom `-march`, which can leave your system unbootable. Add `blacklist`
> and `lto_blacklist` to this file before step 6 — see [Building your
> blacklist](#building-your-blacklist). The bundled config ships with a starter
> blacklist; review it against your installed packages.

### 3. Serve the repo with nginx

```bash
cp /usr/share/arch-native/nginx.conf.example /etc/nginx/sites-available/arch-native
ln -s /etc/nginx/sites-available/arch-native /etc/nginx/sites-enabled/arch-native
# or copy to /etc/nginx/conf.d/arch-native.conf if your nginx uses conf.d
sudo systemctl reload nginx
```

Serves the repo at `:8081/repo/` by default; edit the port in the example first
if needed.

### 4. Initialize

```bash
sudo buildbot init
```

Creates `/var/lib/arch-native/`, builds the clean chroot, and initializes its
keyring. Safe to re-run.

### 5. Generate the signing key

```bash
sudo -u buildbot gpg --homedir /var/lib/arch-native/gnupg \
    --batch --gen-key <<'EOF'
%no-protection
Key-Type: EdDSA
Key-Curve: ed25519
Name-Real: arch-native
Name-Email: arch-native@localhost
Expire-Date: 0
EOF

# Export the public key for clients, and note the fingerprint for step 7:
sudo -u buildbot gpg --homedir /var/lib/arch-native/gnupg \
    --export --armor > /var/lib/arch-native/repo/buildbot-public.asc
sudo -u buildbot gpg --homedir /var/lib/arch-native/gnupg -K
```

### 6. Start the service

> Confirm your [blacklist](#building-your-blacklist) is in place (step 2) — the
> first cycle starts building immediately.

`buildbot` is a long-running daemon. With systemd:

```bash
sudo systemctl enable --now arch-native
sudo systemctl stop arch-native      # to pause for queue edits
sudo systemctl start arch-native
```

**Graceful shutdown** — SIGTERM (`systemctl stop`) lets the current build finish
before exiting, so stopping mid-Firefox can take up to `build_timeout` to return.
If killed (SIGKILL, power loss), the in-progress build is re-queued at the front
on restart and `fsck` runs automatically to repair any divergence.

<details>
<summary>Other init systems (dinit, OpenRC, runit)</summary>

**dinit** — `/etc/dinit.d/arch-native`:

```
type = process
command = /usr/bin/buildbot
options = --config /etc/arch-native.conf
logfile = /var/log/arch-native.log
restart = true
```

```bash
sudo dinitctl enable arch-native && sudo dinitctl start arch-native
```

**OpenRC** — `/etc/init.d/arch-native`:

```bash
#!/sbin/openrc-run
description="arch-native package build daemon"
command=/usr/bin/buildbot
command_args="--config /etc/arch-native.conf"
pidfile=/run/arch-native.pid
command_background=true
output_log=/var/log/arch-native.log
error_log=/var/log/arch-native.log
```

```bash
sudo chmod +x /etc/init.d/arch-native
sudo rc-update add arch-native default && sudo rc-service arch-native start
```

**runit** — `/etc/runit/sv/arch-native/run`:

```bash
#!/bin/sh
exec /usr/bin/buildbot --config /etc/arch-native.conf 2>&1
```

```bash
sudo chmod +x /etc/runit/sv/arch-native/run
sudo ln -s /etc/runit/sv/arch-native /run/runit/service/
```

</details>

## On the desktop

> **Local mode:** the desktop *is* the build server — stay on the same machine.

### 7. Add the repo to pacman.conf

In `/etc/pacman.conf`, place your repo **after** the distro repos:

```ini
[myrepo]
SigLevel = Required DatabaseOptional
Server = http://your-build-host:8081/repo   # or http://localhost:8081/repo (local mode)
```

Trust the signing key:

```bash
sudo pacman-key --fetch-keys http://your-build-host:8081/repo/buildbot-public.asc
sudo pacman-key --lsign-key <KEY-FINGERPRINT>
```

### 8. Install arch-native-client

```bash
yay -S arch-native-client          # or: cd arch-native-client && makepkg -si
```

This installs two tools:

- **`native-sync`** — upgrades your installed packages to the forge builds (see
  [native-sync](#native-sync)).
- **`pkglist-export`** — a pacman hook that syncs your package list to the build
  server after each transaction. It does nothing until you configure it, and is
  unused in local mode.

**Local mode:** you're done. **Remote mode:** configure `pkglist-export` next.

## Set up pkglist-export (remote mode)

```bash
sudo cp /usr/share/arch-native-client/arch-native-client.conf.example \
        /etc/arch-native-client.conf
sudo $EDITOR /etc/arch-native-client.conf
```

```bash
REMOTE_HOST="user@build-server"
REMOTE_PATH="/var/lib/arch-native/manifests/client.json"
# SSH_KEY="/path/to/id_ed25519"   # omit to use ssh-agent or default key
```

The hook runs as root and rsyncs your package list after every transaction, so
root on the desktop needs SSH access to the build server:

```bash
# desktop:
sudo ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ""
sudo cat /root/.ssh/id_ed25519.pub        # add to the build server

# build server:
echo "ssh-ed25519 AAAA... root@desktop" >> ~/.ssh/authorized_keys
sudo setfacl -m u:myuser:rwx /var/lib/arch-native/manifests

# verify end-to-end (desktop):
sudo pkglist-export
```

---

## native-sync

Upgrades installed packages that have a newer build in the forge repo. Run it
manually, or right after `pacman -Syu` (it reuses the synced DBs and finishes in
under a second when there's nothing to do):

```bash
sudo native-sync
```

```
native-sync: 853 / 1197  (71%)
native-sync: patches  41 ok, 2 review, 1 fail
native-sync: nothing to upgrade
```

The first line is forge coverage of your installed packages. The **patches** line
is the build server's local-patch health (`ok`/`review`/`fail`/`orphaned`
explained under [Local PKGBUILD patches](#local-pkgbuild-patches)); it's omitted
if the server publishes none. Works in both modes.

**Run it after every upgrade.** Add a function to your shell config so one
command syncs the official repos and then pulls forge builds. `native-sync` needs
root; the upgrade itself runs as your user.

bash / zsh — in `~/.bashrc` or `~/.zshrc`:

```bash
update() {
    yay -Syu
    sudo native-sync
}
```

fish — in `~/.config/fish/config.fish`:

```fish
function update
    yay -Syu
    and sudo native-sync
end
```

**Non-default repo name** — `native-sync` reads the repo name from the
`FORGE_REPO` environment variable (default `forge`). Because it runs under sudo,
set the variable *on the sudo line* so it isn't stripped:

```bash
sudo FORGE_REPO=myrepo native-sync
```

---

## Configuration

### Configuration reference

All settings live in the `[arch-native]` section of `/etc/arch-native.conf`. The
**Default** column is the built-in fallback used when a key is omitted.

> The bundled config ships with opinionated starter values that differ from the
> code defaults — `mode = local`, `distro = arch`, an example `blacklist`,
> `lto_blacklist = llvm,rust`, `skip_pgp_on_import_failure = true`, and GCC-15
> `extra_cflags`. Edit it rather than starting from a blank file.

#### Core

| Key | Default | Meaning |
|---|---|---|
| `repo_name` | `forge` | pacman DB filename and `PACKAGER` field |
| `march` | `native` | compiler target CPU. `native` for local; an explicit value (`znver4`, `pantherlake`, …) for remote cross-builds. Find yours: `gcc -march=native -Q --help=target \| grep march` |
| `mode` | `remote` | `local` or `remote` (see [Modes](#modes)) |
| `distro` | `artix` | `arch` or `artix`. `artix` installs libelogind/elogind/libudev into the chroot each cycle and deploys an artix-meson wrapper |
| `build_user` | `buildbot` | system user that owns and runs builds; must exist |

#### PKGBUILD resolution

| Key | Default | Meaning |
|---|---|---|
| `repo_priority` | `local,arch` | ordered tier list. `local` is always available; each other tier needs a `<tier>_source` |
| `<tier>_source` | built-ins below | `clone <url>` (per-package git, `{pkgname}` substituted), `monorepo` (walk the tree), or `pkgctl` (Arch devtools). Built-ins: `artix`=clone gitea, `cachyos`=monorepo, `arch`=pkgctl |
| `tier_version_select` | `priority` | when a package is in several tiers: `priority` = first tier wins; `highest` = highest `pkgver` wins |

See [PKGBUILD tier resolution](#pkgbuild-tier-resolution) for the full model.

#### Per-package tier overrides

Override `repo_priority` for specific packages (e.g. pin Artix's elogind-patched
builds). Keep `local` first to retain patch support. `buildbot patch` respects
these too.

```ini
[package_tiers]
networkmanager = local,artix,arch
ffmpeg         = local,cachyos,arch
```

#### Per-package timeout overrides

Override `build_timeout` per package (seconds):

```ini
[package_timeouts]
firefox = 28800
llvm    = 28800
```

#### Blacklists

| Key | Default | Meaning |
|---|---|---|
| `blacklist` | `gcc,glibc,coreutils,linux-api-headers` | never built; supports `fnmatch` globs. See [Building your blacklist](#building-your-blacklist) |
| `lto_blacklist` | *(empty)* | built with LTO disabled (e.g. `llvm,rust`) |

#### Always-build packages

The inverse of the blacklist: build and keep a package **even when it isn't
installed**. Plain names only.

```ini
always_build = xdg-desktop-portal-kde
```

Each name is injected as a virtual manifest entry every cycle — built (applying a
`pkgbuilds/local/` patch if present) and exempt from uninstalled-pruning. Handy
for seeding a *patched* build into the repo. Remove a name and the normal prune
cleans it out next cycle.

#### Build behavior

| Key | Default | Meaning |
|---|---|---|
| `build_timeout` | `14400` | per-build limit in seconds; `0` disables |
| `download_retry_limit` | `3` | re-queue transient download failures this many times before failing |
| `skip_pgp_on_import_failure` | `false` | if all keyserver imports fail, retry with `--skippgpcheck` (source hashes still verified; flagged `pgp_skipped` in built.json) |
| `autoprune` | `true` | delete old `.pkg.tar.zst` (+ `.sig`) after each rebuild |
| `autoprune_keep` | `1` | versions retained per package (≥ 1; raise for rollback) |
| `autoprune_blacklisted` | `true` | drop newly-blacklisted packages from repo + DB each cycle |
| `autoprune_uninstalled` | `true` | drop packages no longer in the manifest |
| `autoprune_pkgbuild_clones` | `true` | drop cached PKGBUILD clones for packages no longer needed |
| `failed_stall_retries` | `5` | mark "stalled" after this many failures (with the days rule) |
| `failed_stall_days` | `7` | …and the last failure ≥ this many days ago |
| `stall_auto_retry_days` | `3` | clear and re-queue a stalled package after this long; `0` disables |

#### Timing

| Key | Default | Meaning |
|---|---|---|
| `poll_interval` | `300` | main loop interval (seconds) |
| `upstream_check_interval` | `3600` | how often to git-pull upstream PKGBUILDs |
| `log_retention_days` | `7` | build-log retention |

#### Debugging

| Key | Default | Meaning |
|---|---|---|
| `extra_cflags` | *(empty)* | appended to CFLAGS for every build. GCC 15 made several legacy C patterns hard errors — demote them while upstream catches up: `-Wno-error=incompatible-pointer-types -Wno-error=discarded-qualifiers -Wno-error=implicit-function-declaration` |
| `log_level` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`; `DEBUG` traces per-package resolution |

#### Artix / chroot

| Key | Default | Meaning |
|---|---|---|
| `chroot_pacman_conf` | *(bundled)* | pacman.conf deployed into the chroot. Artix users must supply their own; default looks for `/usr/share/arch-native/chroot-pacman.conf` |
| `chroot_extra_packages` | artix: `libelogind,libudev,elogind` · arch: empty | extra packages installed into the chroot after each upgrade |

#### Paths

All default to `/var/lib/arch-native/<subdir>`; override only for a non-standard
layout: `chroot_dir`, `chroot_root`, `repo_dir`, `repo_db`, `pkgbuilds_dir`,
`makepkg_configs_dir`, `manifest_path`, `gnupg_home`, `log_dir`, `metrics_path`.

> `chroot_dir` is the parent; `chroot_root` is the clean base chroot inside it.
> If you override `chroot_dir` alone, the base chroot stays at the old default —
> set both.

---

### What gets built: blacklist & eligibility

What lands in the repo is your installed packages (or manifest) minus what the
daemon can't or shouldn't build.

#### Building your blacklist

The blacklist stops packages from being rebuilt — get it right early, since a
bad toolchain rebuild can make the system unbootable. The blacklist supports
`fnmatch` globs.

- **Always:** toolchain/core (`gcc`, `glibc`, `binutils`, `coreutils`,
  `linux-api-headers`) — custom `-march` can break the system; AUR/binary
  packages — no upstream PKGBUILD.
- **Pure data (no benefit):** `ttf-*`, `otf-*`, `font-*`, `*-icon-theme`,
  `*-cursors`, `linux-firmware`, `linux-firmware-*`, `*-keyring`,
  `*-translations`, `hunspell-*`, `tesseract-data-*`, `*-dinit`, `*-openrc`,
  `*-runit`.
- **Often problematic:** `llvm`/`rust` (also need LTO off — add to
  `lto_blacklist`); packages whose build systems ignore `CFLAGS` (some Go/Java).

```ini
blacklist = gcc,glibc,binutils,coreutils,linux-api-headers,
            ttf-*,otf-*,font-*,*-icon-theme,*-cursors,
            linux-firmware,linux-firmware-*,*-keyring,
            *-translations,hunspell-*,tesseract-data-*
```

After editing, `buildbot status` shows the **blacklisted** count and
`buildbot queue -n 200` shows what's actually queued.

#### Split packages and pkgbase resolution

One `pkgbase` (one PKGBUILD) often produces several pkgnames — `gcc` →
`gcc`, `gcc-libs`, `gcc-fortran`, …; `llvm` → `llvm`, `llvm-libs`, `clang`, ….
When a pkgname isn't found directly, the daemon looks up its `pkgbase` and builds
that; all subpackages in `.SRCINFO` are then recorded at once.

So **blacklist the pkgbase, not a subpackage** — `blacklist = gcc` blocks
`gcc-libs`; `blacklist = gcc-libs` doesn't (it still builds via `pkgbase=gcc`).
Anything whose pkgbase is blacklisted is skipped automatically and counted under
**skipped** in `buildbot status`.

#### AUR and binary packages

These have no upstream PKGBUILD to pull (AUR) or no source to compile (`*-bin`),
so they're marked `not_found` in `failed.json`. Blacklist them to keep the failed
list clean: `blacklist = ...,*-bin,*-git,*-svn`.

#### Why a package might not be queued

Beyond the blacklist:

- **ineligible** (auto-detected, counted in `buildbot status`): `arch=any`
  packages; packages with `ghc`/`haskell-ghc` makedepends (GHC-version-locked);
  anything with a blacklisted pkgbase.
- **`pending_upstream`** — installed version is newer than the PKGBUILD in your
  tiers; released automatically once the tier catches up. `buildbot sync` forces
  a re-scan.
- **`pending_world_cascade`** — a soname bump is staged but not published until
  the world repos finish migrating reverse-deps. Resolves on its own; check with
  `buildbot why <pkg>`.
- **stalled** — failed ≥ `failed_stall_retries` times with the last failure ≥
  `failed_stall_days` ago; excluded from auto-retry. `buildbot retry <pkg>` after
  fixing the cause.

---

## Local PKGBUILD patches

arch-native can build a *modified* package, not just an optimized copy. A patch
is a unified diff applied on top of the fetched upstream PKGBUILD on every build
— to disable a failing test, add a configure flag, strip a dep — re-applied as
upstream moves, with drift checking to flag when it needs review.

Patches live at `/var/lib/arch-native/pkgbuilds/local/<pkg>/<pkg>.patch`. Pair
one with [`always_build`](#always-build-packages) to publish a custom build of an
uninstalled package.

### Build a package from a full PKGBUILD

To build something with **no upstream PKGBUILD anywhere** (your own software),
drop a complete PKGBUILD instead:

```
/var/lib/arch-native/pkgbuilds/local/<pkg>/PKGBUILD
```

With no `.patch` beside it, it resolves as tier `local` and is built like any
package; add it to [`always_build`](#always-build-packages) so it builds without
being installed. For a package that *does* exist upstream, prefer a patch — a
full copy won't track upstream updates (the daemon logs a reminder each build).

### Manage patches

```bash
sudo buildbot patch create networkmanager          # open upstream PKGBUILD in $EDITOR, save diff
sudo buildbot patch show networkmanager            # print the current diff
sudo buildbot patch create --force networkmanager  # re-author against current upstream
sudo buildbot patch check --all                    # check all patches for drift
sudo buildbot patch status                          # recompute + publish patch-status.json
```

Common edits:

```diff
-  make check
+  # make check  # broken with -march=native
```
```diff
-  ./configure --prefix=/usr
+  ./configure --prefix=/usr --disable-foo
```

### Patch health

`buildbot patch check --all` reports one state per patch:

```
elogind          ok        (tier: artix)
flashrom         orphaned  (not installed)
networkmanager   review    (written for 1.46.0-3, upstream now 1.48.0-2)
zip              FAIL      checking file PKGBUILD
```

- **ok** — applies cleanly.
- **orphaned** — package no longer installed; safe to remove.
- **review** — still applies, but upstream moved past the version it was written
  for; check whether it's still needed, then `buildbot patch create --force`.
- **FAIL** — no longer applies; the next build of that package fails loudly until
  you update it.

`review` works by recording the upstream `pkgver`/`pkgrel` as a header in the
`.patch` (catching the case where upstream fixes the issue but the patch still
applies). Pre-header patches just report `ok`/`FAIL`; recreate with `--force` to
opt in. VCS packages get pkgrel-drift detection only.

The daemon publishes `patch-status.json` to the repo at startup and each
`upstream_check_interval` so `native-sync` can show the **patches** line. Run
`buildbot patch check --all` after upstream cycles to catch drift early.

---

## How it works

### Build cycle

```
Desktop — after each pacman transaction (remote mode):
  pkglist-export.hook fires
  → reads pacman -Qi + pacman -Sl, writes a JSON manifest, rsyncs it to:
  → build-server:/var/lib/arch-native/manifests/client.json

Build server — main loop (poll_interval = 300s):
  1. Upgrade clean chroot        arch-nspawn -Syu inside chroots/root/
  2. Detect package list change  local: read pacman DB; remote: watch manifest mtime
                                 diff against built.json → queue new/changed packages
  3. Upstream update check       every upstream_check_interval (default 1h):
                                   git pull cached PKGBUILD repos
                                   vercmp each built package against PKGBUILD version
                                   re-queue anything with a newer upstream version
  4. Drain full pending queue    repeat until empty or shutdown:
       resolve_pkgbuild()          local patch → tiers in repo_priority
       parse_srcinfo()             version, deps, pgp keys
       is_eligible()               skip blacklisted / already-built-at-version
       import_pgp_keys() → makechrootpkg → sign → repo-add (+ autoprune)
  5. Sleep remainder of poll_interval
```

Official-repo updates are thus picked up automatically once an hour; newly
installed packages on the next cycle. The full queue drains before the daemon
sleeps; `buildbot status` shows a **next cycle** countdown.

### PKGBUILD tier resolution

`repo_priority` sets the order; tier names are arbitrary and each maps to a
source type.

| Source | How it works | Auto-updated |
|---|---|---|
| `local` | patches/full PKGBUILDs in `pkgbuilds/local/<pkg>/`, applied over upstream | No (user-managed) |
| `clone <url>` | per-package `git clone --depth=1` (`{pkgname}` substituted), cached; checks root and `trunk/` | Yes, per-package pull each cycle |
| `monorepo` | walks `pkgbuilds/<tier>/` by pkgname; clone once manually | Yes, whole-repo pull |
| `pkgctl` | `git clone` from Arch's packaging GitLab; needs `devtools` | Yes, per-package pull |

`tier_version_select`: `priority` (first tier with a PKGBUILD wins — safe when
higher tiers carry distro patches) or `highest` (highest `pkgver` across tiers
wins — useful when a tier lags upstream).

Built-in tiers need no config: `artix` (clone gitea), `cachyos` (monorepo, clone
manually to `pkgbuilds/cachyos/`), `arch` (pkgctl). Add your own by naming a tier
and defining its source:

```ini
repo_priority = local,myfork,arch
myfork_source = clone https://git.example.com/packages/{pkgname}.git
```

```bash
# monorepo tiers (incl. cachyos) are cloned once by hand:
sudo git clone --depth=1 https://github.com/CachyOS/CachyOS-PKGBUILDS \
    /var/lib/arch-native/pkgbuilds/cachyos
```

Version detection during the upstream check only covers packages already built
once (the clone must exist). For a package installed since the last check, run
`buildbot sync` to force it into the queue.

### native-sync detection

Forge builds keep upstream's pkgver/pkgrel. `native-sync` identifies forge
packages by the `PACKAGER` field (`Buildbot <buildbot@forge>`), with a
dotted-pkgrel fallback for older builds. When the distro ships a higher version,
`pacman -Syu` takes it first; `native-sync` re-upgrades once buildbot rebuilds at
that version.

### LTO auto-retry

A build that fails with a linker error (`collect2: error: ld returned`) or Rust
LTO incompatibility is retried once with LTO disabled (`LTOFLAGS=""`, `!lto`),
logged as `<timestamp>-nolto.log`. Add packages to `lto_blacklist` to skip the
doomed first attempt.

### Build host / target CPU mismatch (remote mode)

When the build server can't execute target-CPU binaries, the daemon:

- sets `!check` in `BUILDENV` — test suites run compiled binaries and would
  SIGILL;
- omits `target-cpu=<march>` from `RUSTFLAGS` — Cargo runs `build.rs` on the
  build host; C/C++ still gets full `-march` via `CFLAGS`, only Rust is limited
  to `-C opt-level=3`.

In local mode (`march = native`) both limitations lift and tests run.

### Download failure retry

Transient failures (HTTP 429, SSL errors, connection resets) are detected by log
pattern and re-queued up to `download_retry_limit` times before moving to
`failed.json` as `"download failed after N attempts"`.

### File system layout

```
/var/lib/arch-native/
├── built.json          {pkgname: {version, pkgrel, built_at, pkg_files, pgp_skipped?}}
├── pending.json        [{name, version, repo, build_reason, download_retries?}, ...]
├── failed.json         {pkgname: {version, reason, retries, timestamp}}
├── metrics.json        last-cycle stats (for external monitoring)
├── in_progress.json    currently building; re-inserted at front on restart
├── manifests/client.json    package list from desktop (remote mode)
├── chroots/
│   ├── root/           clean base — upgraded each cycle
│   └── build-<uuid>/   ephemeral per-build copy (stale ones = interrupted build)
├── gnupg/              signing key (0700)
├── logs/<pkgname>/YYYYMMDD-HHMMSS.log   (+ -nolto.log on LTO retry)
├── makepkg-configs/makepkg.<march>.conf
├── pkgbuilds/
│   ├── local/<pkg>/    <pkg>.patch (preferred) and/or full PKGBUILD; _patched/ working dir
│   └── <tier>/<pkg>/   clone/pkgctl per-package, or a monorepo tree
└── repo/
    ├── <repo_name>.db.tar.zst
    ├── *.pkg.tar.zst[.sig]
    ├── patch-status.json     patch health (read by native-sync)
    └── buildbot-public.asc
```

---

## buildbot CLI reference

`buildbot` is both the daemon (no subcommand) and the CLI. Full detail in
`man buildbot`.

| Command | Does |
|---|---|
| `init` | bootstrap a new install (layout, base chroot, keyring); safe to re-run |
| `status` | one-screen overview (see below) |
| `doctor` | check paths, JSON validity, gnupg perms, chroot keyring, cascade state |
| `fsck [--dry-run] [-v] [--force]` | verify/repair built.json ↔ repo DB ↔ `.pkg` files; service must be stopped (`--force` overrides). Also runs each cycle |
| `built [-n N]` | list built packages (N most recent) |
| `queue [-n N]` | list the pending queue (default 25) |
| `failed [-n N]` | list failed builds with reason + retry count |
| `logs PKG [-f]` | print the latest build log; `-f` follows it |
| `why PKG` | explain a package's current state in plain English |
| `sync` | re-scan the package list and build now (works while running) |
| `retry PKG \| --all [--dry-run]` | re-queue a failed (or any) package; service stopped |
| `clear PKG \| --all` | drop from the failed list without retrying |
| `patch …` | manage local patches — see [Local PKGBUILD patches](#local-pkgbuild-patches) |

### Monitoring

```
arch-native  ● active

Building
  package    firefox
  elapsed    1h23m

Queue  52 pending
  breakdown  8 new · 44 updates
  ▸ thunderbird  115.12.0-1  update
    curl         8.12.1-1    update

Recently built
  fish          3.7.1-2   2h ago
  curl          8.7.1-1   3h ago

Stalled  needs attention  1
  gpgme      7d ago       5x  collect2: error: ld returned 1 exit status

Failed  2
  krb5       2h ago    download failed after 3 attempts
  +1 more — run: buildbot failed

Repo  forge
  rebuilt      987 / 1189  (83%)
  blacklisted  47 / 1189  (4%)  (see /etc/arch-native.conf)
  ineligible   12 / 1189  (1%)  (arch=any — not rebuilt)
  patches      44  (41 ok · 2 review · 1 fail · 0 orphaned)
  size         12G
  next cycle   in 4m
```

**next cycle** counts down to the next poll (see [Build cycle](#build-cycle)).
**Building** shows the current compile and elapsed time (`idle` when none). If
the daemon died mid-build it shows `status stale — daemon not running`; if a
build passed `build_timeout` it shows `⚠ exceeded build_timeout`. Both mean the
build is stuck.

### Queue management

`retry`, `clear`, and `sync --reset` need the service stopped. `sync` (no
`--reset`) works while running — it triggers an immediate pass.

```bash
sudo buildbot sync                   # re-scan + build now (running or not)

sudo systemctl stop arch-native
sudo buildbot retry firefox          # one failed package back to the queue
sudo buildbot retry networkmanager   # force-rebuild any package (clears built.json entry)
sudo buildbot retry --all            # all failed (skips ones no longer in the manifest)
sudo buildbot retry --all --dry-run  # preview
sudo buildbot clear firefox          # drop from failed list
sudo buildbot clear --all
sudo buildbot sync --reset           # clear queue and rebuild from scratch
sudo systemctl start arch-native
```

### metrics.json

Written atomically after each cycle for scraping (Prometheus/Grafana):

```json
{
  "timestamp":               "2025-06-01T03:00:00+00:00",
  "status":                  "sleeping",
  "pending_start":           12,
  "pending_end":             0,
  "attempted":               12,
  "succeeded":               11,
  "failed":                  1,
  "skipped_previous_failure": 0,
  "skipped_ineligible":      4,
  "skipped_missing_keys":    0,
  "cycle_seconds":           3820,
  "sleep_seconds":           300
}
```

`status` is `sleeping` / `processing` / `starting`. `cycle_seconds` and
`sleep_seconds` appear only when `sleeping`.

---

## License

[GNU General Public License v3.0 or later](LICENSE) — this program is free
software; you may redistribute and/or modify it under the terms of the GPL as
published by the Free Software Foundation, either version 3 or (at your option)
any later version.
