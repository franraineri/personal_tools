#!/bin/bash

# Script to review PRs automatically using kiro-cli or claude CLI
# Usage: ./review-pr.sh <PR_URL> [OPTIONS]
#
# PR_URL accepted formats:
#   https://github.disney.com/dcl-applications/dcl-cruise-101-spa/pull/3392/files
#   https://github.disney.com/dcl-applications/dcl-cruise-101-spa/pull/3392
#   /dcl-applications/dcl-cruise-101-spa/pull/3392
#   dcl-applications/dcl-cruise-101-spa/pull/3392
#
# Options:
#   --kiro              Use kiro-cli with dcl-dev agent (default)
#   --manual            Generate prompt file for manual review
#   --post=critical     Post only critical issues to the PR (with preview)
#   --post=all          Post critical + major issues to the PR (with preview)
#   --repo-type=TYPE    library or spa (auto-detected if omitted)
#
# Dependencies: gh, jq

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/../output/review-pr"

# ─── Shared helpers (logging, error handling). See utils.sh. ───
source "${SCRIPT_DIR}/utils.sh" || { echo "Missing utils.sh in ${SCRIPT_DIR}" >&2; exit 1; }
utils_enable_error_trap

# ─── Dependency check ───
for cmd in gh jq; do
  if ! command -v "$cmd" &>/dev/null; then
    die "Required command '$cmd' not found. Please install it."
  fi
done

# ─── Parse arguments ───
MODE="kiro"
PR_URL=""
REPO_TYPE=""
POST_LEVEL=""

for arg in "$@"; do
  case "$arg" in
    --kiro)            MODE="kiro" ;;
    --manual)          MODE="manual" ;;
    --post=critical)   POST_LEVEL="critical" ;;
    --post=all)        POST_LEVEL="all" ;;
    --post)            POST_LEVEL="all" ;;
    --repo-type=*)     REPO_TYPE="${arg#*=}" ;;
    *)
      if [ -z "$PR_URL" ]; then
        PR_URL="$arg"
      fi
      ;;
  esac
done

if [ -z "$PR_URL" ]; then
  echo "Error: You must provide the PR URL"
  echo ""
  echo "Usage: ./review-pr.sh <PR_URL> [OPTIONS]"
  echo ""
  echo "Examples:"
  echo "  ./review-pr.sh https://github.disney.com/dcl-applications/dcl-cruise-101-spa/pull/3392"
  echo "  ./review-pr.sh /dcl-applications/dcl-cruise-101-spa/pull/3392"
  echo "  ./review-pr.sh dcl-applications/dcl-cruise-101-spa/pull/3392 --post=critical"
  echo ""
  echo "Options:"
  echo "  --kiro             Use kiro-cli with dcl-dev agent (default)"
  echo "  --manual           Generate prompt for manual review"
  echo "  --post=critical    Post only critical issues to the PR (with preview)"
  echo "  --post=all         Post critical + major issues to the PR (with preview)"
  echo "  --repo-type=TYPE   library or spa (auto-detected from repo name)"
  exit 1
fi

# ─── Extract REPO and PR_NUMBER from URL ───
# 1. Strip protocol+host if present (e.g., https://github.disney.com/)
PR_PATH="${PR_URL#*://*/}"
# 2. Strip leading slash if present (e.g., /dcl-applications -> dcl-applications)
PR_PATH="${PR_PATH#/}"
# 3. Strip /files suffix
PR_PATH="${PR_PATH%%/files*}"
# 4. Strip trailing slash
PR_PATH="${PR_PATH%/}"

REPO=$(echo "$PR_PATH" | sed 's|/pull/[0-9]*$||')
PR_NUMBER=$(echo "$PR_PATH" | grep -oE '[0-9]+$' || true)

