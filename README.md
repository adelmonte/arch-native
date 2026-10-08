# arch-native

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Rebuilds the packages you have installed from source with compiler flags tuned
for your CPU (`-march=znver4`, `-march=native`, …), signs them, and serves them as
a pacman repo. A helper then swaps your installed packages for the optimized
builds. It works on any pacman-based distro.

It ships as two packages:

| Package | Runs on | Provides |
|---|---|---|
| `arch-native` | build host | `buildbot`: the daemon and its CLI |
| `arch-native-client` | the machine using the packages | `native-sync`: installs forge builds · `pkglist-export`: pacman hook that sends your package list to a remote build host |

**forge** is the default repo name and is used throughout this README.

---

## Contents

- [Choose a mode](#choose-a-mode)
- [Quick start](#quick-start)
- [Everyday use](#everyday-use)
- [Choosing what gets built](#choosing-what-gets-built)
- [Patching packages](#patching-packages)
- [Configuration reference](#configuration-reference)
- [How it works](#how-it-works)
- [License](#license)

---

## Choose a mode

| | **Local** | **Remote** |
|---|---|---|
| Builds on | the machine that uses the packages | a separate build server |
| Package list | read straight from the local pacman DB | pushed by a pacman hook over SSH/rsync |
| Repo served by | `file://`, no web server needed | nginx (or any HTTP server) |
| `march` | `native` | your target CPU, e.g. `znver4` |
| Test suites and Rust `target-cpu` | run | run only if the server's CPU supports the target ISA ([details](#build-host--target-cpu-mismatch)) |

Pick **local** if the machine is fast enough to build its own packages. Pick
**remote** to offload builds. A remote server with an older CPU still builds
full `-march=<target>` C/C++.

---

## Quick start

Steps 1–4 run on the build host. Steps 5–6 run on the machine that uses the
packages. In local mode they are the same machine.

### 1. Install and configure the server

```bash
cd arch-native && makepkg -si
sudoedit /etc/arch-native.conf
```

The shipped config is a working starting point. Check these keys:

```ini
[arch-native]
repo_name = forge
mode      = local          # or: remote
march     = native         # remote: your target CPU, e.g. znver4, pantherlake
distro    = arch           # or: artix

blacklist = gcc,glibc,binutils,coreutils,linux-api-headers,
            ttf-*,otf-*,font-*,*-icon-theme,*-cursors,
            linux-firmware,linux-firmware-*,*-keyring,*-bin
lto_blacklist = llvm,rust
```

> **⚠ Get the blacklist right before the first start.** Without one, the daemon
> rebuilds your toolchain (`gcc`, `glibc`, `binutils`) with a custom `-march`,
> which can leave the system unbootable. See [Choosing what gets
> built](#choosing-what-gets-built).

To find a remote target's `march`, run this on the target machine:
`gcc -march=native -Q --help=target | grep -m1 march`.

### 2. Initialize

```bash
sudo buildbot init
```

This creates the data directory and the clean build chroot, and generates the
repo signing key, exporting it to `/var/lib/arch-native/repo/buildbot-public.asc`.
It is safe to re-run.

### 3. Remote mode only: serve the repo over HTTP

```bash
sudo cp /usr/share/arch-native/nginx.conf.example /etc/nginx/conf.d/arch-native.conf
sudo systemctl reload nginx        # serves http://<host>:8081/repo/
```

### 4. Start the daemon

```bash
sudo systemctl enable --now arch-native
sudo buildbot status               # what's building, queued, failed
```

The first cycle starts building immediately and works through every installed
package, which can take days. Builds go straight into the repo as they finish.
Using a different init system? See
[Other init systems](#other-init-systems).

### 5. Point pacman at the repo

Add the repo to `/etc/pacman.conf` **below** your distro's repos:

```ini
[forge]
SigLevel = Required DatabaseOptional
Server = file:///var/lib/arch-native/repo        # local mode
# Server = http://build-host:8081/repo           # remote mode
```

Trust the signing key:

```bash
curl -sO http://build-host:8081/repo/buildbot-public.asc   # remote mode only
sudo pacman-key --add buildbot-public.asc                   # local: /var/lib/arch-native/repo/buildbot-public.asc
sudo pacman-key --lsign-key arch-native@localhost
```

Install the client and pull in the first forge builds:

```bash
cd arch-native-client && makepkg -si
sudo pacman -Sy && sudo native-sync
```

**Local mode setup is complete.**

### 6. Remote mode only: send your package list to the server

`pkglist-export` runs as a pacman hook after every transaction and rsyncs your
installed-package list to the build server. Until it is configured, it does
nothing.

```bash
sudo cp /usr/share/arch-native-client/arch-native-client.conf.example /etc/arch-native-client.conf
sudoedit /etc/arch-native-client.conf
```

```bash
REPO_NAME="forge"
REMOTE_HOST="user@build-host"
REMOTE_PATH="/var/lib/arch-native/manifests/client.json"
# SSH_KEY="/root/.ssh/id_ed25519"     # omit to use the default key
```

The hook runs as root, so root on this machine needs SSH access to the server:

```bash
# this machine
sudo ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ""
sudo cat /root/.ssh/id_ed25519.pub       # append to ~user/.ssh/authorized_keys on the server

# build server
sudo setfacl -m u:user:rwx /var/lib/arch-native/manifests

# this machine: test it end to end
sudo pkglist-export
```

---

## Everyday use

### Upgrading

`native-sync` installs every package that has a newer forge build than the one
installed. Run it after each system upgrade:

```bash
update() { yay -Syu && sudo native-sync; }          # bash/zsh
function update; yay -Syu; and sudo native-sync; end   # fish
```

```
native-sync: 853 / 1197  (71%)
native-sync: patches  41 ok, 2 review, 1 fail
native-sync: upgrading 3 package(s) from forge
```

The first line shows how many of your installed packages forge has built. The
`patches` line reports the health of your [local patches](#patching-packages).

When the distro releases a newer version, `pacman -Syu` installs it first. Once
buildbot rebuilds that version, `native-sync` swaps the forge build back in.
Packages in `IgnorePkg` or `IgnoreGroup` are never touched.

If you renamed the repo, set `REPO_NAME="myrepo"` in
`/etc/arch-native-client.conf`.

### Checking on the server

| Command | Shows |
|---|---|
| `buildbot status` | one-screen overview: current build, queue, failures, repo coverage |
| `buildbot why <pkg>` | why a package is or isn't built, in plain English |
| `buildbot failed` | failed builds with reason and retry count |
| `buildbot logs <pkg> [-f]` | the latest build log |
| `buildbot queue` · `buildbot built` | pending queue · recently built |
| `buildbot doctor` | health checks: paths, permissions, keyring, soname consistency |

`man buildbot` has the full reference.

<details>
<summary>Example <code>buildbot status</code></summary>

```
arch-native  ● active

Building
  package    firefox
  elapsed    1h23m

Queue  52 pending
  breakdown  8 new · 44 updates
  ▸ thunderbird  115.12.0-1  update
    curl         8.12.1-1    update

Stalled  needs attention  1
  gpgme      7d ago       5x  collect2: error: ld returned 1 exit status

Repo  forge
  rebuilt      987 / 1189  (83%)
  blacklisted  47 / 1189  (4%)
  patches      44  (41 ok · 2 review · 1 fail · 0 orphaned)
  next cycle   in 4m
```

`status stale — daemon not running` or `⚠ exceeded build_timeout` under
**Building** means the build is stuck.

</details>

### Fixing the queue

All of these work while the daemon is running:

```bash
sudo buildbot sync                   # rescan installed packages and start building now
sudo buildbot retry firefox          # retry a failed package, or force-rebuild any package
sudo buildbot retry --all            # retry every failed package (--dry-run to preview)
sudo buildbot clear firefox          # drop from the failed list without retrying
sudo buildbot sync --reset           # discard the queue and rebuild it from scratch
```

Failed packages are retried automatically on a backoff. A package that keeps
failing is marked **stalled**, and is retried again after
`stall_auto_retry_days`.

Stopping the service lets the current build finish first, so it can take up to
`build_timeout`. If the daemon is killed mid-build, the build is re-queued at the
front and a consistency check (`fsck`) runs on the next start.

---

## Choosing what gets built

forge builds what you have installed, minus:

- **The blacklist.** These packages are never built. `fnmatch` globs are allowed.
- **Ineligible packages.** `arch=any` packages (nothing to compile) and packages
  that build with GHC are skipped automatically.
- **AUR and other foreign packages.** These have no upstream PKGBUILD to fetch.

### Writing your blacklist

| Category | Examples | Why |
|---|---|---|
| Toolchain and core | `gcc` `glibc` `binutils` `coreutils` `linux-api-headers` | a bad `-march` here can make the system unbootable |
| Data only | `ttf-*` `otf-*` `font-*` `*-icon-theme` `*-cursors` `linux-firmware*` `*-keyring` `*-translations` `hunspell-*` | nothing to optimize |
| Prebuilt | `*-bin` | no source to compile |
| Often troublesome | `llvm` `rust` | also add them to `lto_blacklist` |

**Blacklist the pkgbase, not a subpackage.** One PKGBUILD can produce several
packages, for example `gcc` → `gcc`, `gcc-libs`, `gcc-fortran`. The daemon builds
by pkgbase, so `blacklist = gcc` covers `gcc-libs`, but `blacklist = gcc-libs`
still lets it build through `gcc`.

After editing, `buildbot status` shows the blacklisted count and
`buildbot queue -n 200` shows what is actually queued.

### Building packages you don't have installed

```ini
always_build = xdg-desktop-portal-kde
```

Each listed package is built and kept in the repo even when it isn't installed.
Combine this with a [patch](#patching-packages) to publish a customized build.
Remove a name from the list and the package is pruned on the next cycle.

### Why isn't my package built?

Run `buildbot why <pkg>`. Besides the cases above, a package can be waiting on
one of these:

| State | Meaning | Resolves |
|---|---|---|
| `pending_upstream` | your installed version is newer than any PKGBUILD in your tiers | automatically, once the tier catches up |
| `pending_release` | the PKGBUILD is newer than anything your distro has released (still in staging or testing) | automatically, once the distro releases it |
| `pending_world_cascade` | the build bumped a library soname that the distro repos haven't migrated to yet; it is held back so the build doesn't break distro packages | automatically |
| stalled | failed repeatedly | after fixing the cause, run `buildbot retry <pkg>` |

---

## Patching packages

arch-native can build a *modified* package, not just an optimized one. A patch
is a unified diff applied to the upstream PKGBUILD on every build, for example to
disable a broken test, add a configure flag, or drop a dependency. The patch is
re-applied as upstream moves and checked for drift.

```bash
sudo buildbot patch create networkmanager          # edit the upstream PKGBUILD in $EDITOR, save the diff
sudo buildbot patch show networkmanager
sudo buildbot patch check --all                    # health of every patch
sudo buildbot patch ack networkmanager             # mark as reviewed against current upstream
sudo buildbot patch ack --permanent gstreamer      # never flag for version drift
sudo buildbot patch create --force networkmanager  # rewrite against current upstream
```

Patches are stored at `/var/lib/arch-native/pkgbuilds/local/<pkg>/<pkg>.patch`.

### Patch health

```
elogind          ok        (tier: artix)
networkmanager   review    (written for 1.46.0-3, upstream now 1.48.0-2)
zip              FAIL      checking file PKGBUILD
```

| State | Meaning | What to do |
|---|---|---|
| **ok** | applies cleanly | nothing |
| **review** | applies, but upstream released a new `pkgver` since you last looked | check whether the patch is still needed, then `patch ack` it |
| **obsolete** | upstream already contains this change | delete the patch |
| **FAIL** | no longer applies, so the package's next build will fail | `patch create --force` |
| **orphaned** | package no longer installed | delete the patch |

`review` only triggers on a `pkgver` change. A `pkgrel`-only bump doesn't count,
because the sources are unchanged. Use `--permanent` for patches that adapt
upstream to *your build setup* rather than fix an upstream bug (stripping
`-march` for a cross-build host, the Artix `libexec` layout, disabling LTO), since
new upstream releases won't make them unnecessary.

### Building your own software

Put a complete `PKGBUILD` at `pkgbuilds/local/<pkg>/PKGBUILD`, with no `.patch`
file next to it, and add the package to `always_build`. For a package that exists
upstream, use a patch instead: a full copy doesn't follow upstream updates.

---

## Configuration reference

All settings live in the `[arch-native]` section of `/etc/arch-native.conf`.
The **Default** column is what applies when a key is omitted. The shipped config
overrides some of these defaults. Inline `# comments` are allowed.

#### Core

| Key | Default | Meaning |
|---|---|---|
| `repo_name` | `forge` | repo DB name, also used in the `PACKAGER` field |
| `mode` | `local` | `local` or `remote` |
| `distro` | `arch` | `artix` installs elogind/libudev into the chroot and deploys an `artix-meson` wrapper |
| `build_user` | `buildbot` | unprivileged user that runs builds |

#### Compiler flags

These generate `makepkg.conf`. Changes apply on the next daemon start.

| Key | Default | Meaning |
|---|---|---|
| `march` | `native` | `-march=` target |
| `opt_level` | `3` | `-O` level, also Rust `opt-level`: `0 1 2 3 s g fast` |
| `lto` | `true` | link-time optimization on or off |
| `ltoflags` | `-flto=auto -falign-functions=32` | used when `lto = true` |
| `cflags_base` | Arch defaults, plus `-fno-semantic-interposition` | CFLAGS other than `-march`/`-O` |
| `ldflags` | Arch defaults | LDFLAGS |
| `extra_cflags` | *(empty)* | appended to CFLAGS. The shipped config demotes the GCC 15 errors `incompatible-pointer-types`, `discarded-qualifiers` and `implicit-function-declaration` to warnings |

#### Package selection

| Key | Default | Meaning |
|---|---|---|
| `blacklist` | `gcc,glibc,coreutils,linux-api-headers` | never built (globs allowed) |
| `lto_blacklist` | *(empty)* | built with LTO off (globs allowed) |
| `always_build` | *(empty)* | built even when not installed (plain names only) |

#### PKGBUILD sources

| Key | Default | Meaning |
|---|---|---|
| `repo_priority` | `local,arch` | ordered list of tiers to fetch PKGBUILDs from ([details](#pkgbuild-tiers)) |
| `<tier>_source` | see tiers | `clone <url>`, `monorepo`, or `pkgctl` |
| `tier_version_select` | `priority` | `priority`: first tier that has the package wins. `highest`: highest `pkgver` across tiers wins |

Per-package overrides go in their own sections:

```ini
[package_tiers]          # keep "local" first to keep patch support
networkmanager = local,artix,arch

[package_timeouts]       # seconds
firefox = 28800
```

#### Build behavior

| Key | Default | Meaning |
|---|---|---|
| `build_timeout` | `14400` | per-build limit in seconds; `0` disables |
| `download_retry_limit` | `3` | re-queue transient download failures this many times |
| `skip_pgp_on_import_failure` | `false` | if a source signing key can't be fetched, build with `--skippgpcheck` (hashes still verified). A bad signature or revoked key is always a hard failure |
| `failed_stall_retries` / `failed_stall_days` | `5` / `7` | mark a package stalled after this many failures, once the last failure is this many days old |
| `stall_auto_retry_days` | `3` | retry stalled packages after this long; `0` disables |

#### Housekeeping

| Key | Default | Meaning |
|---|---|---|
| `autoprune` · `autoprune_keep` | `true` · `1` | delete superseded package files, keeping N versions |
| `autoprune_blacklisted` | `true` | remove newly blacklisted packages from the repo |
| `autoprune_uninstalled` | `true` | remove packages you no longer have installed |
| `autoprune_pkgbuild_clones` | `true` | remove cached PKGBUILD clones nothing needs |
| `poll_interval` | `300` | seconds between daemon cycles |
| `upstream_check_interval` | `3600` | seconds between checks for upstream PKGBUILD updates |
| `log_retention_days` | `7` | build log retention |
| `log_level` | `INFO` | `DEBUG` traces how each package's PKGBUILD is resolved |

#### Chroot and paths

| Key | Default | Meaning |
|---|---|---|
| `chroot_pacman_conf` | by `distro` | the build chroot's `pacman.conf`. `artix`: the bundled Artix + CachyOS config. `arch`: devtools' `extra.conf` |
| `chroot_extra_packages` | artix: `libelogind,libudev,elogind` | installed into the chroot on every upgrade |

Every path defaults to a location under `/var/lib/arch-native/`. The overridable
paths are `chroot_dir`, `chroot_root`, `repo_dir`, `repo_db`, `pkgbuilds_dir`,
`makepkg_configs_dir`, `manifest_path`, `gnupg_home`, `log_dir` and
`metrics_path`. If you override `chroot_dir`, set `chroot_root` too.

---

## How it works

### The build cycle

```
every poll_interval (5 min):
  1. upgrade the clean chroot
  2. diff installed packages against built.json → queue new and changed packages
     queue rebuilds for forge packages that link a soname forge no longer ships
  3. every upstream_check_interval (1 h), in the background:
       git pull PKGBUILDs, queue upstream bumps
  4. drain the queue:
       resolve PKGBUILD (local patch → tiers) → parse .SRCINFO → eligibility check
       → import PGP keys → makechrootpkg → sign → repo-add
  5. sleep until the next cycle (buildbot sync wakes it early)
```

### PKGBUILD tiers

`repo_priority` lists where PKGBUILDs come from, in order. Each tier name maps to
a source type:

| Source | Behavior |
|---|---|
| `local` | your patches or full PKGBUILDs in `pkgbuilds/local/`. Always tried first |
| `clone <url>` | per-package `git clone`, with `{pkgname}` substituted in the URL |
| `monorepo` | one big repo walked by package name. Clone it once by hand into `pkgbuilds/<tier>/` |
| `pkgctl` | Arch's packaging GitLab |

The tiers `artix` (clone from Artix gitea), `cachyos` (monorepo) and `arch`
(pkgctl) work with no further configuration. To add your own:

```ini
repo_priority = local,myfork,arch
myfork_source = clone https://git.example.com/packages/{pkgname}.git
```

```bash
sudo git clone --depth=1 https://github.com/CachyOS/CachyOS-PKGBUILDS /var/lib/arch-native/pkgbuilds/cachyos
```

### Soname safety

A library rebuild can change its soname without any version change. For example,
`abseil-cpp 20260817.0-1` → `-2` moved every `libabsl` from `2605` to `2608`.
arch-native guards against this in both directions:

- **Protecting distro packages.** If a forge build introduces a soname that the
  distro repos haven't migrated to yet, the build is held back as
  `pending_world_cascade` until they catch up.
- **Protecting forge packages.** Each cycle the daemon reads the real ELF
  `SONAME`/`DT_NEEDED` entries of every repo package (cached in `sonames.json`).
  Forge packages still linking a soname forge no longer ships are rebuilt. A
  package that needs a *newer* soname than forge provides is only reported, since
  the library itself has to be fixed first.

### Build host / target CPU mismatch

If the build server can't run binaries built for the target CPU, the daemon
disables test suites (`!check`) and drops `target-cpu` from `RUSTFLAGS`, because
both would run target code on the build host and crash with SIGILL. C/C++ still
gets the full `-march`. Local mode has neither limitation.

### Automatic retries

- **Link failures** (`ld returned`, Rust LTO errors) are retried once with LTO
  off. Put repeat offenders in `lto_blacklist` to skip the failing first attempt.
- **Download failures** (HTTP 429, TLS errors, connection resets) are re-queued
  up to `download_retry_limit` times.
- **Missing dependencies or sources in one tier** move the next attempt to the
  next tier.

### How native-sync recognizes forge builds

forge builds keep upstream's `pkgver-pkgrel`. `native-sync` tells them apart by
the `PACKAGER` field (`Buildbot <buildbot@forge>`) and reinstalls any package at
the same version that wasn't built by forge. Before installing, it clears cached
copies from pacman's cache, because a cached distro build of the same version
would fail checksum verification.

### Files

```
/var/lib/arch-native/
├── built.json            per-package build record and status
├── pending.json          build queue
├── failed.json           failures with reason, retry count, backoff
├── in_progress.json      the current build; re-queued if the daemon dies
├── sonames.json          ELF soname cache
├── daemon.pid            held by the running daemon; the CLI checks it
├── metrics.json          last-cycle stats, for Prometheus etc.
├── manifests/client.json package list from the client (remote mode)
├── chroots/root/         clean base chroot
├── gnupg/                signing key
├── logs/<pkg>/           build logs (…-nolto.log for an LTO retry)
├── pkgbuilds/local/      your patches
├── pkgbuilds/<tier>/     fetched PKGBUILDs
└── repo/                 the pacman repo, patch-status.json, buildbot-public.asc
```

### Other init systems

<details>
<summary>dinit, OpenRC, runit</summary>

The daemon is `/usr/bin/buildbot --config /etc/arch-native.conf` run as root.
It shuts down cleanly on SIGTERM.

**dinit**: `/etc/dinit.d/arch-native`

```
type = process
command = /usr/bin/buildbot --config /etc/arch-native.conf
logfile = /var/log/arch-native.log
restart = true
```

**OpenRC**: `/etc/init.d/arch-native`

```bash
#!/sbin/openrc-run
command=/usr/bin/buildbot
command_args="--config /etc/arch-native.conf"
command_background=true
pidfile=/run/arch-native.pid
output_log=/var/log/arch-native.log
error_log=/var/log/arch-native.log
```

**runit**: `/etc/runit/sv/arch-native/run`

```bash
#!/bin/sh
exec /usr/bin/buildbot --config /etc/arch-native.conf 2>&1
```

</details>

---

## License

[GPL-3.0-or-later](LICENSE).
