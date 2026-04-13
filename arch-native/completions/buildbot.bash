# bash completion for buildbot(1)
# Source: /usr/share/bash-completion/completions/buildbot

_buildbot_failed_pkgs() {
    python3 -c \
        "import json; [print(k) for k in json.load(open('/var/lib/arch-native/failed.json')).keys()]" \
        2>/dev/null
}

_buildbot_built_pkgs() {
    python3 -c \
        "import json; [print(k) for k in json.load(open('/var/lib/arch-native/built.json')).keys()]" \
        2>/dev/null
}

_buildbot_local_patches() {
    command ls /var/lib/arch-native/pkgbuilds/local/ 2>/dev/null
}

_buildbot() {
    local cur prev words cword
    _init_completion || return

    # Find the first non-option word after the program name — the subcommand
    local cmd="" i
    for ((i=1; i<cword; i++)); do
        [[ "${words[i]}" != -* ]] && { cmd="${words[i]}"; break; }
    done

    # For 'patch', find the patch subcommand
    local patch_cmd="" j
    if [[ "$cmd" == "patch" ]]; then
        for ((j=i+1; j<cword; j++)); do
            [[ "${words[j]}" != -* ]] && { patch_cmd="${words[j]}"; break; }
        done
    fi

    case "$cmd" in
        "")
            if [[ "$prev" == "--config" ]]; then
                _filedir
            elif [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--config --debug" -- "$cur"))
            else
                COMPREPLY=($(compgen -W \
                    "status doctor built logs queue failed retry clear sync init patch" \
                    -- "$cur"))
            fi
            ;;
        status|doctor|init)
            ;;
        built|queue|failed)
            COMPREPLY=($(compgen -W "-n" -- "$cur"))
            ;;
        logs)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "-f --follow" -- "$cur"))
            else
                COMPREPLY=($(compgen -W "$(_buildbot_built_pkgs)" -- "$cur"))
            fi
            ;;
        retry|clear)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--all --dry-run" -- "$cur"))
            else
                COMPREPLY=($(compgen -W "$(_buildbot_failed_pkgs)" -- "$cur"))
            fi
            ;;
        sync)
            COMPREPLY=($(compgen -W "--reset --dry-run" -- "$cur"))
            ;;
        patch)
            case "$patch_cmd" in
                "")
                    COMPREPLY=($(compgen -W "create show check" -- "$cur"))
                    ;;
                create)
                    [[ "$cur" == -* ]] && \
                        COMPREPLY=($(compgen -W "--force" -- "$cur"))
                    ;;
                show)
                    COMPREPLY=($(compgen -W "$(_buildbot_local_patches)" -- "$cur"))
                    ;;
                check)
                    if [[ "$cur" == -* ]]; then
                        COMPREPLY=($(compgen -W "--all" -- "$cur"))
                    else
                        COMPREPLY=($(compgen -W "$(_buildbot_local_patches)" -- "$cur"))
                    fi
                    ;;
            esac
            ;;
    esac
}

complete -F _buildbot buildbot
