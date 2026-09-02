"""AST parsing helpers shared across maintenance tools.

Provides utilities for extracting information from Python ASTs without
importing modules (pure static analysis). Used by:
- trim_overdocumented.py (docstring analysis)
- generate_module_index.py (symbol extraction)

Usage:
    from core.ast_utils import extract_public_symbols, count_docstring_lines

    tree = ast.parse(source)
    symbols = extract_public_symbols(tree)
    # symbols.classes = ["MyClass", ...]
    # symbols.functions = ["my_func", ...]
    # symbols.constants = ["MY_CONST", ...]
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ModuleSymbols:
    """Public symbols extracted from a module's AST."""

    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    constants: list[str] = field(default_factory=list)
    docstring: str = ""

    @property
    def total(self) -> int:
        return len(self.classes) + len(self.functions) + len(self.constants)

    @property
    def is_empty(self) -> bool:
        return self.total == 0


def extract_public_symbols(tree: ast.Module) -> ModuleSymbols:
    """Extract public classes, functions, and constants from an AST.

    Only considers top-level definitions. Skips private names (leading _).
    Constants are identified as UPPER_CASE assignments at module level.

    Parameters
    ----------
    tree : ast.Module
        Parsed AST module.

    Returns
    -------
    ModuleSymbols
        Extracted public symbols.
    """
    symbols = ModuleSymbols()

    # Module docstring
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        doc = tree.body[0].value.value.strip()
        # First line only
        symbols.docstring = doc.split("\n")[0][:120]

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                symbols.classes.append(node.name)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                symbols.functions.append(node.name)

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    name = target.id
                    # UPPER_CASE = constant
                    if name.isupper() and not name.startswith("_"):
                        symbols.constants.append(name)

        elif isinstance(node, (ast.AnnAssign,)):
            if isinstance(node.target, ast.Name):
                name = node.target.id
                if name.isupper() and not name.startswith("_"):
                    symbols.constants.append(name)

    return symbols


def count_docstring_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count lines in a function/method's docstring.

    Returns 0 if no docstring is present.
    """
    if not node.body:
        return 0
    first_stmt = node.body[0]
    if (
        isinstance(first_stmt, ast.Expr)
        and isinstance(first_stmt.value, ast.Constant)
        and isinstance(first_stmt.value.value, str)
    ):
        return first_stmt.value.value.count("\n") + 1
    return 0


def get_function_body_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count non-docstring body lines of a function.

    Excludes the docstring statement, empty lines are counted.
    Returns 0 for abstract functions (body is only `pass` or `...`).
    """
    body = node.body
    if not body:
        return 0

    # Skip docstring if present
    start_idx = 0
    if (
        isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        start_idx = 1

    real_body = body[start_idx:]
    if not real_body:
        return 0

    # Check for abstract pattern (single pass/Ellipsis)
    if len(real_body) == 1:
        stmt = real_body[0]
        if isinstance(stmt, ast.Pass):
            return 0
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            if stmt.value.value is ...:
                return 0

    # Count lines from first real statement to last
    first_line = real_body[0].lineno
    last_line = real_body[-1].end_lineno or real_body[-1].lineno
    return last_line - first_line + 1


def is_abstract_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function is abstract (body is only docstring + pass/...).

    Also returns True for empty-body hooks.
    """
    return get_function_body_lines(node) == 0


def get_docstring_range(
    source_lines: list[str], node: ast.FunctionDef | ast.AsyncFunctionDef
) -> tuple[int, int, str, str] | None:
    """Find the exact line range and content of a function's docstring.

    Parameters
    ----------
    source_lines : list[str]
        The source file split by newlines.
    node : ast.FunctionDef | ast.AsyncFunctionDef
        The function node.

    Returns
    -------
    tuple[int, int, str, str] | None
        (start_line_idx, end_line_idx, docstring_text, quote_style)
        Returns None if no docstring found.
    """
    if not node.body:
        return None
    first_stmt = node.body[0]
    if not (
        isinstance(first_stmt, ast.Expr)
        and isinstance(first_stmt.value, ast.Constant)
        and isinstance(first_stmt.value.value, str)
    ):
        return None

    # Find the quote style by scanning source
    start_line = first_stmt.lineno - 1  # 0-indexed
    end_line = (first_stmt.end_lineno or first_stmt.lineno) - 1

    # Detect quote style from first line
    line_content = source_lines[start_line].lstrip()
    if line_content.startswith('r"""') or line_content.startswith('r"'):
        quote = 'r"""' if '"""' in line_content else "r'''"
    elif line_content.startswith('"""'):
        quote = '"""'
    elif line_content.startswith("'''"):
        quote = "'''"
    elif line_content.startswith('"'):
        quote = '"'
    elif line_content.startswith("'"):
        quote = "'"
    else:
        quote = '"""'

    doc_text = "\n".join(source_lines[start_line : end_line + 1])
    return (start_line, end_line, doc_text, quote)


def parse_file_safe(filepath: Path) -> ast.Module | None:
    """Parse a Python file, returning None on any error.

    Handles UnicodeDecodeError, PermissionError, and SyntaxError gracefully.
    """
    try:
        source = filepath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError, OSError):
        return None

    try:
        return ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return None
