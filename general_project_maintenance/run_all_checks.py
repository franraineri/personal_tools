#!/usr/bin/env python3
"""Unified maintenance checks runner.

Orchestrates all maintenance tools — both custom scripts and third-party
analyzers — in a single command with unified reporting.

Checks (in execution order):
  1. pyclean       — Remove Python bytecode caches (fast, always safe)
  2. vulture       — Detect dead code (unused functions, classes, variables)
  3. pydoclint     — Verify docstring ↔ signature consistency
  4. phantom       — Detect phantom imports (symbol imported but doesn't exist)
  5. steerings     — Verify .kiro/steering files integrity
  6. module-index  — Check module-index.md freshness

Usage:
    # Run all checks (default):
   .venv/bin/python scripts/general_project_maintenance/run_all_checks.py

    # Run specific checks only:
   .venv/bin/python scripts/general_project_maintenance/run_all_checks.py --only vulture phantom

    # Skip specific checks:
   .venv/bin/python scripts/general_project_maintenance/run_all_checks.py --skip pyclean pydoclint

    # JSON report:
   .venv/bin/python scripts/general_project_maintenance/run_all_checks.py --json

    # Fix mode (pyclean executes, steerings --fix):
   .venv/bin/python scripts/general_project_maintenance/run_all_checks.py --fix

    # Verbose (show full output from each tool):
   .venv/bin/python scripts/general_project_maintenance/run_all_checks.py -v

    # CI mode (JSON + non-zero exit on errors):
   .venv/bin/python scripts/general_project_maintenance/run_all_checks.py --ci
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# ─── Constants ───────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

ALL_CHECKS = [
    "pyclean",
    "vulture",
    "pydoclint",
    "phantom",
    "steerings",
    "module-index",
]


# ─── Data Models ─────────────────────────────────────────────────────────────

Status = Literal["pass", "fail", "warn", "skip", "error"]

STATUS_ICONS: dict[Status, str] = {
    "pass": "✅",
    "fail": "❌",
    "warn": "⚠️ ",
    "skip": "⏭️ ",
    "error": "💥",
}


@dataclass
class CheckResult:
    """Result from a single check."""

    name: str
    status: Status
    duration_s: float = 0.0
    n_issues: int = 0
    summary: str = ""
    details: list[str] = field(default_factory=list)
    exit_code: int = 0


@dataclass
class FullReport:
    """Aggregated report from all checks."""

    results: list[CheckResult] = field(default_factory=list)
    total_duration_s: float = 0.0

    @property
    def passed(self) -> bool:
        return all(r.status in ("pass", "skip", "warn") for r in self.results)

    @property
    def score(self) -> int:
        if not self.results:
            return 100
        active = [r for r in self.results if r.status != "skip"]
        if not active:
            return 100
        passed = sum(1 for r in active if r.status == "pass")
        return int(100 * passed / len(active))

    def to_json(self) -> dict:
        return {
            "passed": self.passed,
            "score": self.score,
            "total_duration_s": round(self.total_duration_s, 2),
            "checks": [
                {
                    "name": r.name,
                    "status": r.status,
                    "duration_s": round(r.duration_s, 2),
                    "n_issues": r.n_issues,
                    "summary": r.summary,
                    "details": r.details[:20],  # Cap details in JSON
                }
                for r in self.results
            ],
        }


# ─── Check Implementations ──────────────────────────────────────────────────


def _run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd or PROJECT_ROOT,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"TIMEOUT after {timeout}s"
    except FileNotFoundError:
        return -2, "", f"Command not found: {cmd[0]}"


def check_pyclean(*, fix: bool = False, verbose: bool = False) -> CheckResult:
    """Run pyclean to remove/detect Python bytecode caches."""
    t0 = time.time()

    pyclean_bin = PROJECT_ROOT / ".venv" / "bin" / "pyclean"
    if not pyclean_bin.exists():
        return CheckResult(
            name="pyclean",
            status="skip",
            summary="pyclean not installed (pip install pyclean)",
        )

    if fix:
        # Actually clean
        rc, stdout, stderr = _run_command([str(pyclean_bin), str(PROJECT_ROOT)])
        duration = time.time() - t0
        if rc == 0:
            return CheckResult(
                name="pyclean",
                status="pass",
                duration_s=duration,
                summary="Bytecode caches cleaned",
            )
        return CheckResult(
            name="pyclean",
            status="error",
            duration_s=duration,
            summary=f"pyclean failed: {stderr[:200]}",
            exit_code=rc,
        )
    else:
        # Dry-run: just check if __pycache__ dirs exist
        pycache_dirs = list(PROJECT_ROOT.rglob("__pycache__"))
        # Filter out .venv
        pycache_dirs = [d for d in pycache_dirs if ".venv" not in str(d)]
        duration = time.time() - t0

        if not pycache_dirs:
            return CheckResult(
                name="pyclean",
                status="pass",
                duration_s=duration,
                summary="No bytecode caches found",
            )
        return CheckResult(
            name="pyclean",
            status="warn",
            duration_s=duration,
            n_issues=len(pycache_dirs),
            summary=f"{len(pycache_dirs)} __pycache__ dirs found (run with --fix to clean)",
            details=[str(d.relative_to(PROJECT_ROOT)) for d in pycache_dirs[:10]],
        )


def _generate_vulture_whitelist(lines: list[str]) -> int:
    """Auto-generate vulture_whitelist.py from detected false positives.

    Known false-positive patterns that get whitelisted automatically:
    - Signal handlers (signum, frame) — required by signal.signal() API
    - Context manager __exit__ params (exc_type, exc_val, exc_tb) — required by protocol
    - Function parameters that are part of an interface contract
    - Unreachable code after return (usually dead branches left intentionally)

    Vulture whitelist format: just use the variable names as bare expressions.
    Vulture sees them as "used" and stops reporting them.

    Returns the number of whitelisted items.
    """
    import re

    whitelist_path = PROJECT_ROOT / "vulture_whitelist.py"

    # Names that are always false positives (interface-mandated params)
    ALWAYS_WHITELIST = {
        # Signal handlers — Python's signal.signal() requires (signum, frame) signature
        "signum",
        "frame",
        # Context manager __exit__ — protocol requires (exc_type, exc_val, exc_tb)
        "exc_type",
        "exc_val",
        "exc_tb",
        # runner_base hook method params (overridden by subclasses)
        "resumed_data",
        "resumed_sections",
        "checkpoint_name",
        # Function params that are API contract (used by callers, not internally)
        "min_quality_score",
        "require_variational",
        "check_gt_coherence",
    }

    # Parse vulture output to determine what to whitelist
    whitelisted_names: set[str] = set()
    remaining: list[str] = []

    for line in lines:
        # Format: "name  # unused variable (path:line)"
        # or: "# unreachable code after 'return' (path:line)"
        match = re.match(r"(\w+)\s+#\s+unused\s+\w+\s+\((.+):(\d+)\)", line)
        if match:
            name = match.group(1)
            if name in ALWAYS_WHITELIST:
                whitelisted_names.add(name)
            else:
                remaining.append(line)
        elif "unreachable code" in line:
            # Can't whitelist unreachable code via whitelist file
            remaining.append(line)
        else:
            remaining.append(line)

    if not whitelisted_names and not whitelist_path.exists():
        return 0

    # Write vulture-compatible whitelist (bare variable names as expressions)
    content_lines = [
        '"""Vulture whitelist — auto-generated by run_all_checks.py --fix.',
        "",
        "Items here are known false positives (interface-mandated params, signal handlers,",
        "context manager protocol params, etc.). Vulture will ignore these.",
        "",
        "Regenerate: .venv/bin/python scripts/general_project_maintenance/run_all_checks.py --fix",
        '"""',
        "",
        "# Signal handler params (required by signal.signal() callback signature)",
        "signum  # noqa",
        "frame  # noqa",
        "",
        "# Context manager __exit__ params (required by protocol)",
        "exc_type  # noqa",
        "exc_val  # noqa",
        "exc_tb  # noqa",
        "",
        "# ValidationRunner hook/interface params (used by subclass overrides)",
        "resumed_data  # noqa",
        "resumed_sections  # noqa",
        "checkpoint_name  # noqa",
        "min_quality_score  # noqa",
        "",
        "# Function API flags (part of public interface, may not be used internally yet)",
        "require_variational  # noqa",
        "check_gt_coherence  # noqa",
    ]

    whitelist_path.write_text("\n".join(content_lines) + "\n")
    return len(ALWAYS_WHITELIST)


