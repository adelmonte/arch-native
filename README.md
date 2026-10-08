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
- [Dependencies](#dependencies)
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

If your nginx uses `sites-available`/`sites-enabled` instead of `conf.d`, copy
it to `sites-available/arch-native` and symlink it into `sites-enabled`. Edit
the port in the file first if 8081 is taken.

### 4. Start the daemon

```bash
sudo systemctl enable --now arch-native
sudo buildbot status               # what's building, queued, failed
```

The first cycle starts building immediately and works through every installed
package, which can take days. Builds go straight into the repo as they finish.

<details>
<summary>Other init systems (dinit, OpenRC, runit)</summary>

The daemon is `/usr/bin/buildbot --config /etc/arch-native.conf`, run as root.
It shuts down cleanly on SIGTERM.

**dinit**: `/etc/dinit.d/arch-native`

```
type = process
command = /usr/bin/buildbot --config /etc/arch-native.conf
logfile = /var/log/arch-native.log
restart = true
```

```bash
sudo dinitctl enable arch-native
```

**OpenRC**: `/etc/init.d/arch-native`

```bash
#!/sbin/openrc-run
description="arch-native package build daemon"
command=/usr/bin/buildbot
command_args="--config /etc/arch-native.conf"
command_background=true
pidfile=/run/arch-native.pid
output_log=/var/log/arch-native.log
error_log=/var/log/arch-native.log
```

```bash
sudo chmod +x /etc/init.d/arch-native
sudo rc-update add arch-native default && sudo rc-service arch-native start
```

**runit**: `/etc/runit/sv/arch-native/run`

```bash
#!/bin/sh
exec /usr/bin/buildbot --config /etc/arch-native.conf 2>&1
```

```bash
sudo chmod +x /etc/runit/sv/arch-native/run
sudo ln -s /etc/runit/sv/arch-native /run/runit/service/
```

</details>

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
echo "ssh-ed25519 AAAA... root@desktop" >> ~user/.ssh/authorized_keys
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

**Packages forge doesn't build.** Some packages can't usefully be built on your
server: firefox, for example, runs the browser during its build, which a build
host with an older CPU can't do at your `march`. If CachyOS's x86-64-v3 repos
are configured, `native-sync` can take those packages from there instead of
your first repo's generic build. List them in `/etc/arch-native-client.conf`:

```bash
PREFER_V3=(firefox deno)
```

A v3 build that depends on systemd is never taken, and a package forge does
build always comes from forge. `sudo native-sync --dry-run` shows what would be
installed without installing it.

### Checking on the server

`buildbot` is both the daemon (run with no subcommand) and the CLI. Full
detail in `man buildbot`.

| Command | Does |
|---|---|
| `status` | one-screen overview: current build, queue, failures, repo coverage |
| `why PKG` | explain a package's current state in plain English |
| `logs PKG [-f]` | print the latest build log; `-f` follows it |
| `failed [-n N]` | failed builds with reason and retry count |
| `queue [-n N]` | the pending queue (default 25) |
| `built [-n N]` | built packages, newest first |
| `doctor` | check paths, state files, gnupg permissions, chroot keyring, staged cascades and soname consistency |
| `sync [--reset] [--dry-run]` | re-scan the package list and build now |
| `retry PKG \| --all [--dry-run]` | re-queue a failed package, or force-rebuild any package |
| `clear PKG \| --all [--dry-run]` | drop from the failed list without retrying |
| `fsck [--dry-run] [-v] [--force]` | check and repair built.json ↔ repo DB ↔ package files. Needs the service stopped (`--force` overrides). Also runs at every daemon start |
| `init` | set up a new install: layout, build chroot, keyring, signing key. Safe to re-run |
| `patch …` | manage local patches; see [Patching packages](#patching-packages) |

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
  ineligible   12 / 1189  (1%)  (12 arch=any)
  patches      44  (41 ok · 2 review · 1 fail · 0 orphaned)
  size         12G
  next cycle   in 4m
```

**Building** shows the current build and how long it has run (`idle` when
none). `status stale — daemon not running` or `⚠ exceeded build_timeout` there
means the build is stuck. **next cycle** counts down to the daemon's next pass.

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
| Data only | `ttf-*` `otf-*` `font-*` `*-icon-theme` `*-cursors` `linux-firmware*` `*-keyring` `*-translations` `hunspell-*` `tesseract-data-*` `*-dinit` `*-openrc` `*-runit` | nothing to optimize |
| Prebuilt and AUR | `*-bin` `*-git` `*-svn` | no source to compile, or no upstream PKGBUILD; they would only fill the failed list |
| Often troublesome | `llvm` `rust`; packages whose build ignores `CFLAGS` (some Go and Java) | add `llvm` and `rust` to `lto_blacklist` too |

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
sudo buildbot patch ack --all                      # ack everything in review
sudo buildbot patch status                         # recompute and publish patch-status.json
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

`review` works by recording the upstream `pkgver`/`pkgrel` in a header line of
the `.patch`. Patches without one only report ok/FAIL; recreate them with
`--force` to opt in. VCS packages get pkgrel-drift detection only.

The daemon publishes `patch-status.json` into the repo at startup and every
`upstream_check_interval`; that's where `native-sync`'s **patches** line comes
from.

### Building your own software

Put a complete `PKGBUILD` at `pkgbuilds/local/<pkg>/PKGBUILD`, with no `.patch`
file next to it, and add the package to `always_build`. For a package that exists
upstream, use a patch instead: a full copy doesn't follow upstream updates.

---

## Configuration reference

All settings live in the `[arch-native]` section of `/etc/arch-native.conf`.
The **Default** column is what applies when a key is omitted. The shipped config
overrides some of these defaults. Inline `# comments` are allowed; values
themselves can't contain `#`.

#### Core

| Key | Default | Meaning |
|---|---|---|
| `repo_name` | `forge` | repo DB name, also used in the `PACKAGER` field |
| `mode` | `local` | `local` or `remote` |
| `distro` | `arch` | `artix` installs elogind/libudev into the chroot and deploys an `artix-meson` wrapper |
| `build_user` | `buildbot` | unprivileged user that owns and runs builds; must exist |

#### Compiler flags

These generate `makepkg.conf`. Changes apply on the next daemon start.

| Key | Default | Meaning |
|---|---|---|
| `march` | `native` | `-march=` target |
| `opt_level` | `3` | `-O` level, also Rust `opt-level`: `0 1 2 3 s g fast` (`fast` → `-Ofast`/Rust `3`, `g` → `-Og`/Rust `1`) |
| `lto` | `true` | link-time optimization. `false` clears `LTOFLAGS` and sets `!lto` |
| `ltoflags` | `-flto=auto -falign-functions=32` | used when `lto = true` |
| `cflags_base` | `-pipe -fno-plt -fexceptions -Wp,-D_FORTIFY_SOURCE=3 -fstack-clash-protection -fcf-protection -fno-semantic-interposition` | CFLAGS other than `-march`/`-O` |
| `ldflags` | `-Wl,-O1 -Wl,--sort-common -Wl,--as-needed -Wl,-z,relro -Wl,-z,now -Wl,-z,pack-relative-relocs` | LDFLAGS |
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
| `skip_pgp_on_import_failure` | `false` | if a source signing key can't be fetched, build with `--skippgpcheck` (hashes still verified; the build is flagged `pgp_skipped` in built.json). A bad signature or revoked key is always a hard failure |
| `failed_stall_retries` / `failed_stall_days` | `5` / `7` | mark a package stalled after this many failures, once the last failure is this many days old |
| `stall_auto_retry_days` | `3` | retry stalled packages after this long; `0` disables |

#### Housekeeping

| Key | Default | Meaning |
|---|---|---|
| `autoprune` · `autoprune_keep` | `true` · `1` | delete superseded package files, keeping N versions (raise it to allow rollback) |
| `autoprune_blacklisted` | `true` | remove newly blacklisted packages from the repo |
| `autoprune_uninstalled` | `true` | remove packages you no longer have installed |
| `autoprune_pkgbuild_clones` | `true` | remove cached PKGBUILD clones nothing needs |
| `poll_interval` | `300` | seconds between daemon cycles |
| `upstream_check_interval` | `3600` | seconds between checks for upstream PKGBUILD updates |
| `log_retention_days` | `7` | build log retention |
| `log_level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. `DEBUG` traces how each package's PKGBUILD is resolved |

#### Chroot and paths

| Key | Default | Meaning |
|---|---|---|
| `chroot_pacman_conf` | by `distro` | the build chroot's `pacman.conf`. `artix`: the bundled Artix + CachyOS config. `arch`: devtools' `extra.conf` |
| `chroot_extra_packages` | artix: `libelogind,libudev,elogind` | installed into the chroot on every upgrade |

Every path defaults to a location under `/var/lib/arch-native/`. The overridable
paths are `chroot_dir`, `chroot_root`, `repo_dir`, `repo_db`, `pkgbuilds_dir`,
`makepkg_configs_dir`, `manifest_path`, `gnupg_home`, `log_dir` and
`metrics_path`. `chroot_dir` is the parent and `chroot_root` the clean chroot
inside it; if you override `chroot_dir`, set `chroot_root` too.

---

## How it works

### The build cycle

In remote mode, the desktop's `pkglist-export` hook sends its installed-package
list to the build server after every pacman transaction. In local mode the
daemon reads the pacman database directly. From there, the daemon repeats this
cycle every `poll_interval` (5 minutes):

1. **Upgrade the build chroot**, the clean environment every build runs in.
2. **Queue what's new.** Packages that were installed or upgraded since the
   last cycle are queued, along with forge packages left linking a library
   version forge no longer ships (see [Safety checks](#safety-checks)).
3. **Check upstream, hourly.** In the background, every
   `upstream_check_interval`, it pulls the PKGBUILDs it has fetched before and
   queues packages your distro has released a newer version of.
4. **Build the queue**, one package at a time: fetch the PKGBUILD and apply your
   patch if there is one, check that the package is eligible, import its PGP
   keys, build it in a fresh copy of the chroot with your flags, sign it, and
   add it to the repo.
5. **Sleep** until the next cycle. `buildbot sync` wakes it early.

The whole queue drains before the daemon sleeps.

### PKGBUILD tiers

`repo_priority` lists the places PKGBUILDs are fetched from, in order. Tier
names are arbitrary; each one maps to a source type:

| Source | How it works | Kept up to date |
|---|---|---|
| `local` | your patches or full PKGBUILDs in `pkgbuilds/local/<pkg>/` | no, they're yours |
| `clone <url>` | one `git clone --depth=1` per package (`{pkgname}` substituted in the URL); checks the repo root and `trunk/` | yes, pulled hourly |
| `monorepo` | one repository holding every package, searched by name in `pkgbuilds/<tier>/` | yes, pulled hourly |
| `pkgctl` | Arch's packaging GitLab, one clone per package | yes, pulled hourly |

Three tiers work with no configuration: `artix` (clone from Artix's gitea),
`arch` (pkgctl) and `cachyos` (monorepo). A monorepo has to be cloned once by
hand; for `cachyos`:

```bash
sudo git clone --depth=1 https://github.com/CachyOS/CachyOS-PKGBUILDS /var/lib/arch-native/pkgbuilds/cachyos
```

To add a tier of your own, name it and give it a source:

```ini
repo_priority = local,myfork,arch
myfork_source = clone https://git.example.com/packages/{pkgname}.git
```

The hourly check only looks at packages that have been built at least once,
because their PKGBUILD has been fetched by then. To push a newly installed
package into the queue without waiting for the next cycle, run `buildbot sync`.
A git fetch gives up after 60 seconds without progress, so an unresponsive
server can't hold up the daemon.

### Safety checks

Rebuilding a whole system can go wrong in ways a single package build can't.

- **Never ahead of your distro.** Packaging git trees often carry versions your
  distro hasn't released yet (staging, testing). A build from there can require
  library versions your system doesn't have, which fails the whole
  `native-sync` transaction. forge only builds a version once the build chroot's
  repos carry it; until then the package waits as `pending_release`.
- **Never breaking distro packages.** A rebuild can change a library's soname
  without any version change: `abseil-cpp 20260817.0-1` → `-2` moved every
  `libabsl` from `2605` to `2608`. If a forge build introduces a soname the
  distro repos haven't migrated to, publishing it would strand the distro
  packages that link the old one, so it's held back as `pending_world_cascade`
  until they catch up.
- **Never stranding forge's own packages.** The reverse case. Each cycle the
  daemon reads the real ELF `SONAME` and `DT_NEEDED` entries of the repo's
  packages (cached in `sonames.json`). Package metadata can't be trusted for
  this: soname `provides`/`depends` are optional and usually missing for exactly
  the libraries that drift. A forge package still linking a soname forge no
  longer ships is rebuilt. One that wants a *newer* soname than forge provides
  is only reported, since the library itself has to be fixed first.
  `buildbot doctor` shows the current state.
- **Never publishing a truncated build.** A package containing empty shared
  libraries or executables, the mark of an interrupted strip or link, is
  rejected.
- **Never skipping a bad signature.** `skip_pgp_on_import_failure` only covers
  keys that can't be fetched. A bad signature or a revoked key always fails the
  build.

### Build host / target CPU mismatch

If the build server can't run binaries built for the target CPU, the daemon
disables test suites (`!check`) and drops `target-cpu` from `RUSTFLAGS`, because
both would run target code on the build host and crash with SIGILL. C/C++ still
gets the full `-march`; Rust is limited to `-C opt-level`. Local mode has
neither limitation.

### Retries

- **Link failures** (`ld returned`, Rust LTO errors) are retried once with LTO
  off, logged as `<timestamp>-nolto.log`. Put repeat offenders in
  `lto_blacklist` to skip the failing first attempt.
- **Download failures** (HTTP 429, TLS errors, connection resets) are re-queued
  up to `download_retry_limit` times, then recorded as `download failed after N
  attempts`.
- **A missing dependency or source file in one tier** sends the next attempt to
  the next tier.
- **Every other failure** waits out a backoff that grows with each attempt,
  starting at 1 hour for download and timeout errors and 6 hours for compile
  errors.
  After `failed_stall_retries` failures the package is **stalled**: it stops
  retrying until `stall_auto_retry_days` have passed, or you run
  `buildbot retry`.

### How native-sync recognizes forge builds

forge builds keep upstream's `pkgver-pkgrel`. `native-sync` tells them apart by
the `PACKAGER` field (`Buildbot <buildbot@forge>`), with a dotted-pkgrel
fallback for older builds, and reinstalls any same-version package that forge
didn't build. Before installing, it clears that package from pacman's cache,
because a cached distro build of the same version would fail checksum
verification.

### Concurrency

Every change to `built.json`, `pending.json` and `failed.json` happens under one
lock (`queue.lock`), taken by the daemon and the CLI alike, so every command is
safe while the daemon runs. The running daemon holds `daemon.pid`; that's how
the CLI knows it's running on any init system, and why a second daemon refuses
to start.

### Files

```
/var/lib/arch-native/
├── built.json          {pkgname: {version, pkgrel, built_at, pkg_files, status?, reason?}}
├── pending.json        [{name, version, repo, build_reason, download_retries?}, ...]
├── failed.json         {pkgname: {version, reason, retries, error_type, timestamp}}
├── in_progress.json    the current build; re-queued at the front on restart
├── sonames.json        ELF soname cache
├── metrics.json        last-cycle stats (below)
├── daemon.pid          held by the running daemon
├── queue.lock          guards the three state files
├── manifests/client.json    package list from the desktop (remote mode)
├── chroots/
│   ├── root/           the clean chroot, upgraded every cycle
│   └── build-<uuid>/   per-build copy (a leftover one means an interrupted build)
├── gnupg/              signing key (0700)
├── logs/<pkg>/YYYYMMDD-HHMMSS.log   (+ -nolto.log on an LTO retry)
├── makepkg-configs/makepkg.<march>.conf
├── pkgbuilds/
│   ├── local/<pkg>/    <pkg>.patch and/or a full PKGBUILD; _patched/ work dir
│   └── <tier>/<pkg>/   per-package clones, or a monorepo tree
└── repo/
    ├── <repo_name>.db.tar.zst
    ├── *.pkg.tar.zst[.sig]
    ├── patch-status.json     patch health (read by native-sync)
    └── buildbot-public.asc
```

### metrics.json

Written atomically after each cycle, for scraping (Prometheus/Grafana):

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

`status` is `sleeping`, `processing` or `starting`. `cycle_seconds` and
`sleep_seconds` appear only when `sleeping`.

---

## Dependencies

Both packages declare these, so pacman installs them automatically.

**`arch-native`** (build host):

| Dependency | Used for |
|---|---|
| `python` | the `buildbot` daemon and CLI (pure Python) |
| `devtools` | `mkarchroot`, `arch-nspawn`, `makechrootpkg` |
| `pacman` | `pacman-key`, DB reads, `repo-add` |
| `gnupg` | package signing and PGP key import |
| `rsync` | receiving the package list from the desktop (remote mode) |
| `git` | cloning and pulling PKGBUILD repos |

**`arch-native-client`**: `python`, `rsync` and `pacman`.

---

## License

[GPL-3.0-or-later](LICENSE).
