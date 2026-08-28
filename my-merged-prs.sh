#!/bin/zsh
# Lists my merged PRs in a given repo within a date range and generates a markdown file.
# Usage: my-merged-prs.sh [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--repo OWNER/REPO] [--out FILE]
# Defaults: from = 3 weeks ago, to = today, repo = detected from git remote (upstream or origin)

set -euo pipefail

# Defaults
FROM_DATE=$(date -v-3w +%Y-%m-%d)
TO_DATE=$(date +%Y-%m-%d)
REPO=""
OUT_FILE=""

# Constants
JIRA_BASE="http://disneyexperiences.atlassian.net/browse"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --from) FROM_DATE="$2"; shift 2 ;;
        --to)   TO_DATE="$2";   shift 2 ;;
        --repo) REPO="$2";      shift 2 ;;
        --out)  OUT_FILE="$2";  shift 2 ;;
        -h|--help)
            echo "Usage: my-merged-prs.sh [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--repo OWNER/REPO] [--out FILE]"
            echo ""
            echo "Options:"
            echo "  --from   Start date (default: 3 weeks ago)"
            echo "  --to     End date (default: today)"
            echo "  --repo   GitHub repo as OWNER/REPO (default: detected from git remotes)"
            echo "  --out    Output markdown file path (default: ./merged-prs-FROM-TO.md)"
            echo ""
            echo "Examples:"
            echo "  my-merged-prs.sh --repo dcl-applications/dcl-cruise-101-spa"
            echo "  my-merged-prs.sh --from 2026-07-01 --to 2026-07-31"
            echo "  my-merged-prs.sh  # uses upstream/origin remote from current directory"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Resolve repo if not provided
if [[ -z "$REPO" ]]; then
    REMOTE_URL=$(git remote get-url upstream 2>/dev/null || git remote get-url origin 2>/dev/null || true)

    if [[ -z "$REMOTE_URL" ]]; then
        echo "Error: No --repo provided and could not detect repo from git remotes."
        echo "Run from inside a git repo, or pass --repo OWNER/REPO."
        exit 1
    fi

    REPO=$(echo "$REMOTE_URL" | sed -E 's#(\.git)$##' | sed -E 's#.*[:/]([^/]+/[^/]+)$#\1#')

    if [[ -z "$REPO" || "$REPO" == "$REMOTE_URL" ]]; then
        echo "Error: Could not parse OWNER/REPO from remote URL: $REMOTE_URL"
        exit 1
    fi
fi

# Derive GitHub base URL from gh CLI
GH_HOST=$(gh api /meta --hostname github.disney.com 2>/dev/null | jq -r '.url // empty' 2>/dev/null || true)
if [[ -z "$GH_HOST" ]]; then
    # Fallback: derive from remote or default
    if [[ -n "${REMOTE_URL:-}" ]]; then
        GH_HOST=$(echo "$REMOTE_URL" | sed -E 's#(https?://[^/]+).*#\1#')
    else
        GH_HOST="https://github.disney.com"
    fi
fi
PR_BASE_URL="${GH_HOST}/${REPO}/pull"

# Default output file
if [[ -z "$OUT_FILE" ]]; then
    OUT_FILE="./merged-prs-${FROM_DATE}-to-${TO_DATE}.md"
fi

echo "Searching merged PRs by @me in $REPO ($FROM_DATE → $TO_DATE)..."

# Fetch merged PRs via gh CLI
JSON=$(gh pr list \
    --repo "$REPO" \
    --author="@me" \
    --state=merged \
    --search="merged:${FROM_DATE}..${TO_DATE}" \
    --json number,title,mergedAt \
    --limit 100)

COUNT=$(echo "$JSON" | jq 'length')

if [[ "$COUNT" -eq 0 ]]; then
    echo "No merged PRs found in that range."
    exit 0
fi

# Build markdown content
{
    echo "# Merged PRs — $REPO"
    echo ""
    echo "> **Range:** $FROM_DATE → $TO_DATE | **Total:** $COUNT PR(s)"
    echo ""
    echo "| # | Date | PR | Ticket | Description |"
    echo "|---|------|-----|--------|-------------|"

    echo "$JSON" | jq -r 'sort_by(.mergedAt) | reverse | .[] | [.mergedAt, .number, .title] | @tsv' | \
        awk -F'\t' -v pr_base="$PR_BASE_URL" -v jira_base="$JIRA_BASE" '{
            split($1, dt, "T")
            date = dt[1]

            title = $3
            ticket = ""
            if (match(title, /[A-Z]+-[0-9]+/)) {
                ticket = substr(title, RSTART, RLENGTH)
            }

            desc = title
            gsub(/^[[:space:]]+/, "", desc)
            sub(/[A-Z]+-[0-9]+[[:space:]]*\|?[[:space:]]*/, "", desc)
            gsub(/^[[:space:]|]+/, "", desc)

            pr_link = "[#" $2 "](" pr_base "/" $2 ")"
            ticket_link = (ticket != "") ? "[" ticket "](" jira_base "/" ticket ")" : "—"

            NR_COUNT++
            printf "| %d | %s | %s | %s | %s |\n", NR_COUNT, date, pr_link, ticket_link, desc
        }'

    echo ""
    echo "---"
    echo ""
    echo "## Links"
    echo ""

    echo "$JSON" | jq -r 'sort_by(.mergedAt) | reverse | .[] | [.mergedAt, .number, .title] | @tsv' | \
        awk -F'\t' -v pr_base="$PR_BASE_URL" -v jira_base="$JIRA_BASE" '{
            title = $3
            ticket = ""
            if (match(title, /[A-Z]+-[0-9]+/)) {
                ticket = substr(title, RSTART, RLENGTH)
            }

            printf "- **PR #%s**: %s/%s\n", $2, pr_base, $2
            if (ticket != "") {
                printf "- **%s**: %s/%s\n", ticket, jira_base, ticket
            }
        }'

} > "$OUT_FILE"

echo ""
echo "Generated: $OUT_FILE"
echo ""
cat "$OUT_FILE"