def _fix_unreachable_code(unreachable_lines: list[str]) -> int:
    """Remove unreachable code blocks detected by vulture.

    Vulture reports: "# unreachable code after 'return' (path:line)"
    This function removes the dead code from the source files.

    Strategy: find the return statement, then remove all code at the same
    function-body indent level until the next def/class or dedented line.

    Returns the number of blocks fixed.
    """
    import re

    fixed = 0

    for report_line in unreachable_lines:
        # Parse: "# unreachable code after 'return' (path:line)"
        match = re.search(r"\((.+):(\d+)\)", report_line)
        if not match:
            continue

        filepath = PROJECT_ROOT / match.group(1)
        dead_start = int(match.group(2))  # Line number where dead code starts (1-indexed)

        if not filepath.exists():
            continue

        source_lines = filepath.read_text().splitlines()

        if dead_start - 1 >= len(source_lines):
            continue

        dead_line = source_lines[dead_start - 1]
        dead_indent = len(dead_line) - len(dead_line.lstrip())

        # Find the end of the dead block: scan forward until we find a line that:
        # 1. Has less indent than the dead code (we've left the block), OR
        # 2. Is a def/class at the same or lesser indent (new function/method)
        end_idx = dead_start - 1  # 0-indexed, inclusive

        for i in range(dead_start - 1, len(source_lines)):
            line = source_lines[i]
            stripped = line.strip()

            # Blank lines are part of the block
            if not stripped:
                end_idx = i
                continue

            line_indent = len(line) - len(line.lstrip())

            # If we hit a def/class at same or lesser indent → block ended before this
            if line_indent <= dead_indent and re.match(
                r"\s*(def |class |@)", line
            ):
                # Don't include this line in the removal
                end_idx = i - 1
                break

            # If we're at lesser indent and it's not blank → block ended
            if line_indent < dead_indent:
                end_idx = i - 1
                break

            # Still in the block
            end_idx = i

        # Trim trailing blank lines from the removal range
        while end_idx >= dead_start - 1 and not source_lines[end_idx].strip():
            end_idx -= 1

        if end_idx < dead_start - 1:
            continue  # Nothing to remove

        # Remove the dead block
        new_lines = source_lines[: dead_start - 1] + source_lines[end_idx + 1 :]

        # Clean up: remove excessive blank lines at the join point
        join_point = dead_start - 1
        # Count blank lines at join point
        blank_count = 0
        for i in range(max(0, join_point - 1), min(len(new_lines), join_point + 3)):
            if i < len(new_lines) and not new_lines[i].strip():
                blank_count += 1
        # If more than 2 consecutive blank lines, trim to 1
        if blank_count > 2:
            while (
                join_point < len(new_lines)
                and not new_lines[join_point].strip()
                and join_point > 0
                and not new_lines[join_point - 1].strip()
            ):
                new_lines.pop(join_point)

        filepath.write_text("\n".join(new_lines) + "\n")
        fixed += 1

    return fixed


