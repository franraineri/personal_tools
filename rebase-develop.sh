#!/bin/zsh
#
# rebase-develop.sh — Stash, update develop from upstream, rebase current branch.
#
# Usage:
#   ./scripts/rebase-develop.sh          Rebase the current repo (default behavior).
#   ./scripts/rebase-develop.sh --full   Rebase dcl-ui-global-components-library-v2,
#                                         then dcl-cruise-101-spa, in that order.
#

#----

# This script uses zsh-specific features (safe_stash_pop uses ${file:t}, etc.).
# Refuse to run under bash/sh with a clear message instead of doing a partial
# rebase and failing halfway.
if [[ -z "${ZSH_VERSION:-}" ]]; then
    echo "This script must be run with zsh (not bash/sh)." >&2
    echo "  Run:  ./rebase-develop.sh [--full]   or   zsh rebase-develop.sh [--full]" >&2
    exit 1
fi

# Resolve this script's directory (zsh).
SCRIPT_DIR="${${(%):-%x}:A:h}"

# Shared helpers (colors, logging, error trap). See utils.sh.
source "${SCRIPT_DIR}/utils.sh" || { echo "Missing utils.sh in '${SCRIPT_DIR}'" >&2; exit 1; }
utils_enable_error_trap

# ─────────────────────────────────────────────────────────────────────────────
# Config — precedence: env var > built-in default. A Koda executor (or
# `koda project`) can export these so the flow runs against any repo layout,
# base branch, or remote without editing the script:
#   REBASE_BASE_BRANCH     → BASE_BRANCH      (default: develop)
#   REBASE_UPSTREAM_REMOTE → UPSTREAM_REMOTE  (default: upstream)
#   REBASE_REPOS_BASE      → REPOS_BASE       (--full: parent dir of the repos)
#   REBASE_LIB_REPO        → LIB_REPO         (--full: library repo dir)
#   REBASE_SPA_REPO        → SPA_REPO         (--full: SPA repo dir)
# --full defaults keep the DCL layout (library + SPA as siblings under REPOS_BASE)
# but any of the three paths can be overridden independently.
# ─────────────────────────────────────────────────────────────────────────────
BASE_BRANCH="${REBASE_BASE_BRANCH:-develop}"
UPSTREAM_REMOTE="${REBASE_UPSTREAM_REMOTE:-upstream}"
REPOS_BASE="${REBASE_REPOS_BASE:-/Users/franco.raineri/devTools/DCL/Silent}"
LIB_REPO="${REBASE_LIB_REPO:-${REPOS_BASE}/dcl-ui-global-components-library-v2}"
SPA_REPO="${REBASE_SPA_REPO:-${REPOS_BASE}/dcl-cruise-101-spa}"

go_bump_lib_version() {
    echo ""
    printf "${BOLD}Bump lib version? (y/n): ${RESET}"
    read -r answer
    if [[ "$answer" == "y" ]]; then
        "${SCRIPT_DIR}/bump-lib-version.sh"
    fi
    echo ""
}

# safe_stash_pop — Restores stashed changes. If the pop produces merge
# conflicts ONLY in package.json / package-lock.json files, they are auto-resolved
# by keeping the develop side (--ours = current working tree, already rebased)
# and dropping the stashed changes for those files. 
# Any other conflicting file is left untouched for the user to resolve manually.
safe_stash_pop() {
    # Temporarily disable set -e so a conflicting pop doesn't abort the script.
    set +e
    git stash pop
    local pop_status=$?
    set -e

    if [[ $pop_status -eq 0 ]]; then
        return 0
    fi

    # Auto-resolve ONLY manifest/CHANGELOG conflicts by keeping OUR side (the
    # rebased working tree = newer develop), via the shared helper. Any real code
    # conflict is left for the user. (See utils.sh — single source of truth.)
    if autoresolve_known_conflicts "$PWD" ours; then
        # Only known files conflicted and were resolved. Drop the stash entry
        # that git kept because of the conflict.
        git stash drop
        return 0
    fi

    echo "${RED}${BOLD}✗ Stash pop conflicts remain. Resolve them, then git stash drop.${RESET}"
    return 1
}

#----

# rebase_with_autoresolve <onto> — `git rebase <onto>`, auto-resolving conflicts
# that involve ONLY manifest/CHANGELOG files as they occur. A rebase can stop on
# several commits; we loop: on a manifest-only conflict, keep OUR side (the base
# = develop) via the shared helper, `git add`, and `rebase --continue`, until the
# rebase finishes or a real code conflict appears (then return 1, leaving the
# rebase in progress for the user).
rebase_with_autoresolve() {
    local onto="$1"

    # Start the rebase. set +e so a conflict return doesn't abort the script.
    set +e
    git rebase "$onto"
    local rc=$?
    set -e

    # Loop while the rebase is paused on conflicts.
    while [[ $rc -ne 0 && -d .git/rebase-merge ]] || [[ $rc -ne 0 && -d .git/rebase-apply ]]; do
        # Are there unmerged files? If not, the rebase failed for another reason.
        local unmerged
        unmerged=$(git diff --name-only --diff-filter=U)
        if [[ -z "$unmerged" ]]; then
            return 1
        fi

        # Try to auto-resolve manifests/CHANGELOG keeping OUR side (develop base).
        if ! autoresolve_known_conflicts "$PWD" ours; then
            # Real code conflict remains — hand back to the user.
            return 1
        fi

        # All conflicts for THIS step were known files and are staged. Continue.
        set +e
        GIT_EDITOR=true git rebase --continue
        rc=$?
        set -e
    done

    return $rc
}

