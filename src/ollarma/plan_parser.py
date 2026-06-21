"""plan_parser.py -- GSD PLAN.md file parser.

Extracts YAML frontmatter metadata and XML task definitions from
GSD PLAN.md files. Handles the specific format where YAML frontmatter
is delimited by --- and XML <tasks> blocks are embedded in Markdown.

Code blocks inside <action> elements are preserved as text content
without breaking the XML parser -- uses regex extraction per element
instead of full XML parse (P-08-02 mitigation).
"""
from __future__ import annotations

import re
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class PlanTask(BaseModel):
    """A single task extracted from a GSD PLAN.md <tasks> block.

    Frozen Pydantic model — immutable after creation.
    """

    name: str
    action: str
    files: list[str] = []
    acceptance_criteria: list[str] = []
    read_first: list[str] = []
    done: str = ""

    model_config = ConfigDict(frozen=True)


class PlanMetadata(BaseModel):
    """Metadata from the YAML frontmatter of a GSD PLAN.md file.

    Frozen Pydantic model — immutable after creation.
    """

    phase: str
    plan: int
    type: str
    wave: int = 1
    requirements: list[str] = []
    files_modified: list[str] = []
    autonomous: bool = True
    depends_on: list[str] = []

    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split YAML frontmatter from body.

    Returns (frontmatter_string, body_string).
    If no frontmatter delimiters found, returns ("", text).
    """
    # Match --- at start (possibly after whitespace), then content, then ---
    parts = text.split("---", 2)
    if len(parts) >= 3:
        return parts[1].strip(), parts[2]
    return "", text


def _extract_tasks_xml(body: str) -> str:
    """Find <tasks>...</tasks> block in body text.

    Returns the content between <tasks> and </tasks>, or empty string.
    Uses re.DOTALL so . matches newlines.
    """
    match = re.search(r"<tasks>(.*?)</tasks>", body, re.DOTALL)
    if match:
        return match.group(1)
    return ""


def _extract_element_text(block: str, tag: str) -> str:
    """Extract text content of a single XML element from a task block.

    Uses regex to find <tag>...</tag> and returns the content between.
    Handles multiline content including code blocks.
    """
    pattern = re.compile(rf"<{tag}>(.*?)</{tag}>", re.DOTALL)
    match = pattern.search(block)
    if match:
        return match.group(1).strip()
    return ""


def _parse_list_items(text: str) -> list[str]:
    """Parse a Markdown-style bulleted list into a list of strings.

    Each line starting with '- ' (after stripping) becomes an item.
    """
    items = []
    for line in text.strip().split("\n"):
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return items


def _parse_task_block(block: str) -> PlanTask:
    """Parse a single <task>...</task> block into a PlanTask."""
    name = _extract_element_text(block, "name")
    action = _extract_element_text(block, "action")

    # Files: comma-separated or newline-separated
    files_raw = _extract_element_text(block, "files")
    if files_raw:
        files = [f.strip() for f in re.split(r"[,\n]", files_raw) if f.strip()]
    else:
        files = []

    # Acceptance criteria: Markdown list items
    criteria_raw = _extract_element_text(block, "acceptance_criteria")
    acceptance_criteria = _parse_list_items(criteria_raw) if criteria_raw else []

    # Read first: Markdown list items
    read_first_raw = _extract_element_text(block, "read_first")
    read_first = _parse_list_items(read_first_raw) if read_first_raw else []

    # Done summary
    done = _extract_element_text(block, "done")

    return PlanTask(
        name=name,
        action=action,
        files=files,
        acceptance_criteria=acceptance_criteria,
        read_first=read_first,
        done=done,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_plan_tasks(plan_text: str) -> tuple[PlanMetadata, list[PlanTask]]:
    """Parse a GSD PLAN.md file into metadata and a list of tasks.

    Args:
        plan_text: Full text of the PLAN.md file.

    Returns:
        (PlanMetadata, list[PlanTask]) — metadata from frontmatter
        and tasks from the <tasks> XML block.
    """
    # 1. Split YAML frontmatter
    frontmatter_str, body = _split_frontmatter(plan_text)

    # 2. Parse metadata from YAML
    if frontmatter_str:
        fm = yaml.safe_load(frontmatter_str)
        if not isinstance(fm, dict):
            fm = {}
    else:
        fm = {}

    metadata = PlanMetadata(
        phase=fm.get("phase", "unknown"),
        plan=fm.get("plan", 0),
        type=fm.get("type", "execute"),
        wave=fm.get("wave", 1),
        requirements=fm.get("requirements", []) or [],
        files_modified=fm.get("files_modified", []) or [],
        autonomous=fm.get("autonomous", True),
        depends_on=[str(d) for d in (fm.get("depends_on", []) or [])],
    )

    # 3. Extract <tasks> block
    tasks_xml = _extract_tasks_xml(body)
    if not tasks_xml.strip():
        return metadata, []

    # 4. Find individual <task>...</task> blocks using regex
    task_pattern = re.compile(r"<task[^>]*>(.*?)</task>", re.DOTALL)
    task_blocks = task_pattern.findall(tasks_xml)

    tasks = [_parse_task_block(block) for block in task_blocks]
    return metadata, tasks
