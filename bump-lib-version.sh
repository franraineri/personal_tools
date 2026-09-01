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

# Shared helpers (logging, error trap). See utils.sh.
source "${0:A:h}/utils.sh" || { echo "Missing utils.sh next to $0" >&2; exit 1; }
utils_enable_error_trap

# Parse optional increment (default: +1)
INCREMENT=${1:-1}

if [[ ! "$INCREMENT" =~ ^-?[0-9]+$ ]]; then
    die "Argument must be an integer (e.g. 1, -1, +3). Got: '${INCREMENT}'"
fi

SPA_PKG="/Users/franco.raineri/devTools/DCL/Silent/dcl-cruise-101-spa/package.json"
LIB_ROOT_PKG="/Users/franco.raineri/devTools/DCL/Silent/dcl-ui-global-components-library-v2/package.json"
LIB_PROJECT_PKG="/Users/franco.raineri/devTools/DCL/Silent/dcl-ui-global-components-library-v2/projects/dcl-ui-global-components-library-v2/package.json"

# Validate the target files exist before mutating anything.
for f in "$LIB_PROJECT_PKG" "$LIB_ROOT_PKG" "$SPA_PKG"; do
    [[ -f "$f" ]] || die "package.json not found: ${f}"
done

# --- Step 1: Bump the library project version
log_info "Bumping library project version by ${INCREMENT}..."
run_checked "Bump version in library project package.json" \
    perl -i -pe "s/(\"version\":\\s*\")(\\d+\\.\\d+\\.)(\\d+)(\")/ \"\$1\$2\" . (\$3+$INCREMENT) . \"\$4\" /e" "$LIB_PROJECT_PKG" \
    || die "Could not bump the library project version."

# --- Step 2: Read the canonical version from library project
CANONICAL_VERSION=$(grep -m1 '"version"' "$LIB_PROJECT_PKG" | sed 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')

if [[ -z "$CANONICAL_VERSION" ]]; then
    die "Could not read version from ${LIB_PROJECT_PKG}"
fi

log_info "Canonical version: ${CANONICAL_VERSION}"
echo ""

# --- Step 3: Set library root package.json to the canonical version
log_info "Syncing ${LIB_ROOT_PKG}"
run_checked "Sync library root package.json" \
    perl -i -pe "s/(\"version\":\\s*\")\\d+\\.\\d+\\.\\d+(\")/\${1}${CANONICAL_VERSION}\${2}/" "$LIB_ROOT_PKG" \
    || die "Could not sync the library root version."

# --- Step 4: Set SPA dependency to ^<canonical version>
log_info "Syncing dependency in ${SPA_PKG}"
run_checked "Sync SPA dependency version" \
    perl -i -pe "s/(\"\\@dcl\\/dcl-ui-global-components-library-v2\":\\s*\"\\^)\\d+\\.\\d+\\.\\d+(\")/\${1}${CANONICAL_VERSION}\${2}/" "$SPA_PKG" \
    || die "Could not sync the SPA dependency version."

# --- Show results
echo ""
log_ok "All synced to version ${CANONICAL_VERSION}:"
log_info "  Library project: $(grep '"version"' "$LIB_PROJECT_PKG" | head -1 | xargs)"
log_info "  Library root:    $(grep '"version"' "$LIB_ROOT_PKG" | head -1 | xargs)"
log_info "  SPA dependency:  $(grep 'dcl-ui-global-components-library-v2' "$SPA_PKG" | grep '@dcl' | xargs)"
