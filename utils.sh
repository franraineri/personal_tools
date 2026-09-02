#!/bin/zsh
#
# utils.sh — Shared helpers for the my_tools git/hotfix scripts.
#
# Source it from another script (works from bash and zsh):
#   source "$(dirname "$0")/utils.sh"        # bash
#   source "${0:A:h}/utils.sh"               # zsh
#
# Provides (portable — bash & zsh):
#   • Colored logging:  log_step / log_info / log_ok / log_warn / log_error / die
#   • Error handling:   utils_enable_error_trap   (traps unexpected failures,
#                       logs the failing command + file:line, then exits)
#                       run_checked "<label>" <cmd...>  (run a command, log a clear
#                       error if it fails)
#   • Connectivity:     check_vpn <host>
#
# Provides (zsh only — guarded; used by hotfix-cherry-pick.sh):
#   • enable_rerere <repo_dir>
#   • cherrypick_preflight <repo_dir> <upstream_remote> <release_branch> <sha...>
#   • autoresolve_known_conflicts <repo_dir>   → 0 fully resolved, 1 real conflicts remain
#
# All functions are safe to source multiple times (guarded).
#

# Guard against double-sourcing.
[[ -n "${_MY_TOOLS_UTILS_LOADED:-}" ]] && return 0
_MY_TOOLS_UTILS_LOADED=1

# Detect the running shell so shell-specific features stay guarded.
if [[ -n "${ZSH_VERSION:-}" ]]; then
    _UTILS_SHELL="zsh"
elif [[ -n "${BASH_VERSION:-}" ]]; then
    _UTILS_SHELL="bash"
else
    _UTILS_SHELL="sh"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Colors (printf so they render in any shell). Only define if not already set,
# so a sourcing script can override them beforehand.
# ─────────────────────────────────────────────────────────────────────────────
: ${RED:=$(printf '\033[0;31m')}
: ${GREEN:=$(printf '\033[0;32m')}
: ${YELLOW:=$(printf '\033[0;33m')}
: ${CYAN:=$(printf '\033[0;36m')}
: ${DIM:=$(printf '\033[2m')}
: ${BOLD:=$(printf '\033[1m')}
: ${RESET:=$(printf '\033[0m')}

# ─────────────────────────────────────────────────────────────────────────────
# Logging helpers
# ─────────────────────────────────────────────────────────────────────────────
log_step()  { echo "${BOLD}▶ $*${RESET}"; }
log_info()  { echo "${DIM}$*${RESET}"; }
log_ok()    { echo "${GREEN}${BOLD}✓ $*${RESET}"; }
log_warn()  { echo "${YELLOW}${BOLD}! $*${RESET}"; }
log_error() { echo "${RED}${BOLD}✗ $*${RESET}" >&2; }
die()       { log_error "$*"; exit 1; }

# ─────────────────────────────────────────────────────────────────────────────
# Error handling
# ─────────────────────────────────────────────────────────────────────────────
#
# _utils_on_error — internal ERR trap handler. Reports the failing command and
# its location (file:line) with the same red styling as log_error, then exits
# with the original status. Sourcing scripts opt in via utils_enable_error_trap.
_utils_on_error() {
    local exit_code=$1 cmd=$2 line=$3 src=$4
    # Fire only once: the ERR trap can be reached more than once as the error
    # propagates through functions under errexit.
    [[ -n "${_UTILS_ERR_REPORTED:-}" ]] && exit "$exit_code"
    _UTILS_ERR_REPORTED=1
    log_error "Unexpected failure (exit ${exit_code})"
    [[ -n "$cmd" && "$cmd" != "exit "* ]] && echo "${RED}    command: ${cmd}${RESET}" >&2
    [[ -n "$src"  ]] && echo "${RED}    at:      ${src}:${line}${RESET}" >&2
    log_info "If this looks like a bug in the tooling, check the step above."
    exit "$exit_code"
}