if [ -z "$REPO" ] || [ -z "$PR_NUMBER" ]; then
  echo "❌ Could not parse repo and PR number from: $PR_URL"
  echo "   Expected formats:"
  echo "   - https://github.disney.com/org/repo/pull/123"
  echo "   - /org/repo/pull/123"
  echo "   - org/repo/pull/123"
  exit 1
fi

echo "   Repo: $REPO | PR: #$PR_NUMBER"

# ─── Auto-detect repo type ───
if [ -z "$REPO_TYPE" ]; then
  case "$REPO" in
    *library*) REPO_TYPE="library" ;;
    *)         REPO_TYPE="spa" ;;
  esac
fi

# Use array for repo flag to avoid word-splitting issues
REPO_FLAG=(--repo "$REPO")

DIFF_FILE="$OUTPUT_DIR/pr_${PR_NUMBER}.diff"
PROMPT_FILE="$OUTPUT_DIR/pr_${PR_NUMBER}_prompt.txt"
REVIEW_FILE="$OUTPUT_DIR/pr_${PR_NUMBER}_review.md"

# ─── Fetch PR data ───
echo "📥 Fetching PR #$PR_NUMBER..."
mkdir -p "$OUTPUT_DIR"

# Note: Deliberately NOT hiding stderr here. If this fails, gh will print the exact reason.
if ! gh pr diff "$PR_NUMBER" "${REPO_FLAG[@]}" > "$DIFF_FILE"; then
  echo "❌ Error: Could not fetch PR diff."
  echo "💡 Tip: Ensure you are logged into the correct GitHub Enterprise host."
  echo "   Run: gh auth login --hostname github.disney.com"
  exit 1
fi

if [ ! -s "$DIFF_FILE" ]; then
  echo "❌ Error: PR diff is empty."
  rm -f "$DIFF_FILE"
  exit 1
fi

echo "   Diff: $(wc -l < "$DIFF_FILE" | tr -d ' ') lines"

# Fetch all PR metadata in a single gh call
PR_META=$(gh pr view "$PR_NUMBER" "${REPO_FLAG[@]}" \
  --json title,author,body,files 2>/dev/null || echo '{}')
PR_TITLE=$(echo "$PR_META" | jq -r '.title // "Unknown"')
PR_AUTHOR=$(echo "$PR_META" | jq -r '.author.login // "Unknown"')
PR_BODY=$(echo "$PR_META" | jq -r '.body // ""')
FILES=$(echo "$PR_META" | jq -r '.files[].path // empty')

echo "   PR: $PR_TITLE"
echo "   Author: $PR_AUTHOR"
echo "   Files: $(echo "$FILES" | wc -l | tr -d ' ')"