def check_vulture(*, fix: bool = False, verbose: bool = False) -> CheckResult:
    """Run vulture to detect dead code."""
    t0 = time.time()

    vulture_bin = PROJECT_ROOT / ".venv" / "bin" / "vulture"
    if not vulture_bin.exists():
        return CheckResult(
            name="vulture",
            status="skip",
            summary="vulture not installed (pip install vulture)",
        )

    cmd = [
        str(vulture_bin),
        "src/qmbp_simulation",
        "--min-confidence",
        "80",
        "--exclude",
        "_deprecated,.venv,tests,experiments",
    ]
    # Include whitelist if it exists
    whitelist = PROJECT_ROOT / "vulture_whitelist.py"
    if whitelist.exists():
        cmd.insert(2, str(whitelist))
    rc, stdout, stderr = _run_command(cmd, timeout=60)
    duration = time.time() - t0

    if rc == 0:
        return CheckResult(
            name="vulture",
            status="pass",
            duration_s=duration,
            summary="No dead code detected (confidence ≥80%)",
        )

    # vulture returns 1 if dead code found, parse output
    lines = [l.strip() for l in stdout.splitlines() if l.strip()]
    n_issues = len(lines)

    # --fix: auto-generate whitelist for known false positives
    if fix:
        n_whitelisted = _generate_vulture_whitelist(lines)

        # Fix unreachable code by removing dead blocks
        unreachable = [l for l in lines if "unreachable code" in l]
        n_unreachable_fixed = 0
        if unreachable:
            n_unreachable_fixed = _fix_unreachable_code(unreachable)

        if n_whitelisted > 0 or n_unreachable_fixed > 0:
            # Re-run vulture with the new whitelist to get actual remaining issues
            cmd_rerun = [
                str(vulture_bin),
                "src/qmbp_simulation",
                str(whitelist),
                "--min-confidence",
                "80",
                "--exclude",
                "_deprecated,.venv,tests,experiments",
            ]
            rc2, stdout2, stderr2 = _run_command(cmd_rerun, timeout=60)
            lines2 = [l.strip() for l in stdout2.splitlines() if l.strip()]
            n_remaining = len(lines2)
            duration = time.time() - t0

            fix_summary_parts = []
            if n_whitelisted:
                fix_summary_parts.append(f"whitelisted {n_whitelisted} FPs")
            if n_unreachable_fixed:
                fix_summary_parts.append(f"removed {n_unreachable_fixed} unreachable blocks")

            if rc2 == 0 or n_remaining == 0:
                return CheckResult(
                    name="vulture",
                    status="pass",
                    duration_s=duration,
                    summary=f"Auto-fixed: {', '.join(fix_summary_parts)} → 0 remaining",
                )
            return CheckResult(
                name="vulture",
                status="warn" if n_remaining < 50 else "fail",
                duration_s=duration,
                n_issues=n_remaining,
                summary=f"{', '.join(fix_summary_parts)}, {n_remaining} real issues remain",
                details=lines2[:10],
                exit_code=rc2,
            )

    # Classify: unused imports are less critical than unused functions
    unused_imports = [l for l in lines if "unused import" in l]
    unused_funcs = [l for l in lines if "unused function" in l or "unused method" in l]
    unused_vars = [l for l in lines if "unused variable" in l]
    unused_classes = [l for l in lines if "unused class" in l]
    other = [
        l for l in lines if l not in unused_imports + unused_funcs + unused_vars + unused_classes
    ]

    summary_parts = []
    if unused_funcs:
        summary_parts.append(f"{len(unused_funcs)} unused functions")
    if unused_classes:
        summary_parts.append(f"{len(unused_classes)} unused classes")
    if unused_imports:
        summary_parts.append(f"{len(unused_imports)} unused imports")
    if unused_vars:
        summary_parts.append(f"{len(unused_vars)} unused variables")
    if other:
        summary_parts.append(f"{len(other)} other")

    # Dead code is a warning, not a hard failure (many false positives with dynamic usage)
    return CheckResult(
        name="vulture",
        status="warn" if n_issues < 50 else "fail",
        duration_s=duration,
        n_issues=n_issues,
        summary=f"{n_issues} dead code candidates: {', '.join(summary_parts)}",
        details=lines[:30] if verbose else lines[:10],
        exit_code=rc,
    )


