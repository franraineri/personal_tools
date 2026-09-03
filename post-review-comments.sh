#!/bin/bash

# Script to parse review-pr.sh output and post comments on the PR
# Usage: ./post-review-comments.sh <PR_NUMBER> --inline|--file [OPTIONS]
#
# The review file and repo are auto-resolved from the PR number:
#   Review file: ~/output/review-pr/pr_<NUMBER>_review.md
#   Repo: auto-detected from <!-- repo:org/name --> metadata in review file
#
# Modes (required):
#   --inline            Post each comment on the specific line in the diff
#   --file              Post all comments anchored to the file (not inline)
#
# Options:
#   --level=critical    Post only critical issues (section 2)
#   --level=all         Post critical + major issues (sections 2 & 3) [default]
#   --dry-run           Parse and show what would be posted without posting
#   --event=COMMENT     Review event: COMMENT (default), REQUEST_CHANGES, APPROVE
#
# Dependencies: gh, jq
# Compatible with: bash 3.2+ (macOS default)

set -euo pipefail

# ─── Config — precedence: env var > built-in default. ───
# A Koda executor (or `koda project`) can export these so the code_review_agent
# runs against any GitHub host / output location without editing the script:
#   REVIEW_GH_HOST    → GH_HOST     (default: github.disney.com)
#   REVIEW_OUTPUT_DIR → OUTPUT_DIR  (default: ~/output/review-pr)
OUTPUT_DIR="${REVIEW_OUTPUT_DIR:-$HOME/output/review-pr}"
GH_HOST="${REVIEW_GH_HOST:-github.disney.com}"

# ─── Dependency check ───
for cmd in gh jq; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "Error: Required command '$cmd' not found. Please install it."
    exit 1
  fi
done

# ─── Parse arguments ───
PR_NUMBER=""
LEVEL="all"
DRY_RUN=false
EVENT="COMMENT"
POST_MODE=""

for arg in "$@"; do
  case "$arg" in
    --inline)          POST_MODE="inline" ;;
    --file)            POST_MODE="file" ;;
    --level=critical)  LEVEL="critical" ;;
    --level=all)       LEVEL="all" ;;
    --dry-run)         DRY_RUN=true ;;
    --event=*)         EVENT="${arg#*=}" ;;
    *)
      if [ -z "$PR_NUMBER" ]; then
        PR_NUMBER="$arg"
      fi
      ;;
  esac
done

if [ -z "$PR_NUMBER" ] || [ -z "$POST_MODE" ]; then
  echo "Usage: ./post-review-comments.sh <PR_NUMBER> --inline|--file [OPTIONS]"
  echo ""
  echo "Modes (required):"
  echo "  --inline            Post each comment on the specific line in the diff"
  echo "  --file              Post all comments grouped per file (file-level)"
  echo ""
  echo "Options:"
  echo "  --level=critical    Post only critical issues"
  echo "  --level=all         Post critical + major issues (default)"
  echo "  --dry-run           Show parsed comments without posting"
  echo "  --event=EVENT       COMMENT | REQUEST_CHANGES | APPROVE"
  echo ""
  echo "Examples:"
  echo "  ./post-review-comments.sh 3447 --file"
  echo "  ./post-review-comments.sh 3447 --inline --level=critical"
  echo "  ./post-review-comments.sh 3447 --file --dry-run"
  exit 1
fi

# ─── Resolve review file ───
REVIEW_FILE="${OUTPUT_DIR}/pr_${PR_NUMBER}_review.md"

if [ ! -f "$REVIEW_FILE" ]; then
  echo "Error: Review file not found: $REVIEW_FILE"
  echo "Run review-pr.sh first to generate it."
  exit 1
fi

# ─── Auto-detect repo from metadata ───
REPO=$(head -1 "$REVIEW_FILE" | sed -nE 's/^<!-- repo:(.+) -->$/\1/p')

if [ -z "$REPO" ]; then
  echo "Error: Could not detect repo from review file metadata."
  echo "Re-run review-pr.sh to regenerate the review with metadata."
  exit 1
fi

