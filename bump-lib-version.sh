#!/bin/zsh
#
# bump-lib-version.sh — Sync the version of @dcl/dcl-ui-global-components-library-v2
# across all package.json files using the library project's version as source of truth.
#
# The version in dcl-ui-global-components-library-v2/projects/.../package.json is
# considered the canonical version. This script:
#   1. Reads that version
#   2. Sets the library root package.json to that same version
#   3. Sets the SPA dependency to ^<that version>
#
# Usage:
#   ./bump-lib-version.sh          # Bump patch +1 in library, sync all
#   ./bump-lib-version.sh 3        # Bump patch +3 in library, sync all
#   ./bump-lib-version.sh -1       # Decrement patch by 1, sync all
#   ./bump-lib-version.sh -- -2    # Use -- for negative numbers
#

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

# Parse optional increment (default: +1)
INCREMENT=${1:-1}

if [[ ! "$INCREMENT" =~ ^-?[0-9]+$ ]]; then
    echo "${RED}Error: argument must be an integer (e.g. 1, -1, +3).${RESET}" >&2
    exit 1
fi

SPA_PKG="/Users/franco.raineri/devTools/DCL/Silent/dcl-cruise-101-spa/package.json"
LIB_ROOT_PKG="/Users/franco.raineri/devTools/DCL/Silent/dcl-ui-global-components-library-v2/package.json"
LIB_PROJECT_PKG="/Users/franco.raineri/devTools/DCL/Silent/dcl-ui-global-components-library-v2/projects/dcl-ui-global-components-library-v2/package.json"

# --- Step 1: Bump the library project version
echo "${DIM}Bumping library project version by ${INCREMENT}...${RESET}"
perl -i -pe "s/(\"version\":\\s*\")(\\d+\\.\\d+\\.)(\\d+)(\")/ \"\$1\$2\" . (\$3+$INCREMENT) . \"\$4\" /e" "$LIB_PROJECT_PKG"

# --- Step 2: Read the canonical version from library project
CANONICAL_VERSION=$(grep -m1 '"version"' "$LIB_PROJECT_PKG" | sed 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')

if [[ -z "$CANONICAL_VERSION" ]]; then
    echo "${RED}Error: could not read version from ${LIB_PROJECT_PKG}${RESET}" >&2
    exit 1
fi

echo "${DIM}Canonical version: ${CANONICAL_VERSION}${RESET}"
echo ""

# --- Step 3: Set library root package.json to the canonical version
echo "${DIM}Syncing ${LIB_ROOT_PKG}${RESET}"
perl -i -pe "s/(\"version\":\\s*\")\\d+\\.\\d+\\.\\d+(\")/\${1}${CANONICAL_VERSION}\${2}/" "$LIB_ROOT_PKG"

# --- Step 4: Set SPA dependency to ^<canonical version>
echo "${DIM}Syncing dependency in ${SPA_PKG}${RESET}"
perl -i -pe "s/(\"\\@dcl\\/dcl-ui-global-components-library-v2\":\\s*\"\\^)\\d+\\.\\d+\\.\\d+(\")/\${1}${CANONICAL_VERSION}\${2}/" "$SPA_PKG"

# --- Show results
echo ""
echo "${GREEN}${BOLD}✓ All synced to version ${CANONICAL_VERSION}:${RESET}"
echo "${DIM}  Library project: $(grep '"version"' "$LIB_PROJECT_PKG" | head -1 | xargs)${RESET}"
echo "${DIM}  Library root:    $(grep '"version"' "$LIB_ROOT_PKG" | head -1 | xargs)${RESET}"
echo "${DIM}  SPA dependency:  $(grep 'dcl-ui-global-components-library-v2' "$SPA_PKG" | grep '@dcl' | xargs)${RESET}"