def check_pydoclint(*, fix: bool = False, verbose: bool = False) -> CheckResult:
    """Run pydoclint to check docstring/signature consistency."""
    t0 = time.time()

    pydoclint_bin = PROJECT_ROOT / ".venv" / "bin" / "pydoclint"
    if not pydoclint_bin.exists():
        return CheckResult(
            name="pydoclint",
            status="skip",
            summary="pydoclint not installed (pip install pydoclint)",
        )

    baseline_file = PROJECT_ROOT / ".pydoclint-baseline"

    cmd = [
        str(pydoclint_bin),
        "--style=numpy",
        "--check-return-types=false",
        "--allow-init-docstring=true",
        "--skip-checking-short-docstrings=true",
        "--skip-checking-raises=true",
        "--quiet",
        "src/qmbp_simulation",
    ]

    # If baseline exists, use it to filter known issues
    if baseline_file.exists() and not fix:
        cmd.extend(["--baseline", str(baseline_file)])

    rc, stdout, stderr = _run_command(cmd, timeout=90)
    duration = time.time() - t0

    if rc == 0:
        return CheckResult(
            name="pydoclint",
            status="pass",
            duration_s=duration,
            summary="All docstrings consistent with signatures"
            + (" (baseline active)" if baseline_file.exists() else ""),
        )

    # Parse violations
    lines = [l.strip() for l in stdout.splitlines() if l.strip() and "DOC" in l]
    n_issues = len(lines)

    if n_issues == 0:
        # pydoclint may print summary to stderr
        lines = [l.strip() for l in stderr.splitlines() if l.strip() and "DOC" in l]
        n_issues = len(lines)

    # Group by violation code
    codes: dict[str, int] = {}
    for line in lines:
        for word in line.split():
            if word.startswith("DOC"):
                code = word.rstrip(":")
                codes[code] = codes.get(code, 0) + 1
                break

    # --fix: generate baseline file to suppress all current issues
    if fix and n_issues > 0:
        # Generate baseline (captures all existing violations as "accepted")
        baseline_cmd = [
            str(pydoclint_bin),
            "--style=numpy",
            "--check-return-types=false",
            "--allow-init-docstring=true",
            "--skip-checking-short-docstrings=true",
            "--skip-checking-raises=true",
            "--quiet",
            f"--generate-baseline=true",
            f"--baseline={baseline_file}",
            "src/qmbp_simulation",
        ]
        rc_bl, stdout_bl, stderr_bl = _run_command(baseline_cmd, timeout=90)
        duration = time.time() - t0

        if baseline_file.exists():
            # Also update pyproject.toml to add skip-checking-raises
            _ensure_pydoclint_pyproject_config()
            return CheckResult(
                name="pydoclint",
                status="pass",
                duration_s=duration,
                summary=f"Generated baseline ({n_issues} existing issues baselined, only new issues will fail)",
            )
        else:
            return CheckResult(
                name="pydoclint",
                status="warn",
                duration_s=duration,
                n_issues=n_issues,
                summary=f"Baseline generation failed (rc={rc_bl}), {n_issues} issues remain",
                details=lines[:5],
                exit_code=rc_bl,
            )

    summary_parts = [f"{code}:{count}" for code, count in sorted(codes.items())[:5]]
    summary = f"{n_issues} docstring issues"
    if summary_parts:
        summary += f" ({', '.join(summary_parts)})"

    # Docstring issues are warnings (not blocking)
    return CheckResult(
        name="pydoclint",
        status="warn",
        duration_s=duration,
        n_issues=n_issues,
        summary=summary,
        details=lines[:20] if verbose else lines[:5],
        exit_code=rc,
    )


