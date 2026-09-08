#!/usr/bin/env python3
"""Prune legacy-format keys from eval_cache.json (p-aware hygiene).

The EvalCache key schema changed over time. The CURRENT schema
(EvalCache.make_key) is:

    model|topology|N|p_layers|J{:.4f}|h{:.2f}|theta_hash
                     ^^^^^^^^          ^^^^^^
                     p is 4th field    h has EXACTLY 2 decimals

Older entries used a different 4th field (n_params, not p_layers) and wrote h
with full float precision (e.g. "2.69000000", 8 decimals). Because the two
formats use different h precision, they live in DISJOINT key namespaces — a
lookup with the current make_key (h:.2f) can NEVER match a legacy 8-decimal
key. So the legacy entries are dead weight that:
  - are not p-differentiated (their 4th field is n_params, mixing p=1/p=2),
  - never produce a cache hit,
  - only consume the LRU budget (cap 50k), evicting useful modern entries.

This tool keeps ONLY current-format entries (h with <= 2 decimals) and drops
the rest. No real information is lost: eval-cache entries are cheap circuit
energy evaluations that recompute in milliseconds, and the dropped keys were
already unreachable.

Safety:
  - Refuses to run if a writer process may be active (--force to override).
  - Timestamped backup of the current file before overwriting.
  - Atomic write (temp file + os.replace).
  - --dry-run reports what would be kept/dropped without writing.

Usage:
    .venv/bin/python scripts/general_project_maintenance/prune_eval_cache_legacy_keys.py --dry-run
    .venv/bin/python scripts/general_project_maintenance/prune_eval_cache_legacy_keys.py
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
DEFAULT_CACHE = ROOT / "data" / "eval_cache.json"

# Current schema: model|topo|N|p|J{:.4f}|h{:.2f}|hash → 7 fields.
_CURRENT_N_FIELDS = 7
_MAX_H_DECIMALS = 2  # current make_key formats h as {:.2f}


def _h_decimals(h_field: str) -> int:
    """Number of decimal digits in the h field (0 if no decimal point)."""
    return len(h_field.split(".")[-1]) if "." in h_field else 0


def is_current_format(key: str) -> bool:
    """True iff the key matches the CURRENT p-aware make_key schema.

    Discriminant: exactly 7 pipe-separated fields, a 'J'-prefixed 5th field,
    and an h field (6th) with at most 2 decimals. Legacy keys wrote h with
    full float precision (>2 decimals) and/or had a different field count.
    """
    parts = key.split("|")
    if len(parts) != _CURRENT_N_FIELDS:
        return False
    if not parts[4].startswith("J"):
        return False
    return _h_decimals(parts[5]) <= _MAX_H_DECIMALS


def _writer_may_be_active() -> list[str]:
    """Return command lines of processes that could write the eval cache."""
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


def write_cache_atomic(path: Path, entries: dict[str, float], version: str) -> None:
    payload = {"version": version, "n_entries": len(entries), "entries": entries}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".eval_prune_", suffix=".tmp")
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Prune legacy-format keys from eval_cache.json")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--dry-run", action="store_true", help="Report kept/dropped without writing")
    ap.add_argument("--force", action="store_true", help="Proceed even if a writer seems active")
    args = ap.parse_args()

    cache: Path = args.cache

    if not cache.exists():
        print(f"  ❌ Cache not found: {cache}")
        return 1

    # ── Safety: refuse if a writer may be active ─────────────────────────
    risky = _writer_may_be_active()
    if risky and not args.force:
        print("  ❌ A process that may write the eval cache is running:")
        for r in risky[:5]:
            print(f"     {r[:120]}")
        print("  Stop it first, or re-run with --force once you've confirmed it's safe.")
        return 2

    # ── Load (must be a single valid JSON doc) ───────────────────────────
    try:
        doc = json.loads(cache.read_text())
    except json.JSONDecodeError as ex:
        print(f"  ❌ {cache.name} is not a single valid JSON document: {ex}")
        print("  Repair concatenated/corrupt caches first before pruning.")
        return 1

    entries = doc.get("entries", {}) if isinstance(doc, dict) else {}
    version = doc.get("version", "1.0") if isinstance(doc, dict) else "1.0"
    if not entries:
        print("  ⚠️ No entries found — nothing to prune.")
        return 0

    kept = {k: v for k, v in entries.items() if is_current_format(k)}
    n_total = len(entries)
    n_kept = len(kept)
    n_dropped = n_total - n_kept

    print(f"  eval_cache: {n_total} entries")
    print(f"    current-format (p-aware, h:.2f) → KEEP: {n_kept}")
    print(f"    legacy-format (unreachable)     → DROP: {n_dropped}")

    if args.dry_run:
        print("\n  (--dry-run) No file written.")
        return 0

    if n_dropped == 0:
        print("  ✅ Nothing to prune — cache already clean.")
        return 0

    if n_kept == 0:
        print("  ❌ Refusing to write: every entry classified as legacy. "
              "Aborting to avoid wiping the cache.")
        return 1

    # ── Backup then atomic write ─────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = cache.with_suffix(f".prebrune_backup_{ts}.json")
    backup.write_bytes(cache.read_bytes())
    print(f"  📦 Backup → {backup.name}")

    write_cache_atomic(cache, kept, version)

    # ── Verify result parses and holds exactly the kept entries ──────────
    reparsed = json.loads(cache.read_text())
    assert len(reparsed.get("entries", {})) == n_kept, "post-write count mismatch"
    print(f"  ✅ Pruned: {cache.name} now holds {n_kept} current-format entries "
          f"(dropped {n_dropped} legacy, valid single JSON).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
