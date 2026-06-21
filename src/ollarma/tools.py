"""tools.py -- Tool implementations for the ollarma agent runtime.

Provides four tools that Ollama models can invoke via tool calling:
read_file, edit_file, run_bash, grep_search.

All tools return strings (success message or error description).
Tool functions never raise exceptions -- errors are returned as string messages
so the model can see and react to failures.

TOOL_REGISTRY maps tool names to callables.
TOOL_DEFINITIONS provides Ollama Tool-compatible schemas for client.chat(tools=...).
"""
from __future__ import annotations

import pathlib
import re
import subprocess
from typing import Callable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FILE_SIZE = 102_400      # 100KB
MAX_OUTPUT_SIZE = 10_240     # 10KB
MAX_GREP_RESULTS = 50


# ---------------------------------------------------------------------------
# Path validation helper
# ---------------------------------------------------------------------------

def _validate_path(path: str, project_root: str) -> pathlib.Path:
    """Resolve *path* and verify it is inside *project_root*.

    Returns the resolved Path on success.
    Raises ValueError with a descriptive message on failure.

    Uses pathlib.PurePath.is_relative_to() (Python 3.9+) instead of
    string prefix matching to avoid false positives when project_root
    is a prefix of an unrelated path (e.g., /tmp/foo vs /tmp/foobar).
    """
    root = pathlib.Path(project_root).resolve()
    candidate = pathlib.Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Path {path} is outside project root")
    # Reject symlinks that point outside project root (TOCTOU mitigation).
    # After resolve(), check the original path: if it's a symlink whose
    # target is inside root, resolve() already canonicalized it — safe.
    # If the original path IS a symlink, re-resolve and re-check to
    # narrow the race window.
    raw = candidate
    if raw.is_symlink():
        target = raw.resolve(strict=True)
        if not target.is_relative_to(root):
            raise ValueError(f"Path {path} is a symlink pointing outside project root")
    return resolved


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def read_file(path: str, project_root: str) -> str:
    """Read file contents, capped at MAX_FILE_SIZE bytes.

    Returns file text or an error message string.
    """
    try:
        resolved = _validate_path(path, project_root)
    except ValueError as exc:
        return f"Error: {exc}"

    if not resolved.exists():
        return f"Error: File not found: {path}"

    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error reading {path}: {exc}"

    if len(content) > MAX_FILE_SIZE:
        return content[:MAX_FILE_SIZE] + "\n[truncated at 100KB]"
    return content


def edit_file(path: str, old_text: str, new_text: str, project_root: str) -> str:
    """Replace *old_text* with *new_text* in file at *path*.

    old_text must appear exactly once. Returns success or error message.
    """
    try:
        resolved = _validate_path(path, project_root)
    except ValueError as exc:
        return f"Error: {exc}"

    if not resolved.exists():
        return f"Error: File not found: {path}"

    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error reading {path}: {exc}"

    count = content.count(old_text)
    if count == 0:
        return f"Error: old_text not found in {path}"
    if count > 1:
        return f"Error: old_text appears {count} times in {path} -- must be unique"

    new_content = content.replace(old_text, new_text, 1)
    try:
        resolved.write_text(new_content, encoding="utf-8")
    except Exception as exc:
        return f"Error writing {path}: {exc}"

    return f"Edited {path}: replaced {len(old_text)} chars"


_BASH_BLOCKLIST: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|--force\b).*(/|~|\*)"),  # rm -rf /, rm -f ~/*
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+/\s*$"),                     # rm -r /
    re.compile(r"\bmkfs\b"),                                                  # mkfs (format disk)
    re.compile(r"\bdd\b.*\bof\s*=\s*/dev/"),                                 # dd of=/dev/*
    re.compile(r"\bchmod\s+777\b"),                                           # chmod 777
    re.compile(r"(curl|wget)\s.*\|\s*(sh|bash|zsh)"),                         # curl ... | sh
    re.compile(r"\b:(){ :\|:& };:\b"),                                        # fork bomb
    re.compile(r">\s*/dev/sd[a-z]"),                                          # write to block device
]


def _check_bash_blocklist(command: str) -> str | None:
    """Return an error message if *command* matches a blocked pattern, else None."""
    for pattern in _BASH_BLOCKLIST:
        if pattern.search(command):
            return f"Error: Command blocked by safety filter (matched: {pattern.pattern})"
    return None


def run_bash(command: str, cwd: str, timeout: int = 30) -> str:
    """Execute *command* in shell and return stdout+stderr, capped at MAX_OUTPUT_SIZE.

    T-08-03: timeout prevents runaway processes.
    T-08-05: output capped at 10KB.
    CR-02: blocklist rejects obviously destructive shell patterns.
    """
    blocked = _check_bash_blocklist(command)
    if blocked:
        return blocked

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout}s"
    except Exception as exc:
        return f"Error: {exc}"

    if len(output) > MAX_OUTPUT_SIZE:
        return output[:MAX_OUTPUT_SIZE] + "\n[truncated at 10KB]"
    return output


def grep_search(
    pattern: str,
    path: str,
    project_root: str = "",
    max_results: int = MAX_GREP_RESULTS,
) -> str:
    """Search for *pattern* in files under *path*, return matching lines.

    Uses grep with shell=False for safety (pattern is an argument, not shell-interpolated).
    WR-01: validates *path* against *project_root* when provided.
    """
    search_path = path
    if project_root:
        try:
            search_path = str(_validate_path(path, project_root))
        except ValueError as exc:
            return f"Error: {exc}"

    try:
        result = subprocess.run(
            [
                "grep", "-rn",
                "--include=*.py", "--include=*.yml", "--include=*.md",
                "--include=*.yaml", "--include=*.json", "--include=*.toml",
                "--include=*.txt",
                pattern,
                search_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return "Error: grep timed out"
    except Exception as exc:
        return f"Error: {exc}"

    output = result.stdout.strip()
    if not output:
        return "No matches found"

    lines = output.split("\n")
    if len(lines) > max_results:
        lines = lines[:max_results]
        lines.append(f"[truncated at {max_results} results]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, Callable] = {
    "read_file": read_file,
    "edit_file": edit_file,
    "run_bash": run_bash,
    "grep_search": grep_search,
}


# ---------------------------------------------------------------------------
# Tool definitions (Ollama Tool-compatible JSON schema format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file at the given path",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute file path to read",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace old_text with new_text in a file (exact match, must be unique)",
            "parameters": {
                "type": "object",
                "required": ["path", "old_text", "new_text"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute file path to edit",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Exact text to find and replace (must appear exactly once)",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Execute a bash command and return stdout+stderr",
            "parameters": {
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Bash command to execute",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Search for a regex pattern in files under the given path",
            "parameters": {
                "type": "object",
                "required": ["pattern"],
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file path to search in (defaults to project root)",
                    },
                },
            },
        },
    },
]
