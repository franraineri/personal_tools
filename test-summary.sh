#!/bin/zsh
#
# test-summary.sh — Runs tests and shows failures + coverage for changed files.
#
# Usage:
#   ./scripts/test-summary.sh                    # All test whitput filtering
#   ./scripts/test-summary.sh -s                 # Only Staged area
#   ./scripts/test-summary.sh -n 3               # Staged + last 3 commits
#   ./scripts/test-summary.sh --include='...'    # Pass extra args to ng test
#   ./scripts/test-summary.sh -h                 # Show help
#
# Output:
#   1. List of relevant changed files found
#   2. Failed test names + error details
#   3. Coverage table filtered to changed files only
#   4. Coverage summary + TOTAL line
#

set -o pipefail

# ─── Colors (disabled if not a terminal) ──────────────────────
# Define colors first (respecting TTY), THEN source utils.sh — its `:=` defaults
# will not override these, so the no-TTY (plain) mode is preserved.
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    CYAN='\033[0;36m'
    YELLOW='\033[0;33m'
    DIM='\033[2m'
    BOLD='\033[1m'
    RESET='\033[0m'
else
    RED='' GREEN='' CYAN='' YELLOW='' DIM='' BOLD='' RESET=''
fi

# Must run with zsh (uses zsh arrays/idioms).
if [[ -z "${ZSH_VERSION:-}" ]]; then
    echo "This script must be run with zsh (not bash/sh)." >&2
    echo "  Run:  ./test-summary.sh [OPTIONS]   or   zsh test-summary.sh [OPTIONS]" >&2
    exit 1
fi

# Shared helpers (logging, error handling). Colors above are preserved.
source "${${(%):-%x}:A:h}/utils.sh" || { echo "Missing utils.sh next to $0" >&2; exit 1; }

# ─── Help ─────────────────────────────────────────────────────
show_help() {
    echo "Usage: $0 [OPTIONS] [NG_TEST_ARGS]"
    echo ""
    echo "Options:"
    echo "  (none)  Run all tests without file filtering"
    echo "  -s      Filter coverage to staged files only"
    echo "  -n N    Filter coverage to staged + last N commits"
    echo "  -h      Show this help"
    echo ""
    echo "Examples:"
    echo "  $0                          # All tests, no filtering"
    echo "  $0 -s                       # Coverage filtered to staged files"
    echo "  $0 -n 5                     # Staged + last 5 commits"
    echo "  $0 -s --include='src/app/features/onboard-activities/'"
    exit 0
}

# ─── Parameters ───────────────────────────────────────────────
COMMITS=0
FILTER_MODE="none"  # none | staged | commits
NG_EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -s)
            FILTER_MODE="staged"
            shift
            ;;
        -n)
            if [[ -z "$2" || ! "$2" =~ ^[0-9]+$ ]]; then
                echo "${RED}Error: -n requires a positive integer.${RESET}" >&2
                exit 1
            fi
            FILTER_MODE="commits"
            COMMITS="$2"
            shift 2
            ;;
        -h|--help)
            show_help
            ;;
        *)
            NG_EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

# ─── File patterns to match ───────────────────────────────────
FILE_PATTERNS=(
    '\.component\.ts$'
    '\.service\.ts$'
    '\.util\.ts$'
)

# ─── Gather changed files ────────────────────────────────────
gather_changed_files() {
    local all_files=""

    # Always: staged files (both new and modified)
    local staged
    staged=$(git diff --cached --name-only --diff-filter=ACMR 2>/dev/null)
    [[ -n "$staged" ]] && all_files="$staged"

    # Optional: files from last N commits (only in "commits" mode)
    if [[ "$FILTER_MODE" == "commits" && "$COMMITS" -gt 0 ]]; then
        local committed
        committed=$(git log --name-only --pretty=format: -n "$COMMITS" 2>/dev/null | grep -v '^$')
        if [[ -n "$committed" ]]; then
            [[ -n "$all_files" ]] && all_files="${all_files}\n${committed}" || all_files="$committed"
        fi
    fi

    [[ -z "$all_files" ]] && return

    # Build combined regex
    local pattern
    pattern=$(printf '%s|' "${FILE_PATTERNS[@]}")
    pattern="${pattern%|}"

    # Deduplicate, filter, exclude specs, exclude deleted files
    echo -e "$all_files" | sort -u | grep -E "$pattern" | grep -v '\.spec\.ts$'
}

