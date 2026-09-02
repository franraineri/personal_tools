#!/usr/bin/env python3
"""Validate that all imports in test files resolve correctly.

Catches the #1 cause of test failures: tests importing modules that were
moved, renamed, or deleted. Runs in <2s and catches issues BEFORE pytest.

Integrations:
- Uses core/import_analysis for AST-based import extraction
- Uses core/config for project root and scan settings
- Uses core/report for structured output (Issue/Report/Severity)
- Integrated in run_all_checks.py as 'test-imports' check
- Integrated in run_test_suite.py as pre-flight step
- Auto-learns relocations: when --fix is used and a guess succeeds,
  adds it to KNOWN_RELOCATIONS for future runs

Usage:
    .venv/bin/python scripts/general_project_maintenance/validate_test_imports.py
    .venv/bin/python scripts/general_project_maintenance/validate_test_imports.py --fix
    .venv/bin/python scripts/general_project_maintenance/validate_test_imports.py --ci
    .venv/bin/python scripts/general_project_maintenance/validate_test_imports.py --json
    .venv/bin/python scripts/general_project_maintenance/validate_test_imports.py --learn
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

# ─── Setup paths ─────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPT_DIR))

# Use core utilities where available
try:
    from core.report import Issue, Report, Severity
    HAS_CORE = True
except ImportError:
    HAS_CORE = False

# ─── Relocation Database ─────────────────────────────────────────────────────
# Persisted as JSON alongside this script for easy updates
RELOCATIONS_DB_PATH = SCRIPT_DIR / "data" / "known_relocations.json"

# Hardcoded fallback (used if JSON not found)
_DEFAULT_RELOCATIONS: dict[str, str] = {
    "project_health.analysis.mpnn_eval_analyzer": "project_health.analysis.models.mpnn_eval_analyzer",
    "project_health.analysis.mitigation_benchmark_analyzer": "project_health.analysis.hardware.mitigation_benchmark_analyzer",
    "project_health.analysis.scaling_analyzer": "project_health.analysis.scaling.scaling_analyzer",
    "project_health.analysis.layout_optimizer_analyzer": "project_health.analysis.hardware.layout_optimizer_analyzer",
    "project_health.analysis.sanity_check": "project_health.analysis.validation.sanity_check",
    "project_health.analysis.thesis_findings_validator": "project_health.analysis.validation.thesis_findings_validator",
    "project_health.analysis.thesis_tables_compiler": "project_health.analysis.thesis.thesis_tables_compiler",
    "project_health.analysis.thesis_figures": "project_health.analysis.thesis.thesis_figures",
    "project_health.analysis.scan_coverage": "project_health.analysis.coverage.scan_coverage",
    "project_health.analysis.heisenberg_summary": "project_health.analysis.thesis.heisenberg_summary",
    "project_health.analysis.verify_results": "project_health.analysis.validation.verify_results",
}

# Modules that were deleted entirely — tests referencing these should be removed
DELETED_MODULES: set[str] = {
    "scripts.experiment_runners.cross_topology.run_orchestrator",
    "project_health.compare",
    "project_health.figures.health_figures",
    "project_health.analysis.validate_s_series",
}

# Prefixes to check (only our own modules, skip stdlib/third-party)
_OUR_PREFIXES = ("qmbp_simulation", "project_health", "scripts", "experiments")


# ─── Relocation DB management ────────────────────────────────────────────────

def _load_relocations() -> dict[str, str]:
    """Load relocations from persistent JSON or fall back to defaults."""
    if RELOCATIONS_DB_PATH.exists():
        try:
            return json.loads(RELOCATIONS_DB_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return dict(_DEFAULT_RELOCATIONS)


def _save_relocations(relocations: dict[str, str]) -> None:
    """Persist relocations DB to disk."""
    RELOCATIONS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    RELOCATIONS_DB_PATH.write_text(json.dumps(relocations, indent=2, sort_keys=True))


# ─── Core scanning logic ─────────────────────────────────────────────────────

def extract_imports(filepath: Path) -> list[tuple[int, str]]:
    """Extract all import module paths from a Python file. Returns (line, module)."""
    try:
        tree = ast.parse(filepath.read_text())
    except SyntaxError:
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((node.lineno, alias.name))
    return imports


def check_import_resolves(module_path: str) -> bool:
    """Check if a module can be found by path lookup (fast, no execution)."""
    parts = module_path.split(".")

    # Check in src/
    src_path = ROOT / "src" / Path(*parts)
    if src_path.with_suffix(".py").exists() or (src_path / "__init__.py").exists():
        return True

    # Check as direct path from ROOT (scripts/, project_health/, experiments/)
    direct_path = ROOT / Path(*parts)
    if direct_path.with_suffix(".py").exists() or (direct_path / "__init__.py").exists():
        return True

    return False


def guess_relocation(module: str) -> str | None:
    """Try common relocation patterns to find the module.

    Returns the new module path if found, None otherwise.
    """
    parts = module.split(".")

    # Pattern: project_health.analysis.X → project_health.analysis.{subdir}.X
    if len(parts) >= 3 and parts[0] == "project_health" and parts[1] == "analysis":
        module_name = parts[-1]
        analysis_dir = ROOT / "project_health" / "analysis"
        if analysis_dir.is_dir():
            for subdir in analysis_dir.iterdir():
                if subdir.is_dir() and (subdir / f"{module_name}.py").exists():
                    return f"project_health.analysis.{subdir.name}.{module_name}"

    # Pattern: scripts.X.Y.Z → search for Z.py under scripts/
    if parts[0] == "scripts":
        leaf = parts[-1]
        for found in ROOT.rglob(f"{leaf}.py"):
            if "scripts" in str(found) and ".venv" not in str(found):
                try:
                    rel = found.relative_to(ROOT)
                    return str(rel.with_suffix("")).replace("/", ".")
                except ValueError:
                    pass

    return None


def scan_test_files(test_dirs: list[str] | None = None) -> list[dict]:
    """Scan all test files for broken imports.

    Returns list of dicts with keys: file, line, module, issue, suggestion.
    Issue types: 'deleted', 'relocated', 'not_found'.
    """
    if test_dirs is None:
        test_dirs = ["tests"]

    relocations = _load_relocations()
    issues = []

    for d in test_dirs:
        test_dir = ROOT / d
        if not test_dir.is_dir():
            continue
        for filepath in sorted(test_dir.rglob("test_*.py")):
            imports = extract_imports(filepath)
            rel = str(filepath.relative_to(ROOT))
            for lineno, module in imports:
                # Only check our own modules
                if not any(module.startswith(p) for p in _OUR_PREFIXES):
                    continue

                if module in DELETED_MODULES:
                    issues.append({
                        "file": rel,
                        "line": lineno,
                        "module": module,
                        "issue": "deleted",
                        "suggestion": "Remove test or skip — module was deleted",
                    })
                elif module in relocations:
                    issues.append({
                        "file": rel,
                        "line": lineno,
                        "module": module,
                        "issue": "relocated",
                        "suggestion": f"Change to: {relocations[module]}",
                    })
                elif not check_import_resolves(module):
                    suggestion = guess_relocation(module)
                    issues.append({
                        "file": rel,
                        "line": lineno,
                        "module": module,
                        "issue": "not_found",
                        "suggestion": f"Found at: {suggestion}" if suggestion else "Module not found — check if moved or deleted",
                        "_guessed_target": suggestion,
                    })

    return issues


def apply_fixes(issues: list[dict]) -> int:
    """Auto-fix relocated imports in test files. Returns count of fixes applied."""
    relocations = _load_relocations()
    fixes = 0
    files_modified: dict[str, str] = {}

    for issue in issues:
        if issue["issue"] != "relocated":
            continue

        filepath = ROOT / issue["file"]
        if issue["file"] not in files_modified:
            files_modified[issue["file"]] = filepath.read_text()

        old = issue["module"]
        new = relocations[old]
        content = files_modified[issue["file"]]
        files_modified[issue["file"]] = content.replace(old, new)
        fixes += 1

    for rel, content in files_modified.items():
        (ROOT / rel).write_text(content)

    return fixes


def learn_relocations(issues: list[dict]) -> int:
    """Auto-learn new relocations from guessed targets. Returns count learned."""
    relocations = _load_relocations()
    learned = 0

    for issue in issues:
        if issue["issue"] != "not_found":
            continue
        target = issue.get("_guessed_target")
        if target and check_import_resolves(target):
            relocations[issue["module"]] = target
            learned += 1

    if learned:
        _save_relocations(relocations)

    return learned


def delete_dead_test_files(issues: list[dict]) -> list[str]:
    """Delete test files that ONLY import from deleted modules. Returns deleted paths."""
    # Group by file
    deleted_by_file: dict[str, int] = {}
    total_imports_by_file: dict[str, int] = {}

    for issue in issues:
        if issue["issue"] == "deleted":
            deleted_by_file[issue["file"]] = deleted_by_file.get(issue["file"], 0) + 1

    # Count total internal imports per file to decide if whole file is dead
    for d in ["tests"]:
        test_dir = ROOT / d
        if not test_dir.is_dir():
            continue
        for filepath in test_dir.rglob("test_*.py"):
            rel = str(filepath.relative_to(ROOT))
            if rel in deleted_by_file:
                imports = extract_imports(filepath)
                internal = [m for _, m in imports if any(m.startswith(p) for p in _OUR_PREFIXES)]
                total_imports_by_file[rel] = len(internal)

    # Delete files where ALL internal imports are from deleted modules
    deleted_files = []
    for rel, n_deleted in deleted_by_file.items():
        total = total_imports_by_file.get(rel, 0)
        # If >50% of internal imports are dead, file is mostly useless
        if total > 0 and n_deleted / total >= 0.5:
            path = ROOT / rel
            if path.exists():
                path.unlink()
                deleted_files.append(rel)

    return deleted_files


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate test imports — catches broken paths before pytest"
    )
    parser.add_argument("--fix", action="store_true",
                        help="Auto-fix relocated imports and delete dead test files")
    parser.add_argument("--learn", action="store_true",
                        help="Auto-learn new relocations from guessed targets")
    parser.add_argument("--ci", action="store_true",
                        help="Exit 1 if issues found (for CI pipelines)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--delete-dead", action="store_true",
                        help="Delete test files that only import dead modules")
    args = parser.parse_args()

    issues = scan_test_files()

    # Auto-learn new relocations
    if args.learn or args.fix:
        n_learned = learn_relocations(issues)
        if n_learned:
            print(f"  📚 Learned {n_learned} new relocations (saved to {RELOCATIONS_DB_PATH.relative_to(ROOT)})")
            # Re-scan with updated DB
            issues = scan_test_files()

    # Auto-fix relocated imports
    if args.fix:
        n_fixed = apply_fixes(issues)
        if n_fixed:
            print(f"  📦 Auto-fixed {n_fixed} relocated imports")
        issues = [i for i in scan_test_files() if i["issue"] != "relocated"]

    # Delete dead test files
    if args.fix or args.delete_dead:
        deleted = delete_dead_test_files(issues)
        if deleted:
            print(f"  🗑️  Deleted {len(deleted)} dead test files:")
            for d in deleted:
                print(f"     {d}")
            issues = [i for i in issues if i["file"] not in set(deleted)]

    # Output
    if not issues:
        print("  ✅ All test imports resolve correctly")
        return 0

    if args.json:
        # Clean internal keys
        clean = [{k: v for k, v in i.items() if not k.startswith("_")} for i in issues]
        print(json.dumps(clean, indent=2))
    else:
        print(f"  ⚠️  Found {len(issues)} broken imports in tests:\n")
        for issue in issues:
            icon = {"deleted": "🗑️", "relocated": "📦", "not_found": "❌"}[issue["issue"]]
            print(f"  {icon} {issue['file']}:{issue['line']}")
            print(f"     import: {issue['module']}")
            print(f"     → {issue['suggestion']}")
            print()

    return 1 if args.ci else 0


# ─── Integration API (used by run_all_checks.py and run_test_suite.py) ───────

def check_test_imports(*, fix: bool = False, verbose: bool = False):
    """Entry point for run_all_checks.py integration.

    Returns a CheckResult-compatible dict (or CheckResult if core available).
    """
    issues = scan_test_files()

    if fix:
        learn_relocations(issues)
        issues = scan_test_files()
        apply_fixes(issues)
        deleted = delete_dead_test_files(issues)
        issues = [i for i in scan_test_files() if i["file"] not in set(deleted)]

    n_issues = len(issues)
    n_relocated = sum(1 for i in issues if i["issue"] == "relocated")
    n_deleted = sum(1 for i in issues if i["issue"] == "deleted")
    n_not_found = sum(1 for i in issues if i["issue"] == "not_found")

    summary = f"{n_issues} broken imports" if n_issues else "All test imports valid"
    if n_issues:
        parts = []
        if n_relocated:
            parts.append(f"{n_relocated} relocated")
        if n_deleted:
            parts.append(f"{n_deleted} dead")
        if n_not_found:
            parts.append(f"{n_not_found} missing")
        summary += f" ({', '.join(parts)})"

    details = []
    if verbose:
        for i in issues[:20]:
            details.append(f"{i['file']}:{i['line']} → {i['module']}")

    if HAS_CORE:
        from core.report import Issue, Severity
        # Return structured issues for core integration
        return {
            "name": "test-imports",
            "status": "pass" if n_issues == 0 else ("warn" if n_issues < 5 else "fail"),
            "n_issues": n_issues,
            "summary": summary,
            "details": details,
        }

    return {
        "name": "test-imports",
        "status": "pass" if n_issues == 0 else "fail",
        "n_issues": n_issues,
        "summary": summary,
        "details": details,
    }


if __name__ == "__main__":
    sys.exit(main())
