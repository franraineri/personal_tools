#!/usr/bin/env python3
# TODO move mainteinance tools to a new project repository in order to reuse them. Also a refactor and modular parameters will be needed

"""Generate a compact module index for .kiro/steering/module-index.md.

Introspects src/, scripts/, project_health/, experiments/ and extracts:
- Public classes and functions from each .py module
- One-line docstring summary
- Key exports per sub-package

Output: ultra-compact markdown optimized for LLM context (min tokens, max info).

Usage:
   .venv/bin/python scripts/general_project_maintenance/generate_module_index.py
   .venv/bin/python scripts/general_project_maintenance/generate_module_index.py --output .kiro/steering/module-index.md
   .venv/bin/python scripts/general_project_maintenance/generate_module_index.py --dry-run
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / ".kiro" / "steering" / "module-index.md"

SCAN_DIRS = [
    ("src/qmbp_simulation", "LIB"),
    ("scripts/analysis", "SCRIPT"),
    ("scripts/benchmarks", "SCRIPT"),
    ("scripts/experiment_runners", "RUNNER"),
    ("scripts/hardware", "SCRIPT"),
    ("scripts/maintenance", "MAINT"),
    ("scripts/validation", "SCRIPT"),
    ("project_health", "HEALTH"),
    ("experiments", "EXP"),
    ("notebooks", "NB"),
]

# Skip these file patterns
SKIP_FILES = {"__pycache__", ".DS_Store", "__init__.py"}
SKIP_DIRS = {"__pycache__", ".hypothesis", ".mypy_cache", ".ruff_cache", "data"}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ModuleEntry:
    """One .py module's extracted metadata."""

    rel_path: str  # relative to project root
    category: str  # LIB, SCRIPT, RUNNER, EXP, HEALTH, MAINT, NB
    docstring: str  # first line of module docstring
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    constants: list[str] = field(default_factory=list)


