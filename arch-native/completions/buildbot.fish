# fish completion for buildbot(1)
# Source: /usr/share/fish/vendor_completions.d/buildbot.fish

# Disable file completion by default
complete -c buildbot -f

# Global flags
complete -c buildbot -l config  -d 'path to config file' -r -F
complete -c buildbot -l debug   -d 'verbose logging (daemon mode only)'

# ---------------------------------------------------------------------------
# Condition helpers
# ---------------------------------------------------------------------------
function __buildbot_no_subcommand
    not __fish_seen_subcommand_from \
        status doctor built logs queue failed retry clear sync init patch
end

function __buildbot_no_patch_subcmd
    __fish_seen_subcommand_from patch
    and not __fish_seen_subcommand_from create show check
end

# ---------------------------------------------------------------------------
# Dynamic completions
# ---------------------------------------------------------------------------
function __buildbot_built_pkgs
    python3 -c \
        "import json; [print(k) for k in json.load(open('/var/lib/arch-native/built.json')).keys()]" \
        2>/dev/null
end

function __buildbot_failed_pkgs
    python3 -c \
        "import json; [print(k) for k in json.load(open('/var/lib/arch-native/failed.json')).keys()]" \
        2>/dev/null
end

function __buildbot_local_patches
    command ls /var/lib/arch-native/pkgbuilds/local/ 2>/dev/null
end

# ---------------------------------------------------------------------------
# Top-level subcommands
# ---------------------------------------------------------------------------
complete -c buildbot -n __buildbot_no_subcommand -a status  -d 'service state and queue counts'
complete -c buildbot -n __buildbot_no_subcommand -a doctor  -d 'health check'
complete -c buildbot -n __buildbot_no_subcommand -a built   -d 'list rebuilt packages'
complete -c buildbot -n __buildbot_no_subcommand -a logs    -d 'show build log for a package'
complete -c buildbot -n __buildbot_no_subcommand -a queue   -d 'list pending build queue'
complete -c buildbot -n __buildbot_no_subcommand -a failed  -d 'list failed builds'
complete -c buildbot -n __buildbot_no_subcommand -a retry   -d 're-queue failed package(s)'
complete -c buildbot -n __buildbot_no_subcommand -a clear   -d 'drop package(s) from failed list'
complete -c buildbot -n __buildbot_no_subcommand -a sync    -d 'rebuild queue from manifest'
complete -c buildbot -n __buildbot_no_subcommand -a init    -d 'initialize a new installation'
complete -c buildbot -n __buildbot_no_subcommand -a patch   -d 'manage local PKGBUILD patches'

# ---------------------------------------------------------------------------
# built / queue / failed
# ---------------------------------------------------------------------------
complete -c buildbot -n '__fish_seen_subcommand_from built queue failed' \
    -s n -d 'max entries to show' -r

# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------
complete -c buildbot -n '__fish_seen_subcommand_from logs' \
    -s f -l follow -d 'follow log in real time'
complete -c buildbot -n '__fish_seen_subcommand_from logs' \
    -a '(__buildbot_built_pkgs)'

# ---------------------------------------------------------------------------
# retry / clear
# ---------------------------------------------------------------------------
complete -c buildbot -n '__fish_seen_subcommand_from retry clear' \
    -l all     -d 'apply to all packages'
complete -c buildbot -n '__fish_seen_subcommand_from retry clear' \
    -l dry-run -d 'preview without writing'
complete -c buildbot -n '__fish_seen_subcommand_from retry clear' \
    -a '(__buildbot_failed_pkgs)'

# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------
complete -c buildbot -n '__fish_seen_subcommand_from sync' \
    -l reset   -d 'clear queue first'
complete -c buildbot -n '__fish_seen_subcommand_from sync' \
    -l dry-run -d 'preview without writing'

# ---------------------------------------------------------------------------
# patch subcommands
# ---------------------------------------------------------------------------
complete -c buildbot -n __buildbot_no_patch_subcmd -a create -d 'create or update a local patch'
complete -c buildbot -n __buildbot_no_patch_subcmd -a show   -d 'print the local patch'
complete -c buildbot -n __buildbot_no_patch_subcmd -a check  -d 'verify patch still applies'

# patch create
complete -c buildbot \
    -n '__fish_seen_subcommand_from patch; and __fish_seen_subcommand_from create' \
    -l force -d 'overwrite existing patch with fresh upstream copy'

# patch show / check
complete -c buildbot \
    -n '__fish_seen_subcommand_from patch; and __fish_seen_subcommand_from show' \
    -a '(__buildbot_local_patches)'
complete -c buildbot \
    -n '__fish_seen_subcommand_from patch; and __fish_seen_subcommand_from check' \
    -l all -d 'check every local patch'
complete -c buildbot \
    -n '__fish_seen_subcommand_from patch; and __fish_seen_subcommand_from check' \
    -a '(__buildbot_local_patches)'
