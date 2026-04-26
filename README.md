# arch-native

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Rebuilds your installed packages from source with CPU-optimized compiler flags,
signs them, and publishes them as a local pacman repo. Add that repo above
`[core]` in `pacman.conf` and your system runs binaries compiled specifically
for your CPU.

Works on Arch Linux and Artix Linux (and any pacman-based distro).

The build model is [ALHP](https://somegit.dev/ALHP/ALHP.GO) — same idea,
self-hosted and config-driven.

---

## Packages

| Package | Installs | Purpose |
|---|---|---|
| `arch-native` | `/usr/bin/buildbot` | Build daemon and CLI |
| `arch-native-client` | `/usr/bin/pkglist-export` | Desktop pacman hook (remote mode only) |

Build from source with `makepkg -si` from each directory.

**Naming** — three names appear throughout this doc:
- **arch-native** — the project. What you install.
- **buildbot** — the binary (`/usr/bin/buildbot`). Both the daemon and the CLI.
- **forge** — an example `repo_name`. This is just the pacman repo name you choose in the config (`repo_name = forge`). It can be anything. Examples in this doc use `forge`; yours might be `local`, `custom`, or whatever you set.

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

## Prerequisites

The `arch-native` package pulls these in automatically via pacman depends, but
if you're inspecting the source or setting up manually:

| Dependency | Used for |
|---|---|
| `python` | `buildbot` daemon and CLI (pure Python) |
| `devtools` | `mkarchroot`, `arch-nspawn`, `makechrootpkg`, `pkgctl` |
| `pacman` | `pacman-key`, DB reads, `repo-add` |
| `gnupg` | Package signing and PGP key import |
| `rsync` | Manifest sync in remote mode (`arch-native-client` → server) |
| `git` | Cloning and pulling upstream PKGBUILD repos (artix, arch tiers) |

`arch-native-client` additionally requires `rsync` and `python` on the desktop
machine.

---

## Installation

### 1. Build and install the server package

```bash
cd arch-native && makepkg -si
```

The package installs:
- `/usr/bin/buildbot` — daemon and CLI
- `/usr/lib/arch-native/buildbot_lib.py` — core library
- `/usr/lib/systemd/system/arch-native.service` — systemd unit (see [Service management](#service-management) for other init systems)
- `/usr/share/arch-native/` — nginx example, artix-meson, chroot-pacman.conf
- `/etc/arch-native.conf` — default config (marked as `backup`, upgrades produce `.pacnew`)
- `/usr/lib/sysusers.d/arch-native.conf` — creates the `buildbot` system user

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

### 4. Initialize

```bash
sudo buildbot init
```

`buildbot init` creates the `/var/lib/arch-native/` directory layout,
calls `mkarchroot` to create the clean chroot, and initializes the pacman
keyring inside it. Safe to re-run — skips steps already done.

### 5. Generate the signing key

After init, generate a GPG key for the `buildbot` user that will sign all
built packages:

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
```

Export the public key into the repo directory so clients can fetch it:

```bash
sudo -u buildbot gpg --homedir /var/lib/arch-native/gnupg \
    --export --armor > /var/lib/arch-native/repo/buildbot-public.asc
```

Find the key fingerprint (used in the next step):

```bash
sudo -u buildbot gpg --homedir /var/lib/arch-native/gnupg -K
```

### 6. Start the service

See [Service management](#service-management) below for your init system.

### 7. Add the repo to pacman.conf

Edit `/etc/pacman.conf` on the machine that will use the packages. Place the
repo **after your distro repos but before `[extra]`**:

```ini
[system]          # or [core] on Arch
Include = ...

[world]           # or [extra] equivalent on your distro
Include = ...

[myrepo]
SigLevel = Required DatabaseOptional
Server = http://your-build-host:8081/repo

[extra]           # Arch [extra] after forge
Include = ...
```

Do **not** place forge first. The `.x` pkgrel bump (`pkgrel=2.1 > 2`) already
ensures forge packages shadow distro equivalents when they are current. Placing
forge first would hide upstream upgrades when the buildbot hasn't rebuilt a
package yet — you'd see no upgrade until forge catches up.

For local mode the server is `localhost`. For remote mode use the build
server's hostname or IP.

Trust the signing key on each client machine:

```bash
# Import from the repo server
sudo pacman-key --fetch-keys http://your-build-host:8081/repo/buildbot-public.asc
sudo pacman-key --lsign-key <KEY-FINGERPRINT>

# Or import directly if local mode
sudo pacman-key --add /var/lib/arch-native/repo/buildbot-public.asc
sudo pacman-key --lsign-key <KEY-FINGERPRINT>
```

---

## Service management

`buildbot` is a long-running daemon (`buildbot --config /etc/arch-native.conf`).
A systemd unit is included; examples for other init systems are below.

### systemd

```bash
sudo systemctl enable --now arch-native
sudo systemctl stop arch-native      # to pause for queue edits
sudo systemctl start arch-native
```

### dinit

Create `/etc/dinit.d/arch-native`:

```
type = process
command = /usr/bin/buildbot
options = --config /etc/arch-native.conf
logfile = /var/log/arch-native.log
restart = true
```

```bash
sudo dinitctl enable arch-native
sudo dinitctl start arch-native
```

### OpenRC

Create `/etc/init.d/arch-native`:

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
sudo rc-update add arch-native default
sudo rc-service arch-native start
```

### runit

Create `/etc/runit/sv/arch-native/run`:

```bash
#!/bin/sh
exec /usr/bin/buildbot --config /etc/arch-native.conf 2>&1
```

```bash
sudo chmod +x /etc/runit/sv/arch-native/run
sudo ln -s /etc/runit/sv/arch-native /run/runit/service/
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
# SSH_KEY="/path/to/id_ed25519"
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
# Tier names can be anything. Each non-local tier needs a <name>_source entry.
# Default: local,arch
repo_priority = local,arch

# Source definition for each tier:
#   clone <url>   — git clone per package on demand ({pkgname} substituted)
#   monorepo      — walk pkgbuilds/<tier>/ tree; git-pulled each upstream cycle
#   pkgctl        — use Arch devtools pkgctl (Arch Linux official repos only)
#
# Built-in defaults (no entry needed for these):
#   artix_source  = clone https://gitea.artixlinux.org/packages/{pkgname}.git
#   cachyos_source = monorepo
#   arch_source   = pkgctl
#
# Add your own:
# myfork_source = clone https://git.example.com/packages/{pkgname}.git
# mypkgs_source = monorepo
```

See [PKGBUILD tier resolution](#pkgbuild-tier-resolution) in the Architecture
section for a full explanation of each type.

### Blacklists

```ini
# Packages never built — toolchain-critical, pure-data, or unfixably broken.
blacklist = gcc,glibc,coreutils,linux-api-headers,binutils

# Packages built without LTO (link-time optimization causes failures).
lto_blacklist = llvm,rust
```

See [Building your blacklist](#building-your-blacklist) for guidance on what
to add.

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

# Auto-prune stale package files from the repo dir.
# When a package is rebuilt with a new version, repo-add updates the database
# but doesn't delete the old .pkg.tar.zst file. Auto-pruning removes orphans
# (and their .sig files) immediately after each successful build.
# Default: true.
autoprune = true

# How many recent versions to retain per package (must be >= 1). Default: 1.
# Set to 2+ if you want a rollback fallback in the repo dir.
autoprune_keep = 1

# Remove blacklisted packages from the repo db and repo dir each cycle.
# When a package is added to the blacklist, its built .pkg.tar.zst files and
# repo-db entry are removed on the next cycle so clients stop seeing it.
# Default: true.
autoprune_blacklisted = true

# Remove packages that are no longer installed on the client from the repo.
# When a package is uninstalled and drops out of the manifest, its built files
# and repo-db entry are removed on the next cycle.
# Default: true.
autoprune_uninstalled = true
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

The `buildbot` binary is both the daemon (no subcommand) and the CLI.

### Monitoring

```
buildbot status
```

```
arch-native  ● active

Building
  package    firefox
  elapsed    1h23m

Queue  52 pending
  new        8
  updates    44
  next       thunderbird

Recently built
  fish          3.7.1-2.1   2h ago
  curl          8.7.1-1.1   3h ago
  zstd          1.5.6-1.1   5h ago

Failed  3
  gpgme      3d ago    build failed: collect2: error: ld returned 1
  krb5       5d ago    download failed after 3 attempts
  +1 more — run: buildbot failed

Repo  forge
  rebuilt    987 / 1189  (83%)
  skipped    47 / 1189  (blacklist — see /etc/arch-native.conf)
  size       12G
  next cycle in 4m
```

**next cycle** is the countdown to the next poll cycle. Each cycle:

1. Upgrades the build chroot (`pacman -Syu` inside the clean chroot) so builds
   always use the latest official packages
2. Re-reads the installed package list (local mode: pacman DB; remote mode:
   checks for a new manifest from `pkglist-export`)
3. Every `upstream_check_interval` (default 1h): pulls cached PKGBUILD repos
   and compares versions against `built.json` — any package whose upstream
   PKGBUILD has a newer version than what was last built is re-queued
   automatically with `build_reason: update`
4. Drains the full pending queue — all queued packages are built before the
   daemon sleeps

This means official-repo package updates are picked up automatically once per
hour without any manual intervention. New packages you install are picked up
on the next cycle.

The **Building** section shows what is currently being compiled and how long it
has been running. When nothing is building it shows `idle`.

If the daemon stopped while a build was running, the status flags it:

```
Building
  status     stale — daemon not running
  package    firefox
  started    3d ago
```

If a build has exceeded `build_timeout`:

```
Building
  package    firefox
  elapsed    5h12m  ⚠ exceeded build_timeout (4h00m)
```

Both conditions mean the build is stuck and the daemon needs attention.

```
buildbot doctor
```
Checks: paths exist, JSON files are valid, gnupg home has correct permissions
(0700), chroot keyring is initialized.

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

These commands require the service to be stopped first (command depends on
your init system; see [Service management](#service-management)):

```bash
# Stop the daemon
sudo systemctl stop arch-native      # systemd
# sudo rc-service arch-native stop   # OpenRC
# sudo dinitctl stop arch-native     # dinit

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

# Restart the daemon
sudo systemctl start arch-native
```

### Setup

```
buildbot init
```
Bootstraps a new installation. Run once after installing the package.
Creates the directory layout under `/var/lib/arch-native/`, calls
`mkarchroot` to create the base chroot, initializes the pacman keyring.
Safe to re-run.

---

## Building your blacklist

The blacklist prevents packages from being rebuilt. Get it right early — a
misconfigured build of a toolchain package can make your system unbootable.

**Always blacklist:**
- Toolchain and core system packages: `gcc`, `glibc`, `binutils`,
  `coreutils`, `linux-api-headers`. Rebuilding these with custom `-march` can
  produce incompatible binaries and break the entire system.
- AUR and binary packages: they have no upstream PKGBUILD to pull. If you add
  AUR packages to your pacman config, blacklist them by name.
- Split packages whose pkgbase is blacklisted: the daemon handles most of this
  automatically (e.g. `gcc-libs` is skipped because `gcc` is its pkgbase and
  is blacklisted), but explicit entries are safer.

**Pure-data packages (no compiled code — no benefit from rebuilding):**
- Fonts and typefaces: `ttf-*`, `otf-*`, `font-*`
- Icon themes: `*-icon-theme`
- Cursor themes: `*-cursors`
- Firmware: `linux-firmware`, `linux-firmware-*`
- Translations and localizations: `*-translations`, `hunspell-*`,
  `tesseract-data-*`
- Keyrings: `*-keyring`
- Init scripts with no compiled components: `*-dinit`, `*-openrc`, `*-runit`

**Packages that frequently fail with custom flags:**
- `llvm` and `rust` are in `lto_blacklist` by default; building them with
  custom `-march` may also cause issues on mismatched host/target setups.
- Packages with bundled build systems that ignore `CFLAGS` (some Go, Java,
  or pure-script packages) yield no benefit and are candidates for the
  blacklist.

**Using glob patterns** — the blacklist supports `fnmatch` wildcards:

```ini
blacklist = gcc,glibc,binutils,coreutils,linux-api-headers,
            ttf-*,otf-*,font-*,*-icon-theme,*-cursors,
            linux-firmware,linux-firmware-*,*-keyring,
            *-translations,hunspell-*,tesseract-data-*
```

Run `buildbot status` after editing the blacklist to see the updated skipped
count. Use `buildbot queue -n 200` to review what is actually queued.

---

## Local PKGBUILD patches

Per-package fixes live in `/var/lib/arch-native/pkgbuilds/local/<pkg>/<pkg>.patch`
as unified diffs applied on top of the fetched upstream PKGBUILD.

### Create a patch

```bash
sudo buildbot patch create networkmanager
```

This resolves the upstream PKGBUILD for `networkmanager` (according to your
configured `repo_priority`), opens a clean copy in `$EDITOR`, and saves the
diff as `networkmanager.patch` when you exit. Example session:

```
  upstream tier: artix  (/var/lib/arch-native/pkgbuilds/artix/networkmanager)
  opening vim ...
  saved: /var/lib/arch-native/pkgbuilds/local/networkmanager/networkmanager.patch
```

### Common patch use-cases

**Disable a failing test:**
```diff
-  make check
+  # make check  # broken with -march=native: https://...
```

**Add a configure flag:**
```diff
-  ./configure --prefix=/usr
+  ./configure --prefix=/usr --disable-foo
```

**Fix a Makefile that ignores CFLAGS:**
```diff
-CFLAGS = -O2
+CFLAGS ?= -O2
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

Run `buildbot patch check --all` after each upstream update cycle
(`upstream_check_interval`) to catch drift early.

### Update a stale patch

```bash
sudo buildbot patch create --force networkmanager
```

This opens a clean copy of the **current upstream** PKGBUILD in `$EDITOR`.
Re-apply your changes from scratch against the new version and save; the old
patch is overwritten when you exit.

To see your old changes while editing:

```bash
sudo buildbot patch show networkmanager   # read the old diff in another terminal
```

---

## Architecture

### Data flow (remote mode)

```
Desktop — after each pacman transaction:
  pkglist-export.hook fires
  → pkglist-export reads: pacman -Qi + pacman -Sl
  → writes JSON manifest to /tmp, rsyncs to build server
  → build-server:/var/lib/arch-native/manifests/client.json

Build server — main loop (poll_interval = 300s):
  1. Upgrade clean chroot        arch-nspawn -Syu inside chroots/root/
  2. Detect package list change  local: read pacman DB; remote: watch manifest mtime
                                 diff against built.json → queue new/changed packages
  3. Upstream update check       every upstream_check_interval (default 1h):
                                   git pull arch/ clones (per-package)
                                   git pull cachyos/ monorepo
                                   vercmp each built package against PKGBUILD version
                                   re-queue anything with a newer upstream version
  4. Drain full pending queue    repeat until queue empty or shutdown:
       resolve_pkgbuild()          local patch → artix → cachyos → arch
       parse_srcinfo()             version, deps, pgp keys
       is_eligible()               skip blacklisted, already-built-at-this-version
       import_pgp_keys()           fetch from keyservers
       bump_pkgrel()               x → x.1 (ALHP-style dot bump)
       makechrootpkg               build in ephemeral chroot copy (chroots/build-<uuid>/)
       sign_packages()             GPG detach-sign each .pkg.tar.zst
       repo-add                    add to pacman DB; autoprune old versions
  5. Sleep remainder of poll_interval
```

### PKGBUILD tier resolution

Priority is set by `repo_priority`. Tier names can be anything — each maps to a
source type defined in config.

`tier_version_select` controls what happens when a package exists in more than one tier:

| Value | Behaviour |
|---|---|
| `priority` (default) | First tier in `repo_priority` that has a PKGBUILD wins. Safe for setups where higher-priority tiers carry distro-specific patches. |
| `highest` | All tiers are checked; the one with the highest `pkgver` wins. Useful when a lower-priority tier may lag behind upstream — e.g. an Artix clone tier that hasn't bumped a package yet while CachyOS or Arch already has the new version. |

On Artix with dinit, `highest` is generally safe: Artix-specific service units live in separate `-dinit` split packages, not in the base package PKGBUILD. The worst case when a non-Artix PKGBUILD is selected is a build failure (lands in `failed.json`) or a pacman install refusal — neither breaks the running system.

| Source type | How it works | Auto-updated? |
|---|---|---|
| `local` | Hand-maintained patches in `pkgbuilds/local/<pkg>/<pkg>.patch`, applied on top of the upstream PKGBUILD. Full `PKGBUILD` copies also work but go stale silently — prefer patches. | No — user-managed |
| `clone <url>` | `git clone --depth=1 <url>` on first use, cached in `pkgbuilds/<tier>/<pkg>/`. `{pkgname}` in the URL is substituted at clone time. Checks both the root and a `trunk/` subdirectory for the PKGBUILD. | Yes — per-package `git pull` each upstream check cycle (existing clones only; initial clone is created on first build) |
| `monorepo` | Walks `pkgbuilds/<tier>/`, matching subdirectories by pkgname. Clone the repo manually once; the daemon pulls it each `upstream_check_interval`. | Yes — whole-repo `git pull` each cycle |
| `pkgctl` | Direct `git clone --depth=1` from `gitlab.archlinux.org/archlinux/packaging/packages/<pkg>`. Requires `devtools` for builds. Cached in `pkgbuilds/<tier>/<pkg>/`. | Yes — per-package `git pull` each upstream check cycle (existing clones only; initial clone is created on first build) |

**Upstream check scope** — version detection during the hourly upstream check only works for packages that have already been built at least once (so the PKGBUILD clone exists on disk). A package installed *after* the last upstream check won't be detected until it gets built for the first time. If a package update was missed, run `buildbot sync` to re-scan and force it into the queue.

**Built-in tier defaults** — these names work with no config entry needed:

| Name | Default source |
|---|---|
| `artix` | `clone https://gitea.artixlinux.org/packages/{pkgname}.git` |
| `cachyos` | `monorepo` — clone manually to `pkgbuilds/cachyos/` |
| `arch` | `pkgctl` |

**Adding your own tiers** — name them anything, define the source, add to `repo_priority`:

```ini
# A personal gitea with per-package repos:
repo_priority = local,myfork,arch
myfork_source = clone https://git.example.com/packages/{pkgname}.git

# A local PKGBUILD monorepo:
repo_priority = local,mypkgs,arch
mypkgs_source = monorepo
# sudo git clone --depth=1 https://git.example.com/pkgbuilds /var/lib/arch-native/pkgbuilds/mypkgs

# Multiple custom tiers stacked with the built-ins:
repo_priority = local,myfork,artix,arch
myfork_source = clone https://git.example.com/packages/{pkgname}.git
```

To use CachyOS PKGBUILDs (or any compatible monorepo), clone it once and add it to `repo_priority`:

```bash
sudo git clone --depth=1 https://github.com/CachyOS/CachyOS-PKGBUILDS \
    /var/lib/arch-native/pkgbuilds/cachyos
```
```ini
repo_priority = local,cachyos,arch
```

### Split packages and pkgbase resolution

Many packages are split: a single `pkgbase` (one PKGBUILD) produces multiple
installable pkgnames. Examples:

| pkgbase | pkgnames produced |
|---|---|
| `gcc` | `gcc`, `gcc-libs`, `gcc-fortran`, `libgcc`, ... |
| `llvm` | `llvm`, `llvm-libs`, `clang`, `lld`, ... |
| `python` | `python`, `python-tests` |

When the daemon encounters a pkgname that isn't found directly, it looks up its
`pkgbase` in the pacman sync DB and retries with that name. After a successful
build, **all subpackages** listed in `.SRCINFO` are recorded in `built.json` and
removed from the pending queue at once.

This means you should **blacklist the pkgbase**, not individual subpackages:

```ini
# correct — blocks all subpackages
blacklist = gcc

# wrong — gcc-libs will still try to build via pkgbase=gcc
blacklist = gcc-libs
```

Any pkgname whose pkgbase is blacklisted is automatically skipped and counted
in the "skipped" total in `buildbot status`.

### AUR and binary packages

AUR packages have no upstream PKGBUILD in any of the four tiers. The daemon
detects them as unresolvable and marks them `not_found` in `failed.json`.
Binary packages (e.g. `*-bin`) have no source to compile.

Blacklist these to keep them off the failed list:

```ini
blacklist = ...,*-bin,*-git,*-svn
```

Or if you have a large AUR footprint, enumerate them explicitly.

### pkgrel dot-notation

Upstream `pkgrel=2` → arch-native rebuilds as `pkgrel=2.1`. pacman sees
`2.1 > 2` so locally-built packages take priority over official repos. When
upstream bumps to `pkgrel=3`, arch-native rebuilds as `3.1`.

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
│   ├── <tier>/         one directory per configured tier
│   │   └── <pkg>/      clone-type: per-package git clone
│   │   or flat tree    monorepo-type: walked by pkgname
│   └── ...             pkgctl-type: pkgctl clones (e.g. arch/)
│
└── repo/
    ├── <repo_name>.db.tar.zst
    ├── *.pkg.tar.zst
    ├── *.pkg.tar.zst.sig
    └── buildbot-public.asc
```

---

## License

[GNU General Public License v3.0 or later](LICENSE)

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.
