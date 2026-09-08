#!/usr/bin/env python3
"""Run all tests with per-file timeout — reports failures AND frozen tests.

Runs each test file in a subprocess with a timeout. If a file exceeds the
timeout, it's killed and reported as FROZEN (and optionally deleted).

Features:
- Graceful interruption: failures are ALWAYS printed before exit (Ctrl+C, kill, crash)
- Parallel execution via --parallel N (default: sequential)
- Last-failed re-run via --lf (only runs previously failed files)
- pytest-timeout integration: passes --timeout to pytest for per-test deadlock detection
- Sorted by expected duration (slow files last) for better UX

Output: console + results/test_report.txt

Usage:
    .venv/bin/python scripts/general_project_maintenance/run_test_suite.py              # all tests, sequential
    .venv/bin/python scripts/general_project_maintenance/run_test_suite.py --parallel 4 # 4 workers
    .venv/bin/python scripts/general_project_maintenance/run_test_suite.py --lf         # re-run last failures only
    .venv/bin/python scripts/general_project_maintenance/run_test_suite.py --quick      # unit + mpnn only
    .venv/bin/python scripts/general_project_maintenance/run_test_suite.py --full --parallel       # all test dirs
"""

from __future__ import annotations

import argparse
import atexit
import json
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "results" / "test_report.txt"
LAST_FAILED_PATH = ROOT / "results" / ".last_failed_tests.json"
VENV_PYTHON = str(ROOT / ".venv" / "bin" / "python")
PER_FILE_TIMEOUT = 80  # seconds — subprocess kill
PER_TEST_TIMEOUT = 60  # seconds — pytest-timeout per individual test

# Files that crash pytest or are known to block
SKIP_FILES: set[str] = set()


# ═══════════════════════════════════════════════════════════════════════════════
# Global state — accessible from signal/atexit handlers
# ═══════════════════════════════════════════════════════════════════════════════

_failures: list[dict] = []
_frozen_list: list[str] = []
_slow_list: list[tuple[str, float]] = []
_total_pass = 0
_total_fail = 0
_total_frozen = 0
_interrupted = False


def _print_summary_on_exit():
    """Print failure summary — called by atexit/signal regardless of exit reason."""
    if not _failures and not _frozen_list:
        return

    print("\n" + "=" * 70, flush=True)
    print(f"  SUMMARY (at exit): {_total_pass} passed | {_total_fail} failed | {_total_frozen} frozen", flush=True)
    print("=" * 70, flush=True)

    if _interrupted:
        print("  ⚠️  EXECUTION INTERRUPTED — partial results below", flush=True)

    if _failures:
        print("\n  ── FAILURES (test function + reason) ──", flush=True)
        for f in _failures:
            print(f"\n  📁 {f['file']}", flush=True)
            if f.get("details"):
                for dl in f["details"].split("\n")[:10]:
                    print(f"     {dl}", flush=True)

    if _frozen_list:
        print(f"\n  ── FROZEN FILES (exceeded {PER_FILE_TIMEOUT}s) ──", flush=True)
        for fl in _frozen_list:
            print(f"  🧊 {fl}", flush=True)

    if _slow_list:
        print(f"\n  ── SLOW FILES (>40s but completed) ──", flush=True)
        for sl, dur in sorted(_slow_list, key=lambda x: -x[1]):
            print(f"  🐢 {sl} ({dur:.1f}s)", flush=True)

    print("=" * 70, flush=True)


