#!/usr/bin/env python3
# TODO move mainteinance tools to a new project repository in order to reuse them. Also a refactor and modular parameters will be needed

"""Generate a rich markdown index/TOC from a directory of .md files.

Usage:
    .venv/bin/python scripts/md_index.py documentation/analysis
    .venv/bin/python scripts/md_index.py documentation/binnacles --output INDEX.md
    .venv/bin/python scripts/md_index.py documentation/analysis --format table
    .venv/bin/python scripts/md_index.py documentation/ -r --format full

Replicable to any folder containing markdown files.

Formats:
    list   — one entry per file with title, date, description, sections
    table  — compact table (file, title, date, lines, words)
    full   — richest view: sections + subsections + key findings + assertions
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants & compiled patterns
# ---------------------------------------------------------------------------

_DATE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\*\*(?:Fecha|Date|Generadas?|Last updated|Updated|Created)\*\*"
        r"\s*[:：]\s*(.+)",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:Fecha|Date|Created|Updated)\s*[:：]\s*(.+)", re.IGNORECASE),
    re.compile(r"(\d{4}-\d{2}-\d{2})"),
]

# Patterns for meaningful numeric metrics (avoid overly broad matches)
_METRIC_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(R²\s*[=><≥≤]\s*0?\.\d+)"),
    re.compile(r"(fidelity\s*[=><≥≤]\s*0?\.\d+)", re.IGNORECASE),
    re.compile(r"(ΔE/gap\s*[=<>≤≥]\s*\d+\.?\d*%?)"),
    re.compile(r"(gain\s*[=:]\s*[+\-]?\d+\.?\d*%)"),
    re.compile(r"(p-value\s*[=<>]\s*[\d.e\-]+)", re.IGNORECASE),
]

# Patterns for textual findings/assertions (blockquotes, bold conclusions)
_FINDING_PATTERNS: list[re.Pattern[str]] = [
    # Blockquote assertions (lines starting with >)
    re.compile(r'^>\s*["\u201c]?(.{20,200})["\u201d]?\s*$'),
    # Bold key findings / conclusions
    re.compile(
        r"\*\*(?:Key Finding|Conclusi[o\u00f3]n|Result|Hallazgo|Finding)\*\*"
        r"\s*[:\uff1a]\s*(.+)"
    ),
    # Lines with check/cross marks that signal confirmed/rejected assertions
    re.compile(r"^[-*]\s+(.{15,150}[\u2705\u274c].*)$"),
    re.compile(r"^[-*]\s+(.{15,150})\s*$"),  # bullet assertions in findings sections
]

# Sections whose content contains key findings/assertions
_FINDING_SECTION_NAMES = {
    "conclusión",
    "conclusiones",
    "conclusion",
    "conclusions",
    "key findings",
    "key finding",
    "hallazgos",
    "hallazgo",
    "implicación para la tesis",
    "thesis implications",
    "implicación global",
    "interpretation for thesis",
    "summary",
    "resumen",
    "resultado",
    "results",
    "executive summary",
    "key lessons",
    "key takeaways",
    "action items",
    "recommendations",
    "updated recommendation",
    "hallazgo clave",
    "diagnosis",
    "root cause",
    "interpretation",
    "interpretación",
}

# Tags to auto-detect from content
_TAG_KEYWORDS: list[tuple[str, str]] = [
    ("zne", "zne"),
    ("hardware", "hardware"),
    ("mpnn", "mpnn"),
    ("tesis", "tesis"),
    ("thesis", "thesis"),
    ("heisenberg", "heisenberg"),
    ("tfim", "tfim"),
    ("pea", "pea"),
    ("scaling", "scaling"),
    ("topology", "topology"),
    ("noise", "noise"),
    ("dmrg", "dmrg"),
]

_HEADER_SCAN_DEPTH = 40  # lines to scan for title/date/description


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Section:
    """A document section (H2) with optional subsections (H3)."""

    name: str
    line: int
    subsections: list[str] = field(default_factory=list)


@dataclass
class FileStats:
    """Quantitative stats about a markdown file."""

    lines: int
    words: int
    size_bytes: int
    table_rows: int
    code_blocks: int

    @property
    def size_human(self) -> str:
        if self.size_bytes < 1024:
            return f"{self.size_bytes} B"
        elif self.size_bytes < 1024 * 1024:
            return f"{self.size_bytes / 1024:.1f} KB"
        return f"{self.size_bytes / (1024 * 1024):.1f} MB"


@dataclass
class DocEntry:
    """Complete metadata for one markdown document."""

    file: str
    path: Path
    title: str
    date: str
    description: str
    sections: list[Section]
    metrics: list[str]
    findings: list[str]
    tags: list[str]
    stats: FileStats


# ---------------------------------------------------------------------------
# Extraction logic
# ---------------------------------------------------------------------------


def _extract_date(line: str) -> str | None:
    """Try to extract a date from a line using known patterns."""
    for pat in _DATE_PATTERNS:
        m = pat.search(line)
        if m:
            raw = m.group(1).strip().rstrip(".")
            # If we captured too much (e.g. a full line after the date),
            # try to isolate just the date portion
            iso_match = re.match(r"(\d{4}-\d{2}-\d{2}(?:\s*\(.+?\))?)", raw)
            if iso_match:
                return iso_match.group(1).strip()
            # Truncate at pipe or double-star (likely table/bold continuation)
            raw = re.split(r"\s*\||\s*\*\*", raw)[0].strip()
            return raw if raw else None
    return None


def _extract_metrics(lines: list[str], max_count: int = 6) -> list[str]:
    """Extract unique numeric metrics from the document."""
    seen: set[str] = set()
    results: list[str] = []
    for line in lines:
        if len(results) >= max_count:
            break
        for pat in _METRIC_PATTERNS:
            for m in pat.finditer(line):
                value = m.group(1).strip()
                if value not in seen:
                    seen.add(value)
                    results.append(value)
                    if len(results) >= max_count:
                        break
    return results


def _extract_findings(lines: list[str], sections: list[Section], max_count: int = 8) -> list[str]:
    """Extract key findings and assertions from the document.

    Strategy:
    1. Look for blockquote assertions (lines starting with >) that are
       substantial (>20 chars).
    2. Look for explicit finding markers (**Key Finding**: ...).
    3. Extract bullet points from sections whose names suggest findings
       (Conclusión, Key Findings, Implicación, etc.).
    """
    findings: list[str] = []
    seen: set[str] = set()

    def _add(text: str) -> bool:
        """Add a finding if unique and meaningful. Returns False when full."""
        clean = text.strip().rstrip(".")
        # Skip table rows, very short, or already seen
        if len(clean) < 15 or clean.startswith("|") or clean in seen:
            return True
        # Remove leading markdown artifacts
        clean = re.sub(r"^[>\-*]\s*", "", clean)
        clean = re.sub(r"\*\*(.+?)\*\*", r"\1", clean)  # remove bold
        clean = clean.strip('" "')
        if len(clean) < 15 or clean in seen:
            return True
        seen.add(clean)
        findings.append(clean)
        return len(findings) < max_count

    # Pass 1: Blockquotes — join consecutive > lines into single findings
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith(">"):
            # Collect consecutive blockquote lines
            bq_parts: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                part = re.sub(r"^>\s*", "", lines[i].strip())
                if part:
                    bq_parts.append(part)
                i += 1
            combined = " ".join(bq_parts)
            # Only keep substantial assertions (not single-word or short refs)
            if len(combined) >= 25 and not _add(combined):
                return findings
        else:
            i += 1

    # Pass 1b: Bold finding markers + ✅❌ lines
    for line in lines:
        for pat in _FINDING_PATTERNS[1:3]:  # bold findings + ✅❌
            m = pat.match(line.strip())
            if m and not _add(m.group(1)):
                return findings

    # Pass 2: Bullets from finding-related sections
    finding_ranges: list[tuple[int, int]] = []
    for i, sec in enumerate(sections):
        sec_name_lower = sec.name.lower().strip()
        if any(kw in sec_name_lower for kw in _FINDING_SECTION_NAMES):
            start = sec.line
            end = sections[i + 1].line if i + 1 < len(sections) else len(lines)
            finding_ranges.append((start, end))

    for start, end in finding_ranges:
        for line in lines[start:end]:
            stripped = line.strip()
            # Bullet points
            if stripped.startswith(("- ", "* ")):
                bullet_text = stripped[2:].strip()
            # Numbered lists (1. text, 2. text, etc.)
            elif re.match(r"^\d+\.\s", stripped):
                bullet_text = re.sub(r"^\d+\.\s+", "", stripped)
            else:
                continue
            # Only meaningful items (not sub-items or short refs)
            if len(bullet_text) > 20 and not bullet_text.startswith("["):
                if not _add(bullet_text):
                    return findings

    return findings


def _detect_tags(text_lower: str, max_count: int = 6) -> list[str]:
    """Detect topic tags from document content."""
    tags: list[str] = []
    seen: set[str] = set()
    for keyword, tag in _TAG_KEYWORDS:
        if keyword in text_lower and tag not in seen:
            # Avoid thesis/tesis duplication
            if tag == "thesis" and "tesis" in seen:
                continue
            if tag == "tesis" and "thesis" in seen:
                continue
            seen.add(tag)
            tags.append(tag)
            if len(tags) >= max_count:
                break
    return tags


def _count_table_rows(lines: list[str]) -> int:
    """Count actual table data rows (exclude separator rows like |---|)."""
    count = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and "|" in stripped[1:]:
            # Skip separator rows
            if not re.match(r"^\|[\s\-:|]+\|$", stripped):
                count += 1
    return count


def extract_metadata(filepath: Path) -> DocEntry:
    """Extract rich metadata from a markdown file."""
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"Warning: Cannot read {filepath}: {exc}", file=sys.stderr)
        return DocEntry(
            file=filepath.name,
            path=filepath,
            title=filepath.stem,
            date="—",
            description="[unreadable]",
            sections=[],
            metrics=[],
            findings=[],
            tags=[],
            stats=FileStats(0, 0, 0, 0, 0),
        )

    lines = text.splitlines()
    total_lines = len(lines)
    total_words = len(text.split())

    try:
        file_size = filepath.stat().st_size
    except OSError:
        file_size = len(text.encode("utf-8"))

    # --- Header scanning (title, date, description) ---
    title: str | None = None
    date: str | None = None
    description = ""

    for line in lines[:_HEADER_SCAN_DEPTH]:
        stripped = line.strip()

        if title is None and stripped.startswith("# "):
            title = stripped[2:].strip()
            continue

        if date is None:
            found_date = _extract_date(stripped)
            if found_date:
                date = found_date

        if title and not description and stripped:
            if not stripped.startswith(("#", "|", "---", "```")):
                description = re.sub(r"\*\*(.+?)\*\*\s*[:：]\s*", r"\1: ", stripped)

    # --- Section tree (H2 → H3) ---
    sections: list[Section] = []
    current_section: Section | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## "):
            current_section = Section(name=stripped[3:].strip(), line=i + 1)
            sections.append(current_section)
        elif stripped.startswith("### ") and current_section is not None:
            current_section.subsections.append(stripped[4:].strip())

    # --- Quantitative stats ---
    table_rows = _count_table_rows(lines)
    code_blocks = text.count("```") // 2
    stats = FileStats(
        lines=total_lines,
        words=total_words,
        size_bytes=file_size,
        table_rows=table_rows,
        code_blocks=code_blocks,
    )

    # --- Content extraction ---
    metrics = _extract_metrics(lines)
    findings = _extract_findings(lines, sections)
    tags = _detect_tags(text.lower())

    return DocEntry(
        file=filepath.name,
        path=filepath,
        title=title or filepath.stem,
        date=date or "—",
        description=description[:160],
        sections=sections,
        metrics=metrics,
        findings=findings,
        tags=tags,
        stats=stats,
    )


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _stats_line(e: DocEntry) -> str:
    """One-liner with file stats."""
    s = e.stats
    return (
        f"{s.lines} líneas, {s.words:,} palabras, {s.size_human}"
        f" | {s.table_rows} filas tabla, {s.code_blocks} bloques código"
    )


def format_full(entries: list[DocEntry], folder: Path) -> str:
    """Richest format: section tree + subsections + findings + assertions."""
    out: list[str] = []
    out.append(f"# Índice Detallado — `{folder}`\n")
    out.append(f"**Archivos**: {len(entries)} documentos markdown")
    total_words = sum(e.stats.words for e in entries)
    total_lines = sum(e.stats.lines for e in entries)
    out.append(f"**Total**: {total_words:,} palabras, {total_lines:,} líneas\n")
    out.append("---\n")

    for i, e in enumerate(entries, 1):
        out.append(f"## {i}. [{e.file}]({e.file})")
        out.append(f"**{e.title}**\n")

        # Stats table
        out.append("| Fecha | Líneas | Palabras | Tamaño | Tablas | Código |")
        out.append("|-------|--------|----------|--------|--------|--------|")
        s = e.stats
        out.append(
            f"| {e.date} | {s.lines} | {s.words:,} "
            f"| {s.size_human} | {s.table_rows} | {s.code_blocks} |"
        )
        out.append("")

        if e.tags:
            out.append(f"**Tags**: {', '.join(f'`{t}`' for t in e.tags)}")

        if e.description:
            out.append(f"\n> {e.description}\n")

        if e.metrics:
            out.append(f"**Métricas clave**: {' · '.join(e.metrics)}\n")

        # Key findings / assertions
        if e.findings:
            out.append(f"**Hallazgos / Aseveraciones** ({len(e.findings)}):\n")
            for f in e.findings:
                # Truncate very long findings for readability
                display = f[:180] + "…" if len(f) > 180 else f
                out.append(f"- {display}")
            out.append("")

        # Section tree
        if e.sections:
            out.append(f"**Contenido** ({len(e.sections)} secciones):\n")
            for sec in e.sections:
                out.append(f"- **{sec.name}** (L{sec.line})")
                for sub in sec.subsections[:6]:
                    out.append(f"  - {sub}")
                if len(sec.subsections) > 6:
                    out.append(f"  - … (+{len(sec.subsections) - 6} más)")
            out.append("")

        out.append("---\n")

    return "\n".join(out)


def format_list(entries: list[DocEntry], folder: Path) -> str:
    """Medium detail: title, date, description, findings, section names."""
    out: list[str] = []
    out.append(f"# Índice — `{folder}`\n")
    out.append(f"**Archivos**: {len(entries)} documentos markdown\n")

    for e in entries:
        out.append(f"## [{e.file}]({e.file})")
        out.append(f"- **Título**: {e.title}")
        out.append(f"- **Fecha**: {e.date}")
        out.append(f"- **Stats**: {_stats_line(e)}")
        if e.tags:
            out.append(f"- **Tags**: {', '.join(e.tags)}")
        if e.description:
            out.append(f"- **Descripción**: {e.description}")

        # Show findings in list format too
        if e.findings:
            out.append(f"- **Hallazgos** ({len(e.findings)}):")
            for f in e.findings[:5]:
                display = f[:140] + "…" if len(f) > 140 else f
                out.append(f"  - {display}")
            if len(e.findings) > 5:
                out.append(f"  - … (+{len(e.findings) - 5} más)")

        if e.sections:
            out.append(f"- **Secciones** ({len(e.sections)}):")
            for sec in e.sections[:12]:
                sub_info = f" ({len(sec.subsections)} sub)" if sec.subsections else ""
                out.append(f"  - {sec.name}{sub_info}")
            if len(e.sections) > 12:
                out.append(f"  - … (+{len(e.sections) - 12} más)")
        out.append("")

    return "\n".join(out)


def format_table(entries: list[DocEntry], folder: Path) -> str:
    """Compact table format."""
    out: list[str] = []
    out.append(f"# Índice — `{folder}`\n")
    out.append(f"**Archivos**: {len(entries)} documentos markdown\n")
    out.append(
        "| # | Archivo | Título | Fecha | Líneas | Palabras | Secciones | Hallazgos | Tags |"
    )
    out.append(
        "|---|---------|--------|-------|--------|----------|-----------|-----------|------|"
    )

    for i, e in enumerate(entries, 1):
        title_short = e.title[:42] + "…" if len(e.title) > 42 else e.title
        sec_count = len(e.sections)
        find_count = len(e.findings)
        tags = ", ".join(e.tags[:3]) if e.tags else "—"
        s = e.stats
        out.append(
            f"| {i} | `{e.file}` | {title_short} "
            f"| {e.date} | {s.lines} | {s.words:,} "
            f"| {sec_count} | {find_count} | {tags} |"
        )

    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

_FORMATTERS = {
    "list": format_list,
    "table": format_table,
    "full": format_full,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a rich markdown index from a folder of .md files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    .venv/bin/python scripts/md_index.py documentation/analysis
    .venv/bin/python scripts/md_index.py documentation/analysis --format full
    .venv/bin/python scripts/md_index.py documentation/binnacles -f table -o INDEX.md
    .venv/bin/python scripts/md_index.py documentation/ -r --format table
""",
    )
    parser.add_argument("folder", type=Path, help="Path to folder with .md files")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Write output to file (default: stdout)",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=list(_FORMATTERS.keys()),
        default="list",
        help="Output format: 'list' (medium), 'table' (compact), 'full' (richest)",
    )
    parser.add_argument(
        "--recursive",
        "-r",
        action="store_true",
        help="Scan subfolders recursively",
    )
    args = parser.parse_args()

    folder = args.folder.resolve()
    if not folder.is_dir():
        print(f"Error: '{folder}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    glob_pattern = "**/*.md" if args.recursive else "*.md"
    md_files = sorted(folder.glob(glob_pattern))

    # Exclude INDEX.md to avoid self-referencing loops
    md_files = [f for f in md_files if f.name.upper() != "INDEX.MD"]

    if not md_files:
        print(f"No .md files found in '{folder}'.", file=sys.stderr)
        sys.exit(1)

    entries = [extract_metadata(f) for f in md_files]

    formatter = _FORMATTERS[args.format]
    result = formatter(entries, args.folder)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result, encoding="utf-8")
        print(f"✓ Index written to {args.output} ({len(entries)} files)")
    else:
        print(result)


if __name__ == "__main__":
    main()
