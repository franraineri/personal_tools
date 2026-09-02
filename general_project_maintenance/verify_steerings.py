#!/usr/bin/env python3
# TODO move mainteinance tools to a new project repository in order to reuse them. Also a refactor and modular parameters will be needed

"""Verify steering files: pattern activation, quality, staleness, split, token budget, and hooks.

Checks that .kiro/steering/*.md|*.txt files are correct, well-maintained, and
optimally structured for AI agent context efficiency.

Usage:
    # Full check (all steerings)
   .venv/bin/python scripts/general_project_maintenance/verify_steerings.py

    # Check specific file(s)
   .venv/bin/python scripts/general_project_maintenance/verify_steerings.py .kiro/steering/accelerated-pipeline.md

    # Only run specific checks
   .venv/bin/python scripts/general_project_maintenance/verify_steerings.py --check activation quality tokens

    # Include staleness analysis (uses git log, slower)
   .venv/bin/python scripts/general_project_maintenance/verify_steerings.py --check staleness

    # Auto-fix trivial issues
   .venv/bin/python scripts/general_project_maintenance/verify_steerings.py --fix

    # Output JSON report (for CI)
   .venv/bin/python scripts/general_project_maintenance/verify_steerings.py --json

    # Output SARIF (for GitHub code scanning)
   .venv/bin/python scripts/general_project_maintenance/verify_steerings.py --sarif > steering-report.sarif
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STEERING_DIR = PROJECT_ROOT / ".kiro" / "steering"
HOOKS_DIR = PROJECT_ROOT / ".kiro" / "hooks"
SKILLS_DIR = PROJECT_ROOT / ".kiro" / "skills"
KNOWLEDGE_DIR = PROJECT_ROOT / ".kiro" / "knowledge"

# Steering file extensions Kiro supports
STEERING_EXTENSIONS = ("*.md", "*.txt")

# Days since last touch after which a file is considered stale
DEFAULT_STALE_DAYS = 60

# Token budget: estimated max "always" tokens before context rot sets in.
# ~1 token ≈ 4 chars for English/code (conservative heuristic without tiktoken).
CHARS_PER_TOKEN = 4
ALWAYS_TOKEN_BUDGET = 12_000  # Warn if always-included steerings exceed this
PER_FILE_TOKEN_WARN = 4_000  # Warn if single steering exceeds this


# ═══════════════════════════════════════════════════════════════════════════════
# Data models
# ═══════════════════════════════════════════════════════════════════════════════


Severity = Literal["error", "warning", "note"]

SEVERITY_WEIGHTS: dict[Severity, int] = {"error": 10, "warning": 5, "note": 1}


@dataclass
class Issue:
    """A single verification issue."""

    check: str
    severity: Severity
    file: str
    message: str
    line: int = 0
    fix_applied: bool = False

    @property
    def rule_id(self) -> str:
        return f"steering/{self.check}"


@dataclass
class VerificationReport:
    """Full verification report with scoring."""

    issues: list[Issue] = field(default_factory=list)
    files_scanned: int = 0
    checks_run: list[str] = field(default_factory=list)
    total_always_tokens: int = 0

    @property
    def score(self) -> int:
        """Health score 0-100. Starts at 100, deducted by issue severity."""
        penalty = sum(SEVERITY_WEIGHTS[i.severity] for i in self.issues if not i.fix_applied)
        return max(0, 100 - penalty)

    @property
    def passed(self) -> bool:
        return all(i.severity != "error" or i.fix_applied for i in self.issues)

    def add(self, check: str, severity: Severity, file: str, message: str, line: int = 0) -> Issue:
        issue = Issue(check=check, severity=severity, file=file, message=message, line=line)
        self.issues.append(issue)
        return issue

    def to_json(self) -> dict:
        return {
            "score": self.score,
            "passed": self.passed,
            "files_scanned": self.files_scanned,
            "checks_run": self.checks_run,
            "total_always_tokens_estimate": self.total_always_tokens,
            "issues": [asdict(i) for i in self.issues],
            "summary": {
                "errors": sum(1 for i in self.issues if i.severity == "error"),
                "warnings": sum(1 for i in self.issues if i.severity == "warning"),
                "notes": sum(1 for i in self.issues if i.severity == "note"),
                "fixed": sum(1 for i in self.issues if i.fix_applied),
            },
        }

    def to_sarif(self) -> dict:
        """Generate SARIF v2.1.0 output for GitHub code scanning integration."""
        results = []
        rules_seen: dict[str, dict] = {}

        for issue in self.issues:
            rule_id = issue.rule_id
            if rule_id not in rules_seen:
                rules_seen[rule_id] = {
                    "id": rule_id,
                    "shortDescription": {"text": f"Steering check: {issue.check}"},
                    "defaultConfiguration": {
                        "level": issue.severity if issue.severity != "note" else "note"
                    },
                }

            results.append(
                {
                    "ruleId": rule_id,
                    "level": issue.severity if issue.severity != "note" else "note",
                    "message": {"text": issue.message},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": issue.file},
                                "region": {"startLine": max(1, issue.line)},
                            }
                        }
                    ],
                }
            )

        return {
            "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "verify-steerings",
                            "version": "2.0.0",
                            "informationUri": "https://github.com/your-repo/scripts/general_project_maintenance/verify_steerings.py",
                            "rules": list(rules_seen.values()),
                        }
                    },
                    "results": results,
                }
            ],
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════════════


def estimate_tokens(text: str) -> int:
    """Estimate token count. Uses tiktoken if available, else heuristic."""
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except ImportError:
        return len(text) // CHARS_PER_TOKEN


def parse_front_matter(filepath: Path) -> dict[str, str]:
    """Extract YAML front-matter from a markdown/txt file.

    Handles values containing colons by splitting only on the first colon.
    """
    text = filepath.read_text(errors="replace")
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    fm: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, val = line.split(":", 1)  # Split only on FIRST colon
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm


def get_body(filepath: Path) -> str:
    """Get steering body content (after front-matter)."""
    text = filepath.read_text(errors="replace")
    match = re.match(r"^---\s*\n.*?\n---\s*\n?", text, re.DOTALL)
    if match:
        return text[match.end() :]
    return text


def glob_match(filepath: str, pattern: str) -> bool:
    """Check if filepath matches a glob pattern (supporting **)."""
    pattern = pattern.strip()
    if "**" not in pattern:
        return fnmatch.fnmatch(filepath, pattern)
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return filepath.startswith(prefix + "/") or filepath == prefix
    parts = pattern.split("**/")
    if len(parts) == 2:
        prefix, suffix = parts
        if prefix and not filepath.startswith(prefix):
            return False
        remaining = filepath[len(prefix) :] if prefix else filepath
        segments = remaining.split("/")
        for i in range(len(segments)):
            candidate = "/".join(segments[i:])
            if fnmatch.fnmatch(candidate, suffix):
                return True
    return False


def matches_any(filepath: str, patterns: list[str]) -> bool:
    """Check if filepath matches any of the glob patterns."""
    return any(glob_match(filepath, p) for p in patterns)


def scan_steering_files(
    targets: list[Path] | None = None, *, include_all: bool = False
) -> list[Path]:
    """Collect all steering files (*.md + *.txt).

    If include_all=True, also scans .kiro/skills/ and .kiro/knowledge/.
    """
    if targets:
        return [t for t in targets if t.exists() and t.is_file()]
    files: list[Path] = []
    # Steering
    for ext in STEERING_EXTENSIONS:
        files.extend(STEERING_DIR.glob(ext))
    # Skills and Knowledge (if requested)
    if include_all:
        if SKILLS_DIR.exists():
            for md in SKILLS_DIR.rglob("*.md"):
                files.append(md)
        if KNOWLEDGE_DIR.exists():
            for ext in STEERING_EXTENSIONS:
                files.extend(KNOWLEDGE_DIR.glob(ext))
    return sorted(set(files))


def scan_steerings_patterns(files: list[Path]) -> dict[str, list[str]]:
    """Scan steering files and extract their fileMatchPatterns."""
    result: dict[str, list[str]] = {}
    for md_file in files:
        fm = parse_front_matter(md_file)
        inclusion = fm.get("inclusion", "always")
        if inclusion == "fileMatch":
            pattern_str = fm.get("fileMatchPattern", "")
            if pattern_str:
                patterns = [p.strip() for p in pattern_str.split(",")]
                result[md_file.stem] = patterns
    return result


def get_git_last_touched(filepath: Path) -> datetime | None:
    """Get the last git commit date that touched a file."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%aI", "--", str(filepath)],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return datetime.fromisoformat(result.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def find_matched_files(patterns: list[str], limit: int = 100) -> list[Path]:
    """Find real project files matching a set of glob patterns."""
    import glob as _glob

    matched: list[Path] = []
    for pat in patterns:
        full_pattern = str(PROJECT_ROOT / pat)
        hits = _glob.glob(full_pattern, recursive=True)
        matched.extend(Path(h) for h in hits[:limit])
    return matched[:limit]


def _relative_path(filepath: Path) -> str:
    """Get a display path relative to PROJECT_ROOT, or absolute if outside."""
    try:
        return str(filepath.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(filepath)


def find_file_by_basename(basename: str) -> Path | None:
    """Search project for a file by basename (for --fix relocation)."""
    for hit in PROJECT_ROOT.rglob(basename):
        if ".venv" not in str(hit) and "__pycache__" not in str(hit):
            return hit
    return None


# User-configurable path prefixes for dead-path detection
_CLI_PATH_PREFIXES: list[str] | None = None


def _get_path_prefixes() -> list[str]:
    """Get source path prefixes for dead-path detection.

    Order of precedence:
    1. CLI --path-prefixes argument
    2. Auto-detect from project structure (common layouts)
    """
    if _CLI_PATH_PREFIXES is not None:
        return _CLI_PATH_PREFIXES

    # Auto-detect common source directories
    candidates = [
        "src",
        "lib",
        "app",
        "scripts",
        "project_health",
        "experiments",
        "tests",
        "packages",
        "cmd",
        "internal",
    ]
    detected = []
    for c in candidates:
        if (PROJECT_ROOT / c).is_dir():
            detected.append(c)
    return detected


# ═══════════════════════════════════════════════════════════════════════════════
# Expected Activations (key files → required steerings)
# ═══════════════════════════════════════════════════════════════════════════════

EXPECTED_ACTIVATIONS: dict[str, list[str]] = {
    "scripts/experiment_runners/bond_resolved/run_accelerated_cross_n.py": [
        "runner-standards",
        "noiseless-runner-patterns",
        "eval-cache-guidelines",
        "accelerated-pipeline",
        "reuse-existing-infrastructure",
    ],
    "scripts/experiment_runners/noiseless/run_noiseless_pipeline.py": [
        "runner-standards",
        "noiseless-runner-patterns",
        "eval-cache-guidelines",
        "reuse-existing-infrastructure",
    ],
    "src/qmbp_simulation/framework/runner_base.py": [
        "runner-standards",
        "code-style",
        "reuse-existing-infrastructure",
    ],
    "src/qmbp_simulation/analysis/metrics.py": [
        "code-style",
        "reuse-existing-infrastructure",
    ],
    "src/qmbp_simulation/execution/eval_cache.py": [
        "eval-cache-guidelines",
        "code-style",
        "reuse-existing-infrastructure",
    ],
    "src/qmbp_simulation/pipeline/accelerated.py": [
        "accelerated-pipeline",
        "code-style",
        "eval-cache-guidelines",
        "reuse-existing-infrastructure",
    ],
}


# ═══════════════════════════════════════════════════════════════════════════════
# Checks
# ═══════════════════════════════════════════════════════════════════════════════


def check_activation(steerings: dict[str, list[str]], report: VerificationReport) -> None:
    """Check 1: Verify expected pattern activations."""
    for filepath, expected in EXPECTED_ACTIVATIONS.items():
        for steer_name in expected:
            if steer_name not in steerings:
                continue  # May be always-included, that's fine
            ok = matches_any(filepath, steerings[steer_name])
            if not ok:
                report.add(
                    "activation",
                    "error",
                    f".kiro/steering/{steer_name}.md",
                    f"Pattern MISS: '{filepath}' should trigger '{steer_name}' "
                    f"but doesn't match patterns: {steerings[steer_name]}",
                )


def check_quality(files: list[Path], report: VerificationReport, *, fix: bool = False) -> None:
    """Check 2: Steering file quality (structure, references, content)."""
    for md_file in files:
        text = md_file.read_text(errors="replace")
        rel_path = _relative_path(md_file)
        body = get_body(md_file)
        fm = parse_front_matter(md_file)
        lines = text.splitlines()

        # Near-empty body
        if len(body.strip()) < 50:
            report.add("quality", "warning", rel_path, "Near-empty body (<50 chars)")

        # Front-matter present but no inclusion key
        if text.startswith("---") and "inclusion" not in fm:
            if fix:
                # Auto-fix: add inclusion: always
                new_text = text.replace("---\n", "---\ninclusion: always\n", 1)
                md_file.write_text(new_text)
                issue = report.add(
                    "quality",
                    "warning",
                    rel_path,
                    "Had front-matter but no 'inclusion' key → FIXED (added 'inclusion: always')",
                )
                issue.fix_applied = True
            else:
                report.add(
                    "quality",
                    "warning",
                    rel_path,
                    "Has front-matter but no 'inclusion' key (use --fix to add 'inclusion: always')",
                )

        # fileMatch mode but empty pattern
        if fm.get("inclusion") == "fileMatch":
            pat = fm.get("fileMatchPattern", "")
            if not pat:
                report.add(
                    "quality",
                    "error",
                    rel_path,
                    "fileMatch mode but no fileMatchPattern defined",
                )

        # Broken #[[file:...]] references
        refs = re.findall(r"#\[\[file:(.*?)\]\]", text)
        for ref in refs:
            ref_path = PROJECT_ROOT / ref
            if not ref_path.exists():
                # Try to find by basename for --fix
                if fix:
                    relocated = find_file_by_basename(Path(ref).name)
                    if relocated:
                        new_ref = _relative_path(relocated)
                        text = text.replace(f"#[[file:{ref}]]", f"#[[file:{new_ref}]]")
                        md_file.write_text(text)
                        issue = report.add(
                            "quality",
                            "warning",
                            rel_path,
                            f"Broken ref #[[file:{ref}]] → FIXED (relocated to {new_ref})",
                        )
                        issue.fix_applied = True
                    else:
                        # Remove the line containing the broken ref
                        new_lines = [ln for ln in text.splitlines() if f"#[[file:{ref}]]" not in ln]
                        md_file.write_text("\n".join(new_lines) + "\n")
                        text = md_file.read_text()
                        issue = report.add(
                            "quality",
                            "warning",
                            rel_path,
                            f"Broken ref #[[file:{ref}]] → FIXED (line removed, file not found)",
                        )
                        issue.fix_applied = True
                else:
                    # Find the line number
                    line_no = next(
                        (i + 1 for i, ln in enumerate(lines) if f"#[[file:{ref}]]" in ln),
                        0,
                    )
                    report.add(
                        "quality",
                        "error",
                        rel_path,
                        f"Broken file reference: #[[file:{ref}]]",
                        line=line_no,
                    )

        # No H1 heading
        if not re.search(r"^#\s+\S", body, re.MULTILINE):
            report.add("quality", "note", rel_path, "No H1 heading found")

        # Detect inline path references to non-existent files
        # Use configurable prefixes or auto-detect from common project layouts
        path_prefixes = _get_path_prefixes()
        if path_prefixes:
            prefix_pattern = "|".join(re.escape(p) for p in path_prefixes)
            path_refs = re.findall(
                rf"(?:{prefix_pattern})/[\w/\-_.]+\.(?:py|ts|js|rs|go|rb)",
                body,
            )
        else:
            path_refs = []
        for pref in set(path_refs):
            if not (PROJECT_ROOT / pref).exists():
                line_no = next((i + 1 for i, ln in enumerate(lines) if pref in ln), 0)
                if fix:
                    relocated = find_file_by_basename(Path(pref).name)
                    if relocated:
                        new_ref = _relative_path(relocated)
                        text = text.replace(pref, new_ref)
                        md_file.write_text(text)
                        issue = report.add(
                            "quality",
                            "note",
                            rel_path,
                            f"Dead path '{pref}' → FIXED (updated to '{new_ref}')",
                            line=line_no,
                        )
                        issue.fix_applied = True
                    else:
                        # Remove the line/paragraph containing the dead path
                        new_lines = [ln for ln in text.splitlines() if pref not in ln]
                        text = "\n".join(new_lines) + "\n"
                        # Clean up triple blank lines left behind
                        text = re.sub(r"\n{3,}", "\n\n", text)
                        md_file.write_text(text)
                        lines = text.splitlines()  # refresh
                        issue = report.add(
                            "quality",
                            "note",
                            rel_path,
                            f"Dead path '{pref}' → FIXED (line removed, file not relocatable)",
                            line=line_no,
                        )
                        issue.fix_applied = True
                else:
                    report.add(
                        "quality",
                        "note",
                        rel_path,
                        f"Dead path reference in prose: '{pref}'",
                        line=line_no,
                    )


def check_overlaps(steerings: dict[str, list[str]], report: VerificationReport) -> None:
    """Check 3: Detect shared patterns between steerings (potential conflicts)."""
    pattern_to_names: dict[str, list[str]] = defaultdict(list)
    for name, pats in steerings.items():
        for p in pats:
            pattern_to_names[p.strip()].append(name)

    for pat, names in sorted(pattern_to_names.items()):
        if len(names) > 1:
            report.add(
                "overlaps",
                "note",
                ".kiro/steering/",
                f"Pattern '{pat}' shared by: {', '.join(names)} (check for contradictions)",
            )


def check_orphans(steerings: dict[str, list[str]], report: VerificationReport) -> None:
    """Check 4: Patterns matching zero existing files."""
    import glob as _glob

    for name, pats in steerings.items():
        for pat in pats:
            # Try glob (handles ** too)
            full_pattern = str(PROJECT_ROOT / pat)
            matches = _glob.glob(full_pattern, recursive=True)
            if not matches:
                report.add(
                    "orphans",
                    "warning",
                    f".kiro/steering/{name}.md",
                    f"Pattern '{pat}' matches zero existing files",
                )


def check_staleness(files: list[Path], stale_days: int, report: VerificationReport) -> None:
    """Check 5: Identify stale steerings not touched recently."""
    now = datetime.now(UTC)

    for md_file in files:
        rel_path = _relative_path(md_file)
        dt = get_git_last_touched(md_file)
        if dt is None:
            continue
        days_ago = (now - dt).days
        if days_ago > stale_days:
            report.add(
                "staleness",
                "note",
                rel_path,
                f"Steering itself not edited in {days_ago} days "
                f"(last: {dt.strftime('%Y-%m-%d')}). May need review.",
            )


def check_split_candidates(files: list[Path], report: VerificationReport) -> None:
    """Check 6: Suggest steerings that could benefit from splitting."""
    for md_file in files:
        rel_path = _relative_path(md_file)
        body = get_body(md_file)
        fm = parse_front_matter(md_file)

        h2_headings = re.findall(r"^##\s+(.+)$", body, re.MULTILINE)
        body_len = len(body)

        # Large file with many sections
        if body_len > 3000 and len(h2_headings) > 5:
            report.add(
                "split",
                "note",
                rel_path,
                f"Large ({body_len} chars) with {len(h2_headings)} sections — "
                f"consider splitting by topic for better context efficiency",
            )

        # Always-included but very large
        inclusion = fm.get("inclusion", "always")
        if inclusion == "always" and body_len > 4000:
            report.add(
                "split",
                "warning",
                rel_path,
                f"Always-included at {body_len} chars (~{body_len // CHARS_PER_TOKEN} tokens) — "
                f"consider converting to fileMatch to reduce context noise",
            )


def check_token_budget(files: list[Path], report: VerificationReport) -> None:
    """Check 7: Estimate total token cost of always-included steerings."""
    always_total = 0
    per_file: list[tuple[str, int]] = []

    for md_file in files:
        fm = parse_front_matter(md_file)
        inclusion = fm.get("inclusion", "always")
        if inclusion == "always":
            text = md_file.read_text(errors="replace")
            tokens = estimate_tokens(text)
            always_total += tokens
            rel_path = _relative_path(md_file)
            per_file.append((rel_path, tokens))

            if tokens > PER_FILE_TOKEN_WARN:
                report.add(
                    "tokens",
                    "warning",
                    rel_path,
                    f"Single steering uses ~{tokens} tokens (>{PER_FILE_TOKEN_WARN} threshold). "
                    f"Consider splitting or converting to fileMatch.",
                )

    report.total_always_tokens = always_total

    if always_total > ALWAYS_TOKEN_BUDGET:
        # Find top offenders
        per_file.sort(key=lambda x: -x[1])
        top3 = ", ".join(f"{Path(f).stem}({t})" for f, t in per_file[:3])
        report.add(
            "tokens",
            "warning",
            ".kiro/steering/",
            f"Total always-included context: ~{always_total} tokens "
            f"(budget: {ALWAYS_TOKEN_BUDGET}). Top: {top3}. "
            f"Risk of context rot — convert some to fileMatch.",
        )


def check_hooks_coherence(report: VerificationReport) -> None:
    """Check 8: Verify hooks reference steerings/files that actually exist."""
    if not HOOKS_DIR.exists():
        return

    steering_names = {p.stem for ext in STEERING_EXTENSIONS for p in STEERING_DIR.glob(ext)}

    for hook_file in sorted(HOOKS_DIR.glob("*.kiro.hook")):
        try:
            hook_data = json.loads(hook_file.read_text())
        except (json.JSONDecodeError, OSError):
            report.add(
                "hooks",
                "error",
                _relative_path(hook_file),
                "Invalid JSON in hook file",
            )
            continue

        prompt = hook_data.get("then", {}).get("prompt", "")
        rel_path = _relative_path(hook_file)

        # Check if hook references steering names that don't exist
        # Look for patterns like "module-index.md", "reuse-workflow steering" etc.
        # Exclude generic words and non-steering .md references
        referenced_steerings = re.findall(
            r'\b([\w][\w-]{2,})\.md\b|steering\s+(?:file\s+)?["\']?([\w-]{3,})',
            prompt,
            re.IGNORECASE,
        )
        generic_names = {
            "readme",
            "changelog",
            "contributing",
            "license",
            "file",
            "the",
            "this",
            "that",
            "your",
            "from",
            "check",
            "verify",
            "ensure",
            "module",
            "existing",
        }
        for match_groups in referenced_steerings:
            ref_name = match_groups[0] or match_groups[1]
            if ref_name and ref_name not in steering_names:
                if ref_name.lower() not in generic_names and len(ref_name) > 4:
                    report.add(
                        "hooks",
                        "warning",
                        rel_path,
                        f"Hook prompt references '{ref_name}' which is not a known steering file",
                    )

        # Check hook schema basics
        if "when" not in hook_data or "then" not in hook_data:
            report.add(
                "hooks",
                "error",
                rel_path,
                "Hook missing required 'when' or 'then' section",
            )
        elif "type" not in hook_data.get("when", {}):
            report.add(
                "hooks",
                "error",
                rel_path,
                "Hook 'when' section missing 'type' field",
            )

        # Check enabled/disabled awareness
        if hook_data.get("enabled") is False:
            report.add(
                "hooks",
                "note",
                rel_path,
                f"Hook '{hook_data.get('name', hook_file.stem)}' is disabled",
            )


def check_contradictions(files: list[Path], report: VerificationReport) -> None:
    """Check 9: Detect potential contradictions between steerings.

    Heuristic: finds NEVER/ALWAYS/MUST NOT directives and checks if
    another steering says the opposite about the same subject.
    """
    # Collect directive patterns: (keyword, subject, file)
    directives: list[tuple[str, str, str]] = []
    directive_re = re.compile(
        r"\b(NEVER|ALWAYS|MUST NOT|MUST|DO NOT|FORBIDDEN)\b\s+(.{5,60}?)(?:\.|$|\n)",
        re.IGNORECASE,
    )

    for md_file in files:
        body = get_body(md_file)
        rel_path = _relative_path(md_file)
        for m in directive_re.finditer(body):
            keyword = m.group(1).upper()
            subject = m.group(2).strip().lower()
            # Normalize: remove filler words
            subject = re.sub(r"\b(use|the|a|an|in|to|for|with|from)\b", "", subject).strip()
            if len(subject) > 5:
                directives.append((keyword, subject, rel_path))

    # Look for conflicts: NEVER X in one file, ALWAYS X in another
    opposites = {
        "NEVER": {"ALWAYS", "MUST"},
        "MUST NOT": {"ALWAYS", "MUST"},
        "DO NOT": {"ALWAYS", "MUST"},
        "FORBIDDEN": {"ALWAYS", "MUST"},
        "ALWAYS": {"NEVER", "MUST NOT", "DO NOT", "FORBIDDEN"},
        "MUST": {"NEVER", "MUST NOT", "DO NOT", "FORBIDDEN"},
    }

    # Simple jaccard similarity on subject words
    def word_sim(a: str, b: str) -> float:
        wa, wb = set(a.split()), set(b.split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    seen_conflicts: set[tuple[str, str]] = set()
    for i, (kw1, subj1, file1) in enumerate(directives):
        for kw2, subj2, file2 in directives[i + 1 :]:
            if file1 == file2:
                continue
            if kw2 in opposites.get(kw1, set()):
                if word_sim(subj1, subj2) > 0.5:
                    key = tuple(sorted([file1, file2]))
                    conflict_desc = f"'{kw1} {subj1[:40]}' vs '{kw2} {subj2[:40]}'"
                    if (key[0], conflict_desc) not in seen_conflicts:
                        seen_conflicts.add((key[0], conflict_desc))
                        report.add(
                            "contradictions",
                            "warning",
                            file1,
                            f"Potential contradiction with {Path(file2).stem}: {conflict_desc}",
                        )


def check_duplicates(files: list[Path], report: VerificationReport) -> None:
    """Check 10: Detect steering files with high content overlap (>40% n-gram jaccard)."""

    def ngrams(text: str, n: int = 3) -> set[str]:
        words = text.lower().split()
        return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}

    bodies: list[tuple[str, set[str]]] = []
    for md_file in files:
        body = get_body(md_file)
        rel_path = _relative_path(md_file)
        grams = ngrams(body)
        if len(grams) > 10:  # Skip tiny files
            bodies.append((rel_path, grams))

    seen: set[tuple[str, str]] = set()
    for i, (path_a, grams_a) in enumerate(bodies):
        for path_b, grams_b in bodies[i + 1 :]:
            intersection = len(grams_a & grams_b)
            union = len(grams_a | grams_b)
            if union > 0:
                similarity = intersection / union
                if similarity > 0.40:
                    key = tuple(sorted([path_a, path_b]))
                    if key not in seen:
                        seen.add(key)
                        report.add(
                            "duplicates",
                            "warning",
                            path_a,
                            f"{int(similarity * 100)}% content overlap with "
                            f"{Path(path_b).stem} — consider merging or deduplicating",
                        )


def check_cross_filematch_redundancy(
    files: list[Path],
    steerings: dict[str, list[str]],
    report: VerificationReport,
    *,
    fix: bool = False,
) -> None:
    """Check 11: Detect repeated paragraphs between steerings with overlapping fileMatch.

    When two steerings share fileMatchPattern overlap (i.e., both activate for the
    same set of files), duplicated content wastes context tokens. The fix removes
    the duplicated paragraph from the OLDER file (by git date), keeping only the
    newer version.

    This is distinct from check_duplicates (check 10) which looks at whole-file
    similarity. This check finds *specific shared paragraphs* between steerings
    that co-activate — meaning the agent sees both simultaneously.
    """
    from difflib import SequenceMatcher

    # Build co-activation map: which steerings activate together?
    # Two fileMatch steerings co-activate if their patterns overlap.
    filematch_files: list[tuple[Path, str, list[str]]] = []
    for md_file in files:
        fm = parse_front_matter(md_file)
        if fm.get("inclusion") == "fileMatch":
            pats = [p.strip() for p in fm.get("fileMatchPattern", "").split(",") if p.strip()]
            if pats:
                filematch_files.append((md_file, md_file.stem, pats))

    # Also include always-included steerings (they co-activate with EVERYTHING)
    always_files: list[Path] = []
    for md_file in files:
        fm = parse_front_matter(md_file)
        if fm.get("inclusion", "always") == "always":
            always_files.append(md_file)

    def patterns_overlap(pats_a: list[str], pats_b: list[str]) -> bool:
        """Check if two pattern sets could activate for the same file."""
        # Quick heuristic: if any pattern in A matches the glob structure of B
        for pa in pats_a:
            for pb in pats_b:
                # If both are identical or one is a subset of the other
                if pa == pb:
                    return True
                # Check if they share a directory prefix
                dir_a = pa.split("/")[0] if "/" in pa else ""
                dir_b = pb.split("/")[0] if "/" in pb else ""
                if dir_a and dir_b and dir_a == dir_b:
                    return True
                # ** patterns overlap with everything in their domain
                if pa.startswith("**/") or pb.startswith("**/"):
                    suffix_a = pa.split("**/")[-1] if "**/" in pa else ""
                    suffix_b = pb.split("**/")[-1] if "**/" in pb else ""
                    if suffix_a and suffix_b:
                        if fnmatch.fnmatch(suffix_a, suffix_b) or fnmatch.fnmatch(
                            suffix_b, suffix_a
                        ):
                            return True
        return False

    def extract_paragraphs(text: str) -> list[str]:
        """Split body into meaningful paragraphs (>30 chars, non-empty)."""
        body = get_body(Path("/dev/null"))  # won't use, we pass text directly
        # Actually parse from text
        match = re.match(r"^---\s*\n.*?\n---\s*\n?", text, re.DOTALL)
        body_text = text[match.end() :] if match else text
        # Split on double newlines or heading boundaries
        raw_paragraphs = re.split(r"\n\n+", body_text)
        return [p.strip() for p in raw_paragraphs if len(p.strip()) > 50]

    # Check pairs of co-activating steerings
    pairs_to_check: list[tuple[Path, Path]] = []

    # fileMatch vs fileMatch
    for i, (file_a, name_a, pats_a) in enumerate(filematch_files):
        for file_b, name_b, pats_b in filematch_files[i + 1 :]:
            if patterns_overlap(pats_a, pats_b):
                pairs_to_check.append((file_a, file_b))

    # always vs fileMatch (always co-activates with everything)
    for always_file in always_files:
        for fm_file, _, _ in filematch_files:
            pairs_to_check.append((always_file, fm_file))

    # always vs always
    for i, file_a in enumerate(always_files):
        for file_b in always_files[i + 1 :]:
            pairs_to_check.append((file_a, file_b))

    # Now find shared paragraphs in co-activating pairs
    seen_pairs: set[tuple[str, str]] = set()
    for file_a, file_b in pairs_to_check:
        key = tuple(sorted([str(file_a), str(file_b)]))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)

        text_a = file_a.read_text(errors="replace")
        text_b = file_b.read_text(errors="replace")
        paras_a = extract_paragraphs(text_a)
        paras_b = extract_paragraphs(text_b)

        for para_a in paras_a:
            for para_b in paras_b:
                # Use SequenceMatcher for paragraph-level similarity
                ratio = SequenceMatcher(None, para_a, para_b).ratio()
                if ratio > 0.75 and len(para_a) > 80:
                    rel_a = _relative_path(file_a)
                    rel_b = _relative_path(file_b)
                    snippet = para_a[:60].replace("\n", " ")

                    if fix:
                        # Determine which is older by git date
                        date_a = get_git_last_touched(file_a)
                        date_b = get_git_last_touched(file_b)
                        # Remove from older file (keep newer)
                        if date_a and date_b:
                            older_file = file_a if date_a < date_b else file_b
                            older_rel = rel_a if date_a < date_b else rel_b
                        else:
                            older_file = file_a  # Default: remove from first
                            older_rel = rel_a

                        older_text = older_file.read_text(errors="replace")
                        # Find and remove the duplicated paragraph
                        older_para = para_a if older_file == file_a else para_b
                        if older_para in older_text:
                            new_text = older_text.replace(older_para, "", 1)
                            # Clean up double blank lines
                            new_text = re.sub(r"\n{3,}", "\n\n", new_text)
                            older_file.write_text(new_text)
                            issue = report.add(
                                "cross-redundancy",
                                "warning",
                                older_rel,
                                f"Removed duplicated paragraph (also in "
                                f"{Path(rel_b if older_file == file_a else rel_a).stem}): "
                                f"'{snippet}...'",
                            )
                            issue.fix_applied = True
                    else:
                        report.add(
                            "cross-redundancy",
                            "warning",
                            rel_a,
                            f"Shared paragraph with {Path(rel_b).stem} "
                            f"(both co-activate, {int(ratio * 100)}% match): "
                            f"'{snippet}...' — use --fix to remove from older",
                        )
                    break  # One match per paragraph pair is enough


def check_clarity(files: list[Path], report: VerificationReport, *, fix: bool = False) -> None:
    """Check 12: Instruction clarity via skillsaw (opt-in, requires pip install skillsaw).

    skillsaw detects: vague language, contradictions, attention dead zones,
    weak instructions, inconsistent terminology, excessive section length,
    and other patterns that reduce agent compliance.

    When --fix is active, auto-removes weak language hedges:
    - "correctly" → removed
    - "properly" → removed
    - "try to" → removed (replaced with direct action)
    - "where possible" → removed
    - "if possible" → removed

    Only runs if skillsaw is installed. Skips silently otherwise.
    """
    try:
        from skillsaw import Linter as SkillsawLinter
        from skillsaw import RepositoryContext
        from skillsaw import Severity as SSeverity
    except ImportError:
        report.add(
            "clarity",
            "note",
            ".kiro/steering/",
            "skillsaw not installed — skipping clarity checks (pip install skillsaw to enable)",
        )
        return

    # Map skillsaw severity to our severity
    severity_map = {
        SSeverity.ERROR: "error",
        SSeverity.WARNING: "warning",
        SSeverity.INFO: "note",
    }

    # Build context from the steering directory (+ skills/knowledge if present)
    try:
        steering_rel = _relative_path(STEERING_DIR)
        content_paths = [steering_rel]
        if SKILLS_DIR.exists():
            content_paths.append(_relative_path(SKILLS_DIR))
        if KNOWLEDGE_DIR.exists():
            content_paths.append(_relative_path(KNOWLEDGE_DIR))
        ctx = RepositoryContext(
            root_path=PROJECT_ROOT,
            content_paths=content_paths,
        )
        linter = SkillsawLinter(context=ctx)
        violations = linter.run()
    except Exception as e:
        report.add(
            "clarity",
            "note",
            ".kiro/steering/",
            f"skillsaw failed to run: {e}",
        )
        return

    # Filter: only report violations for files in our scan scope
    scanned_paths = {f.resolve() for f in files}

    # Rules we consider noise for steering files (too chatty, not actionable)
    skip_rules = {
        "content-unlinked-internal-reference",
        "content-inconsistent-terminology",  # Cross-file issue, not fixable per-file
        "content-section-length",  # Advisory, sections are intentionally dense
    }

    # Collect weak-language fixes to apply per file
    weak_lang_fixes: dict[Path, list[tuple[str, str, str]]] = defaultdict(list)
    # Patterns skillsaw detects as weak language — we can auto-remove these
    # Patterns skillsaw detects as weak language — we can auto-remove these
    # These regexes are designed to remove hedge words while preserving grammar
    WEAK_PATTERNS: dict[str, tuple[str, str]] = {
        "correctly": (r"\b(\w+)\s+correctly\b", r"\1"),  # "cite correctly" → "cite"
        "properly": (r"\b(\w+)\s+properly\b", r"\1"),  # "handle properly" → "handle"
        "try to": (r"\btry to\s+", ""),  # "try to run" → "run"
        "where possible": (r",?\s*where possible", ""),  # ", where possible" → ""
        "if possible": (r",?\s*if possible", ""),  # ", if possible" → ""
    }

    for v in violations:
        if v.rule_id in skip_rules:
            continue
        vpath = Path(v.file_path).resolve() if v.file_path else None
        if vpath and vpath not in scanned_paths:
            continue

        sev = severity_map.get(v.severity, "note")
        rel_file = _relative_path(Path(v.file_path)) if v.file_path else ".kiro/steering/"

        # Skip context-budget violations for manual-inclusion files (they don't consume
        # context unless explicitly referenced — the limit is irrelevant)
        if v.rule_id == "context-budget" and v.file_path:
            fm = parse_front_matter(Path(v.file_path))
            if fm.get("inclusion") == "manual":
                continue  # Manual files don't auto-load — budget is irrelevant

            # --fix: if it's always-included and exceeds error limit, convert to manual
            if fix and sev == "error" and fm.get("inclusion", "always") == "always":
                fpath = Path(v.file_path)
                text = fpath.read_text(errors="replace")
                if text.startswith("---"):
                    # Has front-matter — change inclusion to manual
                    text = re.sub(
                        r"^(---\n(?:.*\n)*?)inclusion:\s*always",
                        r"\1inclusion: manual",
                        text,
                    )
                else:
                    # No front-matter — add one
                    text = "---\ninclusion: manual\n---\n\n" + text
                fpath.write_text(text)
                issue = report.add(
                    "clarity",
                    sev,
                    rel_file,
                    f"[{v.rule_id}] Exceeds error limit → FIXED (converted to manual inclusion)",
                    line=v.file_line or 0,
                )
                issue.fix_applied = True
                continue

        # If --fix and it's a weak-language violation, queue the fix
        if fix and v.rule_id == "content-weak-language" and v.file_path:
            # Extract the offending word from the message
            # Message format: "Weak language (vagueness): 'correctly' — Remove..."
            word_match = re.search(r"'(\w[\w\s]*?)'", v.message)
            if word_match:
                word = word_match.group(1).lower()
                if word in WEAK_PATTERNS:
                    weak_lang_fixes[Path(v.file_path)].append(
                        (word, WEAK_PATTERNS[word][0], WEAK_PATTERNS[word][1])
                    )
                    issue = report.add(
                        "clarity",
                        sev,
                        rel_file,
                        f"[{v.rule_id}] '{word}' → FIXED (removed hedge)",
                        line=v.file_line or 0,
                    )
                    issue.fix_applied = True
                    continue

        report.add(
            "clarity",
            sev,
            rel_file,
            f"[{v.rule_id}] {v.message}",
            line=v.file_line or 0,
        )

    # Apply accumulated weak-language fixes
    if fix and weak_lang_fixes:
        for filepath, fixes in weak_lang_fixes.items():
            text = filepath.read_text(errors="replace")
            # Only apply substitutions OUTSIDE code blocks
            parts = re.split(r"(```[\s\S]*?```)", text)
            for i, part in enumerate(parts):
                if part.startswith("```"):
                    continue  # Skip code blocks entirely
                for word, pattern, replacement in fixes:
                    parts[i] = re.sub(pattern, replacement, parts[i], flags=re.IGNORECASE)
            filepath.write_text("".join(parts))


# ═══════════════════════════════════════════════════════════════════════════════
# Check 13: Module Index Integrity
# ═══════════════════════════════════════════════════════════════════════════════


def check_module_index(report: VerificationReport) -> None:
    """Check 13: Verify module-index.md symbols are importable (no phantoms).

    Runs `generate_module_index.py --verify` and captures results.
    This catches stale entries where functions/classes were removed from
    code but the index wasn't regenerated.
    """
    import subprocess

    generator = PROJECT_ROOT / "scripts" / "maintenance" / "generate_module_index.py"
    if not generator.exists():
        return

    index_file = STEERING_DIR / "module-index.md"
    if not index_file.exists():
        report.add(
            "module-index",
            "warning",
            "module-index.md",
            "module-index.md does not exist — run generate_module_index.py",
        )
        return

    try:
        result = subprocess.run(
            [sys.executable, str(generator), "--verify"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROJECT_ROOT),
        )

        # Parse stderr for PHANTOM warnings
        phantoms = [line.strip() for line in result.stderr.splitlines() if "PHANTOM" in line]

        if phantoms:
            for phantom in phantoms[:10]:
                # Extract the symbol path from "⚠️  PHANTOM: module.path.symbol (reason)"
                symbol = phantom.split("PHANTOM:")[-1].strip().split("(")[0].strip()
                report.add(
                    "module-index",
                    "error",
                    "module-index.md",
                    f"Phantom symbol: {symbol} — listed in index but not importable. "
                    f"Run:.venv/bin/python scripts/general_project_maintenance/generate_module_index.py",
                )
            if len(phantoms) > 10:
                report.add(
                    "module-index",
                    "error",
                    "module-index.md",
                    f"... and {len(phantoms) - 10} more phantom symbols",
                )
        elif result.returncode != 0:
            # Some other failure
            report.add(
                "module-index",
                "warning",
                "module-index.md",
                f"Verification failed: {result.stderr[:200]}",
            )
    except subprocess.TimeoutExpired:
        report.add(
            "module-index",
            "warning",
            "module-index.md",
            "Module index verification timed out (>30s)",
        )
    except (OSError, FileNotFoundError) as e:
        report.add(
            "module-index",
            "note",
            "module-index.md",
            f"Could not run verification: {e}",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# CLI and Main
# ═══════════════════════════════════════════════════════════════════════════════

ALL_CHECKS = [
    "activation",
    "quality",
    "overlaps",
    "orphans",
    "staleness",
    "split",
    "tokens",
    "hooks",
    "contradictions",
    "duplicates",
    "cross-redundancy",
    "clarity",
    "module-index",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify steering files: patterns, quality, tokens, hooks, contradictions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                  # Full check (all steerings)
  %(prog)s .kiro/steering/code-style.md     # Check one file
  %(prog)s --check quality tokens hooks     # Only specific checks
  %(prog)s --fix                            # Auto-fix trivial issues
  %(prog)s --json                           # JSON report (for CI)
  %(prog)s --sarif                          # SARIF output (GitHub code scanning)
  %(prog)s --stale-days 30                  # Custom staleness threshold
""",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        type=Path,
        default=None,
        help="Specific steering file(s) or folder to check. Default: all.",
    )
    parser.add_argument(
        "--check",
        nargs="+",
        choices=ALL_CHECKS,
        default=None,
        help=f"Run only these checks. Options: {', '.join(ALL_CHECKS)}",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=DEFAULT_STALE_DAYS,
        help=f"Days threshold for staleness check (default: {DEFAULT_STALE_DAYS})",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Auto-fix trivial issues (missing inclusion key, broken refs)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output JSON report instead of human-readable text",
    )
    parser.add_argument(
        "--sarif",
        action="store_true",
        help="Output SARIF v2.1.0 (for GitHub code scanning)",
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        default=ALWAYS_TOKEN_BUDGET,
        help=f"Max tokens for always-included steerings (default: {ALWAYS_TOKEN_BUDGET})",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Only print issues, no passing checks",
    )
    parser.add_argument(
        "--all",
        "-a",
        action="store_true",
        dest="include_all",
        help="Also scan .kiro/skills/ and .kiro/knowledge/ (not just steering/)",
    )
    parser.add_argument(
        "--steering-dir",
        type=Path,
        default=None,
        help="Path to steering directory (default: .kiro/steering/). "
        "Use for non-Kiro projects or alternate layouts.",
    )
    parser.add_argument(
        "--hooks-dir",
        type=Path,
        default=None,
        help="Path to hooks directory (default: .kiro/hooks/).",
    )
    parser.add_argument(
        "--path-prefixes",
        nargs="*",
        default=None,
        help="Source code prefixes for dead-path detection "
        "(default: auto-detect from project). E.g. src/ lib/ app/",
    )
    return parser.parse_args()


def resolve_targets(targets: list[Path] | None) -> list[Path] | None:
    """Resolve CLI targets to a list of steering files."""
    if not targets:
        return None
    resolved: list[Path] = []
    for t in targets:
        if not t.is_absolute():
            t = PROJECT_ROOT / t
        if t.is_dir():
            for ext in STEERING_EXTENSIONS:
                resolved.extend(sorted(t.glob(ext)))
        elif t.is_file() and t.suffix in (".md", ".txt"):
            resolved.append(t)
        else:
            print(f"  ⚠ Skipping '{t}' (not .md/.txt or directory)", file=sys.stderr)
    return resolved if resolved else None


def print_human_report(report: VerificationReport, *, quiet: bool = False) -> None:
    """Print human-readable verification output."""
    print("=" * 60)
    print("  Steering Verification Tool v2.0")
    print("=" * 60)
    print(f"\n  Files: {report.files_scanned} steerings")
    print(f"  Checks: {', '.join(report.checks_run)}")
    print(f"  Always-included tokens: ~{report.total_always_tokens}")
    print()

    # Group issues by check
    by_check: dict[str, list[Issue]] = defaultdict(list)
    for issue in report.issues:
        by_check[issue.check].append(issue)

    for check_name in report.checks_run:
        issues = by_check.get(check_name, [])
        label = check_name.upper()
        if not issues:
            if not quiet:
                print(f"─── {label} ───")
                print("  ✓ All passed")
                print()
        else:
            print(f"─── {label} ({len(issues)} issues) ───")
            for issue in issues:
                icon = {"error": "✗", "warning": "⚠", "note": "💡"}[issue.severity]
                fixed = " [FIXED]" if issue.fix_applied else ""
                print(f"  {icon} {issue.file}: {issue.message}{fixed}")
            print()

    # Score
    print("=" * 60)
    score = report.score
    if score >= 90:
        grade = "A"
    elif score >= 75:
        grade = "B"
    elif score >= 60:
        grade = "C"
    elif score >= 40:
        grade = "D"
    else:
        grade = "F"

    summary = report.to_json()["summary"]
    print(
        f"  Health Score: {score}/100 (Grade: {grade})"
        f"  |  E:{summary['errors']} W:{summary['warnings']} "
        f"N:{summary['notes']} Fixed:{summary['fixed']}"
    )
    if report.passed:
        print("  ✅ PASSED (no unresolved errors)")
    else:
        print("  ❌ FAILED (has unresolved errors)")


def main() -> int:
    args = parse_args()
    checks_to_run = set(args.check) if args.check else set(ALL_CHECKS)
    targets = resolve_targets(args.targets)

    # Configurable directories (for generic project support)
    global ALWAYS_TOKEN_BUDGET, STEERING_DIR, HOOKS_DIR, _CLI_PATH_PREFIXES
    ALWAYS_TOKEN_BUDGET = args.token_budget
    if args.steering_dir:
        sd = args.steering_dir
        STEERING_DIR = sd if sd.is_absolute() else PROJECT_ROOT / sd
    if args.hooks_dir:
        hd = args.hooks_dir
        HOOKS_DIR = hd if hd.is_absolute() else PROJECT_ROOT / hd
    if args.path_prefixes is not None:
        _CLI_PATH_PREFIXES = args.path_prefixes

    # Collect files
    files = scan_steering_files(targets, include_all=args.include_all)
    steerings = scan_steerings_patterns(files)

    report = VerificationReport()
    report.files_scanned = len(files)
    report.checks_run = sorted(checks_to_run)

    # Run checks
    if "activation" in checks_to_run and not targets:
        check_activation(steerings, report)

    if "quality" in checks_to_run:
        check_quality(files, report, fix=args.fix)

    if "overlaps" in checks_to_run and not targets:
        check_overlaps(steerings, report)

    if "orphans" in checks_to_run:
        check_orphans(steerings, report)

    if "staleness" in checks_to_run:
        check_staleness(files, args.stale_days, report)

    if "split" in checks_to_run:
        check_split_candidates(files, report)

    if "tokens" in checks_to_run:
        check_token_budget(files, report)

    if "hooks" in checks_to_run and not targets:
        check_hooks_coherence(report)

    if "contradictions" in checks_to_run and not targets:
        check_contradictions(files, report)

    if "duplicates" in checks_to_run and not targets:
        check_duplicates(files, report)

    if "cross-redundancy" in checks_to_run and not targets:
        check_cross_filematch_redundancy(files, steerings, report, fix=args.fix)

    if "clarity" in checks_to_run:
        check_clarity(files, report, fix=args.fix)

    if "module-index" in checks_to_run and not targets:
        check_module_index(report)

    # Output
    if args.sarif:
        json.dump(report.to_sarif(), sys.stdout, indent=2)
        print()
    elif args.json_output:
        json.dump(report.to_json(), sys.stdout, indent=2)
        print()
    else:
        print_human_report(report, quiet=args.quiet)

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