# utils_enable_error_trap — turn on strict error trapping for the CALLING script.
# Call once near the top of a script (after `set -e`). Any command that fails
# without being explicitly handled (|| true, if, &&, etc.) prints a clear,
# located error message instead of dying silently.
utils_enable_error_trap() {
    set -o errexit          # exit on error
    set -o pipefail         # a failing stage in a pipe fails the pipe
    if [[ "$_UTILS_SHELL" == "bash" ]]; then
        set -o errtrace     # ERR trap inherited by functions/subshells
        trap '_utils_on_error "$?" "$BASH_COMMAND" "$LINENO" "${BASH_SOURCE[0]}"' ERR
    elif [[ "$_UTILS_SHELL" == "zsh" ]]; then
        # zsh: $ZSH_EVAL_CONTEXT/$funcfiletrace give location; $ZSH_DEBUG_CMD the command.
        setopt ERR_EXIT PIPE_FAIL 2>/dev/null
        trap '_utils_on_error "$?" "${ZSH_DEBUG_CMD:-}" "${LINENO}" "${(%):-%x}"' ERR
    fi
}

# run_checked "<label>" <command...> — run a command; on failure log a clear,
# labeled error and return the command's exit code (does NOT exit, so the caller
# can decide). Use for the many git/gh/npm calls where a bare failure would be
# cryptic. stderr from the command is preserved.
run_checked() {
    local label=$1; shift
    if [[ $# -eq 0 ]]; then
        log_error "run_checked: no command given for '${label}'."
        return 2
    fi
    if "$@"; then
        return 0
    fi
    local rc=$?
    log_error "${label} failed (exit ${rc})."
    log_info  "    command: $*"
    return "$rc"
}

# ─────────────────────────────────────────────────────────────────────────────
# Connectivity / VPN
#   check_vpn <host>
# Resolves <host> via `dig +short`. nslookup/host return 0 on macOS even when a
# name doesn't resolve, so we rely on dig returning a non-empty answer instead.
# Exits 1 (via die-style messaging) when the host cannot be resolved.
# ─────────────────────────────────────────────────────────────────────────────
check_vpn() {
    local host="$1"
    [[ -n "$host" ]] || die "check_vpn: missing host argument."
    log_step "Checking connectivity to ${host}"
    if [[ -z "$(dig +short "$host" 2>/dev/null)" ]]; then
        log_error "Cannot resolve ${host}."
        log_warn  "You appear to be OFFLINE or NOT connected to the VPN."
        log_info  "Connect to the corporate VPN and re-run."
        exit 1
    fi
    log_ok "Connected — ${host} is reachable."
}

# ─────────────────────────────────────────────────────────────────────────────
# git rerere — reuse recorded conflict resolutions.
#   enable_rerere <repo_dir>
# Turns on rerere locally so a resolution done once is auto-applied if the same
# conflict reappears (e.g. re-runs, or porting to multiple release branches).
# ─────────────────────────────────────────────────────────────────────────────
enable_rerere() {
    local repo_dir="$1"
    if [[ "$(git -C "$repo_dir" config --get rerere.enabled 2>/dev/null)" != "true" ]]; then
        git -C "$repo_dir" config rerere.enabled true
        log_info "Enabled git rerere (records/reuses conflict resolutions)."
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Files whose cherry-pick conflicts are "expected" on a release branch and safe
# to auto-resolve by taking the INCOMING side (the commit being cherry-picked):
# library-version drift in manifests, and additive CHANGELOG entries.
# ─────────────────────────────────────────────────────────────────────────────
KNOWN_AUTORESOLVE_FILES=(package.json package-lock.json CHANGELOG.md)

# _is_known_autoresolve <basename> → 0 if in the known list, else 1
_is_known_autoresolve() {
    local base="$1" f
    for f in "${KNOWN_AUTORESOLVE_FILES[@]}"; do
        [[ "$base" == "$f" ]] && return 0
    done
    return 1
}

# ─────────────────────────────────────────────────────────────────────────────
# cherrypick_preflight <repo_dir> <upstream_remote> <release_branch> <sha...>
#
# Dry-run the cherry-picks WITHOUT touching the working branch: it creates a
# throwaway detached worktree at upstream/<release>, replays each SHA there with
# `cherry-pick --no-commit` (auto -m 1 for merges), and reports which SHAs and
# which files would conflict. Distinguishes "known/auto-resolvable" files from
# "real code" conflicts. Always cleans up the worktree.
#
# Return: 0 = clean or only known-file conflicts; 1 = real code conflicts found.
# Prints a human-readable report. Never mutates the caller's branch/index.
# ─────────────────────────────────────────────────────────────────────────────
cherrypick_preflight() {
    if [[ "$_UTILS_SHELL" != "zsh" ]]; then
        log_error "cherrypick_preflight requires zsh (uses zsh-specific splitting)."
        return 2
    fi
    emulate -L zsh
    set +x +v 2>/dev/null
    unsetopt xtrace verbose localtraps 2>/dev/null
    local repo_dir="$1" upstream="$2" release="$3"; shift 3
    local shas=("$@")
    [[ ${#shas[@]} -gt 0 ]] || { log_warn "Preflight: no SHAs to check."; return 0; }

    local ref="${upstream}/${release}"
    # Refresh the release ref first so the simulation runs against the CURRENT
    # release tip — a stale ref produces misleading "EMPTY"/conflict verdicts.
    # Best-effort: offline is fine as long as the ref already exists locally.
    git -C "$repo_dir" fetch "$upstream" "$release" >/dev/null 2>&1 || true
    git -C "$repo_dir" rev-parse --verify --quiet "$ref" >/dev/null \
        || die "Preflight: ${ref} not found. Fetch first."

    log_step "Pre-flight: simulating ${#shas[@]} cherry-pick(s) on a throwaway worktree"

    # Unique temp worktree path.
    local wt
    wt=$(mktemp -d "${TMPDIR:-/tmp}/cp-preflight.XXXXXX") || die "Preflight: mktemp failed."
    # Remove any dir mktemp made so `git worktree add` can create it fresh.
    rmdir "$wt" 2>/dev/null

    if ! git -C "$repo_dir" worktree add -d "$wt" "$ref" >/dev/null 2>&1; then
        die "Preflight: could not create worktree at ${ref}."
    fi

    # Run git in the simulation worktree with hooks DISABLED: this is a dry-run,
    # so repo hooks (post-merge/post-checkout, e.g. graphify auto-update) must not
    # fire — they add noise and side effects irrelevant to conflict detection.
    wt_git() { git -C "$wt" -c core.hooksPath=/dev/null "$@"; }

    local real_conflict=false known_conflict=false sha rc parents pick
    for sha in "${shas[@]}"; do
        parents=$(wt_git rev-list --parents -n1 "$sha" 2>/dev/null | wc -w)
        # Commit the pick on success so subsequent SHAs stack on top (mirrors the
        # real run). GIT_EDITOR=true accepts the default cherry-pick message.
        if [[ "$parents" -ge 3 ]]; then
            pick=(cherry-pick -m 1 "$sha")
        else
            pick=(cherry-pick "$sha")
        fi

        GIT_EDITOR=true wt_git "${pick[@]}" >/dev/null 2>&1
        rc=$?

        if [[ $rc -eq 0 ]]; then
            log_info "  ${sha:0:12} — clean"
            continue
        fi

        # Conflict (or empty). Inspect unmerged files.
        local conflicted
        conflicted=$(wt_git diff --name-only --diff-filter=U 2>/dev/null)
        if [[ -z "$conflicted" ]]; then
            # No unmerged files → likely an empty pick (already applied).
            log_warn "  ${sha:0:12} — EMPTY (change likely already on ${release})"
            wt_git cherry-pick --quit >/dev/null 2>&1
            continue
        fi

        local f sha_known=false sha_real=false
        for f in "${(f)conflicted}"; do
            [[ -n "$f" ]] || continue
            if _is_known_autoresolve "${f:t}"; then
                sha_known=true
            else
                sha_real=true
            fi
        done

        if [[ "$sha_real" == true ]]; then
            real_conflict=true
            log_warn "  ${sha:0:12} — REAL code conflict in:"
            for f in "${(f)conflicted}"; do
                [[ -n "$f" ]] || continue
                _is_known_autoresolve "${f:t}" || echo "${RED}      • ${f}${RESET}"
            done
        fi
        if [[ "$sha_known" == true ]]; then
            known_conflict=true
            log_info "  ${sha:0:12} — auto-resolvable conflict in known files (manifests/CHANGELOG)"
        fi

        # Abort this pick and hard-reset so the remaining SHAs still get simulated.
        wt_git cherry-pick --abort >/dev/null 2>&1 || true
    done

    # Cleanup worktree unconditionally.
    git -C "$repo_dir" worktree remove --force "$wt" >/dev/null 2>&1
    git -C "$repo_dir" worktree prune >/dev/null 2>&1

    if [[ "$real_conflict" == true ]]; then
        log_warn "Pre-flight found REAL code conflicts — these need manual resolution."
        return 1
    fi
    if [[ "$known_conflict" == true ]]; then
        log_ok "Pre-flight: only known manifest/CHANGELOG conflicts (auto-resolvable)."
    else
        log_ok "Pre-flight: no conflicts expected."
    fi
    return 0
}

# ─────────────────────────────────────────────────────────────────────────────
# autoresolve_known_conflicts <repo_dir> [side]
#
# Resolve ONLY the known manifest/CHANGELOG files (package.json, package-lock.json,
# CHANGELOG.md) and stage them; leave real code conflicts untouched.
#
#   side = "theirs" (default) → take the INCOMING side. Correct during a
#          cherry-pick: incoming = the commit being ported.
#   side = "ours"             → take the CURRENT side. Correct during a
#          `git stash pop` after a rebase: ours = the rebased working tree
#          (e.g. the newer develop version), dropping the stashed manifest.
#
# Return: 0 if the ONLY remaining conflicts were known files (now resolved);
#         1 if any other (real code) file is still in conflict.
# ─────────────────────────────────────────────────────────────────────────────
autoresolve_known_conflicts() {
    if [[ "$_UTILS_SHELL" != "zsh" ]]; then
        log_error "autoresolve_known_conflicts requires zsh."
        return 2
    fi
    local repo_dir="$1"
    local side="${2:-theirs}"
    case "$side" in
        ours|theirs) ;;
        *) log_error "autoresolve_known_conflicts: side must be 'ours' or 'theirs', got '${side}'."; return 2 ;;
    esac

    local conflicted
    conflicted=$(git -C "$repo_dir" diff --name-only --diff-filter=U 2>/dev/null)
    [[ -n "$conflicted" ]] || return 0

    local f real_remaining=false resolved_any=false
    for f in "${(f)conflicted}"; do
        [[ -n "$f" ]] || continue
        if _is_known_autoresolve "${f:t}"; then
            if git -C "$repo_dir" checkout "--${side}" -- "$f" >/dev/null 2>&1; then
                git -C "$repo_dir" add -- "$f" >/dev/null 2>&1
                resolved_any=true
                log_info "  auto-resolved (took ${side}): ${f}"
            else
                log_warn "  could not auto-resolve ${f} (checkout --${side} failed)"
                real_remaining=true
            fi
        else
            real_remaining=true
        fi
    done

    if [[ "$real_remaining" == true ]]; then
        [[ "$resolved_any" == true ]] && log_info "Resolved known files; real conflicts remain."
        return 1
    fi
    log_ok "All conflicts were known files and were auto-resolved (took ${side})."
    return 0
}