PR_URL="https://${GH_HOST}/${REPO}/pull/${PR_NUMBER}"

echo "PR: #$PR_NUMBER | Repo: $REPO"
echo "Review: $REVIEW_FILE"
echo "Mode: $POST_MODE | Level: $LEVEL | Event: $EVENT"
echo ""

# ─── Get the latest commit SHA ───
REPO_FLAG=(--repo "$REPO")
COMMIT_SHA=$(gh pr view "$PR_NUMBER" "${REPO_FLAG[@]}" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)

if [ -z "$COMMIT_SHA" ]; then
  echo "Error: Could not fetch latest commit SHA for PR #$PR_NUMBER"
  echo "Tip: Ensure you are logged into the correct GitHub host."
  echo "  gh auth login --hostname $GH_HOST"
  exit 1
fi
echo "Commit: ${COMMIT_SHA:0:8}"

# ─── Fetch diff (needed for path resolution and --inline mode) ───
DIFF_FILE=$(mktemp)
trap "rm -f '$DIFF_FILE'" EXIT
if ! gh pr diff "$PR_NUMBER" "${REPO_FLAG[@]}" > "$DIFF_FILE" 2>/dev/null; then
  echo "Error: Could not fetch PR diff"
  exit 1
fi
if [ ! -s "$DIFF_FILE" ]; then
  echo "Error: PR diff is empty"
  exit 1
fi

# ─── Parse the review file to extract issues ───
parse_issues_to_file() {
  local file="$1"
  local sections="$2"
  local outfile="$3"

  local in_section=false
  local current_section=""
  local pending_filename=""
  local pending_line=""
  local pending_section=""
  local pending_body=""

  > "$outfile"

  { cat "$file"; echo; echo "### 999. END"; } | while IFS= read -r line; do
    if echo "$line" | grep -qE '^(>+ *)?### 2\. Critical'; then
      if [ -n "$pending_filename" ] && [ -n "$pending_body" ]; then
        echo "${pending_filename}|${pending_line:-0}|${pending_section}|${pending_body}" >> "$outfile"
      fi
      pending_filename=""; pending_line=""; pending_section=""; pending_body=""
      current_section="critical"
      in_section=true
      continue
    elif echo "$line" | grep -qE '^(>+ *)?### 3\. Major'; then
      if [ -n "$pending_filename" ] && [ -n "$pending_body" ]; then
        echo "${pending_filename}|${pending_line:-0}|${pending_section}|${pending_body}" >> "$outfile"
      fi
      pending_filename=""; pending_line=""; pending_section=""; pending_body=""
      current_section="major"
      if [ "$sections" = "critical" ]; then in_section=false; else in_section=true; fi
      continue
    elif echo "$line" | grep -qE '^(>+ *)?### [0-9]'; then
      if [ -n "$pending_filename" ] && [ -n "$pending_body" ]; then
        echo "${pending_filename}|${pending_line:-0}|${pending_section}|${pending_body}" >> "$outfile"
      fi
      pending_filename=""; pending_line=""; pending_section=""; pending_body=""
      in_section=false
      continue
    fi

    if [ "$in_section" = true ]; then
      local is_new_issue=false
      local file_ref=""
      local body_text=""

      if echo "$line" | grep -qE '^\- \*\*[^*]+\*\*'; then
        is_new_issue=true
        file_ref=$(echo "$line" | sed -E 's/^- \*\*([^*]+)\*\*.*/\1/')
        body_text=$(echo "$line" | sed -E 's/^- \*\*[^*]+\*\*[[:space:]]*[—–-][[:space:]]*//')
      elif echo "$line" | grep -qE '^[0-9]+\. [^[:space:]]+:[0-9~]'; then
        is_new_issue=true
        file_ref=$(echo "$line" | sed -E 's/^[0-9]+\. ([^[:space:]]+)[[:space:]]*[—–-].*/\1/')
        body_text=$(echo "$line" | sed -E 's/^[0-9]+\. [^[:space:]]+[[:space:]]*[—–-][[:space:]]*//')
      fi

      if [ "$is_new_issue" = true ]; then
        if [ -n "$pending_filename" ] && [ -n "$pending_body" ]; then
          echo "${pending_filename}|${pending_line:-0}|${pending_section}|${pending_body}" >> "$outfile"
        fi
        pending_filename=$(echo "$file_ref" | sed -E "s|:.*$||" | sed -E "s| .*||")
        # If range (e.g. 178-183 or 178–183), take the LAST line number
        pending_line=$(echo "$file_ref" | sed -E 's/^[^:]+://' | sed -E 's/^~//' | sed -E 's/^[0-9]+[–-]//' | grep -oE '^[0-9]+' || true)
        pending_section="$current_section"
        pending_body="$body_text"
      elif [ -n "$pending_filename" ] && [ -n "$line" ]; then
        pending_body="${pending_body} ${line}"
      fi
    fi
  done
}

