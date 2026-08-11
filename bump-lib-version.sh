#!/bin/zsh
#
# bump-lib-version.sh — Bump patch version of dcl-ui-global-components-library-v2
# in all three package.json files (library root, library project, and SPA consumer).
#
# Usage:
#   ./scripts/bump-lib-version.sh        # +1 (default)
#   ./scripts/bump-lib-version.sh 3      # +3
#   ./scripts/bump-lib-version.sh -1     # -1
#   ./scripts/bump-lib-version.sh -- -2  # -2 (use -- for negative numbers)
#

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

# Parse increment (default +1)
INCREMENT=${1:-1}

if [[ ! "$INCREMENT" =~ ^-?[0-9]+$ ]]; then
    echo "${RED}Error: argument must be an integer (e.g. 1, -1, +3).${RESET}" >&2
    exit 1
fi

SPA_PKG="/Users/franco.raineri/devTools/DCL/Silent/dcl-cruise-101-spa/package.json"
LIB_ROOT_PKG="/Users/franco.raineri/devTools/DCL/Silent/dcl-ui-global-components-library-v2/package.json"
LIB_PROJECT_PKG="/Users/franco.raineri/devTools/DCL/Silent/dcl-ui-global-components-library-v2/projects/dcl-ui-global-components-library-v2/package.json"

# --- Bump "version": "x.y.z" → "x.y.(z+INCREMENT)" in a file
bump_version_field() {
    local file="$1"
    perl -i -pe "s/(\"version\":\\s*\")(\\d+\\.\\d+\\.)(\\d+)(\")/ \"\$1\$2\" . (\$3+$INCREMENT) . \"\$4\" /e" "$file"
}

# --- Bump "@dcl/dcl-ui-global-components-library-v2": "^x.y.z" → "^x.y.(z+INCREMENT)"
bump_dep_field() {
    local file="$1"
    perl -i -pe "s/(\"\\@dcl\\/dcl-ui-global-components-library-v2\":\\s*\"\\^)(\\d+\\.\\d+\\.)(\\d+)(\")/ \"\$1\$2\" . (\$3+$INCREMENT) . \"\$4\" /e" "$file"
}

echo "${DIM}Increment: ${INCREMENT}${RESET}"
echo ""

# 1. Bump library root package.json
echo "${DIM}Bumping ${LIB_ROOT_PKG}${RESET}"
bump_version_field "$LIB_ROOT_PKG"

# 2. Bump library project package.json
echo "${DIM}Bumping ${LIB_PROJECT_PKG}${RESET}"
bump_version_field "$LIB_PROJECT_PKG"

# 3. Bump dependency in SPA package.json
echo "${DIM}Bumping dependency in ${SPA_PKG}${RESET}"
bump_dep_field "$SPA_PKG"

# Show results
echo ""
echo "${GREEN}${BOLD}✓ Versions bumped (${INCREMENT}):${RESET}"
echo "${DIM}  Library root:    $(grep '"version"' "$LIB_ROOT_PKG" | head -1 | xargs)${RESET}"
echo "${DIM}  Library project: $(grep '"version"' "$LIB_PROJECT_PKG" | head -1 | xargs)${RESET}"
echo "${DIM}  SPA dependency:  $(grep 'dcl-ui-global-components-library-v2' "$SPA_PKG" | grep '@dcl' | xargs)${RESET}"