# ─── Extract coverage from lcov.info for a single file ────────
# Outputs: "stmts_pct branch_pct func_pct lines_pct" or empty if not found
extract_lcov_coverage() {
    local file_path="$1"
    local lcov_file="coverage/lcov.info"

    [[ ! -f "$lcov_file" ]] && return

    # Find the record for this file in lcov.info
    local in_record=false
    local lf=0 lh=0 fnf=0 fnh=0 brf=0 brh=0 daf=0 dah=0

    while IFS= read -r line; do
        if [[ "$line" == "SF:"* ]]; then
            local sf="${line#SF:}"
            if [[ "$sf" == *"$file_path"* || "$file_path" == *"$sf"* ]]; then
                in_record=true
            else
                in_record=false
            fi
        elif [[ "$in_record" == true ]]; then
            case "$line" in
                LF:*) lf="${line#LF:}" ;;
                LH:*) lh="${line#LH:}" ;;
                FNF:*) fnf="${line#FNF:}" ;;
                FNH:*) fnh="${line#FNH:}" ;;
                BRF:*) brf="${line#BRF:}" ;;
                BRH:*) brh="${line#BRH:}" ;;
                DA:*)
                    daf=$((daf + 1))
                    local hits="${line#DA:*,}"
                    [[ "$hits" -gt 0 ]] 2>/dev/null && dah=$((dah + 1))
                    ;;
                end_of_record)
                    # Calculate percentages
                    local stmts_pct lines_pct func_pct branch_pct
                    if [[ $daf -gt 0 ]]; then
                        stmts_pct=$(awk "BEGIN {printf \"%.2f\", ($dah/$daf)*100}")
                    else
                        stmts_pct="100.00"
                    fi
                    if [[ $lf -gt 0 ]]; then
                        lines_pct=$(awk "BEGIN {printf \"%.2f\", ($lh/$lf)*100}")
                    else
                        lines_pct="100.00"
                    fi
                    if [[ $fnf -gt 0 ]]; then
                        func_pct=$(awk "BEGIN {printf \"%.2f\", ($fnh/$fnf)*100}")
                    else
                        func_pct="100.00"
                    fi
                    if [[ $brf -gt 0 ]]; then
                        branch_pct=$(awk "BEGIN {printf \"%.2f\", ($brh/$brf)*100}")
                    else
                        branch_pct="100.00"
                    fi
                    echo "$stmts_pct $branch_pct $func_pct $lines_pct"
                    return
                    ;;
            esac
        fi
    done < "$lcov_file"
}

# ─── Color a percentage value ─────────────────────────────────
color_pct() {
    local pct="$1"
    local pct_int="${pct%.*}"
    if [[ $pct_int -ge 80 ]]; then
        printf "${GREEN}%7s${RESET}" "${pct}%"
    elif [[ $pct_int -ge 50 ]]; then
        printf "${YELLOW}%7s${RESET}" "${pct}%"
    else
        printf "${RED}%7s${RESET}" "${pct}%"
    fi
}

# ─── Elapsed time helper ──────────────────────────────────────
format_duration() {
    local secs=$1
    if [[ $secs -ge 60 ]]; then
        printf "%dm %ds" $((secs / 60)) $((secs % 60))
    else
        printf "%ds" "$secs"
    fi
}

# ─── Show commit context when -n is used ──────────────────────
show_commit_context() {
    local n=$1
    echo ""
    echo "${DIM}Filtering from last ${n} commit(s):${RESET}"
    git log --oneline -n "$n" | while IFS= read -r line; do
        echo "${DIM}  • ${line}${RESET}"
    done
}


### ───!! MAIN !!─────────────────────────────────────────────────────##

# Verify we're in a git repo
if ! git rev-parse --is-inside-work-tree &>/dev/null; then
    echo "${RED}Error: Not inside a git repository.${RESET}" >&2
    exit 1
fi

# Step 1: Find relevant files (only when -s or -n is used)
CHANGED_FILES=()

