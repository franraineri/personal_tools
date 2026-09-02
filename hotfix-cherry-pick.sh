#!/bin/zsh
#
# hotfix-cherry-pick.sh — Apply a merged PR as a hotfix onto a release branch
# of dcl-cruise-101-spa (or any repo), then open a hotfix PR against upstream.
#
# Given a Jira ticket and a PR number, this reproduces the manual hotfix flow:
#   1. Pre-checks (VPN/host, gh, clean tooling)
#   2. Stash any uncommitted work (restored at the end)
#   3. Sync: checkout develop, fetch --all, create local <release> from upstream
#   4. Create the hotfix branch
#   5. Resolve the PR's commit SHA(s) via the GitHub API (gh)
#   6. Auto-drop commits already applied on <release> (would cherry-pick empty)
#   7. Cherry-pick (auto -m 1 when the SHA is a merge commit)
#   8. Test gate (via test-summary.sh) — skippable with --skip-tests
#   9. Push and open a PR targeting upstream/<release> (reuses an existing open
#      PR for the branch if there is one), then open the new PR in the browser
#      and print a summary (branch, commits, tickets, PR URL)
#
# NOT automated here (these were done by the AI in the original session, not by
# a script): fetching Jira context via MCP, "manually verifying" flaky tests,
# and context-aware conflict resolution. On conflict the script pauses and asks
# the user to resolve, matching the original workflow's specification.
#
# Usage:
#   # PR mode (resolve commits from a single merged PR):
#   ./hotfix-cherry-pick.sh --ticket MERLIN-4953 --pr 3743 --release release-2.4.0
#   ./hotfix-cherry-pick.sh -t MERLIN-4953 -p 3743 -r release-2.4.0 [options]
#
#   # SHA mode (cherry-pick explicit commits, one pick per SHA, -m 1 auto for merges):
#   ./hotfix-cherry-pick.sh -r release-2.4.0 \
#       --sha a736bbc... --sha a187691... --sha 0c154ff... --sha 7d0e5cc... \
#       --ticket MERLIN-4429 --ticket MERLIN-4865 --ticket MERLIN-4892 --ticket MERLIN-4953
#
# Options:
#
#   REQUIRED
#   -r, --release <BRANCH>  Release branch to hotfix (e.g. release-2.4.0).
#
#   MODE — provide EXACTLY ONE of these (PR mode vs SHA mode; not both):
#   -p, --pr <NUMBER>       [PR mode]  Source PR number; its commits are resolved
#                                      automatically via the GitHub API.
#   -s, --sha <SHA>         [SHA mode] Explicit commit SHA to cherry-pick. Repeatable —
#                                      pass once per commit; picked in the order given.
#       --shas <A,B,C>      [SHA mode] Comma-separated SHAs (same as repeating --sha).
#
#   TICKET
#   -t, --ticket <ID>       Jira ticket ID (e.g. MERLIN-4953). Repeatable. OPTIONAL
#                           in every mode: if omitted, tickets are inferred from the
#                           commit subjects, and if none can be inferred the flow
#                           still completes — the PR just omits ticket descriptions.
#
#   OPTIONAL
#   --repo <owner/name>     Upstream repo               (default: auto-detected from upstream remote)
#   --repo-dir <PATH>       Local repo working directory (default: current dir)
#   --branch <NAME>         Hotfix branch name          (default: hotfix/<release>-updates;
#                                                        push-existing default: current branch)
#   --jira-context <TEXT>   Text pasted verbatim into the PR body
#   --push-existing         Operate on the CURRENT branch: skip sync/branch-create/
#                           cherry-pick and only run the test gate, push, and open the
#                           PR. Use when the branch is already prepared (e.g. conflicts
#                           resolved manually). Given --sha values only populate the body.
#   --skip-preflight        Skip the pre-flight conflict simulation (proceed straight
#                           to the real cherry-pick; conflicts handled inline)
#   --skip-tests            Skip the script's test gate (does NOT skip the git pre-push hook)
#   --no-verify             Pass --no-verify to git push (skips the pre-push hook)
#   --no-pr                 Push the hotfix branch but do NOT open a PR
#   --dry-run               Print what would happen; make no network/branch changes
#   -h, --help              Show this help
#
# Dependencies: git, gh, jq, dig
#

set -e

# This script relies on zsh-specific features (array flags ${(@f)}, ${(@s:,:)},
# 1-based arrays). Refuse to run under bash/sh with a clear message rather than
# failing cryptically later.
if [[ -z "${ZSH_VERSION:-}" ]]; then
    echo "This script must be run with zsh (not bash/sh)." >&2
    echo "  Run:  ./hotfix-cherry-pick.sh ...   or   zsh hotfix-cherry-pick.sh ..." >&2
    exit 1
fi

# Absolute path to this script (for --help rendering) and its directory
# (to locate sibling tools like utils.sh and test-summary.sh).
SCRIPT_PATH="${0:A}"
SCRIPT_DIR="${0:A:h}"
TEST_SUMMARY="${SCRIPT_DIR}/test-summary.sh"

# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers: logging, VPN check, cherry-pick safety (pre-flight, rerere,
# known-conflict auto-resolution). See utils.sh.
# ─────────────────────────────────────────────────────────────────────────────
UTILS_SH="${SCRIPT_DIR}/utils.sh"
[[ -f "$UTILS_SH" ]] || { echo "Missing ${UTILS_SH}" >&2; exit 1; }
source "$UTILS_SH"

