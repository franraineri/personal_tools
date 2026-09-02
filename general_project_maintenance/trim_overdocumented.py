#!/usr/bin/env python3
# TODO move mainteinance tools to a new project repository in order to reuse them. Also a refactor and modular parameters will be needed

"""Trim docstrings en funciones over-documented (ratio docstring/body ≥ 3x).

Estrategia MODERADA:
- Mantiene la summary line (primera línea del docstring)
- Elimina secciones Parameters, Returns, Raises, Yields, Attributes, Notes
- Preserva secciones Example/Examples (son valiosas)
- Nunca toca funciones abstractas (body es solo `...` o `pass`)
- Nunca toca funciones con body == 0 líneas (hooks vacíos)
- Requiere --apply para escribir cambios (dry-run por defecto)

Uso:
    # Dry-run (muestra qué haría):
   .venv/bin/python scripts/general_project_maintenance/trim_overdocumented.py [ruta]

    # Aplicar cambios:


    # Cambiar ratio mínimo:
   .venv/bin/python scripts/general_project_maintenance/trim_overdocumented.py [ruta] --min-ratio 5
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

# Secciones a eliminar (NumPy docstring style)
TRIM_SECTIONS = {
    "parameters",
    "params",
    "args",
    "arguments",
    "returns",
    "return",
    "raises",
    "yields",
    "attributes",
    "notes",
    "note",
    "see also",
    "references",
    "warnings",
    "warns",
}

# Secciones a preservar
KEEP_SECTIONS = {
    "example",
    "examples",
    "todo",
}

# Directorios a ignorar
SKIP_DIRS = {
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
    "__pycache__",
    ".git",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    "egg-info",
    "_deprecated",
}


def is_abstract_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Detecta si el cuerpo es solo abstracto (pass, ..., raise NotImplementedError)."""
    has_docstring = (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    )
    body_stmts = node.body[1:] if has_docstring else node.body

    if not body_stmts:
        return True

    # Solo un statement
    if len(body_stmts) == 1:
        stmt = body_stmts[0]
        # pass
        if isinstance(stmt, ast.Pass):
            return True
        # ... (Ellipsis)
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            if stmt.value.value is ...:
                return True
        # raise NotImplementedError
        if isinstance(stmt, ast.Raise):
            return True
        # Solo un comentario (no detectable via AST, pero body_lines==0 lo cubre)

    return False


def count_docstring_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Cuenta las líneas del docstring."""
    if not node.body:
        return 0
    first = node.body[0]
    if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)):
        return 0
    return first.end_lineno - first.lineno + 1


def count_body_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Cuenta líneas de implementación (sin docstring)."""
    has_docstring = (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    )
    body_nodes = node.body[1:] if has_docstring else node.body

    if not body_nodes:
        return 0

    first_line = body_nodes[0].lineno
    last_line = max(n.end_lineno for n in body_nodes)
    return last_line - first_line + 1


def trim_docstring(raw_docstring: str) -> str:
    """Trim un docstring manteniendo summary y Examples, removiendo Parameters/Returns/etc.

    Returns el nuevo docstring (sin quotes) o None si no hay cambio.
    """
    lines = raw_docstring.split("\n")

    # Encontrar summary line(s) — todo antes de la primera sección o línea vacía post-summary
    summary_lines = []
    rest_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if i == 0:
            summary_lines.append(line)
            if stripped:  # Non-empty first line
                continue
            else:
                rest_start = 1
                break
        elif stripped == "":
            # Blank line after summary — end of summary
            rest_start = i
            break
        elif _is_section_header(stripped, lines, i):
            # Section header directly after first line
            rest_start = i
            break
        else:
            # Multi-line summary
            summary_lines.append(line)
            continue

    if rest_start == 0:
        rest_start = len(summary_lines)

    # Parsear el resto en secciones
    rest_lines = lines[rest_start:]
    sections = _parse_sections(rest_lines)

    # Filtrar: mantener solo las secciones en KEEP_SECTIONS
    kept_sections = []
    for section_name, section_lines in sections:
        if section_name is None:
            # Texto libre entre summary y primera sección — mantener si corto
            non_empty = [l for l in section_lines if l.strip()]
            if len(non_empty) <= 2:
                kept_sections.append((section_name, section_lines))
        elif section_name.lower() in KEEP_SECTIONS:
            kept_sections.append((section_name, section_lines))

    # Reconstruir
    result_lines = list(summary_lines)

    if kept_sections:
        # Asegurar blank line entre summary y secciones preservadas
        if result_lines and result_lines[-1].strip():
            result_lines.append("")
        for _, sec_lines in kept_sections:
            result_lines.extend(sec_lines)

    # Limpiar trailing whitespace/blank lines
    while result_lines and not result_lines[-1].strip():
        result_lines.pop()

    new_doc = "\n".join(result_lines)
    return new_doc