@dataclass
class PackageEntry:
    """A sub-package (directory) summary."""

    rel_path: str
    category: str
    docstring: str  # from __init__.py
    exports: list[str] = field(default_factory=list)  # __all__ from __init__
    modules: list[ModuleEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AST extraction
# ---------------------------------------------------------------------------


def _first_line_docstring(node: ast.Module | ast.ClassDef | ast.FunctionDef) -> str:
    """Extract first line of a node's docstring, or empty string."""
    doc = ast.get_docstring(node)
    if not doc:
        return ""
    return doc.split("\n")[0].strip()[:120]


def _is_public(name: str) -> bool:
    """Check if a name is public (no leading underscore)."""
    return not name.startswith("_")


def _extract_all_list(tree: ast.Module) -> list[str]:
    """Extract __all__ = [...] from module AST."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, ast.List):
                        return [
                            elt.value
                            for elt in node.value.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        ]
    return []


def _extract_constants(tree: ast.Module) -> list[str]:
    """Extract UPPER_CASE module-level constants."""
    constants = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper() and _is_public(target.id):
                    constants.append(target.id)
    return constants[:10]  # cap to avoid noise


def extract_module(filepath: Path, category: str) -> ModuleEntry:
    """Parse a .py file and extract public API metadata."""
    rel = filepath.relative_to(PROJECT_ROOT).as_posix()
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (SyntaxError, OSError):
        return ModuleEntry(rel_path=rel, category=category, docstring="[parse error]")

    docstring = _first_line_docstring(tree)
    classes = []
    functions = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and _is_public(node.name):
            classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_public(node.name):
            functions.append(node.name)

    constants = _extract_constants(tree)

    return ModuleEntry(
        rel_path=rel,
        category=category,
        docstring=docstring,
        classes=classes,
        functions=functions,
        constants=constants,
    )


def extract_package(dirpath: Path, category: str) -> PackageEntry:
    """Extract summary from a package's __init__.py."""
    rel = dirpath.relative_to(PROJECT_ROOT).as_posix()
    init_file = dirpath / "__init__.py"
    docstring = ""
    exports: list[str] = []

    if init_file.exists():
        try:
            source = init_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
            docstring = _first_line_docstring(tree)
            exports = _extract_all_list(tree)
        except (SyntaxError, OSError):
            pass

    return PackageEntry(
        rel_path=rel,
        category=category,
        docstring=docstring,
        exports=exports,
    )


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def scan_directory(rel_dir: str, category: str) -> tuple[list[PackageEntry], list[ModuleEntry]]:
    """Scan a directory tree and extract packages + standalone modules."""
    base = PROJECT_ROOT / rel_dir
    if not base.exists():
        return [], []

    packages: list[PackageEntry] = []
    standalone_modules: list[ModuleEntry] = []

    # Check if base itself is a package
    if (base / "__init__.py").exists():
        pkg = extract_package(base, category)
        # Scan direct .py files (not __init__)
        for f in sorted(base.glob("*.py")):
            if f.name in SKIP_FILES:
                continue
            pkg.modules.append(extract_module(f, category))
        packages.append(pkg)

    # Scan sub-directories
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name in SKIP_DIRS:
            continue
        if (child / "__init__.py").exists():
            sub_pkg = extract_package(child, category)
            for f in sorted(child.glob("*.py")):
                if f.name in SKIP_FILES:
                    continue
                sub_pkg.modules.append(extract_module(f, category))
            # One level deeper for nested packages (e.g. execution/hardware)
            for grandchild in sorted(child.iterdir()):
                if not grandchild.is_dir() or grandchild.name in SKIP_DIRS:
                    continue
                if (grandchild / "__init__.py").exists():
                    deep_pkg = extract_package(grandchild, category)
                    for f in sorted(grandchild.glob("*.py")):
                        if f.name in SKIP_FILES:
                            continue
                        deep_pkg.modules.append(extract_module(f, category))
                    packages.append(deep_pkg)
            packages.append(sub_pkg)
        else:
            # Plain directory with scripts (no __init__)
            for f in sorted(child.glob("*.py")):
                if f.name in SKIP_FILES:
                    continue
                standalone_modules.append(extract_module(f, category))
            # Check deeper dirs too
            for grandchild in sorted(child.iterdir()):
                if not grandchild.is_dir() or grandchild.name in SKIP_DIRS:
                    continue
                for f in sorted(grandchild.glob("*.py")):
                    if f.name in SKIP_FILES:
                        continue
                    standalone_modules.append(extract_module(f, category))

    # Top-level .py files not in a package
    for f in sorted(base.glob("*.py")):
        if f.name in SKIP_FILES:
            continue
        if not (base / "__init__.py").exists():
            standalone_modules.append(extract_module(f, category))

    return packages, standalone_modules


# ---------------------------------------------------------------------------
# Formatter — ultra-compact markdown
# ---------------------------------------------------------------------------


def _short_path(path: str) -> str:
    """Abbreviate common prefixes for compactness."""
    return (
        path.replace("src/qmbp_simulation/", "qsim/")
        .replace("project_health/", "ph/")
        .replace("scripts/", "s/")
        .replace("experiments/", "exp/")
        .replace("notebooks/", "nb/")
    )


def _module_name(rel_path: str) -> str:
    """Extract just the module name without path or extension."""
    from pathlib import PurePosixPath

    return PurePosixPath(rel_path).stem


def _infer_tag(m: ModuleEntry) -> str:
    """Infer a 3-4 char category tag from module name/docstring."""
    name = _module_name(m.rel_path).lower()
    doc = (m.docstring or "").lower()
    # Priority-ordered matching (first match wins)
    tag_rules = [
        ("test", "TEST"),
        ("valid", "VAL"),
        ("audit", "VAL"),
        ("bench", "BENCH"),
        ("cache", "CACHE"),
        ("persist", "IO"),
        ("save", "IO"),
        ("load", "IO"),
        ("serial", "IO"),
        ("visual", "VIS"),
        ("figure", "VIS"),
        ("plot", "VIS"),
        ("cli", "CLI"),
        ("__main__", "CLI"),
        ("config", "CFG"),
        ("preset", "CFG"),
        ("constant", "CFG"),
        ("optim", "OPT"),
        ("spsa", "OPT"),
        ("sweep", "OPT"),
        ("predict", "PRED"),
        ("mpnn", "PRED"),
        ("gnn", "PRED"),
        ("train", "PRED"),
        ("unified", "PRED"),
        ("model_spec", "MODEL"),
        ("hamiltonian", "MODEL"),
        ("registry", "MODEL"),
        ("data_model", "MODEL"),
        ("circuit", "CIRC"),
        ("hva", "CIRC"),
        ("aqc", "CIRC"),
        ("backend", "EXEC"),
        ("noisy", "EXEC"),
        ("mps_backend", "EXEC"),
        ("solver", "SOLVE"),
        ("dmrg", "SOLVE"),
        ("classical", "SOLVE"),
        ("metric", "CORE"),
        ("util", "CORE"),
        ("helper", "CORE"),
        ("diagnos", "DIAG"),
        ("log", "DIAG"),
        ("pipeline", "PIPE"),
        ("runner", "PIPE"),
        ("accelerat", "PIPE"),
        ("analy", "ANAL"),
        ("compar", "ANAL"),
        ("report", "ANAL"),
        ("scale", "SCALE"),
        ("mps", "SCALE"),
        ("align", "POST"),
        ("filter", "POST"),
        ("guard", "POST"),
        ("dataset", "IO"),
        ("result_io", "IO"),
        ("result_store", "IO"),
        ("vqe", "OPT"),
    ]
    for keyword, tag in tag_rules:
        if keyword in name or (keyword in doc and keyword not in ("train",)):
            return tag
    return ""


def _exports_compact(exports: list[str], max_show: int = 8) -> str:
    """Format exports as space-separated list (more compact than comma)."""
    if not exports:
        return ""
    shown = exports[:max_show]
    suffix = f" +{len(exports) - max_show}" if len(exports) > max_show else ""
    return " ".join(shown) + suffix


def _module_line_v2(m: ModuleEntry) -> str:
    """Ultra-compact module line: name TAG symbols."""
    name = _module_name(m.rel_path)
    tag = _infer_tag(m)

    # Build symbols (max 3 each for discovery — agent can read_code for full API)
    symbols = []
    if m.classes:
        n = len(m.classes)
        shown = m.classes[:3]
        extra = f"+{n - 3}" if n > 3 else ""
        symbols.append(f"C:{','.join(shown)}{extra}")
    if m.functions:
        n = len(m.functions)
        shown = m.functions[:3]
        extra = f"+{n - 3}" if n > 3 else ""
        symbols.append(f"F:{','.join(shown)}{extra}")
    if m.constants and not m.classes and not m.functions:
        symbols.append(f"K:{','.join(m.constants[:2])}")

    tag_str = f"{tag:5s}" if tag else "     "
    sym_str = " | ".join(symbols) if symbols else ""
    return f"  {name:<26s} {tag_str} {sym_str}"


def _module_line(m: ModuleEntry) -> str:
    """One compact line per module: path | doc | C:classes F:funcs.

    Legacy format (used as fallback). See _module_line_v2 for compact format.
    """
    parts = [f"`{_short_path(m.rel_path)}`"]
    if m.docstring:
        parts.append(m.docstring[:80])
    symbols = []
    if m.classes:
        symbols.append(f"C:{','.join(m.classes[:5])}")
        if len(m.classes) > 5:
            symbols[-1] += f"+{len(m.classes) - 5}"
    if m.functions:
        symbols.append(f"F:{','.join(m.functions[:6])}")
        if len(m.functions) > 6:
            symbols[-1] += f"+{len(m.functions) - 6}"
    if m.constants:
        symbols.append(f"K:{','.join(m.constants[:4])}")
    if symbols:
        parts.append(" | ".join(symbols))
    return " — ".join(parts)


def _import_path(rel_path: str) -> str:
    """Convert a relative file path to a Python import path.

    Examples:
        src/qmbp_simulation/analysis/metrics.py → qmbp_simulation.analysis.metrics
        project_health/analysis/diagnose.py → project_health.analysis.diagnose
    """
    p = rel_path.replace("src/", "").replace("/", ".").removesuffix(".py")
    return p


def _build_quick_lookup(
    all_packages: list[PackageEntry], all_modules: list[ModuleEntry]
) -> list[str]:
    """Generate Quick Lookup: intent → import.module → key symbol.

    Compact table that resolves the most common agent questions in one scan.
    """
    CURATED_INTENTS = [
        ("Build Hamiltonian", "models/hamiltonian", "HamiltonianBuilder, make_lattice"),
        ("HVA circuit", "circuits/hva", "HVACircuitBuilder"),
        ("VQE optimize", "optimizers/vqe", "VQEOptimizer"),
        ("Noiseless eval", "execution/backends", "NoiselessBackend, select_backend"),
        ("MPS eval (N>22)", "execution/mps_backend", "MPSBackend"),
        ("Cache evals", "execution/eval_cache", "CachedBackend, EvalCache"),
        ("Ground truth", "solvers/classical", "ClassicalSolver"),
        ("GT cache (disk)", "solvers/ground_truth_cache", "GroundTruthCache"),
        ("Train MPNN", "predictors/mpnn", "MPNNPredictor, train_mpnn"),
        ("UnifiedMPNN (cross-N)", "predictors/unified_mpnn", "UnifiedMPNN"),
        ("Model zoo", "predictors/model_zoo", "load_pretrained, register_checkpoint"),
        ("Accelerated pipeline", "pipeline/accelerated", "AcceleratedVQE"),
        ("Full pipeline", "pipeline/runner", "PipelineRunner"),
        ("Validate θ", "analysis/theta_validator", "ThetaValidator"),
        ("Deploy stats", "analysis/metrics", "compute_deploy_summary"),
        ("θ alignment", "analysis/theta_alignment", "align_theta_array"),
        ("Noisy ZNE", "execution/noisy_utils", "run_pea_zne, run_gate_folding_zne"),
        ("Hardware QPU", "execution/hardware/backend", "HardwareBackend"),
        ("Runner base", "framework/runner_base", "ValidationRunner, Section"),
        ("Result I/O", "framework/result_io", "save_experiment_result"),
        ("CLI args", "framework/cli", "create_base_parser"),
        ("Quality predict", "analysis/quality_predictor", "QualityPredictor"),
        ("JSON serialize", "utils/helpers", "json_serialize, json_dump"),
        ("Canonicalize θ", "utils/helpers", "canonicalize_theta"),
        ("Model spec", "models/model_registry", "get_model_spec"),
    ]

    lines = ["## Quick Lookup", "", "| Need | Module | Symbols |", "|---|---|---|"]

    all_paths = {m.rel_path: m for pkg in all_packages for m in pkg.modules}
    all_paths.update({m.rel_path: m for m in all_modules})

    for intent, path_fragment, symbols in CURATED_INTENTS:
        matched_path = next((rp for rp in all_paths if path_fragment in rp), None)
        if matched_path:
            imp = _import_path(matched_path)
            # Shorten the import: remove qmbp_simulation prefix (implied)
            short_imp = imp.replace("qmbp_simulation.", "")
            lines.append(f"| {intent} | {short_imp} | {symbols} |")

    lines.append("")
    return lines


def format_index(
    all_packages: list[PackageEntry],
    all_modules: list[ModuleEntry],
    compact: bool = True,
) -> str:
    """Generate the module-index.md content.

    Parameters
    ----------
    compact : bool
        If True, use v2 ultra-compact format (tags, no backticks, aligned).
        If False, use legacy verbose format.
    """
    lines: list[str] = []
    lines.append("# Module Index (auto-generated)")
    lines.append("")
    if compact:
        lines.append(
            "Legend: C=class F=func K=const. Base import: `from qmbp_simulation.<module> import ...`"
        )
        lines.append("Run `python scripts/general_project_maintenance/generate_module_index.py` to refresh.")
        lines.append("")
        lines.extend(_build_quick_lookup(all_packages, all_modules))
    else:
        lines.append("Compact catalog of all code modules. Use to find reusable functionality.")
        lines.append("Run `python scripts/general_project_maintenance/generate_module_index.py` to refresh.")
    lines.append("")

    # Group packages by category
    cat_order = ["LIB", "HEALTH", "EXP", "RUNNER", "SCRIPT", "MAINT", "NB"]
    cat_labels = {
        "LIB": "Library (src/qmbp_simulation)",
        "HEALTH": "Project Health (project_health/)",
        "EXP": "Experiments (experiments/)",
        "RUNNER": "Runners (scripts/experiment_runners/)",
        "SCRIPT": "Scripts (scripts/)",
        "MAINT": "Maintenance (scripts/general_project_maintenance/)",
        "NB": "Notebooks",
    }

    for cat in cat_order:
        pkgs = [p for p in all_packages if p.category == cat]
        mods = [m for m in all_modules if m.category == cat]
        if not pkgs and not mods:
            continue

        lines.append(f"## {cat_labels.get(cat, cat)}")
        lines.append("")

        for pkg in pkgs:
            sp = _short_path(pkg.rel_path)
            n_mods = len(pkg.modules)
            if compact:
                lines.append(f"### {sp}/ ({n_mods})")
                if pkg.exports:
                    lines.append(f"  ↳ {_exports_compact(pkg.exports)}")
            else:
                doc_short = f" — {pkg.docstring[:60]}" if pkg.docstring else ""
                lines.append(f"### `{sp}/`{doc_short}")
                if pkg.exports:
                    lines.append(f"Exports: {_exports_compact(pkg.exports)}")
            lines.append("")
            for m in pkg.modules:
                if compact:
                    lines.append(_module_line_v2(m))
                else:
                    lines.append(f"- {_module_line(m)}")
            lines.append("")

        if mods:
            lines.append("### Standalone scripts")
            lines.append("")
            for m in mods:
                if compact:
                    lines.append(_module_line_v2(m))
                else:
                    lines.append(f"- {_module_line(m)}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _verify_importability(all_packages: list[PackageEntry]) -> tuple[int, int]:
    """Verify that AST-parsed symbols in src/ are actually importable.

    Only checks packages under src/qmbp_simulation (LIB category).
    Uses importlib to attempt actual imports and reports phantoms.

    Returns (n_checked, n_phantom).
    """
    import importlib

    # Suppress argparse side effects from framework.__main__ modules
    original_argv = sys.argv
    sys.argv = ["verify"]

    n_checked = 0
    n_phantom = 0

    for pkg in all_packages:
        if pkg.category != "LIB":
            continue

        for mod_entry in pkg.modules:
            # Convert rel_path to import path
            import_path = _import_path(mod_entry.rel_path)
            if not import_path:
                continue
            # Skip __main__ modules (they run argparse on import)
            if "__main__" in import_path:
                continue

            # Try to import the module
            try:
                mod = importlib.import_module(import_path)
            except Exception:
                # Module itself can't be imported — skip (separate issue)
                continue

            # Check functions
            for func_name in mod_entry.functions:
                n_checked += 1
                if not hasattr(mod, func_name):
                    n_phantom += 1
                    print(
                        f"  ⚠️  PHANTOM: {import_path}.{func_name} (in AST but not importable)",
                        file=sys.stderr,
                    )

            # Check classes
            for cls_name in mod_entry.classes:
                n_checked += 1
                if not hasattr(mod, cls_name):
                    n_phantom += 1
                    print(
                        f"  ⚠️  PHANTOM: {import_path}.{cls_name} (in AST but not importable)",
                        file=sys.stderr,
                    )

    sys.argv = original_argv
    return n_checked, n_phantom


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate compact module index for .kiro/steering/module-index.md",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output file (default: {DEFAULT_OUTPUT.relative_to(PROJECT_ROOT)})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print to stdout instead of writing file",
    )
    parser.add_argument(
        "--format",
        choices=["compact", "verbose"],
        default="compact",
        help="Output format: 'compact' (v2, tags+aligned) or 'verbose' (legacy). Default: compact",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="After generation, verify that key library symbols are importable. "
        "Prints warnings for phantom entries (AST-parsed but not importable).",
    )
    args = parser.parse_args()

    all_packages: list[PackageEntry] = []
    all_modules: list[ModuleEntry] = []

    for rel_dir, category in SCAN_DIRS:
        pkgs, mods = scan_directory(rel_dir, category)
        all_packages.extend(pkgs)
        all_modules.extend(mods)

    compact = args.format == "compact"
    result = format_index(all_packages, all_modules, compact=compact)

    if args.dry_run:
        print(result)
        print(
            f"\n--- {len(all_packages)} packages, {len(all_modules)} standalone modules ---",
            file=sys.stderr,
        )
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result, encoding="utf-8")
        total = len(all_packages) + len(all_modules)
        n_lines = result.count("\n") + 1
        n_chars = len(result)
        print(f"✓ Module index written to {args.output.relative_to(PROJECT_ROOT)}")
        print(
            f"  {len(all_packages)} packages, {len(all_modules)} standalone, {total} total entries"
        )
        print(f"  Format: {args.format} ({n_lines} lines, {n_chars:,} chars)")

    # ── Verification pass: check that library symbols are importable ──────
    if args.verify:
        n_checked, n_phantom = _verify_importability(all_packages)
        if n_phantom > 0:
            print(
                f"\n⚠️  VERIFICATION: {n_phantom} phantom symbols detected "
                f"(AST-parsed but not importable). See warnings above.",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            print(f"✓ Verification passed: {n_checked} symbols checked, all importable.")


if __name__ == "__main__":
    main()