# ─── Fetch unresolved review comments ───
REPO_OWNER="${REPO%%/*}"
REPO_NAME="${REPO#*/}"
UNRESOLVED_COMMENTS=""
if [ -n "$REPO_OWNER" ] && [ -n "$REPO_NAME" ]; then
  UNRESOLVED_COMMENTS=$(gh api graphql -f query="
    query {
      repository(owner: \"$REPO_OWNER\", name: \"$REPO_NAME\") {
        pullRequest(number: $PR_NUMBER) {
          reviewThreads(first: 50) {
            nodes {
              isResolved
              path
              line
              comments(first: 1) {
                nodes { body author { login } }
              }
            }
          }
        }
      }
    }" --jq '
      .data.repository.pullRequest.reviewThreads.nodes[]
      | select(.isResolved == false)
      | "- \(.path):\(.line) — @\(.comments.nodes[0].author.login): \(.comments.nodes[0].body)"
    ' 2>/dev/null || true)
  if [ -n "$UNRESOLVED_COMMENTS" ]; then
    echo "   💬 Unresolved comments: $(echo "$UNRESOLVED_COMMENTS" | wc -l | tr -d ' ')"
  fi
fi

# ─── Categorize files ───
TS_FILES=$(echo "$FILES" | grep -E '\.ts$' | grep -v '\.spec\.ts$' || true)
SPEC_FILES=$(echo "$FILES" | grep -E '\.spec\.ts$' || true)
SCSS_FILES=$(echo "$FILES" | grep -E '\.scss$' || true)
HTML_FILES=$(echo "$FILES" | grep -E '\.html$' || true)
OTHER_FILES=$(echo "$FILES" | grep -vE '\.(ts|scss|html)$' || true)

# ─── Build the review prompt ───
cat > "$PROMPT_FILE" <<'PROMPT_HEADER'
You are reviewing a Pull Request for a DCL Angular 18 project.

## Conventions to enforce
- **Angular 18 API**: signal(), computed(), input(), output(), inject(), OnPush. NO @Input/@Output decorators, no v19+ APIs
- **Standalone** components required for new code
- **Type safety**: No blind casts — guard union types with runtime checks
- **Testing**: provideHttpClient()+provideHttpClientTesting(), setInput() for signal inputs
- **translate.instant() in computed()**: acceptable if commented
- **File structure**: separate .ts/.html/.scss/.constants.ts/.spec.ts. Constants in UPPER_CASE object. No magic numbers
- **A11y (WCAG AA)**: aria-label on interactive elements, aria-live for dynamic content, 4.5:1 contrast
- **Code style**: no empty lines between imports, blank line before return/if/const after other statements, thin lifecycle hooks

avoid annalysing minnor issues, totally skip them. 

PROMPT_HEADER

cat >> "$PROMPT_FILE" <<EOF

## PR Context
- PR #$PR_NUMBER — $PR_TITLE (by $PR_AUTHOR)
- Repo: $REPO ($REPO_TYPE)

### PR Description
$PR_BODY

### Modified files
**TS:** $(echo "$TS_FILES" | tr '\n' ', ')
**Tests:** $(echo "$SPEC_FILES" | tr '\n' ', ')
**SCSS:** $(echo "$SCSS_FILES" | tr '\n' ', ')
**HTML:** $(echo "$HTML_FILES" | tr '\n' ', ')
**Other:** $(echo "$OTHER_FILES" | tr '\n' ', ')
EOF

if [ -n "$UNRESOLVED_COMMENTS" ]; then
  cat >> "$PROMPT_FILE" <<EOF

## Existing Unresolved Comments (DO NOT repeat these — they are already flagged)
$UNRESOLVED_COMMENTS
EOF
fi

cat >> "$PROMPT_FILE" <<'PROMPT_INSTRUCTIONS'

## Output format

Be concise, summarysed. NO code blocks. Only brief text proposals.

### 2. Critical Issues (must fix)
a simple list. Each item: **file:name** **file:line** — what's wrong, and then very brief text proposal to fix.
Focus: structural improvments, runtime errors, type safety, security.

### 3. Major Issues (should fix)
Each item: **file:name** **file:line** — Focus: performance, deprecated APIs, missing tests.

---
RULES:
- Just append the output on the file, in format described
- Be specific with line numbers on each review. Add the last line number for a code-clock you are reviewing/commenting/suggesting
- Do NOT repeat issues already listed in "Existing Unresolved Comments"
- Do NOT include code snippets or code blocks — quick and concise text proposals only
- Do Not include special characters or characters drawing at all.
- Keep each issue to 1-2 sentences max
- write the comment fixes as a suggestion, like 'we could ...' 'it should be ... ?'
- avoid annalysing minnor issues, skip them. 
---

PROMPT_INSTRUCTIONS

cat >> "$PROMPT_FILE" <<EOF
## Diff

$(cat "$DIFF_FILE")
EOF

echo ""

# ─── Helpers ───
strip_ansi() {
  sed $'s/\x1b\[[0-9;]*[A-Za-z]//g' | sed 's/━//g' | sed '/^[[:space:]]*$/d' | cat -s
}

# ─── Execute review ───
case "$MODE" in
  kiro)
    if ! command -v kiro-cli &>/dev/null; then
      echo "❌ kiro-cli not found."
      exit 1
    fi
    echo "🤖 Generating review with kiro-cli (dcl-dev agent)..."
    echo ""
    kiro-cli chat \
      --agent dcl-dev \
      --no-interactive \
      -a \
      "$(cat "$PROMPT_FILE")" 2>/dev/null | strip_ansi > "$REVIEW_FILE"
    ;;

  claude)
    if ! command -v claude &>/dev/null; then
      echo "❌ Claude CLI not found. Install: npm install -g @anthropic-ai/claude-cli"
      exit 1
    fi
    echo "🤖 Generating review with Claude CLI..."
    echo ""
    claude --print "$(cat "$PROMPT_FILE")" 2>/dev/null | strip_ansi > "$REVIEW_FILE"
    ;;

  manual)
    echo "📄 Review prompt saved at: $PROMPT_FILE"
    echo "   Paste into your AI assistant, save response to: $REVIEW_FILE"
    echo ""
    read -rp "Press Enter when done..."
    ;;