def _ensure_pydoclint_pyproject_config() -> None:
    """Ensure pyproject.toml has comprehensive pydoclint settings."""
    pyproject = PROJECT_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return

    content = pyproject.read_text()

    # Check if skip-checking-raises is already configured
    if "skip-checking-raises" in content:
        return

    # Update the existing [tool.pydoclint] section
    content = content.replace(
        "skip-checking-short-docstrings = true\n",
        "skip-checking-short-docstrings = true\nskip-checking-raises = true\n",
    )
    pyproject.write_text(content)


def check_phantom(*, verbose: bool = False) -> CheckResult:
    """Run our custom phantom import checker."""
    t0 = time.time()

    # Try the new version first, fallback to old
    new_script = SCRIPT_DIR / "check_phantom_functions.py"
    old_script = SCRIPT_DIR / "check_phantom_funcions.py"
    script = new_script if new_script.exists() else old_script

    if not script.exists():
        return CheckResult(
            name="phantom",
            status="skip",
            summary="check_phantom_functions.py not found",
        )

    cmd = [str(VENV_PYTHON), str(script), "--all", "--json"]
    rc, stdout, stderr = _run_command(cmd, timeout=60)
    duration = time.time() - t0

    if rc == 0:
        return CheckResult(
            name="phantom",
            status="pass",
            duration_s=duration,
            summary="No phantom imports found",
        )

    # Parse JSON output if available
    try:
        data = json.loads(stdout)
        n_issues = data.get("summary", {}).get("errors", 0)
        issues = data.get("issues", [])
        details = [f"{i['file']}:{i.get('line', 0)} — {i['message']}" for i in issues[:10]]
        return CheckResult(
            name="phantom",
            status="fail",
            duration_s=duration,
            n_issues=n_issues,
            summary=f"{n_issues} phantom import(s) detected",
            details=details,
            exit_code=rc,
        )
    except (json.JSONDecodeError, KeyError):
        # Fallback: count lines
        lines = [l for l in (stdout + stderr).splitlines() if "PHANTOM" in l or "✗" in l]
        return CheckResult(
            name="phantom",
            status="fail" if rc != 0 else "warn",
            duration_s=duration,
            n_issues=len(lines),
            summary=f"{len(lines)} phantom import(s) detected",
            details=lines[:10],
            exit_code=rc,
        )


