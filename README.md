# arch-native

Rebuilds your installed Arch (or Artix) packages from source with CPU-optimized
compiler flags, signs them, and publishes them as a local pacman repo. Add that
repo above `[core]` in `pacman.conf` and your system runs binaries compiled
specifically for your CPU.

The build model is [ALHP](https://somegit.dev/ALHP/ALHP.GO) — same idea,
self-hosted and config-driven.

---

## Packages

| Package | Installs | Purpose |
|---|---|---|
| `arch-native` | `/usr/bin/buildbot` | Build daemon and CLI |
| `arch-native-client` | `/usr/bin/pkglist-export` | Desktop pacman hook (remote mode only) |

Build from source with `makepkg -si` from each directory.

---

## Modes

### local mode

The daemon runs on the same machine that uses the packages. It reads the local
pacman DB directly — no manifest file or rsync needed. Uses `march=native` and
`target-cpu=native` in RUSTFLAGS so Rust crates are also CPU-optimized.
Test suites run on the compiled binaries, so `check` is enabled.

Good for: a single-machine setup, or a machine fast enough to build everything
itself.

### remote mode

The daemon runs on a dedicated build server. A pacman hook on your desktop
(`arch-native-client`) syncs the installed package list to the build server via
rsync after each transaction. The server resolves, builds, signs, and publishes
packages for the desktop to pull.

Good for: when your desktop CPU is newer than the build server's, so binaries
compiled for it would SIGILL on the build server (e.g. targeting `pantherlake`
on a server that only has `znver3`); or when you simply want builds offloaded to
a separate machine.

---

## Installation

### 1. Build and install the server package

```bash
cd arch-native && makepkg -si
```

The package installs:
- `/usr/bin/buildbot` — daemon and CLI
- `/usr/lib/arch-native/buildbot_lib.py` — core library
- `/usr/lib/systemd/system/arch-native.service`
- `/usr/share/arch-native/` — nginx example, artix-meson, chroot-pacman.conf
- `/etc/arch-native.conf` — default config (marked as `backup`, upgrades produce `.pacnew`)
- `/usr/lib/sysusers.d/arch-native.conf` — creates the `buildbot` system user (via pacman's sysusers hook)

### 2. Edit the config

```bash
$EDITOR /etc/arch-native.conf
```

Minimum changes for local mode (building on the same machine):

```ini
[arch-native]
repo_name = myrepo     # pacman DB name
march = native         # compiler target — "native" = let gcc decide
mode = local
distro = arch          # or "artix" for Artix Linux
```

For remote mode (dedicated build server cross-compiling for your desktop CPU):

```ini
[arch-native]
repo_name = myrepo
march = znver4         # explicit CPU target (e.g. znver4, skylake, pantherlake)
mode = remote
distro = arch
```

### 3. Serve the repo with nginx

```bash
cp /usr/share/arch-native/nginx.conf.example /etc/nginx/sites-available/arch-native
ln -s /etc/nginx/sites-available/arch-native /etc/nginx/sites-enabled/arch-native
# or: cp to /etc/nginx/conf.d/arch-native.conf if your nginx uses conf.d
sudo systemctl reload nginx
```

Default config serves the repo at `:8081/repo/`. Edit the port in
`nginx.conf.example` before copying if needed.

### 4. Initialize and start

```bash
sudo buildbot init
sudo systemctl enable --now arch-native
```

`buildbot init` creates the `/var/lib/arch-native/` directory layout,
calls `mkarchroot` to create the clean chroot, initializes the pacman keyring,
and sets up the GPG signing key. Safe to re-run — skips steps already done.

### 5. Add the repo to pacman.conf

Edit `/etc/pacman.conf` on the machine that will use the packages. Place the
repo **above** `[core]` so forge packages shadow official ones:

```ini
[myrepo]
SigLevel = Required
Server = http://your-build-host:8081/repo
```

For local mode the server is `localhost`. For remote mode use the build
server's hostname or IP.

Trust the signing key (key ID printed by `buildbot doctor`):

```bash
sudo pacman-key --recv-keys <KEY-ID>
sudo pacman-key --lsign-key <KEY-ID>
```

---

## Installing arch-native-client (remote mode only)

On the desktop machine that the build server compiles for:

```bash
cd arch-native-client && makepkg -si
```

Configure the connection to your build server:

```bash
sudo cp /usr/share/arch-native-client/arch-native-client.conf.example \
        /etc/arch-native-client.conf
sudo $EDITOR /etc/arch-native-client.conf
```

`/etc/arch-native-client.conf`:

```bash
# SSH user@host of the build server
REMOTE_HOST="user@build-server"

# Where the manifest is written on the build server
REMOTE_PATH="/var/lib/arch-native/manifests/client.json"

# SSH key (optional — omit to use ssh-agent or default key)
SSH_KEY="/home/user/.ssh/id_ed25519"
```

The pacman hook fires automatically after every transaction. Test it manually:

```bash
sudo pkglist-export
```

---

## Configuration reference

All settings belong in the `[arch-native]` section of `/etc/arch-native.conf`.

### Core

```ini
# Name of the pacman repo — used for the DB filename and PACKAGER field.
# The DB will be at <repo_dir>/<repo_name>.db.tar.zst
repo_name = forge

# Compiler target CPU.
# "native" — let the compiler decide based on the build host (correct for local mode)
# Explicit value required for remote cross-builds: "znver4", "skylake", "pantherlake", etc.
# Run: gcc -march=native -Q --help=target | grep march
march = native

# "local" or "remote"  (see Modes above)
mode = local

# "arch" or "artix"
# artix: installs libelogind/elogind/libudev into the chroot each cycle;
#        deploys artix-meson wrapper so meson-based packages find it.
# arch:  standard clean Arch chroot, no extra packages.
distro = arch
```

### PKGBUILD resolution

```ini
# Order in which PKGBUILD sources are checked (left = higher priority).
# Supported tiers: local, artix, cachyos, arch
# Default: local,artix,cachyos,arch
repo_priority = local,artix,cachyos,arch

# Arch (non-Artix) users who don't want the artix/cachyos tiers:
# repo_priority = local,arch
```

### Blacklists

```ini
# Packages never built — toolchain-critical or unfixably broken PKGBUILDs.
blacklist = gcc,glibc,coreutils,linux-api-headers,binutils

# Packages built without LTO (link-time optimization causes failures).
lto_blacklist = llvm,rust
```

### Build behavior

```ini
# Per-package build timeout in seconds.
# Large packages (Firefox, LLVM) legitimately take 2–3 hours.
# Set to 0 to disable. Default: 14400 (4 hours).
build_timeout = 14400

# Re-queue transient download failures (rate limits, SSL errors, connection
# resets) this many times before permanently failing. Default: 3.
download_retry_limit = 3

# When all keyserver PGP key imports fail, retry the build with
# --skippgpcheck. Source hashes (sha256/sha512) are still verified.
# Packages built this way are flagged pgp_skipped:true in built.json.
skip_pgp_on_import_failure = true
```

### Timing

```ini
# Main loop poll interval in seconds. Default: 300.
poll_interval = 300

# How often to git pull upstream PKGBUILD repos. Default: 3600.
upstream_check_interval = 3600

# Build log retention in days. Default: 7.
log_retention_days = 7
```

### Artix / chroot

```ini
# Path to pacman.conf deployed into the chroot at startup.
# Artix users must provide their own — it differs from the standard Arch one.
# Default: looks for /usr/share/arch-native/chroot-pacman.conf
# chroot_pacman_conf = /etc/arch-native/chroot-pacman.conf

# Additional packages installed into the chroot after each upgrade.
# Defaults to "libelogind,libudev,elogind" when distro=artix; empty for arch.
# chroot_extra_packages = libelogind,libudev,elogind
```

### Paths (optional overrides)

All paths default to `/var/lib/arch-native/<subdir>`. Override only if you need
a non-standard layout:

```ini
# chroot_dir         = /var/lib/arch-native/chroots
# repo_dir           = /var/lib/arch-native/repo
# repo_db            = /var/lib/arch-native/repo/<repo_name>.db.tar.zst
# pkgbuilds_dir      = /var/lib/arch-native/pkgbuilds
# manifest_path      = /var/lib/arch-native/manifests/client.json
# gnupg_home         = /var/lib/arch-native/gnupg
# log_dir            = /var/lib/arch-native/logs
```

---

## `buildbot` CLI reference

The `buildbot` binary is both the daemon and the CLI. With no subcommand it
runs as daemon.

### Monitoring

```
buildbot status
```
```
service    active
building   firefox
rebuilt    901 / 1204 installed
pending    328
failed     35
blacklisted 66
not queued 58  (AUR / binary / split-pkg)
last run   16 built, 0 failed  (57s ago)
```

Fields:
- **rebuilt** — packages installed on the desktop that have been rebuilt by forge
- **pending** — queued and waiting
- **failed** — gave up; see `buildbot failed`
- **blacklisted** — in the config `blacklist`, never built
- **not queued** — AUR/binary packages, or split packages whose pkgbase is
  blacklisted (e.g. `libstdc++` comes from the blacklisted `gcc` pkgbase)
- **last run** — stats from the most recent poll cycle

Note: rebuilt + pending + failed + blacklisted + not-queued may exceed
installed because packages that have been rebuilt AND have a new version
pending are counted in both rebuilt and pending.

```
buildbot doctor
```
Checks: paths exist, JSON files are valid, gnupg home has correct permissions
(0700), chroot keyring is initialized. Run this after `buildbot init` to
confirm setup.

```
buildbot built [-n N]
```
Lists successfully built packages. `-n N` limits to the N most recent.
Packages with dot-bumped pkgrel (`3.4.1-1.1`) are marked with `*`.

```
buildbot queue [-n N]
```
Lists the pending build queue (default: first 25 entries).

```
buildbot failed [-n N]
```
Lists failed builds with failure reason and retry count.

```
buildbot logs PKG [-f]
```
Prints the latest build log for PKG. `-f` follows the log in real time
(equivalent to `tail -f`), useful while a build is running.

### Queue management

These commands require the service to be stopped first:

```bash
sudo systemctl stop arch-native

# Move one failed package back to the queue
sudo buildbot retry firefox

# Move all failed packages back (skips ones no longer in the manifest)
sudo buildbot retry --all

# Remove a package from the failed list without retrying
sudo buildbot clear firefox

# Recompute the pending queue from the current installed package list.
# Use --reset to clear existing queue first (removes stale entries).
# Use --dry-run to preview what would be added without writing anything.
sudo buildbot sync --reset

sudo systemctl start arch-native
```

### Setup

```
buildbot init
```
Bootstraps a new installation. Run once after installing the package.
Creates the directory layout under `/var/lib/arch-native/`, calls
`mkarchroot` to create the base chroot, initializes the pacman keyring,
sets up the GPG signing home. Safe to re-run.

---

## Local PKGBUILD patches

Per-package fixes live in `/var/lib/arch-native/pkgbuilds/local/<pkg>/<pkg>.patch`
as unified diffs applied on top of the fetched upstream PKGBUILD.

### Create a patch

```bash
sudo buildbot patch create networkmanager
```

This resolves the upstream PKGBUILD for `networkmanager` (according to your
configured `repo_priority`), opens a copy in `$EDITOR`, and saves the diff
as `networkmanager.patch` when you exit. Example session:

```
  upstream tier: artix  (/var/lib/arch-native/pkgbuilds/artix/networkmanager)
  opening vim ...
  saved: /var/lib/arch-native/pkgbuilds/local/networkmanager/networkmanager.patch
```

### View a patch

```bash
sudo buildbot patch show networkmanager
```

### Verify patches after upstream updates

```bash
sudo buildbot patch check --all
```
```
  elogind                      ok  (tier: artix)
  networkmanager               ok  (tier: artix)
  zip                          ok  (tier: artix)
```

If a patch no longer applies, the build for that package fails loudly with:
```
[networkmanager] local patch no longer applies cleanly — upstream PKGBUILD
may have changed. Review and update the patch:
  /var/lib/arch-native/pkgbuilds/local/networkmanager/networkmanager.patch
```

Update the patch with `--force`:

```bash
sudo buildbot patch create --force networkmanager
```

This opens the current upstream with your old changes applied (if they still
partially apply) or a clean upstream copy to re-apply your fix against.

---

## Architecture

### Data flow (remote mode)

```
Desktop — after each pacman transaction:
  pkglist-export.hook fires
  → pkglist-export reads: pacman -Qi + pacman -Sl
  → writes JSON manifest to /tmp, rsyncs to build server
  → build-server:/var/lib/arch-native/manifests/client.json

Build server — main loop (every 300s):
  1. Upgrade clean chroot        arch-nspawn -Syu
  2. Detect manifest change      diff_manifest() → queue new/updated packages
  3. Check upstream updates      git pull on cached repos, vercmp
  4. Process pending queue:
       resolve_pkgbuild()        check local/ patch, then artix/cachyos/arch
       parse_srcinfo()           extract version, deps, pgp keys
       is_eligible()             skip AUR, blacklisted, already built
       import_pgp_keys()         fetch from keyservers
       bump_pkgrel()             x → x.1 (ALHP-style)
       makechrootpkg             build in ephemeral chroot copy
       sign_packages()           GPG detach-sign
       repo-add                  add to pacman DB
  5. Sleep remainder of 300s
```

### PKGBUILD tier resolution

Priority is set by `repo_priority` (default: `local,artix,cachyos,arch`). First match wins.

| Tier | How it works |
|---|---|
| `local` | Hand-maintained patches in `pkgbuilds/local/<pkg>/<pkg>.patch`. Applied on top of the upstream PKGBUILD at build time. |
| `artix` | `git clone --depth=1` from `gitea.artixlinux.org/packages/<pkg>.git` on demand. Cached after first fetch. |
| `cachyos` | Walks a locally cloned CachyOS PKGBUILDs monorepo. **Must be cloned manually** (see below). Updated each upstream check cycle via `git pull`. |
| `arch` | `pkgctl repo clone --protocol=https <pkg>` (from `devtools`) on demand. Cached after first fetch. |

The `artix` and `arch` tiers clone on demand — no setup needed. The `cachyos` tier
requires a one-time manual clone before it can find anything:

```bash
sudo git clone --depth=1 https://github.com/CachyOS/CachyOS-PKGBUILDS \
    /var/lib/arch-native/pkgbuilds/cachyos
```

If you don't use CachyOS, remove `cachyos` from `repo_priority` in the config.

For split packages (e.g. `gcc-libs`): if a pkgname isn't found directly, the
daemon looks up its pkgbase in the pacman sync DB and retries with that. After
a successful build, all subpackages from `.SRCINFO` are recorded in `built.json`
and removed from the pending queue.

### pkgrel dot-notation

Upstream `pkgrel=2` → forge rebuilds as `pkgrel=2.1`. pacman sees `2.1 > 2`
so forge packages take priority over official repos. When upstream bumps to
`pkgrel=3`, forge rebuilds as `3.1`.

Version comparisons inside the daemon (detecting already-built packages,
checking upstream updates) strip the local `.N` suffix before comparing:
`3.4.1-2.1` → `3.4.1-2` for comparison purposes.

### LTO auto-retry

If a build fails with a linker error (`collect2: error: ld returned`) or Rust
LTO incompatibility, the build is automatically retried once with LTO disabled
(`LTOFLAGS=""` and `!lto`). The retry log is saved as `<timestamp>-nolto.log`.

Add packages to `lto_blacklist` in config to always skip LTO without retrying.

### Build host / target CPU mismatch (remote mode)

When the build server cannot execute binaries compiled for the target CPU:

- `!check` in `BUILDENV` (default for remote mode) — disables test suites,
  which run compiled binaries on the build host and would SIGILL
- Do **not** set `target-cpu=<march>` in `RUSTFLAGS` — Cargo compiles
  `build.rs` scripts and immediately runs them on the build host; if compiled
  for the wrong CPU they SIGILL. C/C++ still gets full `-march=<target>` via
  `CFLAGS`; only Rust is limited to `-C opt-level=3`

If you build on the target CPU (`mode = local`, `march = native`), both
limitations go away and test suites are enabled.

### Download failure retry

Transient failures (HTTP 429 rate limits, SSL cert errors, connection resets)
are detected by log pattern matching. Instead of failing immediately, the
package is re-queued up to `download_retry_limit` times (default: 3). After
that it moves to `failed.json` with a clear reason like
`"download failed after 3 attempts"`.

---

## Data layout

```
/var/lib/arch-native/
├── built.json          {pkgname: {version, pkgrel, built_at, pkg_files, pgp_skipped?}}
├── pending.json        [{name, version, repo, build_reason, download_retries?}, ...]
├── failed.json         {pkgname: {version, reason, retries, timestamp}}
├── metrics.json        last-cycle stats (for external monitoring)
├── in_progress.json    currently building; re-inserted at front on daemon restart
│
├── manifests/
│   └── client.json     package list from desktop (remote mode)
│
├── chroots/
│   ├── root/           clean chroot base — upgraded each poll cycle
│   └── build-<uuid>/   ephemeral per-build copy — deleted after build
│                       stale build-* dirs indicate an interrupted build
├── gnupg/              signing key (mode 0700)
│
├── logs/
│   └── <pkgname>/
│       ├── YYYYMMDD-HHMMSS.log
│       └── YYYYMMDD-HHMMSS-nolto.log   LTO-retry log
│
├── makepkg-configs/
│   └── makepkg.<march>.conf    generated at startup from config values
│
├── pkgbuilds/
│   ├── local/          patches and overrides (tier 1)
│   │   └── <pkg>/
│   │       ├── <pkg>.patch     preferred: diff -u against upstream
│   │       └── _patched/       working dir (auto-generated, do not edit)
│   ├── artix/          git clones from gitea.artixlinux.org (tier 2)
│   ├── cachyos/        CachyOS PKGBUILDs repo (tier 3)
│   └── arch/           pkgctl clones from Arch (tier 4)
│
└── repo/
    ├── <repo_name>.db.tar.zst
    ├── *.pkg.tar.zst
    ├── *.pkg.tar.zst.sig
    └── buildbot-public.asc
```