# ─── Calculate diff position for --inline mode ───
get_diff_position() {
  local target_file="$1"
  local target_line="$2"

  awk -v tfile="$target_file" -v tline="$target_line" '
    /^diff --git/ { in_file=0; pos=0 }
    /^--- / { next }
    /^\+\+\+ b\// {
      current_file = substr($0, 7)
      if (current_file == tfile) { in_file=1; pos=0 }
      else { in_file=0 }
      next
    }
    in_file && /^@@/ {
      pos++
      s = $0
      sub(/^@@ -[0-9,]+ \+/, "", s)
      sub(/ @@.*/, "", s)
      sub(/,.*/, "", s)
      file_line = int(s)
      next
    }
    in_file {
      pos++
      c = substr($0,1,1)
      if (c == "-") {
        # deleted line — does not advance file_line
      } else if (c == "+") {
        if (file_line == int(tline)) { print pos; exit }
        file_line++
      } else {
        if (file_line == int(tline)) { print pos; exit }
        file_line++
      }
    }
  ' "$DIFF_FILE"
}

# ─── Resolve short filename to full path from diff ───
resolve_full_path() {
  local short_name="$1"
  if [[ "$short_name" == */* ]]; then
    echo "$short_name"
    return
  fi
  local match
  match=$(grep "^+++ b/" "$DIFF_FILE" | sed "s|^+++ b/||" | grep "/${short_name}$" | head -1)
  if [ -n "$match" ]; then
    echo "$match"
  else
    match=$(grep "^+++ b/" "$DIFF_FILE" | sed "s|^+++ b/||" | grep "${short_name}$" | head -1)
    if [ -n "$match" ]; then
      echo "$match"
    else
      echo "$short_name"
    fi
  fi
}

# ─── Parse issues ───
echo "Parsing review..."
ISSUES_FILE=$(mktemp)
trap "rm -f '$DIFF_FILE' '$ISSUES_FILE'" EXIT

parse_issues_to_file "$REVIEW_FILE" "$LEVEL" "$ISSUES_FILE"

if [ ! -s "$ISSUES_FILE" ]; then
  echo "No issues found at level '$LEVEL'."
  rm -f "$ISSUES_FILE"
  exit 0
fi

ISSUE_COUNT=$(wc -l < "$ISSUES_FILE" | tr -d ' ')
echo "Found $ISSUE_COUNT issues."

# ─── Resolve short filenames to full paths ───
RESOLVED_FILE=$(mktemp)
while IFS="|" read -r fname lnum sev bod; do
  resolved=$(resolve_full_path "$fname")
  echo "${resolved}|${lnum}|${sev}|${bod}"
done < "$ISSUES_FILE" > "$RESOLVED_FILE"
mv "$RESOLVED_FILE" "$ISSUES_FILE"
echo ""

# ─── Build comments JSON based on mode ───
COMMENTS_JSON="[]"
ORPHAN_COMMENTS=""

if [ "$POST_MODE" = "inline" ]; then
  SKIPPED=0
  NOT_IN_DIFF=0
  ORPHAN_COMMENTS=""
  while IFS='|' read -r filename line_num severity body; do
    [ -z "$filename" ] && continue
    [ "$line_num" = "0" ] && { SKIPPED=$((SKIPPED + 1)); ORPHAN_COMMENTS="${ORPHAN_COMMENTS}\n- **${filename}** — ${body}"; continue; }

    # Check if file is even in the diff
    FILE_IN_DIFF=$(grep -c "^+++ b/${filename}$" "$DIFF_FILE" || true)
    if [ "$FILE_IN_DIFF" = "0" ]; then
      NOT_IN_DIFF=$((NOT_IN_DIFF + 1))
      ORPHAN_COMMENTS="${ORPHAN_COMMENTS}\n- **${filename}:${line_num}** — ${body}"
      continue
    fi

    POSITION=$(get_diff_position "$filename" "$line_num")

    if [ -n "$POSITION" ] && [ "$POSITION" -gt 0 ] 2>/dev/null; then
      COMMENTS_JSON=$(echo "$COMMENTS_JSON" | jq \
        --arg path "$filename" \
        --argjson position "$POSITION" \
        --arg body "$body" \
        '. + [{"path": $path, "position": $position, "body": $body}]')
    else
      # Line not in diff hunk — cannot post inline, collect as orphan
      NOT_IN_DIFF=$((NOT_IN_DIFF + 1))
      ORPHAN_COMMENTS="${ORPHAN_COMMENTS}\n- **${filename}:${line_num}** — ${body}"
    fi
  done < "$ISSUES_FILE"

  if [ "$SKIPPED" -gt 0 ]; then
    echo "Skipped $SKIPPED issues without line numbers."
  fi
  if [ "$NOT_IN_DIFF" -gt 0 ]; then
    echo "⚠️  $NOT_IN_DIFF issues could not be posted inline (line not in diff)."
    echo "   These will be included in the review body instead."
  fi

elif [ "$POST_MODE" = "file" ]; then
  # Use limit(3; split) pattern: split into at most 4 fields, body gets the remainder
  COMMENTS_JSON=$(jq -R -s '
    split("\n") | map(select(length > 0)) |
    map(
      # Split only on the first 3 pipes to protect | in body text
      capture("^(?<file>[^|]*)[|](?<line>[^|]*)[|](?<severity>[^|]*)[|](?<body>.*)$")
    ) |
    group_by(.file) |
    map({
      path: .[0].file,
      subject_type: "file",
      body: (if length == 1 then .[0].body else map("- " + .body) | join("\n") end)
    })
  ' < "$ISSUES_FILE")
fi

COMMENT_COUNT=$(echo "$COMMENTS_JSON" | jq 'length')

if [ "$COMMENT_COUNT" -eq 0 ] && [ -z "$ORPHAN_COMMENTS" ]; then
  echo "No comments could be mapped. Nothing to post."
  exit 0
fi

if [ "$COMMENT_COUNT" -eq 0 ] && [ -n "$ORPHAN_COMMENTS" ]; then
  echo "No inline comments possible — all issues are outside the diff."
  echo "They will be posted as a general review comment (no inline annotations)."
fi

# ─── Preview ───
echo ""
echo "============================================"
echo " COMMENT PREVIEW ($COMMENT_COUNT comments)"
echo "============================================"
echo ""

if [ "$POST_MODE" = "inline" ]; then
  echo "$COMMENTS_JSON" | jq -r '.[] | "  [\(.path):\(.line // .position)]", "  \(.body)", ""'
elif [ "$POST_MODE" = "file" ]; then
  echo "$COMMENTS_JSON" | jq -r '.[] | "  [\(.path)]", .body, ""'
fi

echo "============================================"
echo ""

if [ "$DRY_RUN" = true ]; then
  echo "Dry run complete. No comments posted."
  echo "PR: $PR_URL"
  rm -f "$ISSUES_FILE"
  exit 0
fi

# ─── Confirm before posting ───
TOTAL_TO_POST=$COMMENT_COUNT
if [ -n "$ORPHAN_COMMENTS" ]; then
  TOTAL_TO_POST="$COMMENT_COUNT inline + orphans in body"
fi
read -rp "Post ${TOTAL_TO_POST} to PR #$PR_NUMBER? (y/n): " CONFIRM
case "$CONFIRM" in
  y|Y) ;;
  *)
    echo "Skipped. Nothing posted."
    exit 0
    ;;
esac

# ─── Post comments ───
echo ""
echo "Posting comments to PR #$PR_NUMBER..."

if [ "$POST_MODE" = "file" ]; then
  # FILE MODE: post each file comment individually via pulls/comments endpoint
  # (GH Enterprise 3.19 doesn't support subject_type on the batch reviews endpoint)
  POSTED=0
  FAILED=0
  COMMENT_ENDPOINT="repos/${REPO}/pulls/${PR_NUMBER}/comments"

  for row in $(echo "$COMMENTS_JSON" | jq -r '.[] | @base64'); do
    decoded=$(echo "$row" | base64 -D 2>/dev/null || echo "$row" | base64 --decode)
    c_path=$(echo "$decoded" | jq -r '.path')
    c_body=$(echo "$decoded" | jq -r '.body')

    POST_RESULT=0
    RESPONSE=$(gh api \
      --hostname "$GH_HOST" \
      --method POST \
      "$COMMENT_ENDPOINT" \
      -f body="$c_body" \
      -f path="$c_path" \
      -f subject_type="file" \
      -f commit_id="$COMMIT_SHA" 2>&1) || POST_RESULT=$?

    if [ "$POST_RESULT" -eq 0 ]; then
      POSTED=$((POSTED + 1))
    else
      FAILED=$((FAILED + 1))
      echo "  Failed: $c_path"
      echo "  $RESPONSE" | head -2
    fi
  done

  if [ "$FAILED" -eq 0 ]; then
    echo "Posted $POSTED file-level comments to PR #$PR_NUMBER"
  else
    echo "Posted $POSTED comments, $FAILED failed."
  fi

elif [ "$POST_MODE" = "inline" ]; then
  # INLINE MODE: batch via reviews endpoint
  SUMMARY=$(awk '/^### 1\. Summary/{found=1; next} /^### [0-9]/{found=0} found' "$REVIEW_FILE" | head -5 | tr '\n' ' ')

  # Build review body — include orphan comments that couldn't be posted inline
  REVIEW_BODY="LGTM."
#   if [ -n "$ORPHAN_COMMENTS" ]; then
#     REVIEW_BODY="Code review comments.

# **Issues outside the diff (could not be posted inline):**
# $(echo -e "$ORPHAN_COMMENTS")"
  fi

  API_ENDPOINT="repos/${REPO}/pulls/${PR_NUMBER}/reviews"

  REQUEST_BODY=$(jq -n \
    --arg commit_id "$COMMIT_SHA" \
    --arg body "$REVIEW_BODY" \
    --arg event "$EVENT" \
    --argjson comments "$COMMENTS_JSON" \
    '{
      commit_id: $commit_id,
      body: $body,
      event: $event
    } + (if ($comments | length) > 0 then {comments: $comments} else {} end)')

  POST_RESULT=0
  RESPONSE=$(echo "$REQUEST_BODY" | gh api \
    --hostname "$GH_HOST" \
    --method POST \
    "$API_ENDPOINT" \
    --input - 2>&1) || POST_RESULT=$?

  if [ "$POST_RESULT" -ne 0 ]; then
    echo "Error: Failed to post review (exit code: $POST_RESULT)"
    echo "$RESPONSE"
    echo ""
    echo "Tip: Some comments may fail if the line is not part of the diff."
    echo "Try with --dry-run to inspect the payload, or use --level=critical."
    echo ""
    echo "PR: $PR_URL"
    exit 1
  fi

  REVIEW_ID=$(echo "$RESPONSE" | jq -r '.id // empty')
  if [ -n "$REVIEW_ID" ]; then
    echo "Review posted. $COMMENT_COUNT inline comments added to PR #$PR_NUMBER"
  else
    echo "Response received but could not confirm review ID."
    echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"
  fi


echo "PR: $PR_URL"

# Cleanup
rm -f "$ISSUES_FILE"