# rebase_repo — Runs the full stash + update develop + rebase flow in the
# current working directory. Assumes CWD is a git repo.
rebase_repo() {
    # 1. Save current branch
    local BRANCH
    BRANCH=$(git rev-parse --abbrev-ref HEAD)

    if [[ "$BRANCH" == "$BASE_BRANCH" ]]; then
        echo "${DIM}Already on ${BASE_BRANCH}, just pulling upstream...${RESET}"
        if [[ -n $(git status --porcelain) ]]; then
            git stash -m "rebase-develop: auto-stash on ${BASE_BRANCH}"
        fi
        git stash -m "rebase-develop: auto-stash on ${BRANCH}"
        git pull "$UPSTREAM_REMOTE" "$BASE_BRANCH"
        echo "${GREEN}${BOLD}✓ ${BASE_BRANCH} is up to date.${RESET}"
        echo "${DIM}Restoring stashed changes...${RESET}"
        safe_stash_pop || return 1
        go_bump_lib_version
        return 0
    fi

    echo "${DIM}Branch: ${BRANCH}${RESET}"

    # 2. Stash any uncommitted changes
    local STASHED=false
    if [[ -n $(git status --porcelain) ]]; then
        echo "${DIM}Stashing changes...${RESET}"
        git stash -m "rebase-develop: auto-stash on ${BRANCH}"
        STASHED=true
    else
        echo "${DIM}Working tree clean, no stash needed.${RESET}"
    fi

    # 3. Update develop from upstream
    echo "${DIM}Updating ${BASE_BRANCH} from ${UPSTREAM_REMOTE}...${RESET}"
    git checkout "$BASE_BRANCH"
    git pull "$UPSTREAM_REMOTE" "$BASE_BRANCH"

    # 4. Return to feature branch
    echo "${DIM}Returning to ${BRANCH}...${RESET}"
    git checkout "$BRANCH"

    # 5. Rebase onto develop, auto-resolving manifest/CHANGELOG conflicts as they
    #    appear (the rebase can stop on several commits). During a rebase the
    #    incoming commit is "theirs"; we keep OUR side (develop) for manifests so
    #    the branch ends up on develop's package versions.
    echo "${DIM}Rebasing onto ${BASE_BRANCH}...${RESET}"
    if ! rebase_with_autoresolve "$BASE_BRANCH"; then
        echo "${RED}${BOLD}✗ Rebase conflicts remain (real code). Resolve them, then:${RESET}"
        echo "${DIM}  git rebase --continue (or --abort)${RESET}"
        echo "${DIM}  and re-run, or git stash pop manually.${RESET}"
        return 1
    fi
    echo "${GREEN}${BOLD}✓ Rebase successful.${RESET}"

    # 6. Pop stash if we stashed
    if [[ "$STASHED" == true ]]; then
        echo "${DIM}Restoring stashed changes...${RESET}"
        safe_stash_pop || return 1
    fi

    echo "${GREEN}${BOLD}✓ Done. ${BRANCH} is up to date with ${BASE_BRANCH}.${RESET}"
}

#----

# Parse arguments
FULL=false
if [[ "$1" == "--full" ]]; then
    FULL=true
fi

if [[ "$FULL" == true ]]; then
    # Ensure the hardcoded base directory exists before doing anything.
    if [[ ! -d "$REPOS_BASE" ]]; then
        echo "${RED}${BOLD}✗ REPOS_BASE not found: ${REPOS_BASE}${RESET}"
        echo "${DIM}  Fix the REPOS_BASE path near the top of this script (rebase-develop.sh).${RESET}"
        exit 1
    fi

    # Full mode: process the library first, then the SPA.
    repos=("$LIB_REPO" "$SPA_REPO")
    total=${#repos[@]}
    index=1

    for repo in "${repos[@]}"; do
        # per-repo progress indicator.
        echo "${BOLD}▶ [${index}/${total}] Processing ${repo:t}...${RESET}"
        if ! cd "$repo"; then
            echo "${RED}${BOLD}✗ Could not enter ${repo}. Aborting.${RESET}"
            exit 1
        fi

        # Run the flow without letting set -e abort the whole script on failure,
        # so we can report a clear error for the offending repo.
        if ! rebase_repo; then
            echo "${RED}${BOLD}✗ Failed while processing ${repo:t}. Aborting --full run.${RESET}"
            exit 1
        fi
        index=$((index + 1))
    done

    # final summary across all repos.
    echo "${GREEN}${BOLD}✓ Full rebase complete: all ${total} repos are up to date with ${BASE_BRANCH}.${RESET}"
    go_bump_lib_version
    exit 0
fi

# Default mode: run in the current directory.
rebase_repo
go_bump_lib_version
