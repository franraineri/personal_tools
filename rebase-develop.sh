#!/bin/zsh
#
# rebase-develop.sh — Stash, update develop from upstream, rebase current branch.
#
# Usage:
#   ./scripts/rebase-develop.sh
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

#----

SCRIPT_DIR="${0:A:h}"

go_bump_lib_version() {
    echo ""
    printf "${BOLD}Bump lib version? (y/n): ${RESET}"
    read -r answer
    if [[ "$answer" == "y" ]]; then
        "${SCRIPT_DIR}/bump-lib-version.sh"
    fi
    echo ""
}

#----


# 1. Save current branch
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
    git stash pop
    go_bump_lib_version
    exit 0
fi

echo "${DIM}Branch: ${BRANCH}${RESET}"

# 2. Stash any uncommitted changes
STASHED=false
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
    exit 1
fi

# 6. Pop stash if we stashed
if [[ "$STASHED" == true ]]; then
    echo "${DIM}Restoring stashed changes...${RESET}"
    git stash pop
fi

echo "${GREEN}${BOLD}✓ Done. ${BRANCH} is up to date with develop.${RESET}"
go_bump_lib_version
