"""YAML front-matter parsing for markdown/txt files.

Shared by verify_steerings.py and md_index.py for consistent
front-matter extraction and body separation.

Usage:
    from core.frontmatter import parse_front_matter, get_body

    fm = parse_front_matter(Path("file.md"))
    # fm = {"inclusion": "fileMatch", "fileMatchPattern": "src/**/*.py"}

    body = get_body(Path("file.md"))
    # body = "# Main content\\n..."
"""

from __future__ import annotations

import re
from pathlib import Path

# Regex for YAML front-matter block
_FM_PATTERN = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_FM_WITH_BODY = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)


def parse_front_matter(filepath: Path) -> dict[str, str]:
    """Extract YAML front-matter from a markdown/txt file.

    Handles values containing colons by splitting only on the first colon.
    Returns empty dict if no front-matter block found.

    Parameters
    ----------
    filepath : Path
        Path to the markdown or text file.

    Returns
    -------
    dict[str, str]
        Key-value pairs from the front-matter block.
    """
    try:
        text = filepath.read_text(errors="replace")
    except (OSError, PermissionError):
        return {}

    match = _FM_PATTERN.match(text)
    if not match:
        return {}

    fm: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, val = line.split(":", 1)  # Split only on FIRST colon
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm


def get_body(filepath: Path) -> str:
    """Get file body content (everything after front-matter).

    If no front-matter is present, returns the entire file content.

    Parameters
    ----------
    filepath : Path
        Path to the markdown or text file.

    Returns
    -------
    str
        Body content without the front-matter block.
    """
    try:
        text = filepath.read_text(errors="replace")
    except (OSError, PermissionError):
        return ""

    match = _FM_WITH_BODY.match(text)
    if match:
        return text[match.end() :]
    return text


def get_inclusion_mode(filepath: Path) -> str:
    """Determine the inclusion mode of a steering file."""
    fm = parse_front_matter(filepath)
    return fm.get("inclusion", "always")


def get_file_match_patterns(filepath: Path) -> list[str]:
    """Extract fileMatchPattern(s) from a steering file's front-matter.

    Returns
    -------
    list[str]
        List of glob patterns (may be empty if not a fileMatch steering).
    """
    fm = parse_front_matter(filepath)
    if fm.get("inclusion") != "fileMatch":
        return []
    pattern_str = fm.get("fileMatchPattern", "")
    if not pattern_str:
        return []
    return [p.strip() for p in pattern_str.split(",") if p.strip()]
