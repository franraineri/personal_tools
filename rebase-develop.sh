#!/bin/zsh
#
# rebase-develop.sh — Stash, update develop from upstream, rebase current branch.
#
# Usage:
#   ./scripts/rebase-develop.sh          Rebase the current repo (default behavior).
#   ./scripts/rebase-develop.sh --full   Rebase dcl-ui-global-components-library-v2,
#                                         then dcl-cruise-101-spa, in that order.
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

#----

SCRIPT_DIR="${0:A:h}"

# Hardcoded base directory where both repos live as siblings.
REPOS_BASE="/Users/franco.raineri/devTools/DCL/Silent"
LIB_REPO="${REPOS_BASE}/dcl-ui-global-components-library-v2"
SPA_REPO="${REPOS_BASE}/dcl-cruise-101-spa"

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

    # Collect the list of files still in conflict (unmerged).
    local conflicts
    conflicts=$(git diff --name-only --diff-filter=U)

    if [[ -z "$conflicts" ]]; then
        return 0
    fi

    local other_conflicts=false
    local file
    while IFS= read -r file; do
        case "${file:t}" in
            package.json|package-lock.json)
                # Keep develop's version (working tree), drop the stashed one.
                git checkout --ours -- "$file"
                git add -- "$file"
                ;;
            *)
                other_conflicts=true
                ;;
        esac
    done <<< "$conflicts"

    if [[ "$other_conflicts" == true ]]; then
        echo "${RED}${BOLD}✗ Stash pop conflicts remain. Resolve them, then git stash drop.${RESET}"
        return 1
    fi

    # Only package.json/package-lock.json conflicts existed and were resolved.
    # Drop the now-applied stash entry that git kept because of the conflict.
    git stash drop
    return 0
}

#----

# rebase_repo — Runs the full stash + update develop + rebase flow in the
# current working directory. Assumes CWD is a git repo.
rebase_repo() {
    # 1. Save current branch
    local BRANCH
    BRANCH=$(git rev-parse --abbrev-ref HEAD)

    if [[ "$BRANCH" == "develop" ]]; then
        echo "${DIM}Already on develop, just pulling upstream...${RESET}"
        if [[ -n $(git status --porcelain) ]]; then
            git stash -m "rebase-develop: auto-stash on develop"
        fi
        git stash -m "rebase-develop: auto-stash on ${BRANCH}"
        git pull upstream develop
        echo "${GREEN}${BOLD}✓ develop is up to date.${RESET}"
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
    echo "${DIM}Updating develop from upstream...${RESET}"
    git checkout develop
    git pull upstream develop

    # 4. Return to feature branch
    echo "${DIM}Returning to ${BRANCH}...${RESET}"
    git checkout "$BRANCH"

    # 5. Rebase onto develop
    echo "${DIM}Rebasing onto develop...${RESET}"
    if git rebase develop; then
        echo "${GREEN}${BOLD}✓ Rebase successful.${RESET}"
    else
        echo "${RED}${BOLD}✗ Rebase conflicts detected. Resolve them, then:${RESET}!"
        echo "${DIM}  git rebase --continue (or --abort)${RESET}"
        echo "${DIM}  and git stash pop)${RESET}"
        return 1
    fi

    # 6. Pop stash if we stashed
    if [[ "$STASHED" == true ]]; then
        echo "${DIM}Restoring stashed changes...${RESET}"
        safe_stash_pop || return 1
    fi

    echo "${GREEN}${BOLD}✓ Done. ${BRANCH} is up to date with develop.${RESET}"
    go_bump_lib_version
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
    echo "${GREEN}${BOLD}✓ Full rebase complete: all ${total} repos are up to date with develop.${RESET}"
    exit 0
fi

# Default mode: run in the current directory.
rebase_repo
