"""Configuration loading for maintenance tools.

Supports loading from:
1. TOML files (preferred, human-editable)
2. Python dicts (for programmatic use)
3. Fallback defaults (hardcoded, always available)

Configuration files are searched in order:
1. Explicit path passed to load_config()
2. .kiro/settings/maintenance.toml (project-level)
3. Built-in defaults

Usage:
    from core.config import load_config, get_cleanup_config, get_scan_config

    # Full config from TOML or defaults
    cfg = load_config()

    # Specific section
    cleanup = get_cleanup_config()
    cleanup["dirs_to_remove"]  # ["__pycache__", ".hypothesis", ...]

    scan = get_scan_config()
    scan["scan_dirs"]  # [("src/qmbp_simulation", "LIB"), ...]
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Try tomllib (3.11+) or tomli as fallback
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


def _project_root() -> Path:
    """Resolve project root (parent of scripts/general_project_maintenance/)."""
    return Path(__file__).resolve().parents[2]


# ─── Default Configurations ────────────────────────────────────────────────

DEFAULT_CLEANUP_CONFIG: dict[str, Any] = {
    "dirs_to_remove": [
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "htmlcov",
        "tmp",
        "dist",
        "build",
    ],
    "cache_dir_patterns": [
        "__pycache__",
        "*.egg-info",
    ],
    "files_to_remove": [
        "temporal.txt",
        "test_output.log",
        ".coverage",
        ".project_health_state.json",
    ],
    "junk_file_patterns": [
        ".DS_Store",
        "*.pyc",
        "*.pyo",
        "checkpoint_*",
    ],
    "check_empty_dirs": [
        "figures",
        "reports",
        "thesis_plots",
    ],
    "protected_dirs": [
        ".git",
        ".venv",
        "node_modules",
        "_versions",
        "_best",
    ],
}

DEFAULT_SCAN_CONFIG: dict[str, Any] = {
    "scan_dirs": [
        ["src/qmbp_simulation", "LIB"],
        ["scripts/analysis", "SCRIPT"],
        ["scripts/benchmarks", "SCRIPT"],
        ["scripts/experiment_runners", "RUNNER"],
        ["scripts/hardware", "SCRIPT"],
        ["scripts/maintenance", "MAINT"],
        ["scripts/validation", "SCRIPT"],
        ["project_health", "HEALTH"],
        ["experiments", "EXP"],
        ["notebooks", "NB"],
    ],
    "skip_dirs": [
        "__pycache__",
        ".venv",
        "_deprecated",
        ".git",
        "node_modules",
        ".hypothesis",
    ],
    "skip_files": [
        "__init__.py",
        "__pycache__",
        "conftest.py",
    ],
    "max_depth": 3,
    "target_prefix": "qmbp_simulation",
}

DEFAULT_PHANTOM_CONFIG: dict[str, Any] = {
    "default_modules": [
        "qmbp_simulation.analysis.metrics",
    ],
    "skip_dirs": [
        "__pycache__",
        ".venv",
        "_deprecated",
        ".git",
        "node_modules",
        ".hypothesis",
    ],
}

DEFAULT_TRIM_CONFIG: dict[str, Any] = {
    "min_ratio": 3.0,
    "sections_to_remove": [
        "Parameters",
        "Params",
        "Args",
        "Arguments",
        "Returns",
        "Return",
        "Raises",
        "Yields",
        "Yield",
        "Attributes",
        "Attrs",
        "Notes",
        "Note",
        "References",
        "See Also",
        "Warnings",
        "Warning",
        "Todo",
        "TODO",
    ],
    "sections_to_preserve": [
        "Example",
        "Examples",
        "Usage",
    ],
    "skip_dirs": [
        "__pycache__",
        ".venv",
        "_deprecated",
        ".git",
        "node_modules",
        ".hypothesis",
        "tests",
    ],
}

DEFAULT_VERIFY_CONFIG: dict[str, Any] = {
    "stale_days": 60,
    "chars_per_token": 4,
    "always_token_budget": 12_000,
    "per_file_token_warn": 4_000,
    "steering_extensions": ["*.md", "*.txt"],
}


# ─── Loading Logic ─────────────────────────────────────────────────────────


def _find_config_file(explicit_path: Path | None = None) -> Path | None:
    """Find the first existing config file."""
    if explicit_path and explicit_path.exists():
        return explicit_path

    # Project-level config
    project_config = _project_root() / ".kiro" / "settings" / "maintenance.toml"
    if project_config.exists():
        return project_config

    return None


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load full configuration from TOML file or return defaults.

    Parameters
    ----------
    path : Path | None
        Explicit path to a TOML config file. If None, searches standard locations.

    Returns
    -------
    dict[str, Any]
        Full configuration dict with sections: cleanup, scan, phantom, trim, verify.
    """
    defaults = {
        "cleanup": DEFAULT_CLEANUP_CONFIG.copy(),
        "scan": DEFAULT_SCAN_CONFIG.copy(),
        "phantom": DEFAULT_PHANTOM_CONFIG.copy(),
        "trim": DEFAULT_TRIM_CONFIG.copy(),
        "verify": DEFAULT_VERIFY_CONFIG.copy(),
    }

    config_file = _find_config_file(path)
    if config_file is None or tomllib is None:
        return defaults

    try:
        with open(config_file, "rb") as f:
            user_config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError if tomllib else Exception) as e:
        print(f"  ⚠ Config load error ({config_file}): {e}", file=sys.stderr)
        return defaults

    # Deep merge: user_config overrides defaults per-section
    for section in defaults:
        if section in user_config:
            if isinstance(defaults[section], dict) and isinstance(user_config[section], dict):
                defaults[section].update(user_config[section])
            else:
                defaults[section] = user_config[section]

    return defaults


def get_cleanup_config(path: Path | None = None) -> dict[str, Any]:
    """Get cleanup-specific configuration."""
    return load_config(path)["cleanup"]


def get_scan_config(path: Path | None = None) -> dict[str, Any]:
    """Get module-index scan configuration."""
    return load_config(path)["scan"]


def get_phantom_config(path: Path | None = None) -> dict[str, Any]:
    """Get phantom-check configuration."""
    return load_config(path)["phantom"]


def get_trim_config(path: Path | None = None) -> dict[str, Any]:
    """Get trim-overdocumented configuration."""
    return load_config(path)["trim"]


def get_verify_config(path: Path | None = None) -> dict[str, Any]:
    """Get verify-steerings configuration."""
    return load_config(path)["verify"]