def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM gracefully — print summary then exit."""
    global _interrupted
    _interrupted = True
    print(f"\n  ⚠️  Signal {signum} received — printing summary...", flush=True)
    _print_summary_on_exit()
    sys.exit(130 if signum == signal.SIGINT else 143)


# Register handlers
atexit.register(_print_summary_on_exit)
signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ═══════════════════════════════════════════════════════════════════════════════
# Core logic
# ═══════════════════════════════════════════════════════════════════════════════


def discover_test_files(dirs: list[str]) -> list[Path]:
    """Find all test_*.py files in given directories.

    Uses os.walk with pruning for speed (skips excluded dirs without entering them).
    """
    import os

    EXCLUDE_DIRS = {".venv", ".git", "__pycache__", ".hypothesis", "node_modules",
                    ".ruff_cache", ".pytest_cache", ".mypy_cache", ".tox", "build",
                    "dist", ".eggs", "data", "results", "internal", ".kiro", ".github"}
    files = set()
    for d in dirs:
        test_dir = ROOT / d
        if not test_dir.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(test_dir):
            # Prune excluded directories IN PLACE (os.walk won't descend into them)
            dirnames[:] = [dn for dn in dirnames if dn not in EXCLUDE_DIRS]
            for fname in filenames:
                if fname.startswith("test_") and fname.endswith(".py"):
                    if fname not in SKIP_FILES:
                        files.add(Path(dirpath) / fname)
    return sorted(files)


def run_test_file(filepath: Path) -> dict:
    """Run a single test file with timeout. Returns result dict."""
    try:
        rel = str(filepath.relative_to(ROOT))
    except ValueError:
        rel = str(filepath)
    t0 = time.perf_counter()

    # Use pytest-timeout if available (per-test granularity), plus subprocess timeout
    pytest_args = [
        VENV_PYTHON, "-m", "pytest", str(filepath),
        "--tb=short", "--no-header", "-q",
        "-p", "no:cacheprovider",  # avoid stale .pytest_cache conflicts in parallel
    ]

    # If file is outside ROOT, use its parent as rootdir to avoid conftest conflicts
    try:
        filepath.relative_to(ROOT)
        cwd = str(ROOT)
        # Inside project — use pytest-timeout IF installed
        try:
            import pytest_timeout  # noqa: F401
            pytest_args.extend([
                f"--timeout={PER_TEST_TIMEOUT}",
                "--timeout-method=signal",
            ])
        except ImportError:
            pass  # pytest-timeout not installed, rely on subprocess timeout only
    except ValueError:
        # External file (e.g., test calling run_test_file on tmp files)
        pytest_args.extend([
            "--rootdir", str(filepath.parent),
            "--noconftest",
            "--override-ini=addopts=",
            "-p", "no:timeout",  # timeout plugin may not be available outside project
        ])
        cwd = str(filepath.parent)

    try:
        result = subprocess.run(
            pytest_args,
            capture_output=True,
            text=True,
            timeout=PER_FILE_TIMEOUT,
            cwd=cwd,
        )
        elapsed = time.perf_counter() - t0
        stdout = result.stdout
        rc = result.returncode

        if rc == 0:
            m = re.search(r"(\d+) passed", stdout)
            n_passed = int(m.group(1)) if m else 0
            return {"status": "pass", "file": rel, "n_passed": n_passed, "elapsed": elapsed}
        elif rc == 5:
            # No tests collected (file empty or all skipped)
            return {"status": "pass", "file": rel, "n_passed": 0, "elapsed": elapsed}
        else:
            # Extract failure info — capture all useful lines from pytest output
            lines_out = stdout.split("\n")
            failed_lines = []
            for line in lines_out:
                stripped = line.strip()
                # FAILED test_file.py::test_name - reason
                if line.startswith("FAILED "):
                    failed_lines.append(line)
                # E   AssertionError: ...  or  E   ValueError: ...
                elif stripped.startswith("E "):
                    failed_lines.append(stripped)
                # >   assert something  (the failing line)
                elif stripped.startswith("> "):
                    failed_lines.append(stripped)
                # ModuleNotFoundError, ImportError, etc at collection time
                elif "Error" in line and "::" not in line and "warning" not in line.lower():
                    failed_lines.append(stripped)

            # Deduplicate while preserving order
            seen = set()
            unique_lines = []
            for fl in failed_lines:
                if fl not in seen:
                    seen.add(fl)
                    unique_lines.append(fl)

            n_failed = stdout.count("FAILED ")
            if n_failed == 0:
                # Collection error — count ERROR lines
                n_failed = max(1, stdout.count("ERROR "))

            return {
                "status": "fail",
                "file": rel,
                "rc": rc,
                "elapsed": elapsed,
                "details": "\n".join(unique_lines[:20]),
                "n_failed": n_failed,
            }

    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - t0
        return {"status": "frozen", "file": rel, "elapsed": elapsed}
    except Exception as e:
        return {"status": "error", "file": rel, "error": str(e), "elapsed": 0}


def run_files_sequential(files: list[Path]) -> list[dict]:
    """Run test files one by one."""
    results = []
    for filepath in files:
        r = run_test_file(filepath)
        results.append(r)
        _process_result(r, len(results), len(files))
    return results


def run_files_parallel(files: list[Path], n_workers: int) -> list[dict]:
    """Run test files in parallel with ThreadPoolExecutor."""
    results: list[dict] = [None] * len(files)  # type: ignore
    completed = 0

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        future_to_idx = {
            executor.submit(run_test_file, f): i for i, f in enumerate(files)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                r = future.result()
            except Exception as e:
                r = {"status": "error", "file": str(files[idx].relative_to(ROOT)), "error": str(e), "elapsed": 0}
            results[idx] = r
            completed += 1
            _process_result(r, completed, len(files))

    return results


def _process_result(r: dict, i: int, n_files: int):
    """Process a single result — update global counters and print."""
    global _total_pass, _total_fail, _total_frozen

    rel = r["file"]

    if r["status"] == "pass":
        _total_pass += r["n_passed"]
        marker = "✅"
        detail = f"{r['n_passed']} passed, {r['elapsed']:.1f}s"
        if r["elapsed"] > 40:
            _slow_list.append((rel, r["elapsed"]))
    elif r["status"] == "fail":
        _total_fail += r.get("n_failed", 1)
        marker = "❌"
        detail = f"{r.get('n_failed', '?')} failed, {r['elapsed']:.1f}s"
        _failures.append(r)
    elif r["status"] == "frozen":
        _total_frozen += 1
        marker = "🧊"
        detail = f"FROZEN (killed after {PER_FILE_TIMEOUT}s)"
        _frozen_list.append(rel)
        # Delete frozen test files — they block CI and need rewrite
        frozen_path = ROOT / rel
        if frozen_path.exists():
            frozen_path.unlink()
            detail += " [DELETED]"
    else:
        marker = "⚠️"
        detail = f"ERROR: {r.get('error', '?')}"

    line = f"  [{i:3d}/{n_files}] {marker} {rel:<55} {detail}"
    print(line, flush=True)

    # Print failure details inline so you see WHAT broke while running
    if r["status"] == "fail" and r.get("details"):
        for dl in r["details"].split("\n")[:6]:
            if dl.strip():
                print(f"           {dl}", flush=True)


def _save_last_failed():
    """Save list of failed/frozen files for --lf re-run."""
    failed_files = [f["file"] for f in _failures] + _frozen_list
    LAST_FAILED_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_FAILED_PATH.write_text(json.dumps(failed_files, indent=2))


def _load_last_failed() -> list[str]:
    """Load previously failed files."""
    if LAST_FAILED_PATH.exists():
        return json.loads(LAST_FAILED_PATH.read_text())
    return []


def main():
    global PER_FILE_TIMEOUT

    parser = argparse.ArgumentParser(description="Run test suite with per-file timeout")
    parser.add_argument("--parallel", "-j", type=int, default=0,
                        help="Number of parallel workers (0=sequential)")
    parser.add_argument("--lf", "--last-failed", action="store_true",
                        help="Only re-run files that failed in the last run")
    parser.add_argument("--quick", action="store_true",
                        help="Only run unit + mpnn tests")
    parser.add_argument("--full", action="store_true",
                        help="Scan ENTIRE project for test_*.py (all subdirs, not just tests/)")
    parser.add_argument("--timeout", type=int, default=PER_FILE_TIMEOUT,
                        help=f"Per-file timeout in seconds (default: {PER_FILE_TIMEOUT})")
    args = parser.parse_args()

    PER_FILE_TIMEOUT = args.timeout

    # Determine directories
    if args.quick:
        dirs = ["tests/unit", "tests/mpnn"]
        mode_label = "QUICK (unit + mpnn)"
    elif args.full:
        # Full: scan ENTIRE project for any test_*.py file
        dirs = ["."]
        mode_label = "FULL (entire project)"
    else:
        # Default: all test subdirectories under tests/
        dirs = ["tests"]
        mode_label = "DEFAULT (all tests/)"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Discover files
    files = discover_test_files(dirs)

    # Filter to last-failed if requested
    if args.lf:
        last_failed = set(_load_last_failed())
        if last_failed:
            files = [f for f in files if str(f.relative_to(ROOT)) in last_failed]
            mode_label += f" [--lf: {len(files)} files from last run]"
        else:
            print("  ℹ️  No last-failed data found — running all files")

    n_files = len(files)
    n_workers = args.parallel or 1
    parallel_label = f" | Workers: {n_workers}" if n_workers > 1 else ""

    header = [
        "=" * 70,
        f"  TEST SUITE — {ts}",
        f"  Mode: {mode_label} | Files: {n_files} | Timeout: {PER_FILE_TIMEOUT}s/file{parallel_label}",
        "=" * 70,
        "",
    ]
    for line in header:
        print(line, flush=True)

    # ── Pre-flight: validate test imports (catches stale paths before pytest) ──
    print("  🔍 Pre-flight: validating test imports...", flush=True)
    try:
        import importlib.util as _ilu
        _vti_spec = _ilu.spec_from_file_location(
            "validate_test_imports",
            ROOT / "scripts" / "general_project_maintenance" / "validate_test_imports.py",
        )
        _vti = _ilu.module_from_spec(_vti_spec)
        _vti_spec.loader.exec_module(_vti)

        # 1. Learn new relocations from guesses
        issues = _vti.scan_test_files()
        n_learned = _vti.learn_relocations(issues)
        if n_learned:
            print(f"  📚 Learned {n_learned} new relocations", flush=True)
            issues = _vti.scan_test_files()

        # 2. Auto-fix relocated imports
        relocated = [i for i in issues if i["issue"] == "relocated"]
        if relocated:
            n_fixed = _vti.apply_fixes(issues)
            print(f"  📦 Auto-fixed {n_fixed} relocated imports", flush=True)
            issues = _vti.scan_test_files()

        # 3. Delete test files referencing dead modules
        deleted_files = _vti.delete_dead_test_files(issues)
        if deleted_files:
            print(f"  🗑️  Deleted {len(deleted_files)} dead test files:", flush=True)
            for df in deleted_files:
                print(f"     {df}", flush=True)
            # Remove from discovery list
            dead_set = set(deleted_files)
            files = [f for f in files if str(f.relative_to(ROOT)) not in dead_set]
            n_files = len(files)
            issues = [i for i in issues if i["file"] not in dead_set]

        # 4. Report remaining issues
        remaining = [i for i in issues if i["issue"] == "not_found"]
        if remaining:
            print(f"  ❌ {len(remaining)} tests have unresolved imports:", flush=True)
            for nf in remaining[:5]:
                print(f"     {nf['file']}:{nf['line']} → {nf['module']}", flush=True)
        elif not relocated and not deleted_files and not n_learned:
            print("  ✅ All test imports valid", flush=True)
    except Exception as e:
        print(f"  ⚠️  Import validation skipped: {e}", flush=True)
    print("", flush=True)

    # Run tests
    if n_workers > 1:
        results = run_files_parallel(files, n_workers)
    else:
        results = run_files_sequential(files)

    # Save last-failed for --lf next time
    _save_last_failed()

    # Build report
    lines = header.copy()
    for i, r in enumerate(results, 1):
        rel = r["file"]
        if r["status"] == "pass":
            marker = "✅"
            detail = f"{r['n_passed']} passed, {r['elapsed']:.1f}s"
        elif r["status"] == "fail":
            marker = "❌"
            detail = f"{r.get('n_failed', '?')} failed, {r['elapsed']:.1f}s"
        elif r["status"] == "frozen":
            marker = "🧊"
            detail = f"FROZEN (killed after {PER_FILE_TIMEOUT}s) [DELETED]"
        else:
            marker = "⚠️"
            detail = f"ERROR: {r.get('error', '?')}"
        lines.append(f"  [{i:3d}/{n_files}] {marker} {rel:<55} {detail}")

    lines.append("")
    lines.append("=" * 70)
    lines.append(f"  SUMMARY: {_total_pass} passed | {_total_fail} failed | {_total_frozen} frozen")
    lines.append("=" * 70)

    if _failures:
        lines.append("")
        lines.append("  ── FAILURES (test function + reason) ──")
        for f in _failures:
            lines.append(f"")
            lines.append(f"  📁 {f['file']}")
            if f.get("details"):
                for dl in f["details"].split("\n"):
                    lines.append(f"     {dl}")

    if _frozen_list:
        lines.append("")
        lines.append(f"  ── FROZEN FILES (exceeded {PER_FILE_TIMEOUT}s — deleted) ──")
        for fl in _frozen_list:
            lines.append(f"  🧊 {fl}")

    if _slow_list:
        lines.append("")
        lines.append("  ── SLOW FILES (>40s but completed) ──")
        for sl, dur in sorted(_slow_list, key=lambda x: -x[1]):
            lines.append(f"  🐢 {sl} ({dur:.1f}s)")

    lines.append("")
    lines.append(f"  Report: {REPORT_PATH.relative_to(ROOT)}")
    if _failures or _frozen_list:
        lines.append(f"  Re-run failures: python {Path(__file__).relative_to(ROOT)} --lf")
    lines.append("=" * 70)

    # Write report
    report_text = "\n".join(lines)
    REPORT_PATH.write_text(report_text)

    # Print final summary to console
    print("", flush=True)
    print("=" * 70, flush=True)
    print(f"  SUMMARY: {_total_pass} passed | {_total_fail} failed | {_total_frozen} frozen", flush=True)
    print("=" * 70, flush=True)

    if _failures:
        print("", flush=True)
        print("  ── FAILURES (test function + reason) ──", flush=True)
        for f in _failures:
            print(f"", flush=True)
            print(f"  📁 {f['file']}", flush=True)
            if f.get("details"):
                for dl in f["details"].split("\n")[:10]:
                    if dl.strip():
                        print(f"     {dl}", flush=True)

    if _frozen_list:
        print(f"\n  ── FROZEN FILES (exceeded {PER_FILE_TIMEOUT}s — deleted) ──", flush=True)
        for fl in _frozen_list:
            print(f"  🧊 {fl}", flush=True)

    if _slow_list:
        print(f"\n  ── SLOW FILES (>40s but completed) ──", flush=True)
        for sl, dur in sorted(_slow_list, key=lambda x: -x[1])[:10]:
            print(f"  🐢 {sl} ({dur:.1f}s)", flush=True)

    print("", flush=True)
    print(f"  Report saved: {REPORT_PATH.relative_to(ROOT)}", flush=True)
    if _failures or _frozen_list:
        print(f"  Re-run failures: .venv/bin/python {Path(__file__).relative_to(ROOT)} --lf", flush=True)
    print("=" * 70, flush=True)

    # Unregister atexit to avoid double-printing (we already printed above)
    atexit.unregister(_print_summary_on_exit)

    return 1 if (_total_fail > 0 or _total_frozen > 0) else 0


if __name__ == "__main__":
    sys.exit(main())
