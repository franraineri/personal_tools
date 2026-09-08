#!/usr/bin/env python3
"""Repair a corrupted ground_truth_cache.json without losing computed points.

The GT cache can end up with TWO (or more) JSON documents concatenated into a
single file ("Extra data" JSONDecodeError) when two processes flush it near
simultaneously. This tool merges every recoverable source into one valid v2.0
document, keeping the best entry per key.

Merge policy (loss-free):
  - Union of all entries across every source and every concatenated document.
  - On key conflict, keep the entry with the LOWEST energy (more converged =
    better ground truth — the project's canonical tie-breaker), rejecting
    non-finite energies and negative gaps.

Safety:
  - Refuses to run if a writer process may still be active (pass --force to
    override once you have confirmed no runner is writing the cache).
  - Always writes a timestamped backup of the current file before overwriting.
  - Writes atomically (temp file + os.replace).
  - --dry-run reports the merged entry count without writing.

Usage:
    # Preview the merge (no write)
    .venv/bin/python scripts/general_project_maintenance/repair_ground_truth_cache.py --dry-run

    # Repair, pulling extra sources (frozen snapshots, git HEAD export, etc.)
    .venv/bin/python scripts/general_project_maintenance/repair_ground_truth_cache.py \
        --extra /tmp/gt_recovery/gt_head.json --extra /tmp/gt_recovery/gt_6dec.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE = ROOT / "data" / "ground_truth_cache.json"


def parse_multi_doc(text: str) -> list[dict]:
    """Parse one or more concatenated JSON documents from a string.

    A healthy cache is a single doc; a corrupted one has 2+ docs separated by
    whitespace/newlines. Returns the list of successfully-parsed docs and stops
    at the first unrecoverable position (logging how much was skipped).
    """
    dec = json.JSONDecoder()
    docs: list[dict] = []
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = dec.raw_decode(text, idx)
        except json.JSONDecodeError as ex:
            print(f"  ⚠️ stopped parsing at char {idx}: {ex} "
                  f"({n - idx} trailing chars ignored)")
            break
        docs.append(obj)
        idx = end
    return docs


def extract_entries(doc: dict) -> dict[str, dict]:
    """Extract the {key: entry} mapping from a cache document (v2 or legacy)."""
    if not isinstance(doc, dict):
        return {}
    if "entries" in doc and isinstance(doc["entries"], dict):
        return doc["entries"]
    # Legacy flat format: the doc itself is the entries mapping (values are dicts).
    if all(isinstance(v, dict) for v in doc.values()):
        return doc
    return {}


def _entry_is_valid(entry: dict) -> bool:
    """Reject entries that would fail GroundTruthCache.put() validation."""
    try:
        e = float(entry.get("energy"))
        g = float(entry.get("gap"))
    except (TypeError, ValueError):
        return False
    if not (e == e) or abs(e) > 1e6:  # NaN or absurd magnitude
        return False
    if not (g == g) or g < 0:  # NaN or negative gap
        return False
    return True


def merge_sources(sources: list[Path]) -> tuple[dict[str, dict], dict]:
    """Merge all sources into a single {key: best_entry} mapping.

    Returns (merged_entries, stats).
    """
    merged: dict[str, dict] = {}
    stats = {
        "sources": [],
        "n_conflicts": 0,
        "n_conflicts_replaced": 0,
        "n_invalid_skipped": 0,
    }

    for src in sources:
        if not src.exists():
            stats["sources"].append({"path": str(src), "status": "missing"})
            continue
        text = src.read_text(encoding="utf-8", errors="replace")
        docs = parse_multi_doc(text)
        n_from_src = 0
        for doc in docs:
            for key, entry in extract_entries(doc).items():
                if not _entry_is_valid(entry):
                    stats["n_invalid_skipped"] += 1
                    continue
                n_from_src += 1
                if key not in merged:
                    merged[key] = entry
                else:
                    stats["n_conflicts"] += 1
                    # Tie-break: keep the LOWER energy (more converged).
                    if float(entry["energy"]) < float(merged[key]["energy"]) - 1e-12:
                        merged[key] = entry
                        stats["n_conflicts_replaced"] += 1
        stats["sources"].append(
            {"path": str(src), "status": "ok", "docs": len(docs), "valid_entries": n_from_src}
        )

    return merged, stats


def write_cache_atomic(path: Path, entries: dict[str, dict]) -> None:
    """Write a single valid v2.0 cache document atomically."""
    payload = {"version": "2.0", "n_entries": len(entries), "entries": entries}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".gt_repair_", suffix=".tmp")
    try:
        with open(fd, "w") as f:
            json.dump(payload, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _writer_may_be_active() -> list[str]:
    """Return command lines of processes that could write the GT cache."""
    import subprocess

    try:
        out = subprocess.run(["ps", "axo", "pid,command"], capture_output=True, text=True).stdout
    except Exception:
        return []
    risky = []
    for line in out.splitlines():
        low = line.lower()
        if "run_accelerated_cross_n" in low or "post_experiment_sync" in low:
            risky.append(line.strip())
    return risky


def main() -> int:
    ap = argparse.ArgumentParser(description="Repair corrupted ground_truth_cache.json (loss-free)")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="Cache file to repair")
    ap.add_argument("--extra", type=Path, action="append", default=[],
                    help="Extra source(s) to merge (frozen snapshots, git exports). Repeatable.")
    ap.add_argument("--dry-run", action="store_true", help="Report merge result without writing")
    ap.add_argument("--force", action="store_true",
                    help="Proceed even if a writer process appears active (use with care)")
    args = ap.parse_args()

    cache: Path = args.cache

    # ── Safety: refuse if a writer may be active ─────────────────────────
    risky = _writer_may_be_active()
    if risky and not args.force:
        print("  ❌ A process that may write the GT cache is running:")
        for r in risky[:5]:
            print(f"     {r[:120]}")
        print("  Stop it first, or re-run with --force once you've confirmed it's safe.")
        return 2

    # ── Assemble sources: current file FIRST (freshest points win ties only
    #    when energy is lower), then any extras. ──────────────────────────
    sources = [cache, *args.extra]
    print(f"  Merging {len(sources)} source(s):")
    merged, stats = merge_sources(sources)

    for s in stats["sources"]:
        if s["status"] == "ok":
            print(f"     ✓ {s['path']} — {s['docs']} doc(s), {s['valid_entries']} valid entries")
        else:
            print(f"     ⏭️ {s['path']} — {s['status']}")

    print(f"\n  Merged unique keys: {len(merged)}")
    print(f"  Conflicts: {stats['n_conflicts']} "
          f"({stats['n_conflicts_replaced']} replaced by lower energy)")
    print(f"  Invalid entries skipped: {stats['n_invalid_skipped']}")

    if args.dry_run:
        print("\n  (--dry-run) No file written.")
        return 0

    if not merged:
        print("  ❌ Nothing to write (0 valid entries merged). Aborting to avoid data loss.")
        return 1

    # ── Backup current file before overwriting ───────────────────────────
    if cache.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = cache.with_suffix(f".corrupt_backup_{ts}.json")
        backup.write_bytes(cache.read_bytes())
        print(f"  📦 Backup of current file → {backup.name}")

    write_cache_atomic(cache, merged)

    # ── Verify the result parses as a single valid document ──────────────
    reparsed = json.loads(cache.read_text())
    assert len(reparsed.get("entries", {})) == len(merged), "post-write entry count mismatch"
    print(f"  ✅ Repaired: {cache} now holds {len(merged)} entries (valid single JSON).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