def check_steerings(*, fix: bool = False, verbose: bool = False) -> CheckResult:
    """Run our custom steering verifier."""
    t0 = time.time()

    script = SCRIPT_DIR / "verify_steerings.py"
    if not script.exists():
        return CheckResult(
            name="steerings",
            status="skip",
            summary="verify_steerings.py not found",
        )

    # Run fast checks only (skip cross-redundancy which takes 2+ min)
    cmd = [
        str(VENV_PYTHON),
        str(script),
        "--check",
        "activation",
        "quality",
        "overlaps",
        "split",
        "tokens",
        "hooks",
        "contradictions",
        "duplicates",
        "clarity",
        "module-index",
        "--json",
    ]
    if fix:
        cmd.append("--fix")
    rc, stdout, stderr = _run_command(cmd, timeout=120)
    duration = time.time() - t0

    # Parse JSON
    try:
        data = json.loads(stdout)
        score = data.get("score", 0)
        n_errors = data.get("summary", {}).get("errors", 0)
        n_warnings = data.get("summary", {}).get("warnings", 0)
        passed = data.get("passed", True)

        # Classify: context-budget errors are informational, not blocking
        real_errors = [
            i
            for i in data.get("issues", [])
            if i.get("severity") == "error" and "context-budget" not in i.get("message", "")
        ]
        has_real_errors = len(real_errors) > 0

        status: Status = "pass" if passed else ("fail" if has_real_errors else "warn")
        summary = f"Score: {score}/100"
        if n_errors:
            summary += f" ({n_errors} errors, {n_warnings} warnings)"
        elif n_warnings:
            summary += f" ({n_warnings} warnings)"

        details = []
        for issue in data.get("issues", [])[:10]:
            sev = issue.get("severity", "?")
            msg = issue.get("message", "")
            f = issue.get("file", "")
            details.append(f"[{sev}] {f}: {msg}")

        return CheckResult(
            name="steerings",
            status=status,
            duration_s=duration,
            n_issues=n_errors + n_warnings,
            summary=summary,
            details=details,
            exit_code=rc,
        )
    except (json.JSONDecodeError, KeyError):
        return CheckResult(
            name="steerings",
            status="warn" if rc == 0 else "fail",
            duration_s=duration,
            summary=f"verify_steerings exited with code {rc}",
            details=[stderr[:200]] if stderr else [],
            exit_code=rc,
        )


