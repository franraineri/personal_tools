#!/usr/bin/env python3
"""Nightly maintenance: sync all data stores, regenerate reports, update status.

Designed to run unattended (cron/launchd). Exits 0 on success, 1 on partial failure.
All steps are idempotent and crash-safe.

Usage:
    .venv/bin/python scripts/general_project_maintenance/nightly_sync.py
    .venv/bin/python scripts/general_project_maintenance/nightly_sync.py --dry-run
    .venv/bin/python scripts/general_project_maintenance/nightly_sync.py --verbose
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

logger = logging.getLogger("nightly_sync")


def _step(name: str, fn, *, dry_run: bool = False) -> tuple[bool, float]:
    """Run a step, return (success, elapsed_seconds)."""
    logger.info("── %s ──", name)
    if dry_run:
        logger.info("  [DRY-RUN] skipped")
        return True, 0.0
    t0 = time.perf_counter()
    try:
        fn()
        elapsed = time.perf_counter() - t0
        logger.info("  OK (%.1fs)", elapsed)
        return True, elapsed
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        logger.error("  FAILED (%.1fs): %s", elapsed, exc)
        return False, elapsed


def step_post_experiment_sync():
    """Full post-experiment sync: GT check, dashboard, scoreboard, eval report, etc."""
    from qmbp_simulation.analysis.metrics import post_experiment_sync

    post_experiment_sync(verbose=False)


def step_update_cross_n_coverage():
    """Regenerate cross-N coverage documentation."""
    import subprocess

    result = subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            str(ROOT / "scripts" / "maintenance" / "update_cross_n_coverage.py"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError(f"update_cross_n_coverage failed: {result.stderr[-500:]}")


def step_update_project_status():
    """Regenerate .kiro/steering/project-status.md."""
    import subprocess

    result = subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            str(ROOT / "scripts" / "general_project_maintenance" / "update_project_status.py"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError(f"update_project_status failed: {result.stderr[-500:]}")


def step_zoo_coherence():
    """Quick zoo coherence check."""
    import subprocess

    result = subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            str(ROOT / "scripts" / "maintenance" / "check_zoo_coherence.py"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        logger.warning("  Zoo coherence issues: %s", result.stdout[-200:])


def step_gt_npz_coherence():
    """Validate GT cache vs NPZ data consistency."""
    from qmbp_simulation.analysis.metrics import validate_gt_npz_coherence

    result = validate_gt_npz_coherence()
    summary = result.get("summary", "")
    if "STALE" in summary.upper() or "MISMATCH" in summary.upper():
        logger.warning("  GT-NPZ issues: %s", summary[:200])


def main() -> int:
    parser = argparse.ArgumentParser(description="Nightly maintenance sync")
    parser.add_argument("--dry-run", action="store_true", help="Print steps without executing")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("Nightly sync started")
    t_start = time.perf_counter()

    steps = [
        ("Post-experiment sync (dashboard + scoreboard + ResultIndex)", step_post_experiment_sync),
        ("GT ↔ NPZ coherence check", step_gt_npz_coherence),
        ("Zoo coherence check", step_zoo_coherence),
        ("Update project-status.md", step_update_project_status),
        ("Update cross-N coverage docs", step_update_cross_n_coverage),
    ]

    results = []
    for name, fn in steps:
        ok, elapsed = _step(name, fn, dry_run=args.dry_run)
        results.append((name, ok, elapsed))

    total = time.perf_counter() - t_start
    n_ok = sum(1 for _, ok, _ in results if ok)
    n_fail = len(results) - n_ok

    logger.info("─" * 50)
    logger.info("Nightly sync done: %d/%d passed, %.0fs total", n_ok, len(results), total)
    if n_fail > 0:
        for name, ok, _ in results:
            if not ok:
                logger.error("  FAILED: %s", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