# ─────────────────────────────────────────────────────────────────────────────
# Config (edit here if hosts/branches change)
# ─────────────────────────────────────────────────────────────────────────────
GIT_HOST="github.disney.com"
DEFAULT_BASE_BRANCH="develop"    # branch to sync before creating the release branch
UPSTREAM_REMOTE="upstream"       # remote that holds the canonical release branch
ORIGIN_REMOTE="origin"           # remote to push the hotfix branch to (your fork)

# ─────────────────────────────────────────────────────────────────────────────
# Argument parsing
# ─────────────────────────────────────────────────────────────────────────────
TICKETS=()          # one or more Jira ticket IDs (repeatable)
PR_NUMBER=""
EXPLICIT_SHAS=()    # SHA mode: explicit commit SHAs to cherry-pick, in order
SHA_MODE=false
RELEASE_BRANCH=""
REPO=""
REPO_DIR="$(pwd)"
HOTFIX_BRANCH=""
JIRA_CONTEXT=""
SKIP_TESTS=false
PUSH_NO_VERIFY=false
DRY_RUN=false
NO_PR=false
PUSH_EXISTING=false
SKIP_PREFLIGHT=false

usage() {
    # Print the leading comment block (usage/options) with the leading "# " stripped.
    sed -n '2,68p' "$SCRIPT_PATH" | sed 's/^#\{1,\} \{0,1\}//; s/^#$//'
    exit "${1:-0}"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -t|--ticket)       TICKETS+=("$2"); shift 2 ;;
            -p|--pr)           PR_NUMBER="$2"; shift 2 ;;
            -s|--sha)          EXPLICIT_SHAS+=("$2"); shift 2 ;;
            --shas)            EXPLICIT_SHAS+=("${(@s:,:)2}"); shift 2 ;;
            -r|--release)      RELEASE_BRANCH="$2"; shift 2 ;;
            --repo)            REPO="$2"; shift 2 ;;
            --repo-dir)        REPO_DIR="$2"; shift 2 ;;
            --branch)          HOTFIX_BRANCH="$2"; shift 2 ;;
            --jira-context)    JIRA_CONTEXT="$2"; shift 2 ;;
            --push-existing)   PUSH_EXISTING=true; shift ;;
            --skip-preflight)  SKIP_PREFLIGHT=true; shift ;;
            --skip-tests)      SKIP_TESTS=true; shift ;;
            --no-verify)       PUSH_NO_VERIFY=true; shift ;;
            --no-pr)           NO_PR=true; shift ;;
            --dry-run)         DRY_RUN=true; shift ;;
            -h|--help)         usage 0 ;;
            *)                 die "Unknown argument: $1 (use --help)" ;;
        esac
    done

    [[ -n "$RELEASE_BRANCH" ]] || die "Missing --release (e.g. release-2.4.0). See --help."

    # Determine mode: SHA mode (explicit commits) vs PR mode (resolve from one PR).
    if [[ ${#EXPLICIT_SHAS[@]} -gt 0 ]]; then
        SHA_MODE=true
        [[ -z "$PR_NUMBER" ]] || die "Use EITHER --pr OR --sha/--shas, not both. See --help."
        # Validate each SHA is a plausible hex object name (7-40 hex chars).
        local sha
        for sha in "${EXPLICIT_SHAS[@]}"; do
            [[ "$sha" =~ ^[0-9a-fA-F]{7,40}$ ]] || die "Invalid SHA: '$sha' (expected 7-40 hex chars)."
        done
    elif [[ "$PUSH_EXISTING" == true ]]; then
        # push-existing with no SHAs and no PR: PR body derived from branch commits only.
        SHA_MODE=false
    else
        SHA_MODE=false
        [[ -n "$PR_NUMBER" ]] || die "Provide --pr (PR mode) or --sha/--shas (SHA mode). See --help."
        [[ "$PR_NUMBER" =~ ^[0-9]+$ ]] || die "--pr must be a number, got: $PR_NUMBER"
        # Ticket is OPTIONAL: if none is given, the flow continues and the PR
        # simply omits ticket descriptions (inferred from commits when possible).
        [[ ${#TICKETS[@]} -ge 1 ]] || log_info "No --ticket given; continuing without ticket descriptions."
    fi

    # Branch name: in push-existing mode default to the CURRENT branch (we don't
    # create one); otherwise derive the default from the release branch.
    if [[ -z "$HOTFIX_BRANCH" ]]; then
        if [[ "$PUSH_EXISTING" == true ]]; then
            HOTFIX_BRANCH=$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)
            [[ -n "$HOTFIX_BRANCH" && "$HOTFIX_BRANCH" != "HEAD" ]] \
                || die "--push-existing: cannot determine current branch (detached HEAD?). Pass --branch."
        else
            HOTFIX_BRANCH="hotfix/${RELEASE_BRANCH}-updates"
        fi
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Pre-checks
# ─────────────────────────────────────────────────────────────────────────────
check_dependencies() {
    local missing=()
    for cmd in git gh jq dig; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    [[ ${#missing[@]} -eq 0 ]] || die "Missing required commands: ${missing[*]}"
}

check_repo_dir() {
    [[ -d "$REPO_DIR" ]] || die "Repo dir not found: ${REPO_DIR}"
    git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        || die "Not a git repository: ${REPO_DIR}"
    # Ensure the required remotes exist.
    git -C "$REPO_DIR" remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1 \
        || die "Remote '${UPSTREAM_REMOTE}' not found in ${REPO_DIR}"
    git -C "$REPO_DIR" remote get-url "$ORIGIN_REMOTE" >/dev/null 2>&1 \
        || die "Remote '${ORIGIN_REMOTE}' not found in ${REPO_DIR}"
}

# Resolve the upstream owner/name (e.g. dcl-applications/dcl-cruise-101-spa)
# from the upstream remote URL, unless --repo was given.
resolve_repo() {
    [[ -n "$REPO" ]] && return 0
    local url
    url=$(git -C "$REPO_DIR" remote get-url "$UPSTREAM_REMOTE")
    # Strip protocol/host and trailing .git → owner/name
    REPO=$(echo "$url" | sed -E 's#(https?://[^/]+/|git@[^:]+:)##; s#\.git$##')
    [[ -n "$REPO" ]] || die "Could not resolve upstream repo from: ${url}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Git helpers
# ─────────────────────────────────────────────────────────────────────────────
run_git() { git -C "$REPO_DIR" "$@"; }

CURRENT_BRANCH=""
STASHED=false
RESTORE_ON_EXIT=true   # set false when git is left mid-operation (e.g. conflict)

save_current_branch() {
    CURRENT_BRANCH=$(run_git rev-parse --abbrev-ref HEAD)
    log_info "Starting branch: ${CURRENT_BRANCH}"
}

stash_work() {
    if [[ -n $(run_git status --porcelain) ]]; then
        log_info "Stashing uncommitted work (including untracked)..."
        run_git stash push --include-untracked \
            -m "hotfix-cherry-pick: auto-stash ${CURRENT_BRANCH} for ${TICKETS[*]:-hotfix}"
        STASHED=true
    else
        log_info "Working tree clean, no stash needed."
    fi
}

restore_stash() {
    # Skip when git is intentionally left mid-operation (conflict path).
    [[ "$RESTORE_ON_EXIT" == true ]] || return 0
    # Best-effort restore of our stash; never abort the script on failure here.
    [[ "$STASHED" == true ]] || return 0
    log_info "Restoring stashed work onto ${CURRENT_BRANCH}..."
    run_git checkout "$CURRENT_BRANCH" >/dev/null 2>&1 || true
    if run_git stash pop >/dev/null 2>&1; then
        log_ok "Stashed work restored."
    else
        log_warn "Could not auto-restore stash. Recover manually with: git stash list / git stash pop"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Step: sync base + create local release branch from upstream
# ─────────────────────────────────────────────────────────────────────────────
sync_and_prepare_release() {
    log_step "Syncing ${DEFAULT_BASE_BRANCH} and preparing ${RELEASE_BRANCH}"
    run_git checkout "$DEFAULT_BASE_BRANCH"
    run_git fetch --all --prune

    run_git rev-parse --verify --quiet "${UPSTREAM_REMOTE}/${RELEASE_BRANCH}" >/dev/null \
        || die "${UPSTREAM_REMOTE}/${RELEASE_BRANCH} not found after fetch."

    # Recreate the local release branch fresh from upstream to avoid drift.
    if run_git show-ref --verify --quiet "refs/heads/${RELEASE_BRANCH}"; then
        log_info "Local ${RELEASE_BRANCH} exists; resetting it to ${UPSTREAM_REMOTE}/${RELEASE_BRANCH}."
        run_git checkout "$RELEASE_BRANCH"
        run_git reset --hard "${UPSTREAM_REMOTE}/${RELEASE_BRANCH}"
    else
        run_git checkout -b "$RELEASE_BRANCH" --track "${UPSTREAM_REMOTE}/${RELEASE_BRANCH}"
    fi
    log_ok "${RELEASE_BRANCH} is up to date with ${UPSTREAM_REMOTE}/${RELEASE_BRANCH}."
}

create_hotfix_branch() {
    log_step "Creating hotfix branch ${HOTFIX_BRANCH}"
    if run_git show-ref --verify --quiet "refs/heads/${HOTFIX_BRANCH}"; then
        die "Branch ${HOTFIX_BRANCH} already exists. Delete it or pass a different --branch."
    fi
    run_git checkout -b "$HOTFIX_BRANCH"
    log_ok "On ${HOTFIX_BRANCH}."
}

# ─────────────────────────────────────────────────────────────────────────────
# Step: resolve the PR's commit SHA(s) via gh
# ─────────────────────────────────────────────────────────────────────────────
PR_TITLE=""
PR_URL=""
PR_STATE=""
CHERRY_SHAS=()   # list of SHAs to cherry-pick, in order
HOTFIX_PR_URL="" # URL of the hotfix PR opened (or reused) by this run
HOTFIX_PR_EXISTED=false  # true when an open PR already existed and was reused
SOURCE_PRS=()     # source PR numbers derived from each commit subject "(#NNNN)"
SOURCE_TICKETS=() # Jira ticket IDs (e.g. MERLIN-4429) inferred from each commit subject
INFERRED_TICKETS=() # de-duplicated tickets inferred across all commits (for PR body/title)

# Derive the source PR number and Jira ticket for a commit SHA from its subject.
# Sets globals PR_OUT and TICKET_OUT (empty when not found). Prints nothing.
derive_pr_and_ticket() {
    local sha="$1" subject
    subject=$(run_git show -s --format='%s' "$sha" 2>/dev/null)
    # PR: last "(#NNNN)" token in the subject.
    PR_OUT=$(printf '%s' "$subject" | grep -oE '#[0-9]+' | tail -1 | tr -d '#')
    # Ticket: first PROJECT-NNNN style token (MERLIN-4429, DCLX-93, DCLCOMSUST-12670…).
    TICKET_OUT=$(printf '%s' "$subject" | grep -oiE '[A-Z][A-Z0-9]+-[0-9]+' | head -1 | tr '[:lower:]' '[:upper:]')
}

# Populate CHERRY_SHAS-derived SOURCE_PRS / SOURCE_TICKETS / INFERRED_TICKETS.
# Silent: no stray output. Call after CHERRY_SHAS is set.
populate_source_metadata() {
    SOURCE_PRS=(); SOURCE_TICKETS=(); INFERRED_TICKETS=()
    local sha seen=""
    for sha in "${CHERRY_SHAS[@]}"; do
        derive_pr_and_ticket "$sha"
        SOURCE_PRS+=("$PR_OUT")
        SOURCE_TICKETS+=("$TICKET_OUT")
        if [[ -n "$TICKET_OUT" ]]; then
            case " $seen " in
                *" $TICKET_OUT "*) ;;
                *) INFERRED_TICKETS+=("$TICKET_OUT"); seen+=" $TICKET_OUT" ;;
            esac
        fi
    done
}

# Ensure the given SHAs are present locally, fetching missing objects from upstream.
ensure_shas_present() {
    local sha
    for sha in "$@"; do
        if ! run_git cat-file -e "${sha}^{commit}" 2>/dev/null; then
            log_info "Commit ${sha:0:12} not present locally, fetching from ${UPSTREAM_REMOTE}..."
            run_git fetch "$UPSTREAM_REMOTE" "$sha" 2>/dev/null \
                || die "Could not fetch commit ${sha} from ${UPSTREAM_REMOTE}."
        fi
    done
}

# SHAs auto-dropped because their change is already on the release branch.
DROPPED_SHAS=()

# Detect whether a commit's change is already present on <ref> and would produce
# an EMPTY cherry-pick. `git cherry` (patch-id) is a fast first signal, but it
# can miss no-ops (e.g. a deletion of a line that no longer exists), so we also
# confirm with a real, throwaway `cherry-pick --no-commit` in a temp index.
# Echoes "empty" or "apply". Never mutates the working tree/branch.
_pick_would_be_empty() {
    local ref="$1" sha="$2"

    # Fast path: git cherry marks "- <sha>" when an equivalent patch exists.
    local cherry_mark
    cherry_mark=$(run_git cherry "$ref" "$sha" 2>/dev/null \
        | awk -v s="$sha" '$2 ~ ("^" substr(s,1,12)) || $2==s {print $1; exit}')
    if [[ "$cherry_mark" == "-" ]]; then
        echo "empty"; return 0
    fi

    # Confirm via the tree: does <sha>'s change still differ from <ref>? Compare
    # the trees the commit touches. If applying it onto <ref> changes nothing,
    # it's a no-op. We do a cheap diff of the commit's files between <ref> and
    # the commit's own result.
    local files
    files=$(run_git show --no-renames --name-only --format='' "$sha" 2>/dev/null | sed '/^$/d')
    [[ -n "$files" ]] || { echo "empty"; return 0; }  # commit touches nothing → empty

    local f differs=false
    for f in "${(f)files}"; do
        # Compare <ref>:f against <sha>:f. If identical, applying changes nothing
        # for that file. If ANY file differs, the pick is not empty.
        if ! run_git diff --quiet "$ref:$f" "$sha:$f" 2>/dev/null; then
            differs=true; break
        fi
    done
    [[ "$differs" == true ]] && echo "apply" || echo "empty"
}

# Auto-drop any CHERRY_SHAS whose change is already on upstream/<release>: they
# would cherry-pick empty and halt the run. Dropped SHAs are logged and moved to
# DROPPED_SHAS; CHERRY_SHAS is rewritten to the still-applicable commits, and the
# derived metadata (PRs/tickets) is re-populated.
filter_already_applied() {
    local ref="${UPSTREAM_REMOTE}/${RELEASE_BRANCH}"
    run_git rev-parse --verify --quiet "$ref" >/dev/null || return 0

    local sha kept=() sha_line
    DROPPED_SHAS=()
    for sha in "${CHERRY_SHAS[@]}"; do
        if [[ "$(_pick_would_be_empty "$ref" "$sha")" == "empty" ]]; then
            DROPPED_SHAS+=("$sha")
            log_warn "  ${sha:0:12} already applied on ${RELEASE_BRANCH} — DROPPING (empty pick)."
        else
            kept+=("$sha")
        fi
    done

    if [[ ${#DROPPED_SHAS[@]} -gt 0 ]]; then
        CHERRY_SHAS=("${kept[@]}")
        populate_source_metadata   # refresh PR/ticket metadata for the kept set
        log_info "Dropped ${#DROPPED_SHAS[@]} already-applied commit(s); ${#CHERRY_SHAS[@]} remain."
    fi

    # Nothing left to do → stop cleanly (not an error: the work is already there).
    if [[ ${#CHERRY_SHAS[@]} -eq 0 ]]; then
        log_ok "All requested commits are already on ${RELEASE_BRANCH}. Nothing to hotfix."
        exit 0
    fi
}

# SHA mode: take explicit SHAs verbatim (order preserved) and derive the source
# PR number from each commit subject's trailing "(#NNNN)" for the hotfix PR body.
resolve_explicit_shas() {
    log_step "Resolving ${#EXPLICIT_SHAS[@]} explicit commit(s)"
    ensure_shas_present "${EXPLICIT_SHAS[@]}"
    CHERRY_SHAS=("${EXPLICIT_SHAS[@]}")
    populate_source_metadata

    # Auto-drop commits already on the release branch (would cherry-pick empty).
    filter_already_applied

    local sha i=1 subject
    for sha in "${CHERRY_SHAS[@]}"; do
        subject=$(run_git show -s --format='%s' "$sha")
        local pr="${SOURCE_PRS[$i]}" tk="${SOURCE_TICKETS[$i]}"
        local pr_disp="(no PR)"
        [[ -n "$pr" ]] && pr_disp="#${pr}"
        log_info "  ${sha:0:12} → PR ${pr_disp}${tk:+ · ${tk}} — ${subject}"
        i=$((i + 1))
    done
    log_ok "Resolved ${#CHERRY_SHAS[@]} explicit commit(s)."
}

resolve_pr_commits() {
    log_step "Resolving commits for PR #${PR_NUMBER} in ${REPO}"
    # Extract each field with gh's built-in --jq. gh consumes the API JSON
    # directly, so it tolerates control characters in titles/commit messages
    # that would otherwise break a second, external jq pass.
    local gh_view=(gh pr view "$PR_NUMBER" --repo "$REPO")
    PR_TITLE=$(GH_HOST="$GIT_HOST" "${gh_view[@]}" --json title --jq '.title // ""' 2>/dev/null) \
        || die "Could not fetch PR #${PR_NUMBER} from ${REPO} (gh auth / host?)."
    PR_URL=$(GH_HOST="$GIT_HOST" "${gh_view[@]}" --json url --jq '.url // ""' 2>/dev/null)
    PR_STATE=$(GH_HOST="$GIT_HOST" "${gh_view[@]}" --json state --jq '.state // ""' 2>/dev/null)

    # Sanitize the title: strip any control chars so it is safe in a PR title later.
    PR_TITLE=$(printf '%s' "$PR_TITLE" | tr -d '\000-\037')

    local merge_oid
    merge_oid=$(GH_HOST="$GIT_HOST" "${gh_view[@]}" --json mergeCommit --jq '.mergeCommit.oid // empty' 2>/dev/null)

    if [[ -n "$merge_oid" ]]; then
        # Merged PR → cherry-pick the merge commit with mainline parent 1.
        # (cherry_pick_commits auto-detects the merge and applies -m 1.)
        CHERRY_SHAS=("$merge_oid")
        log_info "PR is merged. Using merge commit ${merge_oid:0:12} (cherry-pick -m 1)."
    else
        # Not merged (or squash/rebase) → cherry-pick the individual commits.
        local shas
        shas=$(GH_HOST="$GIT_HOST" "${gh_view[@]}" --json commits --jq '.commits[].oid' 2>/dev/null)
        [[ -n "$shas" ]] || die "PR #${PR_NUMBER} has no commits and no merge commit."
        CHERRY_SHAS=("${(@f)shas}")
        log_info "PR not merged. Cherry-picking ${#CHERRY_SHAS[@]} individual commit(s)."
    fi

    # Make sure the commits are present locally (fetch objects if needed).
    ensure_shas_present "${CHERRY_SHAS[@]}"
    populate_source_metadata
    filter_already_applied
    log_ok "Resolved ${#CHERRY_SHAS[@]} commit(s) for PR #${PR_NUMBER}."
}

# ─────────────────────────────────────────────────────────────────────────────
# Step: cherry-pick (pauses on conflict, matching the manual workflow)
# ─────────────────────────────────────────────────────────────────────────────
cherry_pick_commits() {
    log_step "Cherry-picking ${#CHERRY_SHAS[@]} commit(s)"
    local sha pick_args
    for sha in "${CHERRY_SHAS[@]}"; do
        # Auto-detect merge commit → needs -m 1; single-parent → plain pick.
        local parents
        parents=$(run_git rev-list --parents -n1 "$sha" | wc -w)
        if [[ "$parents" -ge 3 ]]; then
            log_info "Cherry-pick -m 1 ${sha:0:12} (merge commit)"
            pick_args=(cherry-pick -m 1 "$sha")
        else
            log_info "Cherry-pick ${sha:0:12}"
            pick_args=(cherry-pick "$sha")
        fi

        if ! run_git "${pick_args[@]}"; then
            # Distinguish a real conflict from an "empty" pick (change already present).
            if [[ -z "$(run_git status --porcelain)" ]] && run_git rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null 2>&1; then
                log_error "Cherry-pick of ${sha:0:12} is EMPTY — the change is likely already in ${RELEASE_BRANCH}."
                log_warn "Aborting the empty cherry-pick and stopping."
                run_git cherry-pick --abort >/dev/null 2>&1 || true
            else
                log_warn "Conflicts while cherry-picking ${sha:0:12}. Trying known-file auto-resolution…"
                # Balanced strategy: auto-resolve ONLY manifests/CHANGELOG (take
                # incoming). If that clears every conflict, continue the pick
                # automatically; otherwise pause for the real code conflict.
                if autoresolve_known_conflicts "$REPO_DIR"; then
                    if GIT_EDITOR=true run_git cherry-pick --continue >/dev/null 2>&1; then
                        log_ok "Auto-resolved known conflicts for ${sha:0:12}; continued."
                        continue
                    fi
                    log_warn "cherry-pick --continue failed after auto-resolution."
                fi
                log_error "Real code conflict while cherry-picking ${sha:0:12}."
                log_warn "Resolve conflicts (Angular/Node SPA context), then run:"
                echo "${DIM}    git -C \"${REPO_DIR}\" cherry-pick --continue${RESET}"
                echo "${DIM}    (or --abort to cancel)${RESET}"
                log_warn "This is a complex step; pausing per workflow spec. Re-run push/PR (or --push-existing) after."
            fi
            # A cherry-pick (conflict or aborted-empty) leaves us off the original
            # branch. Disable the auto-restore so we never silently fail to
            # restore over git state, and tell the user where their stash is.
            RESTORE_ON_EXIT=false
            if [[ "$STASHED" == true ]]; then
                log_warn "Your work is still stashed. After resolving, restore it with:"
                echo "${DIM}    git -C \"${REPO_DIR}\" checkout ${CURRENT_BRANCH} && git -C \"${REPO_DIR}\" stash pop${RESET}"
            fi
            exit 2
        fi
    done
    log_ok "Cherry-pick complete, no conflicts."
}

# ─────────────────────────────────────────────────────────────────────────────
# Step: test gate
# ─────────────────────────────────────────────────────────────────────────────
run_test_gate() {
    if [[ "$SKIP_TESTS" == true ]]; then
        log_warn "Skipping test gate (--skip-tests)."
        return 0
    fi
    log_step "Running test suite (test-summary.sh)"
    [[ -x "$TEST_SUMMARY" ]] || die "test-summary.sh not found or not executable at ${TEST_SUMMARY}"
    # test-summary.sh runs ng test --no-watch and exits with the test exit code.
    if ( cd "$REPO_DIR" && "$TEST_SUMMARY" ); then
        log_ok "Tests passed."
    else
        die "Tests failed. Fix them, then re-run (or use --skip-tests if verified elsewhere)."
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Step: push + open PR
# ─────────────────────────────────────────────────────────────────────────────
build_pr_body() {
    # Build the PR body from commit + diff data (Jira context only if provided).
    local body_file="$1"
    local sha_lines="" sha i=1
    if [[ "$SHA_MODE" == true ]]; then
        # One line per commit, annotated with the source PR derived from its subject.
        for sha in "${CHERRY_SHAS[@]}"; do
            local pr="${SOURCE_PRS[$i]}"
            local subject
            subject=$(run_git show -s --format='%s' "$sha")
            if [[ -n "$pr" ]]; then
                sha_lines+="- \`${sha:0:12}\` — ${subject} (from PR #${pr})"$'\n'
            else
                sha_lines+="- \`${sha:0:12}\` — ${subject}"$'\n'
            fi
            i=$((i + 1))
        done
    else
        for sha in "${CHERRY_SHAS[@]}"; do
            sha_lines+="- \`${sha:0:12}\` — $(run_git show -s --format='%s' "$sha")"$'\n'
        done
    fi

    # Deduplicated, ordered list of source PR numbers (SHA mode) for the summary.
    local source_pr_line=""
    if [[ "$SHA_MODE" == true ]]; then
        local seen="" pr uniq=()
        for pr in "${SOURCE_PRS[@]}"; do
            [[ -n "$pr" ]] || continue
            case " $seen " in *" $pr "*) ;; *) uniq+=("#$pr"); seen+=" $pr" ;; esac
        done
        source_pr_line="${uniq[*]}"
    fi

    # Ticket list for the summary: explicit --ticket values take precedence, else
    # tickets inferred from the commit subjects. When neither exists, ticket lines
    # are OMITTED entirely (no "(none)" noise) and the flow continues normally.
    local ticket_line="" has_tickets=false
    if [[ ${#TICKETS[@]} -gt 0 ]]; then
        ticket_line="${TICKETS[*]}"; has_tickets=true
    elif [[ ${#INFERRED_TICKETS[@]} -gt 0 ]]; then
        ticket_line="${INFERRED_TICKETS[*]} (inferred from commits)"; has_tickets=true
    fi

    {
        echo "## Summary"
        echo ""
        if [[ "$SHA_MODE" == true ]]; then
            echo "Hotfix onto \`${RELEASE_BRANCH}\`, cherry-picking ${#CHERRY_SHAS[@]} commit(s)."
            echo ""
            [[ "$has_tickets" == true ]] && echo "- Jira tickets: ${ticket_line}"
            [[ -n "$source_pr_line" ]] && echo "- Original PRs: ${source_pr_line}"
        else
            if [[ "$has_tickets" == true ]]; then
                echo "Hotfix for **${ticket_line}**, cherry-picked from PR #${PR_NUMBER} onto \`${RELEASE_BRANCH}\`."
            else
                echo "Hotfix cherry-picked from PR #${PR_NUMBER} onto \`${RELEASE_BRANCH}\`."
            fi
        fi
        echo ""
        echo "## Source"
        echo ""
        if [[ "$SHA_MODE" != true ]]; then
            echo "- Original PR: ${PR_URL:-#$PR_NUMBER} (state: ${PR_STATE:-unknown})"
        fi
        echo "- Cherry-picked commit(s):"
        echo "${sha_lines}"
        if [[ -n "$JIRA_CONTEXT" ]]; then
            echo "## Jira Context"
            echo ""
            echo "${JIRA_CONTEXT}"
            echo ""
        else
            echo "> Jira context not provided; summary derived from the source PR(s) and commit(s)."
            echo ""
        fi
        echo "## Testing"
        echo ""
        if [[ "$SKIP_TESTS" == true ]]; then
            echo "- Test gate skipped in script; verified separately."
        else
            echo "- Test suite ran and passed via the script's test gate."
        fi
    } > "$body_file"
}

push_hotfix_branch() {
    log_step "Pushing ${HOTFIX_BRANCH} to ${ORIGIN_REMOTE}"

    local push_args=(push -u "$ORIGIN_REMOTE" HEAD)
    [[ "$PUSH_NO_VERIFY" == true ]] && push_args+=(--no-verify)
    run_git "${push_args[@]}"

    # Verify the branch actually reached the remote.
    run_git ls-remote --heads "$ORIGIN_REMOTE" "$HOTFIX_BRANCH" | grep -q . \
        || die "Push did not land ${HOTFIX_BRANCH} on ${ORIGIN_REMOTE}."
    log_ok "${HOTFIX_BRANCH} pushed to ${ORIGIN_REMOTE}."
}

open_pr() {
    log_step "Opening PR against ${UPSTREAM_REMOTE}/${RELEASE_BRANCH}"

    local origin_owner body_file pr_title
    origin_owner=$(git -C "$REPO_DIR" remote get-url "$ORIGIN_REMOTE" \
        | sed -E 's#(https?://[^/]+/|git@[^:]+:)##; s#/.*$##')

    # Idempotency: if an OPEN PR already exists for this head branch, reuse it
    # instead of failing on `gh pr create`. gh matches head as owner:branch.
    local existing
    existing=$(GH_HOST="$GIT_HOST" gh pr list \
        --repo "$REPO" \
        --head "$HOTFIX_BRANCH" \
        --state open \
        --json url --jq '.[0].url // ""' 2>/dev/null)
    if [[ -n "$existing" ]]; then
        HOTFIX_PR_URL="$existing"
        HOTFIX_PR_EXISTED=true
        log_ok "PR already exists for ${HOTFIX_BRANCH}; reusing it:"
        echo "${BOLD}${HOTFIX_PR_URL}${RESET}"
        return 0
    fi

    body_file=$(mktemp)
    build_pr_body "$body_file"
    # Ticket tag for the title: explicit --ticket, else inferred, else nothing.
    local ticket_tag=""
    if [[ ${#TICKETS[@]} -gt 0 ]]; then
        ticket_tag="${TICKETS[*]} "
    elif [[ ${#INFERRED_TICKETS[@]} -gt 0 ]]; then
        ticket_tag="${INFERRED_TICKETS[*]} "
    fi

    if [[ "$SHA_MODE" == true ]]; then
        pr_title="[HOTFIX] ${ticket_tag}${RELEASE_BRANCH} updates (${#CHERRY_SHAS[@]} commits)"
    else
        pr_title="[HOTFIX] ${ticket_tag}${PR_TITLE:-cherry-pick from PR #$PR_NUMBER}"
    fi
    # Collapse any accidental double spaces (e.g. empty ticket tag).
    pr_title="${pr_title//  / }"

    HOTFIX_PR_URL=$(GH_HOST="$GIT_HOST" gh pr create \
        --repo "$REPO" \
        --base "$RELEASE_BRANCH" \
        --head "${origin_owner}:${HOTFIX_BRANCH}" \
        --title "$pr_title" \
        --body-file "$body_file") \
        || { rm -f "$body_file"; die "gh pr create failed."; }
    rm -f "$body_file"

    log_ok "Hotfix PR created:"
    echo "${BOLD}${HOTFIX_PR_URL}${RESET}"
}

# ─────────────────────────────────────────────────────────────────────────────
# push-existing flow: operate on the current branch (already prepared).
# ─────────────────────────────────────────────────────────────────────────────
push_existing_flow() {
    log_step "Push-existing mode on branch ${HOTFIX_BRANCH}"

    # Sanity: we must actually be on HOTFIX_BRANCH.
    local cur
    cur=$(run_git rev-parse --abbrev-ref HEAD)
    [[ "$cur" == "$HOTFIX_BRANCH" ]] \
        || die "Expected to be on ${HOTFIX_BRANCH} but HEAD is ${cur}. Checkout the branch or pass --branch."

    # Working tree must be clean (no half-resolved conflicts / uncommitted work).
    [[ -z "$(run_git status --porcelain)" ]] \
        || die "Working tree not clean. Commit or stash changes before --push-existing."

    # Refresh the release ref so the commit list (branch minus release) is
    # accurate even if the local ref is stale. Best-effort — offline is fine if
    # the ref already exists locally.
    run_git fetch "$UPSTREAM_REMOTE" "$RELEASE_BRANCH" >/dev/null 2>&1 || true
    run_git rev-parse --verify --quiet "${UPSTREAM_REMOTE}/${RELEASE_BRANCH}" >/dev/null \
        || die "${UPSTREAM_REMOTE}/${RELEASE_BRANCH} not found. Fetch first."

    # Populate SHAs (explicit list, or the commits this branch adds over release).
    if [[ "$SHA_MODE" == true ]]; then
        ensure_shas_present "${EXPLICIT_SHAS[@]}"
        CHERRY_SHAS=("${EXPLICIT_SHAS[@]}")
    else
        local shas
        shas=$(run_git rev-list --reverse "${UPSTREAM_REMOTE}/${RELEASE_BRANCH}..HEAD")
        [[ -n "$shas" ]] || die "No commits on ${HOTFIX_BRANCH} beyond ${UPSTREAM_REMOTE}/${RELEASE_BRANCH}."
        CHERRY_SHAS=("${(@f)shas}")
        SHA_MODE=true   # make build_pr_body use the multi-commit rendering
    fi
    populate_source_metadata
    log_info "PR body will list ${#CHERRY_SHAS[@]} commit(s)."

    run_test_gate
    push_hotfix_branch

    if [[ "$NO_PR" == true ]]; then
        log_warn "Skipping PR creation (--no-pr). Branch pushed only."
        print_summary
        return 0
    fi

    open_pr
    print_summary
    maybe_open_pr_in_browser
}

# ─────────────────────────────────────────────────────────────────────────────
# Final summary + open PR in browser
# ─────────────────────────────────────────────────────────────────────────────
print_summary() {
    # Tickets: explicit, else inferred, else "none".
    local tickets=""
    if [[ ${#TICKETS[@]} -gt 0 ]]; then
        tickets="${TICKETS[*]}"
    elif [[ ${#INFERRED_TICKETS[@]} -gt 0 ]]; then
        tickets="${INFERRED_TICKETS[*]} (inferred)"
    else
        tickets="(none)"
    fi

    echo ""
    log_step "Summary"
    log_info "  Release branch: ${RELEASE_BRANCH}"
    log_info "  Hotfix branch:  ${HOTFIX_BRANCH}  → pushed to ${ORIGIN_REMOTE}"
    log_info "  Commits:        ${#CHERRY_SHAS[@]} cherry-picked"
    [[ ${#DROPPED_SHAS[@]} -gt 0 ]] && log_info "  Skipped:        ${#DROPPED_SHAS[@]} already-applied commit(s)"
    log_info "  Tickets:        ${tickets}"
    if [[ -n "$HOTFIX_PR_URL" ]]; then
        if [[ "$HOTFIX_PR_EXISTED" == true ]]; then
            log_info "  PR (existing):  ${HOTFIX_PR_URL}"
        else
            log_info "  PR (created):   ${HOTFIX_PR_URL}"
        fi
    fi
}

# Open the hotfix PR in the browser, but ONLY when it was freshly created in this
# run (not when an existing one was reused, and not when --no-pr was used).
maybe_open_pr_in_browser() {
    [[ -n "$HOTFIX_PR_URL" ]] || return 0
    [[ "$HOTFIX_PR_EXISTED" == true ]] && return 0
    log_info "Opening the new PR in your browser..."
    if command -v gh >/dev/null 2>&1; then
        GH_HOST="$GIT_HOST" gh pr view "$HOTFIX_PR_URL" --web >/dev/null 2>&1 && return 0
    fi
    # Fallback to the OS opener.
    command -v open >/dev/null 2>&1 && open "$HOTFIX_PR_URL" >/dev/null 2>&1 || true
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
main() {
    parse_args "$@"

    check_dependencies
    check_vpn "$GIT_HOST"
    check_repo_dir
    resolve_repo

    if [[ "$PUSH_EXISTING" == true ]]; then
        log_info "Mode:    push-existing (current branch, no cherry-pick)"
        [[ ${#TICKETS[@]} -gt 0 ]] && log_info "Tickets: ${TICKETS[*]}"
    elif [[ "$SHA_MODE" == true ]]; then
        log_info "Mode:    SHA (${#EXPLICIT_SHAS[@]} explicit commit(s))"
        [[ ${#TICKETS[@]} -gt 0 ]] && log_info "Tickets: ${TICKETS[*]}"
    else
        log_info "Mode:    PR"
        [[ ${#TICKETS[@]} -gt 0 ]] && log_info "Ticket:  ${TICKETS[*]}"
        log_info "PR:      #${PR_NUMBER}"
    fi
    log_info "Repo:    ${REPO}"
    log_info "Release: ${RELEASE_BRANCH}"
    log_info "Hotfix:  ${HOTFIX_BRANCH}"

    if [[ "$DRY_RUN" == true ]]; then
        log_warn "Dry run: resolving commits and simulating cherry-picks; no branch/network changes."
        if [[ "$SHA_MODE" == true ]]; then
            resolve_explicit_shas
        else
            resolve_pr_commits
        fi
        # Simulate the cherry-picks so a dry-run surfaces conflicts too.
        # stderr is silenced: child git processes can inherit an ambient xtrace
        # from repo hooks/wrappers and leak assignment traces there; our own
        # diagnostics go to stdout via log_*.
        cherrypick_preflight "$REPO_DIR" "$UPSTREAM_REMOTE" "$RELEASE_BRANCH" "${CHERRY_SHAS[@]}" 2>/dev/null || true
        log_ok "Dry run complete. Would cherry-pick: ${CHERRY_SHAS[*]}"
        exit 0
    fi

    # push-existing: the branch is already prepared (e.g. conflicts resolved
    # manually). Skip sync / branch-create / cherry-pick; just test, push, PR.
    if [[ "$PUSH_EXISTING" == true ]]; then
        push_existing_flow
        return 0
    fi

    save_current_branch
    # Restore the user's stash on any exit path except a cherry-pick conflict
    # pause (which exits 2 before this trap-worthy point).
    trap restore_stash EXIT

    stash_work
    sync_and_prepare_release
    create_hotfix_branch
    if [[ "$SHA_MODE" == true ]]; then
        resolve_explicit_shas
    else
        resolve_pr_commits
    fi

    # Balanced conflict strategy:
    #  • rerere records/reuses resolutions
    #  • pre-flight simulates every pick on a throwaway worktree and reports which
    #    SHAs/files would conflict, distinguishing auto-resolvable manifests from
    #    real code. Real code conflicts abort BEFORE any branch mutation unless
    #    --skip-preflight is passed.
    enable_rerere "$REPO_DIR"
    if [[ "$SKIP_PREFLIGHT" != true ]]; then
        if ! cherrypick_preflight "$REPO_DIR" "$UPSTREAM_REMOTE" "$RELEASE_BRANCH" "${CHERRY_SHAS[@]}" 2>/dev/null; then
            log_error "Pre-flight detected real code conflicts."
            log_info  "Options: resolve upstream first, adjust the SHA list, or re-run with --skip-preflight"
            log_info  "         to proceed and resolve conflicts interactively during the cherry-pick."
            die "Aborting before any branch/network change (nothing was modified)."
        fi
    else
        log_warn "Skipping cherry-pick pre-flight (--skip-preflight)."
    fi

    cherry_pick_commits
    run_test_gate
    push_hotfix_branch

    if [[ "$NO_PR" == true ]]; then
        log_warn "Skipping PR creation (--no-pr). Branch pushed only."
        print_summary
        return 0
    fi

    open_pr
    print_summary
    maybe_open_pr_in_browser
}

main "$@"
