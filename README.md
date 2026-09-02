# my_tools

Personal automation toolkit for the DCL Angular workflow (dcl-cruise-101-spa +
dcl-ui-global-components-library-v2) plus a reusable project-maintenance suite.
Shell scripts (zsh/bash) for git, hotfix, review, and release chores, with a
shared helper library and a Python maintenance package.

> **For AI agents:** a graphify knowledge graph of this repo lives in
> [`graphify-out/`](#context-for-ai-agents). Start there for architecture,
> call-graphs, and file relationships before manual exploration.

---

## Layout

```
my_tools/
├── utils.sh                     # shared: logging, VPN check, error trap, cherry-pick safety
├── hotfix-cherry-pick.sh        # apply merged PR / explicit SHAs as a hotfix onto a release branch
├── rebase-develop.sh            # stash → update develop → rebase current branch (auto-resolves manifests)
├── test-summary.sh              # run ng test, show failures + coverage for changed files
├── bump-lib-version.sh          # bump the global-components library version across the 3 package.json files
├── review-pr.sh                 # generate a structured PR review (writes ~/output/review-pr/pr_<N>_review.md)
├── post-review-comments.sh      # post a generated review back to the PR (inline or file-level)
├── my-merged-prs.sh             # list your merged PRs in a date range → markdown report
├── fresh-install.sh             # interactive clean-install helper (VPN-gated)
├── general_project_maintenance/ # reusable Python maintenance suite (see below)
└── graphify-out/                # knowledge graph of this repo (for humans + AI agents)
```

---

## Shell conventions

- **Shell:** the git/hotfix/test scripts are **zsh** (they use zsh idioms like
  `${(f)…}`, `${0:A:h}`, 1-based arrays). They refuse to run under bash with a
  clear message. `review-pr.sh`, `post-review-comments.sh`, and
  `fresh-install.sh` are **bash**. `utils.sh` is portable and sourced by both.
- **Shared helpers live in `utils.sh`** — do not duplicate logging, VPN checks,
  or conflict-resolution logic. Source it and call the helpers.
- Every script supports `-h` / `--help`.

### `utils.sh` — the shared library

Sourced by all the other scripts. Provides:

| Helper | Purpose |
|--------|---------|
| `log_step / log_info / log_ok / log_warn / log_error / die` | Consistent colored logging |
| `check_vpn <host>` | Resolve a host via `dig`; exit with a clear message if offline/off-VPN |
| `utils_enable_error_trap` | `errexit`+`pipefail`+ERR trap that reports the failing command and `file:line` |
| `run_checked "<label>" <cmd…>` | Run a command; on failure log a labeled error and return its code |
| `enable_rerere <repo>` | Turn on git rerere (reuse recorded conflict resolutions) |
| `cherrypick_preflight <repo> <remote> <release> <sha…>` | Simulate cherry-picks on a throwaway worktree; report conflicts before mutating (zsh only) |
| `autoresolve_known_conflicts <repo> [ours\|theirs]` | Auto-resolve only `package.json` / `package-lock.json` / `CHANGELOG.md`; leave real code conflicts (zsh only) |

---

## Tools

### hotfix-cherry-pick.sh (zsh)

Apply a merged PR or an explicit list of commit SHAs as a hotfix onto a release
branch, then open a PR against upstream. Reproduces the full manual hotfix flow.

```bash
# PR mode — resolve commits from a single merged PR
./hotfix-cherry-pick.sh --ticket MERLIN-4953 --pr 3743 --release release-2.4.0

# SHA mode — one cherry-pick per SHA, in order (auto -m 1 for merge commits)
./hotfix-cherry-pick.sh -r release-2.4.0 \
    --sha a736bbc… --sha 0c154ff… --sha 7d0e5cc… \
    --ticket MERLIN-4429 --ticket MERLIN-4865
```

Highlights: VPN pre-check, pre-flight conflict simulation, git rerere,
auto-resolution of manifest/CHANGELOG conflicts, **auto-drop of commits already
on the release branch**, **PR idempotency** (reuses an existing open PR),
ticket **optional** (inferred from commit subjects when omitted), and a final
summary that opens the new PR in the browser. `--push-existing` finishes a
manually-prepared branch (test → push → PR). See `--help` for all flags.

### rebase-develop.sh (zsh)

Stash uncommitted work, update `develop` from upstream, and rebase the current
branch onto it. Auto-resolves `package.json` / `package-lock.json` / `CHANGELOG.md`
conflicts (keeping develop's side) both during the rebase and the stash pop;
pauses on real code conflicts.

```bash
./rebase-develop.sh            # rebase the current repo
./rebase-develop.sh --full     # library first, then the SPA
```

### test-summary.sh (zsh)

Run `ng test --no-watch` and print a compact summary: failing test names +
coverage filtered to changed files.

```bash
./test-summary.sh              # all tests
./test-summary.sh -s           # coverage filtered to staged files
./test-summary.sh -n 3         # staged + last 3 commits
```

### bump-lib-version.sh (zsh)

Bump the `@dcl/dcl-ui-global-components-library-v2` version and keep the three
`package.json` files (library project, library root, SPA dependency) in sync.

```bash
./bump-lib-version.sh          # +1 patch
./bump-lib-version.sh 3        # +3
```

### review-pr.sh (bash)

Generate a structured code review for a PR, written to
`~/output/review-pr/pr_<N>_review.md` (with a `<!-- repo:owner/name -->` header
used by `post-review-comments.sh`).

```bash
./review-pr.sh <PR_URL> [OPTIONS]
```

### post-review-comments.sh (bash)

Parse a generated review file and post its issues back to the PR, either inline
on the diff or grouped per file.

```bash
./post-review-comments.sh <PR_NUMBER> --file
./post-review-comments.sh <PR_NUMBER> --inline --level=critical --dry-run
```

### my-merged-prs.sh (zsh)

List your merged PRs in a repo within a date range and write a markdown report
(PR links + inferred Jira tickets).

```bash
./my-merged-prs.sh --repo dcl-applications/dcl-cruise-101-spa
./my-merged-prs.sh --from 2026-07-01 --to 2026-07-31
```

### fresh-install.sh (bash)

Interactive clean-install helper, gated on VPN connectivity.

---

## general_project_maintenance/ (Python)

A reusable suite of AST-based maintenance tools with a shared `core/` package.
Portable across projects (some entry points still reference a prior project's
`src/` layout — adapt paths when reusing).

| Module | Purpose |
|--------|---------|
| `verify_steerings.py` | Audit `.kiro/steering/*` for duplicates, contradictions, clarity, cross-filematch redundancy |
| `check_phantom_functions.py` | Find functions referenced but not defined/importable |
| `cleanup_repo.py` | Report and execute repo cleanup (orphans, caches) |
| `md_index.py` | Build a markdown index of docs (dates, tags, findings, metadata) |
| `generate_module_index.py` | Generate a compact module index for LLM context |
| `trim_overdocumented.py` | Trim over-long docstrings |
| `validate_test_imports.py` | Validate/fix test imports, delete dead test files |
| `core/` | `ast_utils`, `config`, `frontmatter`, `import_analysis`, `report` — shared building blocks |

---

## Context for AI agents

This repo is indexed as a **graphify** knowledge graph in `graphify-out/`:

- `graph.json` — nodes (functions, scripts, classes) and edges (calls/imports)
- `GRAPH_REPORT.md` — audit report: god nodes, communities, cross-module bridges
- `graph.html` — interactive visualization

**Use it first** for any question about architecture, call-graphs, or file
relationships before manual searching. Query it with the graphify skill:

```bash
graphify query "how does hotfix-cherry-pick resolve conflicts?"
graphify --update            # refresh after code changes (AST-only, no API key)
```

### Map (from the graph)

- **Shared core:** `utils.sh` is the hub — `fresh-install.sh`,
  `rebase-develop.sh`, and `review-pr.sh` all call into it (`check_vpn`,
  `utils_enable_error_trap`, `die`). `rebase-develop.sh`'s `safe_stash_pop` and
  `rebase_with_autoresolve` both delegate to `autoresolve_known_conflicts`.
- **Most-connected abstractions (god nodes):** `verify_steerings.VerificationReport`,
  `core.report.Report`, `hotfix_cherry_pick.run_git`,
  `cleanup_repo.CleanupReport`, and the several `main()` entry points.
- **Communities:** the graph splits into ~16 clusters — one per shell tool plus
  the maintenance modules and the `core/` package (AST utils, config,
  frontmatter parsing).

Refresh the graph after meaningful code changes so this context stays accurate.