def _is_section_header(stripped: str, lines: list[str], idx: int) -> bool:
    """Detecta si una línea es header de sección NumPy-style (seguida de --- o terminada en :)."""
    # NumPy style: "Parameters\n----------"
    if idx + 1 < len(lines):
        next_stripped = lines[idx + 1].strip()
        if next_stripped and all(c == "-" for c in next_stripped):
            return True

    # Google style: "Args:" or "Returns:"
    if re.match(r"^[A-Za-z ]+:$", stripped):
        word = stripped.rstrip(":").strip().lower()
        return word in TRIM_SECTIONS or word in KEEP_SECTIONS

    return False


def _parse_sections(lines: list[str]) -> list[tuple[str | None, list[str]]]:
    """Parsea líneas en secciones [(nombre, líneas), ...]."""
    sections: list[tuple[str | None, list[str]]] = []
    current_name: str | None = None
    current_lines: list[str] = []

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Check if this is a section header
        is_header = False
        header_name = None

        # NumPy style: word\n----
        if stripped and i + 1 < len(lines):
            next_stripped = lines[i + 1].strip()
            if next_stripped and all(c == "-" for c in next_stripped):
                is_header = True
                header_name = stripped.lower()
                # Guardar sección actual
                if current_lines or current_name is not None:
                    sections.append((current_name, current_lines))
                current_name = header_name
                current_lines = [lines[i], lines[i + 1]]
                i += 2
                continue

        # Google style: "Args:" at start
        if re.match(r"^[A-Za-z ]+:$", stripped):
            word = stripped.rstrip(":").strip().lower()
            if word in TRIM_SECTIONS or word in KEEP_SECTIONS:
                is_header = True
                header_name = word
                if current_lines or current_name is not None:
                    sections.append((current_name, current_lines))
                current_name = header_name
                current_lines = [lines[i]]
                i += 1
                continue

        current_lines.append(lines[i])
        i += 1

    # Última sección
    if current_lines or current_name is not None:
        sections.append((current_name, current_lines))

    return sections


def get_docstring_range(
    source_lines: list[str], node: ast.FunctionDef
) -> tuple[int, int, str, str]:
    """Extrae el rango del docstring en source_lines (0-indexed) y el quote style.

    Returns: (start_idx, end_idx, raw_content, quote_char)
    """
    first_stmt = node.body[0]
    # lineno is 1-indexed
    start_idx = first_stmt.lineno - 1
    end_idx = first_stmt.end_lineno - 1

    # Extraer las líneas del docstring completo
    doc_lines = source_lines[start_idx : end_idx + 1]
    doc_text = "\n".join(doc_lines)

    # Detectar quote style
    stripped_first = doc_lines[0].lstrip()
    if stripped_first.startswith('"""'):
        quote = '"""'
    elif stripped_first.startswith("'''"):
        quote = "'''"
    elif stripped_first.startswith('r"""'):
        quote = 'r"""'
    elif stripped_first.startswith("r'''"):
        quote = "r'''"
    else:
        quote = '"""'

    return start_idx, end_idx, doc_text, quote


def extract_raw_docstring(doc_text: str, quote: str) -> str:
    """Extrae el contenido entre quotes del docstring."""
    # Encontrar apertura
    open_quote = quote
    close_quote = quote.lstrip("r")

    idx_start = doc_text.find(open_quote)
    if idx_start == -1:
        return ""
    content_start = idx_start + len(open_quote)

    idx_end = doc_text.rfind(close_quote, content_start)
    if idx_end == -1:
        return doc_text[content_start:]

    return doc_text[content_start:idx_end]


def rebuild_docstring(raw_content: str, indent: str, quote: str) -> list[str]:
    """Reconstruye las líneas del docstring con indentación correcta."""
    close_quote = quote.lstrip("r")
    content_lines = raw_content.split("\n")

    if len(content_lines) == 1:
        # Single-line docstring
        return [f"{indent}{quote}{content_lines[0]}{close_quote}"]
    else:
        # Multi-line
        result = [f"{indent}{quote}{content_lines[0]}"]
        for line in content_lines[1:]:
            if line.strip():
                result.append(f"{indent}{line.lstrip()}" if not line.startswith(indent) else line)
            else:
                result.append("")
        # Cerrar en nueva línea
        if result[-1].strip():
            result.append(f"{indent}{close_quote}")
        else:
            result[-1] = f"{indent}{close_quote}"
        return result


