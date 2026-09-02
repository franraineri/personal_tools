"""Import statement parsing and phantom-import resolution.

Provides an inverted-index approach: scan all .py files ONCE, collect
all `from X import Y` statements, then verify per-module. This reduces
complexity from O(modules × files) to O(files + modules).

Shared by check_phantom_functions.py and generate_module_index.py --verify.

Usage:
    from core.import_analysis import build_import_index, resolve_phantom_imports

    # Scan once, query many times
    index = build_import_index(project_root)
    phantoms = resolve_phantom_imports(index, "qmbp_simulation.analysis.metrics")
"""

from __future__ import annotations

import importlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Directories to always skip during scanning
SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".venv",
        "_deprecated",
        ".git",
        "node_modules",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "htmlcov",
    }
)


@dataclass
class ImportEntry:
    """A single import statement found in a file."""

    file_path: str
    module_path: str
    symbol_name: str
    line_number: int = 0


@dataclass
class ImportIndex:
    """Inverted index: module_path → list of (file, symbol, line) entries.

    Built once from a full project scan, then queried per-module.
    """

    # module_path → list of ImportEntry
    by_module: dict[str, list[ImportEntry]] = field(default_factory=lambda: {})
    files_scanned: int = 0
    total_imports: int = 0

    def get_imports_from(self, module_path: str) -> list[ImportEntry]:
        """Get all imports from a specific module."""
        return self.by_module.get(module_path, [])

    @property
    def modules_referenced(self) -> list[str]:
        """All module paths that appear in at least one import."""
        return sorted(self.by_module.keys())


def extract_names_from_import(import_text: str) -> list[str]:
    """Extract clean symbol names from an import statement text.

    Handles:
    - Comma-separated names: "A, B, C"
    - Trailing commas: "A, B,"
    - Aliases: "A as alias" → extracts "A"
    - Parenthesized: "(A, B, C)"
    - Multiline blocks with comments: "A,  # comment\\nB,"
    - Noqa annotations: "A  # noqa: E402" → extracts "A"
    - Inline noqa on first line of multi-line: "  # noqa: F401, E402\\n..."

    Parameters
    ----------
    import_text : str
        The right-hand side of `from module import <this part>`.

    Returns
    -------
    list[str]
        Clean symbol names (without aliases, comments, or whitespace).
    """
    # Remove parentheses and normalize
    text = import_text.strip().strip("()")

    # Split by comma and newline
    raw_names = re.split(r"[,\n]", text)

    names: list[str] = []
    for raw in raw_names:
        # Strip inline comments (# noqa, # type: ignore, etc.)
        name = raw.split("#")[0].strip()
        if not name or name == ")":
            continue
        # Handle "Name as alias" → keep "Name"
        if " as " in name:
            name = name.split(" as ")[0].strip()
        # Handle trailing whitespace or stray characters
        name = name.split()[0] if " " in name else name
        # Final validation: must be a valid Python identifier
        # AND must start with a letter or underscore (not a digit-starting flake8 code)
        if (
            name.isidentifier()
            and name not in ("self", "cls", "True", "False", "None")
            and not _is_likely_lint_code(name)
        ):
            names.append(name)

    return names


def _is_likely_lint_code(name: str) -> bool:
    """Check if a name looks like a linter code (E402, F401, W503, etc.)."""
    if len(name) < 2 or len(name) > 6:
        return False
    # Pattern: 1 uppercase letter + digits (E402, F401, W503, B027, etc.)
    return name[0].isupper() and name[1:].isdigit()


