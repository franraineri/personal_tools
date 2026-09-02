#!/usr/bin/env python3
"""Check for phantom imports from any module across all Python files.

Validates that every symbol imported via `from X import Y` actually exists
in module X. Uses an inverted-index approach: scans all files ONCE, then
verifies per-module — O(files + modules) instead of O(modules × files).

Catches:
- Typos in import names
- Symbols removed during refactoring but still imported elsewhere
- Stale re-exports from __init__.py that reference deleted functions

Usage:
    # Default: check analysis.metrics
   .venv/bin/python scripts/general_project_maintenance/check_phantom_functions.py

    # Check specific module(s):
   .venv/bin/python scripts/general_project_maintenance/check_phantom_functions.py qmbp_simulation.predictors.model_zoo

    # Check ALL qmbp_simulation submodules (inverted-index makes this fast):
   .venv/bin/python scripts/general_project_maintenance/check_phantom_functions.py --all

    # JSON output (for CI integration):
   .venv/bin/python scripts/general_project_maintenance/check_phantom_functions.py --all --json

    # SARIF output (for GitHub code scanning):
   .venv/bin/python scripts/general_project_maintenance/check_phantom_functions.py --all --sarif

    # Self-test:
   .venv/bin/python scripts/general_project_maintenance/check_phantom_functions.py --self-test

    # Exclude specific directories:
   .venv/bin/python scripts/general_project_maintenance/check_phantom_functions.py --all --exclude tests experiments
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure core/ is importable
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from core.config import get_phantom_config
from core.import_analysis import (
    build_import_index,
    discover_submodules,
    extract_names_from_import,
    resolve_phantom_imports,
)
from core.report import Report

PROJECT_ROOT = _SCRIPT_DIR.parents[1]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect phantom imports (symbols imported but not existing in target module).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s                                         # Check default modules
  %(prog)s qmbp_simulation.predictors.model_zoo    # Check specific module
  %(prog)s --all                                   # Check ALL submodules (fast)
  %(prog)s --all --json                            # JSON report for CI
  %(prog)s --all --sarif                           # SARIF for GitHub scanning
  %(prog)s --all --exclude tests                   # Skip test directories
  %(prog)s --self-test                             # Verify checker works
""",
    )
    parser.add_argument(
        "modules",
        nargs="*",
        help="Dotted module paths to check. Default: from config.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Check ALL qmbp_simulation submodules.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output JSON report.",
    )
    parser.add_argument(
        "--sarif",
        action="store_true",
        help="Output SARIF v2.1.0.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed scan progress.",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=None,
        help="Additional directory names to exclude from scanning.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run self-test to verify checker correctness.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to maintenance.toml config file.",
    )
    return parser


