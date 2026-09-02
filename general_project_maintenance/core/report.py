"""Structured reporting with multi-format output (text, JSON, SARIF).

Provides a unified Issue/Report model shared across all maintenance tools.
Each tool can add issues to a Report and render it in the user's preferred format.

Usage:
    from core.report import Report, Severity

    report = Report(tool_name="check-phantom-functions", tool_version="2.0.0")
    report.add("phantom-import", "error", "src/foo.py", "Symbol 'bar' not found in module X")
    report.add("stale-import", "warning", "src/baz.py", "Unused import detected")

    # Output
    report.print_text()           # Human-readable
    report.to_json()              # Dict for JSON serialization
    report.to_sarif()             # SARIF v2.1.0 for CI/GitHub scanning
    report.exit_code()            # 0 if passed, 1 if errors
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Literal

Severity = Literal["error", "warning", "note"]

SEVERITY_WEIGHTS: dict[Severity, int] = {"error": 10, "warning": 5, "note": 1}
SEVERITY_ICONS: dict[Severity, str] = {"error": "✗", "warning": "⚠", "note": "💡"}


@dataclass
class Issue:
    """A single verification/analysis issue."""

    check: str
    severity: Severity
    file: str
    message: str
    line: int = 0
    fix_applied: bool = False

    @property
    def rule_id(self) -> str:
        return f"{self.check}"


@dataclass
class Report:
    """Aggregated report from one or more checks.

    Parameters
    ----------
    tool_name : str
        Name of the tool generating the report (used in SARIF output).
    tool_version : str
        Version string for SARIF metadata.
    """

    tool_name: str = "maintenance-tool"
    tool_version: str = "1.0.0"
    issues: list[Issue] = field(default_factory=list)
    files_scanned: int = 0
    checks_run: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def score(self) -> int:
        """Health score 0-100. Starts at 100, deducted by issue severity."""
        penalty = sum(SEVERITY_WEIGHTS[i.severity] for i in self.issues if not i.fix_applied)
        return max(0, 100 - penalty)

    @property
    def passed(self) -> bool:
        """True if no unfixed errors."""
        return all(i.severity != "error" or i.fix_applied for i in self.issues)

    @property
    def n_errors(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error" and not i.fix_applied)

    @property
    def n_warnings(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning" and not i.fix_applied)

    @property
    def n_notes(self) -> int:
        return sum(1 for i in self.issues if i.severity == "note")

    def add(
        self,
        check: str,
        severity: Severity,
        file: str,
        message: str,
        line: int = 0,
    ) -> Issue:
        """Add an issue and return it."""
        issue = Issue(check=check, severity=severity, file=file, message=message, line=line)
        self.issues.append(issue)
        return issue

    def merge(self, other: Report) -> None:
        """Merge another report's issues into this one."""
        self.issues.extend(other.issues)
        self.files_scanned += other.files_scanned
        for check in other.checks_run:
            if check not in self.checks_run:
                self.checks_run.append(check)

    def to_json(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        return {
            "tool": self.tool_name,
            "version": self.tool_version,
            "score": self.score,
            "passed": self.passed,
            "files_scanned": self.files_scanned,
            "checks_run": self.checks_run,
            "metadata": self.metadata,
            "issues": [asdict(i) for i in self.issues],
            "summary": {
                "errors": self.n_errors,
                "warnings": self.n_warnings,
                "notes": self.n_notes,
                "fixed": sum(1 for i in self.issues if i.fix_applied),
                "total": len(self.issues),
            },
        }

    def to_sarif(self) -> dict:
        """Generate SARIF v2.1.0 output for GitHub code scanning."""
        results = []
        rules_seen: dict[str, dict] = {}

        for issue in self.issues:
            rule_id = issue.rule_id
            if rule_id not in rules_seen:
                rules_seen[rule_id] = {
                    "id": rule_id,
                    "shortDescription": {"text": f"Check: {issue.check}"},
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
                            "name": self.tool_name,
                            "version": self.tool_version,
                            "rules": list(rules_seen.values()),
                        }
                    },
                    "results": results,
                }
            ],
        }

    def print_text(self, *, quiet: bool = False, file=None) -> None:
        """Print human-readable report to stream."""
        out = file or sys.stdout
        print(f"\n{'=' * 60}", file=out)
        print(f"  {self.tool_name} v{self.tool_version}", file=out)
        print(f"{'=' * 60}\n", file=out)
        print(f"  Files scanned: {self.files_scanned}", file=out)
        if self.checks_run:
            print(f"  Checks: {', '.join(self.checks_run)}", file=out)
        print(file=out)

        # Group by check
        by_check: dict[str, list[Issue]] = defaultdict(list)
        for issue in self.issues:
            by_check[issue.check].append(issue)

        for check_name in self.checks_run or sorted(by_check.keys()):
            issues = by_check.get(check_name, [])
            label = check_name.upper()
            if not issues:
                if not quiet:
                    print(f"─── {label} ───", file=out)
                    print("  ✓ All passed", file=out)
                    print(file=out)
            else:
                print(f"─── {label} ({len(issues)} issues) ───", file=out)
                for issue in issues:
                    icon = SEVERITY_ICONS[issue.severity]
                    fixed = " [FIXED]" if issue.fix_applied else ""
                    print(f"  {icon} {issue.file}: {issue.message}{fixed}", file=out)
                print(file=out)

        # Score
        print(f"{'=' * 60}", file=out)
        score = self.score
        if score >= 90:
            grade = "A"
        elif score >= 75:
            grade = "B"
        elif score >= 50:
            grade = "C"
        else:
            grade = "D"
        status = "✅ PASSED" if self.passed else "❌ FAILED"
        print(f"  Score: {score}/100 (grade {grade}) — {status}", file=out)
        if self.n_errors:
            print(
                f"  Errors: {self.n_errors} | Warnings: {self.n_warnings} | Notes: {self.n_notes}",
                file=out,
            )
        print(file=out)

    def print_json(self, *, file=None) -> None:
        """Print JSON report to stream."""
        out = file or sys.stdout
        json.dump(self.to_json(), out, indent=2)
        print(file=out)

    def print_sarif(self, *, file=None) -> None:
        """Print SARIF report to stream."""
        out = file or sys.stdout
        json.dump(self.to_sarif(), out, indent=2)
        print(file=out)

    def exit_code(self) -> int:
        """Return 0 if passed, 1 if has unfixed errors."""
        return 0 if self.passed else 1
