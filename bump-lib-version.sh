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

# Must run with zsh (uses ${0:A:h} and zsh idioms).
if [[ -z "${ZSH_VERSION:-}" ]]; then
    echo "This script must be run with zsh (not bash/sh)." >&2
    echo "  Run:  ./bump-lib-version.sh [N]   or   zsh bump-lib-version.sh [N]" >&2
    exit 1
fi

# Shared helpers (logging, error trap). See utils.sh.
source "${${(%):-%x}:A:h}/utils.sh" || { echo "Missing utils.sh next to $0" >&2; exit 1; }
utils_enable_error_trap

# Parse optional increment (default: +1)
INCREMENT=${1:-1}

if [[ ! "$INCREMENT" =~ ^-?[0-9]+$ ]]; then
    die "Argument must be an integer (e.g. 1, -1, +3). Got: '${INCREMENT}'"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Config — precedence: env var > built-in default. A Koda executor (or
# `koda project`) can export these so the bump runs against any library/SPA
# layout and package name without editing the script:
#   BUMP_PKG_NAME        → PKG_NAME         (default: @dcl/dcl-ui-global-components-library-v2)
#   BUMP_LIB_PROJECT_PKG → LIB_PROJECT_PKG  (canonical version source of truth)
#   BUMP_LIB_ROOT_PKG    → LIB_ROOT_PKG     (library root package.json)
#   BUMP_SPA_PKG         → SPA_PKG          (consumer/SPA package.json)
#   BUMP_REPOS_BASE      → REPOS_BASE       (parent dir; used to derive the 3 defaults)
# ─────────────────────────────────────────────────────────────────────────────
PKG_NAME="${BUMP_PKG_NAME:-@dcl/dcl-ui-global-components-library-v2}"
REPOS_BASE="${BUMP_REPOS_BASE:-/Users/franco.raineri/devTools/DCL/Silent}"
SPA_PKG="${BUMP_SPA_PKG:-${REPOS_BASE}/dcl-cruise-101-spa/package.json}"
LIB_ROOT_PKG="${BUMP_LIB_ROOT_PKG:-${REPOS_BASE}/dcl-ui-global-components-library-v2/package.json}"
LIB_PROJECT_PKG="${BUMP_LIB_PROJECT_PKG:-${REPOS_BASE}/dcl-ui-global-components-library-v2/projects/dcl-ui-global-components-library-v2/package.json}"

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
# Escape regex metacharacters in the package name so it can be embedded in the
# perl pattern (handles the leading @ and the / in the scope).
PKG_NAME_RE=$(printf '%s' "$PKG_NAME" | sed 's/[.[\*^$()+?{|\/@]/\\&/g')
log_info "Syncing dependency in ${SPA_PKG}"
run_checked "Sync SPA dependency version" \
    perl -i -pe "s/(\"${PKG_NAME_RE}\":\\s*\"\\^)\\d+\\.\\d+\\.\\d+(\")/\${1}${CANONICAL_VERSION}\${2}/" "$SPA_PKG" \
    || die "Could not sync the SPA dependency version."

# --- Show results
echo ""
log_ok "All synced to version ${CANONICAL_VERSION}:"
log_info "  Library project: $(grep '"version"' "$LIB_PROJECT_PKG" | head -1 | xargs)"
log_info "  Library root:    $(grep '"version"' "$LIB_ROOT_PKG" | head -1 | xargs)"
log_info "  SPA dependency:  $(grep -F "$PKG_NAME" "$SPA_PKG" | head -1 | xargs)"