def _run_self_test() -> bool:
    """Verify the checker works correctly with known cases."""
    import os
    import tempfile

    print("\n" + "=" * 60)
    print("SELF-TEST: Verifying checker correctness")
    print("=" * 60)

    all_ok = True

    # Test 1: extract_names_from_import
    cases = [
        ("A, B, C", ["A", "B", "C"]),
        ("A, B,", ["A", "B"]),
        ("A as x, B", ["A", "B"]),
        ("A,  # comment\nB,", ["A", "B"]),
        ("(A, B)", ["A", "B"]),
        ("", []),
        ("   ", []),
        ("123bad", []),
        ("A  # noqa: E402", ["A"]),
        ("self", []),
        ("cls", []),
    ]
    for input_text, expected in cases:
        result = extract_names_from_import(input_text)
        assert result == expected, f"Failed for {input_text!r}: got {result}, expected {expected}"
    print("  ✅ extract_names_from_import: all cases pass")

    # Test 2: Full phantom detection via inverted index
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", dir=".", delete=False) as f:
        f.write("from qmbp_simulation.analysis.metrics import NONEXISTENT_SYMBOL_XYZ_TEST\n")
        tmp_name = f.name

    try:
        index = build_import_index(Path("."), target_prefix="qmbp_simulation")
        phantoms = resolve_phantom_imports(index, "qmbp_simulation.analysis.metrics")
        found = any(e.symbol_name == "NONEXISTENT_SYMBOL_XYZ_TEST" for e in phantoms)
        if found:
            print("  ✅ Detects phantom import correctly")
        else:
            print("  ❌ Failed to detect phantom import!")
            all_ok = False
    finally:
        os.unlink(tmp_name)

    # Test 3: No false positives on stable module
    index2 = build_import_index(Path("."), target_prefix="qmbp_simulation")
    phantoms_real = resolve_phantom_imports(index2, "qmbp_simulation.utils.helpers")
    if not phantoms_real:
        print("  ✅ No false positives for stable module")
    else:
        print(f"  ⚠️  {len(phantoms_real)} phantom(s) in utils.helpers (may be pre-existing)")

    # Test 4: Inverted index efficiency
    assert index.files_scanned > 0, "No files were scanned"
    print(
        f"  ✅ Inverted index: {index.files_scanned} files, {index.total_imports} imports indexed"
    )

    if all_ok:
        print("\n  ✅ Self-test PASSED")
    else:
        print("\n  ❌ Self-test FAILED")

    return all_ok


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.self_test:
        return 0 if _run_self_test() else 1

    # Load config
    cfg = get_phantom_config(args.config)
    skip_dirs = set(cfg.get("skip_dirs", []))
    if args.exclude:
        skip_dirs.update(args.exclude)

    # Determine modules to check
    if args.all:
        src_dir = PROJECT_ROOT / "src"
        modules = discover_submodules(src_dir)
        if not modules:
            print("ERROR: No submodules found. Run from project root.", file=sys.stderr)
            return 1
        if args.verbose:
            print(f"Discovered {len(modules)} submodules to check")
    elif args.modules:
        modules = args.modules
    else:
        modules = cfg.get("default_modules", ["qmbp_simulation.analysis.metrics"])

    # Build inverted index ONCE (the key scalability improvement)
    if args.verbose:
        print("Building import index...")
    index = build_import_index(
        PROJECT_ROOT,
        target_prefix="qmbp_simulation",
        exclude_dirs=frozenset(skip_dirs),
    )
    if args.verbose:
        print(f"  Indexed {index.files_scanned} files, {index.total_imports} import statements")
        print(f"  {len(index.modules_referenced)} distinct modules referenced")

    # Resolve phantoms per module
    report = Report(tool_name="check-phantom-functions", tool_version="2.0.0")
    report.files_scanned = index.files_scanned
    report.checks_run = ["phantom-imports"]
    report.metadata["modules_checked"] = len(modules)
    report.metadata["index_total_imports"] = index.total_imports

    total_phantoms = 0
    for module_path in modules:
        phantoms = resolve_phantom_imports(index, module_path, verbose=args.verbose)
        total_phantoms += len(phantoms)
        for entry in phantoms:
            rel_path = str(Path(entry.file_path).relative_to(PROJECT_ROOT))
            short_mod = module_path.split(".")[-1]
            report.add(
                "phantom-imports",
                "error",
                rel_path,
                f"Symbol '{entry.symbol_name}' not found in {module_path} "
                f"(imported at line {entry.line_number})",
                line=entry.line_number,
            )

    # Output
    if args.sarif:
        report.print_sarif()
    elif args.json_output:
        report.print_json()
    else:
        if total_phantoms == 0:
            print(f"\n✅ No phantom imports found across {len(modules)} module(s)")
            print(
                f"   ({index.files_scanned} files scanned, {index.total_imports} imports checked)"
            )
        else:
            report.print_text(quiet=False)
            # Actionable grouped output
            print("\nActionable summary (grouped by file):")
            by_file: dict[str, list[str]] = {}
            for issue in report.issues:
                by_file.setdefault(issue.file, []).append(issue.message)
            for filepath, messages in sorted(by_file.items()):
                print(f"\n  {filepath}:")
                for msg in messages:
                    print(f"    ✗ {msg}")
            print()

    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