def check_module_index(*, verbose: bool = False) -> CheckResult:
    """Check if module-index.md is fresh (matches current code)."""
    t0 = time.time()

    script = SCRIPT_DIR / "generate_module_index.py"
    if not script.exists():
        return CheckResult(
            name="module-index",
            status="skip",
            summary="generate_module_index.py not found",
        )

    index_file = PROJECT_ROOT / ".kiro" / "steering" / "module-index.md"
    if not index_file.exists():
        return CheckResult(
            name="module-index",
            status="fail",
            duration_s=time.time() - t0,
            n_issues=1,
            summary="module-index.md does not exist — run generate_module_index.py",
        )

    # Run --verify to check for phantoms
    cmd = [str(VENV_PYTHON), str(script), "--verify"]
    rc, stdout, stderr = _run_command(cmd, timeout=30)
    duration = time.time() - t0

    # Check for PHANTOM warnings in stderr
    phantoms = [l.strip() for l in stderr.splitlines() if "PHANTOM" in l]

    if not phantoms and rc == 0:
        return CheckResult(
            name="module-index",
            status="pass",
            duration_s=duration,
            summary="module-index.md is up-to-date",
        )

    return CheckResult(
        name="module-index",
        status="warn" if len(phantoms) < 5 else "fail",
        duration_s=duration,
        n_issues=len(phantoms),
        summary=f"{len(phantoms)} stale entries in module-index.md",
        details=phantoms[:10],
        exit_code=rc,
    )


def check_test_imports(*, fix: bool = False, verbose: bool = False) -> CheckResult:
    """Validate test imports — catch broken paths before pytest."""
    t0 = time.time()
    try:
        from validate_test_imports import check_test_imports as _check
        result = _check(fix=fix, verbose=verbose)
        duration = time.time() - t0
        return CheckResult(
            name="test-imports",
            status=result["status"],
            duration_s=duration,
            n_issues=result["n_issues"],
            summary=result["summary"],
            details=result.get("details", []),
        )
    except Exception as e:
        return CheckResult(
            name="test-imports",
            status="error",
            duration_s=time.time() - t0,
            summary=f"Failed: {e}",
        )


# ─── Orchestrator ────────────────────────────────────────────────────────────

CHECK_FUNCTIONS = {
    "pyclean": check_pyclean,
    "vulture": check_vulture,
    "pydoclint": check_pydoclint,
    "phantom": check_phantom,
    "steerings": check_steerings,
    "module-index": check_module_index,
    "test-imports": check_test_imports,
}


def run_checks(
    *,
    checks: list[str],
    fix: bool = False,
    verbose: bool = False,
) -> FullReport:
    """Run all specified checks and return aggregated report."""
    report = FullReport()
    t0 = time.time()

    for check_name in checks:
        func = CHECK_FUNCTIONS.get(check_name)
        if func is None:
            report.results.append(
                CheckResult(
                    name=check_name,
                    status="error",
                    summary=f"Unknown check: {check_name}",
                )
            )
            continue

        # Pass fix/verbose kwargs where applicable
        kwargs: dict = {"verbose": verbose}
        if check_name in ("pyclean", "steerings", "vulture", "pydoclint"):
            kwargs["fix"] = fix

        try:
            result = func(**kwargs)
        except Exception as e:
            result = CheckResult(
                name=check_name,
                status="error",
                summary=f"Unexpected error: {e}",
            )
        report.results.append(result)

    report.total_duration_s = time.time() - t0
    return report