esac

# ─── Validate output ───
if [ ! -s "$REVIEW_FILE" ]; then
  echo "❌ Review is empty. Prompt saved at: $PROMPT_FILE"
  exit 1
fi

echo "✅ Review generated: $REVIEW_FILE"

# Prepend repo metadata for post-review-comments.sh auto-detection
TEMP_REVIEW=$(mktemp)
echo "<!-- repo:$REPO -->" > "$TEMP_REVIEW"
cat "$REVIEW_FILE" >> "$TEMP_REVIEW"
mv "$TEMP_REVIEW" "$REVIEW_FILE"
rm -f "$DIFF_FILE" "$PROMPT_FILE"

# ─── Post review comments to PR ───
if [ -n "$POST_LEVEL" ]; then
  echo ""
  echo "📝 Extracting issues for PR comment (level: $POST_LEVEL)..."

  COMMENT_FILE="$OUTPUT_DIR/pr_${PR_NUMBER}_comment.md"

  if [ "$POST_LEVEL" = "critical" ]; then
    AWK_FILTER='/^### 2\. Critical/ { capture=1; next }'
  else
    AWK_FILTER='/^### 2\. Critical/ { capture=1; next }
    /^### 3\. Major/ { capture=1; next }'
  fi

  awk "$AWK_FILTER"'
    /^### [0-9]/ { capture=0 }
    capture && /^[0-9]+\./ {
      sub(/^[0-9]+\. /, "")
      n = split($0, w, " ")
      line = ""
      for (i = 1; i <= n && i <= 80; i++) line = line (i>1 ? " " : "") w[i]
      if (n > 80) line = line "..."
      print "- " line
    }
  ' "$REVIEW_FILE" > "$COMMENT_FILE"

  ISSUE_COUNT=$(grep -c '^- ' "$COMMENT_FILE" || true)
  if [ "$ISSUE_COUNT" -eq 0 ]; then
    echo "   No issues found at level '$POST_LEVEL' — nothing to post."
    rm -f "$COMMENT_FILE"
    exit 0
  fi

  echo ""
  echo "─── Comment Preview ($ISSUE_COUNT issues) ───"
  cat "$COMMENT_FILE"
  echo "───────────────────────────────────────────"
  echo ""

  read -rp "Post this comment to PR #$PR_NUMBER? (y/n): " CONFIRM
  case "$CONFIRM" in
    y|Y)
      if gh pr review "$PR_NUMBER" "${REPO_FLAG[@]}" --comment --body-file "$COMMENT_FILE"; then
        echo "✅ Comment posted to PR #$PR_NUMBER!"
        rm -f "$COMMENT_FILE"
      else
        echo "❌ Failed to post comment. Saved at: $COMMENT_FILE"
        exit 1
      fi
      ;;
    *)
      echo "⏭️  Skipped. Comment saved at: $COMMENT_FILE"
      ;;
  esac
fi