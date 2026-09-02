# TODO move mainteinance tools to a new project repository in order to reuse them. Also a refactor and modular parameters will be needed

"""Repository Cleanup Script — Remove temp files, caches, empty dirs, and junk.

Usage:
   .venv/bin/python scripts/general_project_maintenance/cleanup_repo.py              # Dry-run (show what would be removed)
   .venv/bin/python scripts/general_project_maintenance/cleanup_repo.py --execute    # Actually delete
   .venv/bin/python scripts/general_project_maintenance/cleanup_repo.py --verbose    # Show all scanned paths
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# ─── Configuration ──────────────────────────────────────────────────────────

# Directories to remove entirely (relative to ROOT)
DIRS_TO_REMOVE = [
    ".hypothesis",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "htmlcov",
    "tmp",
    "dist",
    "build",
]

# Glob patterns for cache directories (searched recursively)
CACHE_DIR_PATTERNS = [
    "__pycache__",
    "*.egg-info",
]

# Specific files to remove (relative to ROOT)
FILES_TO_REMOVE = [
    "temporal.txt",
    "test_output.log",
    ".coverage",
    ".project_health_state.json",
]

# Glob patterns for temp/junk files (searched recursively)
JUNK_FILE_PATTERNS = [
    ".DS_Store",
    "*.pyc",
    "*.pyo",
    "checkpoint_*",
]

# Directories to check for emptiness (will remove if empty after cleanup)
# Note: does NOT remove dirs with content
CHECK_EMPTY_DIRS = [
    "figures",
    "reports",
    "thesis_plots",
]

# Directories to NEVER touch (even if matched by patterns)
PROTECTED_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "_versions",  # model_zoo versioned checkpoints — NEVER delete
    "_best",  # model_zoo best pass_rate backups — NEVER delete
}


# ─── Core Logic ─────────────────────────────────────────────────────────────


class CleanupReport:
    """Tracks what was found and optionally removed."""

    def __init__(self) -> None:
        self.dirs_found: list[Path] = []
        self.files_found: list[Path] = []
        self.empty_dirs_found: list[Path] = []
        self.errors: list[tuple[Path, str]] = []

    @property
    def total_items(self) -> int:
        return len(self.dirs_found) + len(self.files_found) + len(self.empty_dirs_found)

    def size_estimate(self) -> int:
        """Estimate total bytes to be freed."""
        total = 0
        for f in self.files_found:
            try:
                total += f.stat().st_size
            except OSError:
                pass
        for d in self.dirs_found:
            try:
                total += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            except OSError:
                pass
        return total


def is_protected(path: Path) -> bool:
    """Check if path is inside a protected directory."""
    parts = path.relative_to(ROOT).parts
    return any(p in PROTECTED_DIRS for p in parts)


def scan_named_dirs(report: CleanupReport) -> None:
    """Find explicitly named directories to remove."""
    for rel_path in DIRS_TO_REMOVE:
        full = ROOT / rel_path
        if full.exists() and full.is_dir() and not is_protected(full):
            report.dirs_found.append(full)


def scan_cache_dirs(report: CleanupReport) -> None:
    """Find __pycache__ and similar directories recursively."""
    for pattern in CACHE_DIR_PATTERNS:
        for match in ROOT.rglob(pattern):
            if match.is_dir() and not is_protected(match):
                report.dirs_found.append(match)


def scan_named_files(report: CleanupReport) -> None:
    """Find explicitly named files to remove."""
    for rel_path in FILES_TO_REMOVE:
        full = ROOT / rel_path
        if full.exists() and full.is_file():
            report.files_found.append(full)


def scan_junk_files(report: CleanupReport) -> None:
    """Find .DS_Store, .pyc, etc. recursively."""
    for pattern in JUNK_FILE_PATTERNS:
        for match in ROOT.rglob(pattern):
            if match.is_file() and not is_protected(match):
                report.files_found.append(match)


def scan_empty_dirs(report: CleanupReport) -> None:
    """Find directories that are completely empty (no files, no subdirs).

    Dirs with only .DS_Store or .gitkeep are considered empty.
    Dirs with .gitkeep are NOT removed (intentional placeholders for git).
    """
    # First check explicitly listed dirs
    for rel_path in CHECK_EMPTY_DIRS:
        full = ROOT / rel_path
        if full.exists() and full.is_dir():
            contents = list(full.iterdir())
            real_contents = [c for c in contents if c.name not in (".DS_Store", ".gitkeep")]
            if not real_contents:
                # Don't remove dirs kept alive by .gitkeep
                has_gitkeep = any(c.name == ".gitkeep" for c in contents)
                if not has_gitkeep:
                    report.empty_dirs_found.append(full)

    # Also find any empty dirs recursively (bottom-up)
    for dirpath, dirnames, filenames in os.walk(ROOT, topdown=False):
        p = Path(dirpath)
        if is_protected(p):
            continue
        if p == ROOT:
            continue
        # Skip dirs that are already in our removal list
        if p in report.dirs_found:
            continue
        # Check if truly empty
        try:
            contents = list(p.iterdir())
            if not contents:
                report.empty_dirs_found.append(p)
            elif all(c.name == ".DS_Store" for c in contents):
                # Only .DS_Store — effectively empty, remove it
                report.empty_dirs_found.append(p)
        except PermissionError:
            pass


def scan_workload_dirs(report: CleanupReport) -> None:
    """Find 'workloads*' directories (temp workload artifacts)."""
    for item in ROOT.iterdir():
        if item.is_dir() and item.name.startswith("workloads"):
            if not is_protected(item):
                report.dirs_found.append(item)


def execute_cleanup(report: CleanupReport) -> None:
    """Actually remove the found items."""
    for d in report.dirs_found:
        try:
            shutil.rmtree(d)
        except OSError as e:
            report.errors.append((d, str(e)))

    for f in report.files_found:
        try:
            f.unlink()
        except OSError as e:
            report.errors.append((f, str(e)))

    for d in report.empty_dirs_found:
        try:
            if d.exists():  # May have been removed as child of another
                d.rmdir()
        except OSError as e:
            report.errors.append((d, str(e)))


def archive_zoo_orphans() -> list[tuple[Path, Path]]:
    """Move zoo orphan checkpoints to an archive directory.

    Orphans are .pt files in data/model_zoo/checkpoints/ that are NOT
    referenced by the manifest. They are moved (not deleted) to
    data/model_zoo/archived/ for safety.

    Returns list of (source, destination) pairs that were moved.
    """
    import json

    zoo_dir = ROOT / "data" / "model_zoo"
    checkpoints_dir = zoo_dir / "checkpoints"
    manifest_path = zoo_dir / "manifest.json"
    archive_dir = zoo_dir / "archived"

    if not checkpoints_dir.exists() or not manifest_path.exists():
        return []

    # Load manifest to get registered checkpoint filenames
    with open(manifest_path) as f:
        manifest = json.load(f)
    registered = {e["checkpoint_file"] for e in manifest}

    # Find orphans
    moved = []
    for pt_file in sorted(checkpoints_dir.glob("*.pt")):
        if pt_file.name not in registered:
            archive_dir.mkdir(parents=True, exist_ok=True)
            dest = archive_dir / pt_file.name
            shutil.move(str(pt_file), str(dest))
            moved.append((pt_file, dest))

    return moved


def format_size(nbytes: int) -> str:
    """Human-readable size string."""
    if nbytes < 1024:
        return f"{nbytes} B"
    elif nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} KB"
    else:
        return f"{nbytes / (1024 * 1024):.1f} MB"


def print_report(report: CleanupReport, *, verbose: bool = False, executed: bool = False) -> None:
    """Print summary of findings/actions."""
    action = "Removed" if executed else "Would remove"
    print(f"\n{'=' * 60}")
    print(f"  Repository Cleanup {'EXECUTED' if executed else '(DRY RUN)'}")
    print(f"{'=' * 60}\n")

    if report.dirs_found:
        print(f"📁 Directories ({len(report.dirs_found)}):")
        for d in sorted(report.dirs_found):
            rel = d.relative_to(ROOT)
            n_files = sum(1 for _ in d.rglob("*") if _.is_file()) if d.exists() else 0
            print(f"   {action}: {rel}/ ({n_files} files)")
        print()

    if report.files_found:
        print(f"📄 Files ({len(report.files_found)}):")
        if verbose or len(report.files_found) <= 20:
            for f in sorted(report.files_found):
                print(f"   {action}: {f.relative_to(ROOT)}")
        else:
            # Group by type
            by_ext: dict[str, int] = {}
            for f in report.files_found:
                ext = f.suffix or f.name
                by_ext[ext] = by_ext.get(ext, 0) + 1
            for ext, count in sorted(by_ext.items(), key=lambda x: -x[1]):
                print(f"   {action}: {count}× {ext} files")
        print()

    if report.empty_dirs_found:
        print(f"📂 Empty directories ({len(report.empty_dirs_found)}):")
        for d in sorted(report.empty_dirs_found):
            print(f"   {action}: {d.relative_to(ROOT)}/")
        print()

    size = report.size_estimate() if not executed else 0
    print(f"  Total items: {report.total_items}")
    if not executed:
        print(f"  Estimated space freed: {format_size(size)}")
        print("\n  Run with --execute to actually delete these items.")
    else:
        if report.errors:
            print(f"\n  ⚠️  Errors ({len(report.errors)}):")
            for path, err in report.errors:
                print(f"     {path.relative_to(ROOT)}: {err}")
        else:
            print("\n  ✅ All items removed successfully.")

    print()


# ─── CLI ────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean temporary files, caches, and empty directories from the repo."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete (default is dry-run)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show all individual files (not just summaries)",
    )
    parser.add_argument(
        "--no-archive-orphans",
        action="store_true",
        help="Skip archiving zoo orphan checkpoints (default: archive in --execute mode)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = CleanupReport()

    # Scan in order
    scan_named_dirs(report)
    scan_workload_dirs(report)
    scan_cache_dirs(report)
    scan_named_files(report)
    scan_junk_files(report)
    scan_empty_dirs(report)

    # Deduplicate (a file inside a dir-to-remove shouldn't be listed separately)
    dirs_set = set(report.dirs_found)
    report.files_found = [
        f for f in report.files_found if not any(f.is_relative_to(d) for d in dirs_set)
    ]
    report.empty_dirs_found = [
        d
        for d in report.empty_dirs_found
        if d not in dirs_set and not any(d.is_relative_to(dd) for dd in dirs_set)
    ]

    if report.total_items == 0:
        print("\n✅ Repository is already clean. Nothing to do.\n")
        return

    if args.execute:
        execute_cleanup(report)
        print_report(report, verbose=args.verbose, executed=True)
        # Archive zoo orphans (default in --execute mode)
        if not args.no_archive_orphans:
            moved = archive_zoo_orphans()
            if moved:
                print(f"\n  📦 Archived {len(moved)} zoo orphan checkpoint(s):")
                for src, dst in moved:
                    print(f"    {src.name} → archived/")
            else:
                print("\n  ✅ No zoo orphans to archive.")
    else:
        print_report(report, verbose=args.verbose, executed=False)
        # Show orphan preview in dry-run too
        if not args.no_archive_orphans:
            import json

            zoo_dir = ROOT / "data" / "model_zoo"
            checkpoints_dir = zoo_dir / "checkpoints"
            manifest_path = zoo_dir / "manifest.json"
            if checkpoints_dir.exists() and manifest_path.exists():
                with open(manifest_path) as f:
                    manifest = json.load(f)
                registered = {e["checkpoint_file"] for e in manifest}
                orphans = [p for p in checkpoints_dir.glob("*.pt") if p.name not in registered]
                if orphans:
                    total_size = sum(p.stat().st_size for p in orphans)
                    print(
                        f"\n  📦 Would archive {len(orphans)} zoo orphan(s) ({format_size(total_size)}):"
                    )
                    for p in sorted(orphans):
                        print(f"    {p.name} ({format_size(p.stat().st_size)})")


if __name__ == "__main__":
    main()