if [[ "$FILTER_MODE" != "none" ]]; then
    CHANGED_FILES_RAW=$(gather_changed_files)
    if [[ -n "$CHANGED_FILES_RAW" ]]; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && CHANGED_FILES+=("$line")
        done <<< "$CHANGED_FILES_RAW"
    fi

    if [[ ${#CHANGED_FILES[@]} -eq 0 ]]; then
        echo ""
        echo "${YELLOW}⚠  No matching files found.${RESET}"
        echo "${DIM}   Source: staged area${RESET}"
        [[ "$FILTER_MODE" == "commits" ]] && echo "${DIM}   + last ${COMMITS} commit(s)${RESET}"
        echo "${DIM}   Patterns: ${FILE_PATTERNS[*]}${RESET}"
        echo ""
        echo "${YELLOW}   Tip: Stage files with 'git add' or use '-n N' to scan recent commits.${RESET}"
    else
        if [[ "$FILTER_MODE" == "commits" ]]; then
            show_commit_context "$COMMITS"
        fi

        echo ""
        echo "${CYAN}${BOLD}┌─ CHANGED FILES (${#CHANGED_FILES[@]}) ────────────────────────────────${RESET}"
        for f in "${CHANGED_FILES[@]}"; do
            echo "${CYAN}│${RESET}  $f"
        done
        echo "${CYAN}${BOLD}└──────────────────────────────────────────────────────${RESET}"
    fi
fi
echo ""
echo "${DIM}Running tests...${RESET}"
START_TIME=$SECONDS

OUTPUT=$(npx ng test --no-watch --browsers=ChromeHeadless --code-coverage "${NG_EXTRA_ARGS[@]}" 2>&1)
EXIT_CODE=$?
ELAPSED=$((SECONDS - START_TIME))

echo "${DIM}Done in $(format_duration $ELAPSED).${RESET}"

# Step 3: Show failures grouped by component/file
FAILED_LINES=$(echo "$OUTPUT" | grep "FAILED$" | sort -u)

echo ""
if [[ -n "$FAILED_LINES" ]]; then
    FAIL_COUNT=$(echo "$FAILED_LINES" | wc -l | tr -d ' ')

    # Extract failures with assertions and spec paths using awk (BSD-compatible)
    # Output format: component<TAB>test_name<TAB>assertion<TAB>spec_path
    PARSED_FAILURES=$(echo "$OUTPUT" | awk '
    /FAILED[[:space:]]*$/ {
        failed_line = $0
        # Extract component: first word after ") "
        idx = match(failed_line, /\) [A-Za-z0-9_]+ /)
        if (idx > 0) {
            tmp = substr(failed_line, RSTART + 2)
            split(tmp, w, " ")
            component = w[1]
        } else {
            component = "Unknown"
        }
        # Extract test name: between component and FAILED
        idx2 = index(failed_line, component " ")
        if (idx2 > 0) {
            test_part = substr(failed_line, idx2 + length(component) + 1)
            sub(/ FAILED[[:space:]]*$/, "", test_part)
        } else {
            test_part = "unknown test"
        }
        # Read next lines for assertion and spec path
        assertion = ""
        spec_path = ""
        for (i = 1; i <= 6; i++) {
            if ((getline next_line) > 0) {
                if (assertion == "" && (next_line ~ /^[[:space:]]+Expected/ || next_line ~ /^[[:space:]]+Error:/)) {
                    gsub(/^[[:space:]]+/, "", next_line)
                    assertion = next_line
                } else if (next_line ~ /UserContext\.apply/) {
                    # Extract path from: "at UserContext.apply (src/path/file.spec.ts:123:45)"
                    n = split(next_line, parts, "(")
                    if (n >= 2) {
                        path_part = parts[2]
                        sub(/:.*/, "", path_part)
                        spec_path = path_part
                    }
                    break
                }
            }
        }
        printf "%s\t%s\t%s\t%s\n", component, test_part, assertion, spec_path
    }
    ' | sort -u -t$'\t' -k1,2)

    echo "${RED}${BOLD}┌─ FAILURES (${FAIL_COUNT}) ────────────────────────────────────${RESET}"

    # Group and display by component
    CURRENT_COMPONENT=""

    echo "$PARSED_FAILURES" | while IFS=$'\t' read -r COMPONENT TEST_NAME ASSERTION SPEC_PATH; do
        [[ -z "$COMPONENT" ]] && continue

        if [[ "$COMPONENT" != "$CURRENT_COMPONENT" ]]; then
            echo "${RED}│${RESET}"
            echo "${RED}│${RESET}  ${BOLD}${COMPONENT}${RESET}"
            CURRENT_COMPONENT="$COMPONENT"
        fi

        echo "${RED}│${RESET}    • ${TEST_NAME}"
        [[ -n "$ASSERTION" ]] && echo "${RED}│${RESET}      ${DIM}${ASSERTION}${RESET}"
    done

    # Extract unique spec directories for rerun commands
    RERUN_DIRS=$(echo "$PARSED_FAILURES" | awk -F'\t' '{if($4!="") print $4}' | xargs -I{} dirname {} | sort -u)

    if [[ -n "$RERUN_DIRS" ]]; then
        echo "${RED}│${RESET}"
        echo "${RED}│${RESET}  ${CYAN}${BOLD}Rerun failing specs:${RESET}"
        echo "$RERUN_DIRS" | while IFS= read -r DIR; do
            echo "${RED}│${RESET}  ${DIM}npm run test -- --include='${DIR}/**/*.spec.ts'${RESET}"
        done
    fi

    echo "${RED}${BOLD}└──────────────────────────────────────────────────────${RESET}"
else
    echo "${GREEN}${BOLD}✓ ALL TESTS PASSED${RESET}"
fi

# Step 4: Coverage table (from lcov.info for changed files)
if [[ ${#CHANGED_FILES[@]} -gt 0 ]]; then
    echo ""
    echo "${CYAN}${BOLD}┌─ COVERAGE (changed files) ────────────────────────────${RESET}"

    if [[ -f "coverage/lcov.info" ]]; then
        # Print header
        printf "${CYAN}│${RESET}  %-41s %6s %6s %6s %6s\n" "File" "Stmts" "Branch" "Funcs" "Lines"
        printf "${CYAN}│${RESET}  %-41s %6s %6s %6s %6s\n" "-------------------------------------------" "-------" "-------" "-------" "-------"

        HAS_DATA=false
        for f in "${CHANGED_FILES[@]}"; do
            COV_DATA=$(extract_lcov_coverage "$f")
            if [[ -n "$COV_DATA" ]]; then
                HAS_DATA=true
                STMTS=$(echo "$COV_DATA" | awk '{print $1}')
                BRANCH=$(echo "$COV_DATA" | awk '{print $2}')
                FUNCS=$(echo "$COV_DATA" | awk '{print $3}')
                LINES=$(echo "$COV_DATA" | awk '{print $4}')
                DISPLAY_NAME=$(basename "$f")
                printf "${CYAN}│${RESET}  %-43s " "$DISPLAY_NAME"
                color_pct "$STMTS"
                printf " "
                color_pct "$BRANCH"
                printf " "
                color_pct "$FUNCS"
                printf " "
                color_pct "$LINES"
                printf "\n"
            fi
        done

        if [[ "$HAS_DATA" == false ]]; then
            echo "${CYAN}│${RESET}"
            echo "${CYAN}│${RESET}  ${YELLOW}⚠  No coverage data found for changed files.${RESET}"
            echo "${CYAN}│${RESET}  ${DIM}This may mean the files have no specs covering them.${RESET}"
        fi
    else
        echo "${CYAN}│${RESET}"
        echo "${CYAN}│${RESET}  ${YELLOW}⚠  coverage/lcov.info not found.${RESET}"
        echo "${CYAN}│${RESET}  ${DIM}Ensure --code-coverage is enabled.${RESET}"
    fi

    echo "${CYAN}${BOLD}└──────────────────────────────────────────────────────${RESET}"
fi

# Step 5: Coverage summary
SUMMARY=$(echo "$OUTPUT" | grep -A 6 "Coverage summary")
if [[ -n "$SUMMARY" ]]; then
    echo ""
    echo "$SUMMARY"
fi

# Step 6: Total
TOTAL=$(echo "$OUTPUT" | grep "^TOTAL:")
if [[ -n "$TOTAL" ]]; then
    echo ""
    echo "${BOLD}${TOTAL}${RESET}"
fi

exit $EXIT_CODE