def process_file(filepath: Path, min_ratio: float, apply: bool) -> list[dict]:
    """Procesa un archivo, retorna lista de trimmed functions."""
    try:
        source = filepath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return []

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    source_lines = source.split("\n")
    candidates = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        doc_lines_count = count_docstring_lines(node)
        if doc_lines_count == 0:
            continue

        body_lines_count = count_body_lines(node)

        # Skip abstract/empty bodies
        if body_lines_count == 0 or is_abstract_body(node):
            continue

        ratio = doc_lines_count / max(body_lines_count, 1)
        if ratio < min_ratio:
            continue

        candidates.append(
            {
                "node": node,
                "doc_lines": doc_lines_count,
                "body_lines": body_lines_count,
                "ratio": ratio,
            }
        )

    if not candidates:
        return []

    # Ordenar por línea descendente (para reemplazar de abajo hacia arriba)
    candidates.sort(key=lambda c: c["node"].lineno, reverse=True)

    results = []
    modified = False

    for cand in candidates:
        node = cand["node"]
        start_idx, end_idx, doc_text, quote = get_docstring_range(source_lines, node)

        # Extraer contenido raw
        raw = extract_raw_docstring(doc_text, quote)
        if not raw.strip():
            continue

        # Trim
        trimmed = trim_docstring(raw)

        # Si no cambió, skip
        if trimmed.strip() == raw.strip():
            continue

        # Calcular indentación
        func_line = source_lines[node.lineno - 1]
        func_indent = len(func_line) - len(func_line.lstrip())
        body_indent = " " * (func_indent + 4)

        # Reconstruir
        close_quote = quote.lstrip("r")
        trimmed_lines_content = trimmed.split("\n")

        if len(trimmed_lines_content) == 1 and len(trimmed_lines_content[0]) < 72:
            # Single-line
            new_lines = [f"{body_indent}{quote}{trimmed_lines_content[0]}{close_quote}"]
        else:
            new_lines = [f"{body_indent}{quote}{trimmed_lines_content[0]}"]
            for tl in trimmed_lines_content[1:]:
                if tl.strip():
                    # Preservar indentación relativa
                    new_lines.append(f"{body_indent}{tl.strip()}")
                else:
                    new_lines.append("")
            new_lines.append(f"{body_indent}{close_quote}")

        # Contar nuevo tamaño
        new_doc_lines = len(new_lines)
        saved = cand["doc_lines"] - new_doc_lines

        # Skip si no ahorra al menos 1 línea
        if saved <= 0:
            continue

        results.append(
            {
                "file": str(filepath),
                "line": node.lineno,
                "function": node.name,
                "old_doc_lines": cand["doc_lines"],
                "new_doc_lines": new_doc_lines,
                "body_lines": cand["body_lines"],
                "saved": saved,
                "old_ratio": round(cand["ratio"], 1),
                "new_ratio": round(new_doc_lines / max(cand["body_lines"], 1), 1),
            }
        )

        if apply:
            # Reemplazar en source_lines
            source_lines[start_idx : end_idx + 1] = new_lines
            modified = True

    if apply and modified:
        filepath.write_text("\n".join(source_lines), encoding="utf-8")

    # Revertir orden para reporte (ascendente por línea)
    results.reverse()
    return results


def scan_project(root: Path, min_ratio: float, apply: bool) -> list[dict]:
    """Escanea proyecto recursivamente."""
    all_results = []

    for py_file in sorted(root.rglob("*.py")):
        parts = set(py_file.relative_to(root).parts)
        if parts & SKIP_DIRS:
            continue
        if any(p.endswith(".egg-info") for p in py_file.parts):
            continue

        all_results.extend(process_file(py_file, min_ratio, apply))

    return all_results


def main():
    parser = argparse.ArgumentParser(
        description="Trim docstrings over-documented (ratio ≥ 3x). Moderado: mantiene summary, elimina Parameters/Returns."
    )
    parser.add_argument("path", nargs="?", default=".", help="Ruta al proyecto (default: .)")
    parser.add_argument("--apply", action="store_true", help="Aplicar cambios (default: dry-run)")
    parser.add_argument(
        "--min-ratio", type=float, default=3.0, help="Ratio mínimo para trim (default: 3.0)"
    )
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"Error: {root} no es un directorio válido", file=sys.stderr)
        sys.exit(1)

    mode = "APLICANDO CAMBIOS" if args.apply else "DRY-RUN (usar --apply para escribir)"
    print(f"Modo: {mode}")
    print(f"Ratio mínimo: {args.min_ratio}x")
    print(f"Escaneando: {root}\n")

    results = scan_project(root, args.min_ratio, args.apply)

    if not results:
        print("✓ No se encontraron funciones para trim.")
        return

    total_saved = sum(r["saved"] for r in results)

    print(f"{'Archivo':<55} {'Línea':>5} {'Función':<28} {'Antes':>5} {'Desp':>5} {'Ahorro':>6}")
    print("-" * 110)

    for r in results:
        try:
            display_path = str(Path(r["file"]).relative_to(root))
        except ValueError:
            display_path = r["file"]
        if len(display_path) > 53:
            display_path = "..." + display_path[-50:]

        print(
            f"{display_path:<55} {r['line']:>5} {r['function']:<28} "
            f"{r['old_doc_lines']:>5} {r['new_doc_lines']:>5} {r['saved']:>5}L"
        )

    print(f"\nTotal funciones: {len(results)}")
    print(f"Total líneas ahorradas: {total_saved}")
    if args.apply:
        print("\n✓ Cambios aplicados.")
    else:
        print(f"\nPara aplicar: python {sys.argv[0]} {args.path} --apply")


if __name__ == "__main__":
    main()
