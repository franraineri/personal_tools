"""Core shared utilities for general project maintenance tools.

This package provides reusable building blocks used across multiple
maintenance scripts (phantom-check, module-index, verify-steerings, etc.),
avoiding code duplication and ensuring consistent behavior.

Modules:
    ast_utils       — AST parsing helpers (docstring extraction, symbol enumeration)
    import_analysis — Import statement parsing and resolution
    frontmatter     — YAML front-matter parsing for markdown files
    report          — Structured reporting (Issue, Report, SARIF/JSON formatters)
    config          — Configuration loading from TOML/dict sources
"""

from core.ast_utils import (
    count_docstring_lines,
    extract_public_symbols,
    get_function_body_lines,
    is_abstract_function,
)
from core.frontmatter import get_body, parse_front_matter
from core.import_analysis import (
    build_import_index,
    extract_names_from_import,
    resolve_phantom_imports,
)
from core.report import Issue, Report, Severity

__all__ = [
    "count_docstring_lines",
    "extract_public_symbols",
    "get_function_body_lines",
    "is_abstract_function",
    "get_body",
    "parse_front_matter",
    "build_import_index",
    "extract_names_from_import",
    "resolve_phantom_imports",
    "Issue",
    "Report",
    "Severity",
]