def build_import_index(
    project_root: Path,
    *,
    target_prefix: str = "",
    exclude_dirs: frozenset[str] | None = None,
) -> ImportIndex:
    """Scan all .py files once and build an inverted import index.

    Parameters
    ----------
    project_root : Path
        Root directory to scan recursively.
    target_prefix : str
        If set, only index imports from modules starting with this prefix.
        E.g. "qmbp_simulation" to skip stdlib/third-party imports.
    exclude_dirs : frozenset[str] | None
        Directory names to skip. Defaults to SKIP_DIRS.

    Returns
    -------
    ImportIndex
        The fully built index, ready for queries.
    """
    skip = exclude_dirs if exclude_dirs is not None else SKIP_DIRS
    index = ImportIndex()

    # Compile patterns
    # Matches: from <module> import <names>  (single line, NO opening paren)
    single_re = re.compile(r"^from\s+([\w.]+)\s+import\s+([^(\n]+)$", re.MULTILINE)
    # Matches: from <module> import (\n<names>\n)  (multi-line)
    multi_re = re.compile(
        r"^from\s+([\w.]+)\s+import\s+\(([^)]*)\)",
        re.MULTILINE | re.DOTALL,
    )

    for py_file in project_root.rglob("*.py"):
        # Skip excluded directories
        if any(part in skip for part in py_file.parts):
            continue

        try:
            source = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        index.files_scanned += 1
        file_str = str(py_file)

        # Single-line imports
        for match in single_re.finditer(source):
            module_path = match.group(1)
            if target_prefix and not module_path.startswith(target_prefix):
                continue
            names = extract_names_from_import(match.group(2))
            for name in names:
                entry = ImportEntry(
                    file_path=file_str,
                    module_path=module_path,
                    symbol_name=name,
                    line_number=source[: match.start()].count("\n") + 1,
                )
                index.by_module.setdefault(module_path, []).append(entry)
                index.total_imports += 1

        # Multi-line imports
        for match in multi_re.finditer(source):
            module_path = match.group(1)
            if target_prefix and not module_path.startswith(target_prefix):
                continue
            names = extract_names_from_import(match.group(2))
            line_num = source[: match.start()].count("\n") + 1
            for name in names:
                entry = ImportEntry(
                    file_path=file_str,
                    module_path=module_path,
                    symbol_name=name,
                    line_number=line_num,
                )
                index.by_module.setdefault(module_path, []).append(entry)
                index.total_imports += 1

    return index


def resolve_phantom_imports(
    index: ImportIndex,
    module_path: str,
    *,
    verbose: bool = False,
) -> list[ImportEntry]:
    """Check which imports from a module are phantoms (symbol doesn't exist).

    Parameters
    ----------
    index : ImportIndex
        Pre-built import index (from build_import_index).
    module_path : str
        Dotted module path to verify (e.g. "qmbp_simulation.analysis.metrics").
    verbose : bool
        Print progress information.

    Returns
    -------
    list[ImportEntry]
        Entries where the symbol does not exist in the target module.
    """
    entries = index.get_imports_from(module_path)
    if not entries:
        return []

    # Import the target module
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        if verbose:
            print(f"  ERROR: Cannot import {module_path}: {e}", file=sys.stderr)
        return []

    # Check each symbol
    phantoms: list[ImportEntry] = []
    seen: set[tuple[str, str]] = set()

    for entry in entries:
        key = (entry.file_path, entry.symbol_name)
        if key in seen:
            continue
        seen.add(key)

        if not hasattr(module, entry.symbol_name):
            phantoms.append(entry)
            if verbose:
                print(
                    f"    PHANTOM: {entry.file_path}:{entry.line_number} → "
                    f"{module_path}.{entry.symbol_name}",
                    file=sys.stderr,
                )

    return phantoms


def discover_submodules(src_dir: Path, package_name: str = "qmbp_simulation") -> list[str]:
    """Discover all importable submodules under a package.

    Parameters
    ----------
    src_dir : Path
        Path to the src/ directory containing the package.
    package_name : str
        Top-level package name.

    Returns
    -------
    list[str]
        Sorted list of dotted module paths.
    """
    pkg_dir = src_dir / package_name
    if not pkg_dir.exists():
        return []

    modules: list[str] = []
    for py_file in sorted(pkg_dir.rglob("*.py")):
        if "__pycache__" in str(py_file) or py_file.name.startswith("_"):
            continue
        rel = py_file.relative_to(src_dir)
        module_path = str(rel.with_suffix("")).replace("/", ".")
        modules.append(module_path)

    return modules