def print_report(report: FullReport, *, verbose: bool = False) -> None:
    """Print human-readable report."""
    print()
    print("═" * 64)
    print("  🔧 Project Maintenance — All Checks")
    print("═" * 64)
    print()

    max_name = max(len(r.name) for r in report.results) if report.results else 10

    for r in report.results:
        icon = STATUS_ICONS[r.status]
        time_str = f"({r.duration_s:.1f}s)" if r.duration_s > 0 else ""
        issues_str = f" [{r.n_issues} issues]" if r.n_issues > 0 else ""
        print(f"  {icon} {r.name:<{max_name}}  {r.summary}{issues_str} {time_str}")

        if verbose and r.details:
            for detail in r.details:
                print(f"      → {detail}")

    print()
    print("─" * 64)

    # Summary
    n_pass = sum(1 for r in report.results if r.status == "pass")
    n_warn = sum(1 for r in report.results if r.status == "warn")
    n_fail = sum(1 for r in report.results if r.status == "fail")
    n_skip = sum(1 for r in report.results if r.status == "skip")
    total = len(report.results)

    grade = (
        "A"
        if report.score >= 90
        else "B"
        if report.score >= 75
        else "C"
        if report.score >= 50
        else "D"
    )
    status_str = "✅ ALL PASSED" if report.passed else "❌ ISSUES FOUND"

    print(f"  Score: {report.score}/100 (grade {grade}) — {status_str}")
    print(f"  Pass: {n_pass}/{total} | Warn: {n_warn} | Fail: {n_fail} | Skip: {n_skip}")
    print(f"  Total time: {report.total_duration_s:.1f}s")
    print()


# ─── CLI ─────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run all project maintenance checks in one command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Available checks:
  pyclean       Remove/detect Python bytecode caches
  vulture       Detect dead code (unused functions, classes, vars)
  pydoclint     Verify docstring ↔ signature consistency
  phantom       Detect phantom imports (symbol doesn't exist in module)
  steerings     Verify .kiro/steering files integrity
  module-index  Check module-index.md freshness

Examples:
  %(prog)s                           # Run all checks
  %(prog)s --only vulture phantom    # Only specific checks
  %(prog)s --skip pydoclint          # Skip specific checks
  %(prog)s --fix                     # Apply auto-fixes where possible
  %(prog)s --json                    # JSON output for CI
  %(prog)s --ci                      # CI mode (JSON + strict exit code)
  %(prog)s -v                        # Verbose (show details)
""",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        choices=ALL_CHECKS,
        default=None,
        help="Run only these checks.",
    )
    parser.add_argument(
        "--skip",
        nargs="+",
        choices=ALL_CHECKS,
        default=None,
        help="Skip these checks.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply auto-fixes: pyclean cleans, vulture generates whitelist, "
        "pydoclint generates baseline, steerings --fix (dead paths, weak language, "
        "context-budget conversion).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output JSON report instead of human-readable text.",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: JSON output + non-zero exit on any failure.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed output from each check.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Determine which checks to run
    if args.only:
        checks = args.only
    else:
        checks = list(ALL_CHECKS)
        if args.skip:
            checks = [c for c in checks if c not in args.skip]

    # Run
    report = run_checks(
        checks=checks,
        fix=args.fix,
        verbose=args.verbose,
    )

    # Output
    if args.json_output or args.ci:
        json.dump(report.to_json(), sys.stdout, indent=2)
        print()
    else:
        print_report(report, verbose=args.verbose)

    # Exit code
    if args.ci:
        return 0 if report.passed else 1
    # In non-CI mode, only fail on hard errors (not warnings)
    has_errors = any(r.status in ("fail", "error") for r in report.results)
    return 1 if has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
