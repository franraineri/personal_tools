#!/bin/bash

# Script to review PRs automatically using kiro-cli or claude CLI
# Usage: ./review-pr.sh <PR_URL> [OPTIONS]
#
# PR_URL formats:
#   https://github.disney.com/dcl-applications/dcl-cruise-101-spa/pull/3392/files
#   https://github.disney.com/dcl-applications/dcl-cruise-101-spa/pull/3392
#   dcl-applications/dcl-cruise-101-spa/pull/3392
#
# Options:
#   --kiro     Use kiro-cli with dcl-dev agent (default)
#   --claude   Use Claude CLI
#   --manual   Generate prompt file for manual review
#   --repo-type <library|spa>  Specify project type for context (auto-detected if omitted)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/../output/review-pr"

# ─── Parse arguments ───
MODE="kiro"
PR_URL=""
REPO_TYPE=""

for arg in "$@"; do
  case $arg in
    --kiro)    MODE="kiro" ;;
    --claude)  MODE="claude" ;;
    --manual)  MODE="manual" ;;
    --repo-type=*) REPO_TYPE="${arg#*=}" ;;
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
  echo "  ./review-pr.sh dcl-applications/dcl-ui-global-components-library-v2/pull/386 --claude"
  echo ""
  echo "Options:"
  echo "  --kiro           Use kiro-cli with dcl-dev agent (default)"
  echo "  --claude         Use Claude CLI"
  echo "  --manual         Generate prompt for manual review"
  echo "  --repo-type=TYPE library or spa (auto-detected from repo name if omitted)"
  exit 1
fi

# ─── Extract REPO and PR_NUMBER from URL ───
# Strip protocol/host prefix and trailing segments after PR number
PR_PATH=$(echo "$PR_URL" | sed 's|.*://[^/]*/||' | sed 's|/files.*||' | sed 's|/$||')
# PR_PATH is now like: dcl-applications/dcl-cruise-101-spa/pull/3392
REPO=$(echo "$PR_PATH" | sed 's|/pull/[0-9]*$||')
PR_NUMBER=$(echo "$PR_PATH" | grep -oE '[0-9]+$')

if [ -z "$REPO" ] || [ -z "$PR_NUMBER" ]; then
  echo "❌ Could not parse repo and PR number from: $PR_URL"
  echo "   Expected format: org/repo/pull/NUMBER"
  exit 1
fi

echo "   Repo: $REPO | PR: #$PR_NUMBER"

# ─── Auto-detect repo type ───
if [ -z "$REPO_TYPE" ]; then
  if echo "$REPO" | grep -q "library"; then
    REPO_TYPE="library"
  else
    REPO_TYPE="spa"
  fi
fi

# ─── Build gh command ───
REPO_FLAG=""
if [ -n "$REPO" ]; then
  REPO_FLAG="--repo $REPO"
fi

DIFF_FILE="$OUTPUT_DIR/pr_${PR_NUMBER}.diff"
PROMPT_FILE="$OUTPUT_DIR/pr_${PR_NUMBER}_prompt.txt"
REVIEW_FILE="$OUTPUT_DIR/pr_${PR_NUMBER}_review.md"

# ─── Fetch PR data ───
echo "📥 Fetching changes from PR #$PR_NUMBER..."
mkdir -p "$OUTPUT_DIR"

if ! gh pr diff "$PR_NUMBER" $REPO_FLAG > "$DIFF_FILE" 2>/dev/null; then
  echo "❌ Error: Could not fetch PR diff. Check PR number and repo."
  exit 1
fi

DIFF_SIZE=$(wc -c < "$DIFF_FILE" | tr -d ' ')
if [ "$DIFF_SIZE" -eq 0 ]; then
  echo "❌ Error: PR diff is empty."
  rm -f "$DIFF_FILE"
  exit 1
fi

DIFF_LINES=$(wc -l < "$DIFF_FILE" | tr -d ' ')
echo "   Diff size: $DIFF_LINES lines"

# Get PR metadata
FILES=$(gh pr view "$PR_NUMBER" $REPO_FLAG --json files --jq '.files[].path' 2>/dev/null || echo "Could not fetch file list")
PR_TITLE=$(gh pr view "$PR_NUMBER" $REPO_FLAG --json title --jq '.title' 2>/dev/null || echo "Unknown")
PR_AUTHOR=$(gh pr view "$PR_NUMBER" $REPO_FLAG --json author --jq '.author.login' 2>/dev/null || echo "Unknown")
PR_BODY=$(gh pr view "$PR_NUMBER" $REPO_FLAG --json body --jq '.body' 2>/dev/null || echo "")

echo "   PR: $PR_TITLE"
echo "   Author: $PR_AUTHOR"
echo "   Files changed: $(echo "$FILES" | wc -l | tr -d ' ')"

# ─── Fetch unresolved review comments ───
REPO_OWNER=$(echo "$REPO" | cut -d'/' -f1)
REPO_NAME=$(echo "$REPO" | cut -d'/' -f2)
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
    }" --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | "- \(.path):\(.line) — @\(.comments.nodes[0].author.login): \(.comments.nodes[0].body)"' 2>/dev/null || true)
  if [ -n "$UNRESOLVED_COMMENTS" ]; then
    COMMENT_COUNT=$(echo "$UNRESOLVED_COMMENTS" | wc -l | tr -d ' ')
    echo "   💬 Unresolved comments: $COMMENT_COUNT"
  fi
fi

# ─── Categorize files for focused review ───
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
- **SCSS**: NO ::ng-deep (use CSS custom properties). NO @import (use @use). Define CSS vars on :host
- **Type safety**: No blind casts — guard union types with runtime checks
- **Testing**: provideHttpClient()+provideHttpClientTesting(), setInput() for signal inputs
- **translate.instant() in computed()**: acceptable if commented
- **File structure**: separate .ts/.html/.scss/.constants.ts/.spec.ts. Constants in UPPER_CASE object. No magic numbers
- **A11y (WCAG AA)**: aria-label on interactive elements, aria-live for dynamic content, 4.5:1 contrast
- **Code style**: no empty lines between imports, blank line before return/if/const after other statements, thin lifecycle hooks

PROMPT_HEADER

cat >> "$PROMPT_FILE" <<EOF

## PR Context
- PR #$PR_NUMBER — $PR_TITLE (by $PR_AUTHOR)
- Repo: $REPO ($REPO_TYPE)

### PR Description
$PR_BODY

### Modified files
**TS:** $(echo "$TS_FILES" | tr '\n' ', ' || echo "none")
**Tests:** $(echo "$SPEC_FILES" | tr '\n' ', ' || echo "none")
**SCSS:** $(echo "$SCSS_FILES" | tr '\n' ', ' || echo "none")
**HTML:** $(echo "$HTML_FILES" | tr '\n' ', ' || echo "none")
**Other:** $(echo "$OTHER_FILES" | tr '\n' ', ' || echo "none")
EOF

# Append unresolved comments if any
if [ -n "$UNRESOLVED_COMMENTS" ]; then
  cat >> "$PROMPT_FILE" <<EOF

## Existing Unresolved Comments (DO NOT repeat these — they are already flagged)
$UNRESOLVED_COMMENTS
EOF
fi

cat >> "$PROMPT_FILE" <<'PROMPT_INSTRUCTIONS'

## Output format

Be concise. NO code blocks. Only brief text proposals.

### 1. Summary
One short paragraph + verdict: ✅ APPROVED / ⚠️ NEEDS IMPROVEMENTS / ❌ REQUEST CHANGES

### 2. Critical Issues (must fix)
Numbered list. Each item: **file:line** — what's wrong → brief text proposal to fix.
Focus: runtime errors, type safety, ::ng-deep, security.

### 3. Major Issues (should fix)
Same format. Focus: performance, a11y, deprecated APIs, missing tests.

### 4. Minor Issues
Bullet list with file references. One line each.

### 5. Tables

#### Angular Best Practices
| Practice | ✅/❌ |

#### Accessibility
| Aspect | ✅/⚠️/❌ | Note |

### 6. File Verdicts
| File | ✅/⚠️/❌ | Key Issue |

---
RULES:
- Be specific with line numbers
- Do NOT repeat issues already listed in "Existing Unresolved Comments"
- Do NOT include code snippets or code blocks — text proposals only
- Keep each issue to 1-2 sentences max
---

PROMPT_INSTRUCTIONS

cat >> "$PROMPT_FILE" <<EOF
## Diff

$(cat "$DIFF_FILE")
EOF

echo ""

# ─── Strip ANSI escape codes, unicode box-drawing lines, and excess blank lines ───
strip_ansi() {
  sed $'s/\x1b\[[0-9;]*[A-Za-z]//g' | sed 's/━//g' | sed '/^[[:space:]]*$/d' | cat -s
}

# ─── Execute review based on mode ───

case "$MODE" in
  kiro)
    if ! command -v kiro-cli &> /dev/null; then
      echo "❌ kiro-cli not found. Install it or use --claude/--manual mode."
      exit 1
    fi
    echo "🤖 Generating review with kiro-cli (dcl-dev agent)..."
    echo ""
    kiro-cli chat \
      --agent dcl-dev \
      --no-interactive \
      -a \
      "$(cat "$PROMPT_FILE")" 2>/dev/null | strip_ansi > "$REVIEW_FILE"

    if [ -s "$REVIEW_FILE" ]; then
      echo "✅ Review generated!"
      echo "   📄 $REVIEW_FILE"
      rm -f "$DIFF_FILE" "$PROMPT_FILE"
    else
      echo "❌ kiro-cli produced empty output. Prompt saved at: $PROMPT_FILE"
      exit 1
    fi
    ;;

  claude)
    if ! command -v claude &> /dev/null; then
      echo "❌ Claude CLI not found. Install: npm install -g @anthropic-ai/claude-cli"
      exit 1
    fi
    echo "🤖 Generating review with Claude CLI..."
    echo ""
    if claude --print "$(cat "$PROMPT_FILE")" 2>/dev/null | strip_ansi > "$REVIEW_FILE"; then
      echo "✅ Review generated!"
      echo "   📄 $REVIEW_FILE"
      rm -f "$DIFF_FILE" "$PROMPT_FILE"
    else
      echo "❌ Claude CLI failed. Prompt saved at: $PROMPT_FILE"
      exit 1
    fi
    ;;

  manual)
    echo "✅ Review prompt prepared at:"
    echo "   $PROMPT_FILE"
    echo ""
    echo "Next steps:"
    echo "  1. Copy content from the prompt file"
    echo "  2. Paste into your AI assistant"
    echo "  3. Save response to: $REVIEW_FILE"
    echo ""
    read -p "Press Enter when done..."
    if [ -f "$REVIEW_FILE" ]; then
      echo "✅ Review found! Cleaning up..."
      rm -f "$DIFF_FILE" "$PROMPT_FILE"
      echo "   📄 $REVIEW_FILE"
    else
      echo "⚠️  Review file not found. Keeping temp files."
    fi
    ;;
esac
