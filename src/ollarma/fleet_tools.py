"""fleet_tools.py -- Extended tool implementations for multi-project fleet agents.

Provides 5 additional tools beyond the base 4 in tools.py:
  gh_tool       -- GitHub CLI wrapper with structured JSON output
  git_cmd_tool  -- Safe git operations (read-only by default, destructive ops blocked)
  skill_invoke_tool -- Read gsigmad SKILL.md files for model context injection
  db_query_tool -- ArangoDB AQL query execution (deferred import)
  mcp_query_tool -- MCP tool call via stdio client transport

Also provides:
  build_tool_registry() -- Factory that merges base tools with fleet tools
  FLEET_TOOL_DEFINITIONS -- Aggregate list of all fleet tool JSON schemas
  GIT_CMD_DESTRUCTIVE_PATTERNS -- Blocked git operation patterns

All tools return strings (success or error). Never raise exceptions.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
from datetime import timedelta
from typing import Callable, Optional

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ollarma.tools import MAX_OUTPUT_SIZE, TOOL_DEFINITIONS, TOOL_REGISTRY

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SKILLS_DIR = "<repo>/skills"

# Regex for validating skill names (T-9-07: prevent path traversal)
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Destructive git operation patterns (T-9-06)
GIT_CMD_DESTRUCTIVE_PATTERNS: list[tuple[str, list[str]]] = [
    ("push", ["--force", "-f"]),
    ("reset", ["--hard"]),
    ("clean", ["-f", "--force"]),
    ("branch", ["-D"]),
    ("rebase", ["--force", "-f"]),
    ("merge", ["--force", "-f"]),
    ("checkout", ["--force", "-f"]),
]

# Default --json fields per gh subcommand
_GH_JSON_DEFAULTS: dict[str, str] = {
    "pr list": "number,title,state,author",
    "issue list": "number,title,state",
}


# ---------------------------------------------------------------------------
# gh_tool
# ---------------------------------------------------------------------------


def gh_tool(args: str, cwd: str) -> str:
    """Execute a GitHub CLI command with structured JSON output.

    Appends --json with sensible default fields when not already present.
    Output is capped at MAX_OUTPUT_SIZE bytes.

    Args:
        args: Arguments to pass to gh (e.g., "pr list --limit 5").
        cwd: Working directory for the subprocess.

    Returns:
        stdout from gh, or an error string on failure.
    """
    arg_parts = args.split()

    # Append --json with default fields if not already present
    if "--json" not in arg_parts:
        # Determine subcommand for field defaults
        sub = " ".join(arg_parts[:2]) if len(arg_parts) >= 2 else ""
        json_fields = _GH_JSON_DEFAULTS.get(sub, "")
        if json_fields:
            arg_parts.extend(["--json", json_fields])
        else:
            arg_parts.append("--json")

    try:
        result = subprocess.run(
            ["gh"] + arg_parts,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
        )
        output = result.stdout
        if result.returncode != 0 and result.stderr:
            return f"Error: {result.stderr.strip()}"
    except FileNotFoundError as exc:
        return f"Error: {exc}"
    except subprocess.TimeoutExpired:
        return "Error: gh command timed out after 30s"
    except Exception as exc:
        return f"Error: {exc}"

    if len(output) > MAX_OUTPUT_SIZE:
        return output[:MAX_OUTPUT_SIZE] + "\n[truncated at 10KB]"
    return output


# ---------------------------------------------------------------------------
# git_cmd_tool
# ---------------------------------------------------------------------------


def _is_destructive_git(arg_parts: list[str]) -> bool:
    """Check if git arguments match a destructive pattern.

    Also catches combined short flags (e.g., -vf contains -f).
    Returns True if the command should be blocked.
    """
    if not arg_parts:
        return False

    subcommand = arg_parts[0]
    rest = arg_parts[1:]

    for blocked_cmd, blocked_flags in GIT_CMD_DESTRUCTIVE_PATTERNS:
        if subcommand == blocked_cmd:
            for flag in blocked_flags:
                if flag in rest:
                    return True
                # WR-03: detect combined short flags (e.g., -vf contains -f)
                if flag.startswith("-") and len(flag) == 2:
                    char = flag[1]
                    for token in rest:
                        if (
                            token.startswith("-")
                            and not token.startswith("--")
                            and char in token
                        ):
                            return True
    return False


def git_cmd_tool(args: str, cwd: str) -> str:
    """Execute a git command and return output.

    Blocks destructive operations (push --force, reset --hard, clean -f,
    branch -D). Output is capped at MAX_OUTPUT_SIZE bytes.

    Args:
        args: Arguments to pass to git (e.g., "status", "log --oneline -5").
        cwd: Working directory for the subprocess.

    Returns:
        stdout from git, or an error string on failure.
    """
    arg_parts = args.split()

    if _is_destructive_git(arg_parts):
        return f"Error: Blocked destructive git operation: git {args}"

    try:
        result = subprocess.run(
            ["git"] + arg_parts,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
        )
        output = result.stdout
        if result.returncode != 0 and result.stderr:
            output = result.stdout + result.stderr
    except FileNotFoundError as exc:
        return f"Error: {exc}"
    except subprocess.TimeoutExpired:
        return "Error: git command timed out after 30s"
    except Exception as exc:
        return f"Error: {exc}"

    if len(output) > MAX_OUTPUT_SIZE:
        return output[:MAX_OUTPUT_SIZE] + "\n[truncated at 10KB]"
    return output


# ---------------------------------------------------------------------------
# skill_invoke_tool
# ---------------------------------------------------------------------------


def skill_invoke_tool(
    skill_name: str, skills_dir: str = DEFAULT_SKILLS_DIR
) -> str:
    """Read a gsigmad SKILL.md file and return its content.

    Validates skill_name to prevent path traversal (T-9-07).
    Content is capped at MAX_OUTPUT_SIZE.

    Args:
        skill_name: Name of the skill directory (e.g., "gsigmad-run-experiment").
        skills_dir: Base directory containing skill subdirectories.

    Returns:
        SKILL.md content, or an error string on failure.
    """
    if not skill_name:
        return "Error: skill_name is required"

    if not _SKILL_NAME_RE.match(skill_name):
        return f"Error: Invalid skill name '{skill_name}' -- only [a-zA-Z0-9_-] allowed"

    skill_path = os.path.join(skills_dir, skill_name, "SKILL.md")

    if not os.path.isfile(skill_path):
        return f"Error: Skill '{skill_name}' not found at {skill_path}"

    try:
        with open(skill_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as exc:
        return f"Error reading skill '{skill_name}': {exc}"

    if len(content) > MAX_OUTPUT_SIZE:
        return content[:MAX_OUTPUT_SIZE] + "\n[truncated at 10KB]"
    return content


# ---------------------------------------------------------------------------
# Tool definitions (Ollama Tool-compatible JSON schema)
# ---------------------------------------------------------------------------

GH_TOOL_DEFINITION: dict = {
    "type": "function",
    "function": {
        "name": "gh",
        "description": "Execute a GitHub CLI command with structured JSON output",
        "parameters": {
            "type": "object",
            "required": ["args"],
            "properties": {
                "args": {
                    "type": "string",
                    "description": "Arguments to pass to gh (e.g., 'pr list --limit 5')",
                }
            },
        },
    },
}

GIT_CMD_TOOL_DEFINITION: dict = {
    "type": "function",
    "function": {
        "name": "git_cmd",
        "description": "Execute a safe git command (read-only operations; destructive ops blocked)",
        "parameters": {
            "type": "object",
            "required": ["args"],
            "properties": {
                "args": {
                    "type": "string",
                    "description": "Arguments to pass to git (e.g., 'status', 'log --oneline -5')",
                }
            },
        },
    },
}

SKILL_INVOKE_DEFINITION: dict = {
    "type": "function",
    "function": {
        "name": "skill_invoke",
        "description": "Read a gsigmad SKILL.md file for model context injection",
        "parameters": {
            "type": "object",
            "required": ["skill_name"],
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Name of the skill (e.g., 'gsigmad-run-experiment')",
                }
            },
        },
    },
}


# ---------------------------------------------------------------------------
# build_tool_registry (partial -- db_query and mcp_query added in Task 2)
# ---------------------------------------------------------------------------


def build_tool_registry(
    project_root: str,
    adapter: Optional["AdapterConfig"] = None,
    *,
    service_mode: bool = False,
) -> tuple[dict[str, Callable], list[dict]]:
    """Build a merged tool registry from base tools + fleet tools.

    When adapter is None, returns only the base 4 tools.
    When adapter is provided, always adds gh, git_cmd, skill_invoke.
    Conditionally adds db_query (if adapter.databases non-empty)
    and mcp_query (if adapter.mcps non-empty).

    Args:
        project_root: Project root directory for tool execution context.
        adapter: Optional AdapterConfig for conditional tool registration.

    Returns:
        Tuple of (registry_dict, definitions_list).
    """
    if service_mode:
        allowed_base = {"read_file", "grep_search"}
        registry = {
            name: fn for name, fn in TOOL_REGISTRY.items() if name in allowed_base
        }
        definitions = [
            definition
            for definition in TOOL_DEFINITIONS
            if definition["function"]["name"] in allowed_base
        ]
    else:
        # Start with copies of base registries
        registry = dict(TOOL_REGISTRY)
        definitions = list(TOOL_DEFINITIONS)

    if adapter is None:
        return registry, definitions

    registry["skill_invoke"] = skill_invoke_tool
    definitions.append(SKILL_INVOKE_DEFINITION)

    if service_mode:
        return registry, definitions

    # Always add gh and git_cmd for local interactive fleet use.
    registry["gh"] = lambda args, cwd=project_root: gh_tool(args, cwd)
    registry["git_cmd"] = lambda args, cwd=project_root: git_cmd_tool(args, cwd)
    definitions.append(GH_TOOL_DEFINITION)
    definitions.append(GIT_CMD_TOOL_DEFINITION)

    # Conditional tools based on adapter config
    if adapter.databases:
        registry["db_query"] = (
            lambda query, db_config=adapter.databases[0]: db_query_tool(query, db_config)
        )
        definitions.append(DB_QUERY_DEFINITION)

    if adapter.mcps:
        first_mcp = adapter.mcps[0]
        registry["mcp_query"] = (
            lambda query,
            tool_name=None,
            arguments=None,
            server_command=first_mcp.get("command", ""): mcp_query_tool(
                query,
                server_command,
                tool_name=tool_name,
                arguments=arguments,
            )
        )
        definitions.append(MCP_QUERY_DEFINITION)

    return registry, definitions


# ---------------------------------------------------------------------------
# db_query_tool
# ---------------------------------------------------------------------------

# Max results cap (T-9-03: limit data exposure from AQL queries)
_DB_QUERY_MAX_RESULTS = 100

# AQL write keyword guard (CR-01: prevent write operations via db_query tool)
_AQL_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|REMOVE|REPLACE|UPSERT|CREATE|DROP|TRUNCATE)\b",
    re.IGNORECASE,
)

# Host validation (CR-02: prevent host injection in db_query_tool connection)
_VALID_HOST_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def _import_arango_client():
    """Deferred import of ArangoClient to avoid import overhead when unused.

    Returns the ArangoClient class, or raises ImportError.
    """
    from arango import ArangoClient
    return ArangoClient


def db_query_tool(query: str, db_config: dict) -> str:
    """Execute an AQL query against ArangoDB and return JSON results.

    Credentials are read from ARANGO_USER and ARANGO_PASSWORD env vars (T-9-04).
    Results are capped at 100 items. Output is serialized with orjson.

    Args:
        query: AQL query string to execute.
        db_config: Connection config dict with keys: host, port, db.

    Returns:
        JSON string of query results, or an error string on failure.
    """
    # CR-01: reject write operations to prevent AQL injection
    if _AQL_WRITE_KEYWORDS.search(query):
        return "Error: Write operations are not allowed via db_query tool"

    try:
        ArangoClient = _import_arango_client()

        host = db_config.get("host", "localhost")
        port = db_config.get("port", 8531)
        db_name = db_config.get("db", "")

        # CR-02: validate host and port to prevent connection injection
        if not _VALID_HOST_RE.match(host):
            return f"Error: Invalid host in db_config: {host!r}"
        if not isinstance(port, int) or not (1 <= port <= 65535):
            return f"Error: Invalid port in db_config: {port!r}"

        user = os.environ.get("ARANGO_USER", "root")
        password = os.environ.get("ARANGO_PASSWORD", "")

        client = ArangoClient(hosts=f"http://{host}:{port}")
        db = client.db(db_name, username=user, password=password)

        cursor = db.aql.execute(query, count=True)
        results = list(cursor)

        # Cap results (T-9-03)
        results = results[:_DB_QUERY_MAX_RESULTS]

        import orjson
        return orjson.dumps(results).decode()

    except Exception as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# mcp_query_tool
# ---------------------------------------------------------------------------


def _make_stdio_server_params(server_command: str) -> StdioServerParameters:
    """Parse a shell-style server command into stdio client parameters."""
    parts = shlex.split(server_command)
    if not parts:
        raise ValueError("MCP server command is empty")
    return StdioServerParameters(command=parts[0], args=parts[1:])


def _format_call_tool_result(result) -> str:
    """Return MCP tool results as plain text when possible, JSON otherwise."""
    text_chunks: list[str] = []
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if text:
            text_chunks.append(text)

    output = "\n".join(text_chunks).strip()
    if not output:
        output = json.dumps(result.model_dump(), indent=2, default=str)

    if len(output) > MAX_OUTPUT_SIZE:
        return output[:MAX_OUTPUT_SIZE] + "\n[truncated at 10KB]"
    return output


async def _call_mcp_tool(
    query: str,
    server_command: str,
    tool_name: str | None,
    arguments: dict | None,
    timeout: int,
) -> str:
    """Connect to an MCP server over stdio and call a tool."""
    params = _make_stdio_server_params(server_command)

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            if tool_name is None:
                tool_result = await session.list_tools()
                available_tools = [tool.name for tool in tool_result.tools]
                if len(available_tools) != 1:
                    joined = ", ".join(available_tools) or "(none)"
                    return (
                        "Error: tool_name is required when MCP server exposes "
                        f"multiple tools. Available: {joined}"
                    )
                tool_name = available_tools[0]

            tool_args = arguments if arguments is not None else {"query": query}
            result = await session.call_tool(
                tool_name,
                tool_args,
                read_timeout_seconds=timedelta(seconds=timeout),
            )
            if result.isError:
                return _format_call_tool_result(result)
            return _format_call_tool_result(result)


def mcp_query_tool(
    query: str,
    server_command: str,
    tool_name: str | None = None,
    arguments: dict | None = None,
    timeout: int = 30,
) -> str:
    """Call a tool on an MCP server over stdio.

    The server_command comes from adapter config (T-9-08: user-controlled).
    If tool_name is omitted, the call succeeds only when the server exposes
    exactly one tool; otherwise the caller must specify a tool explicitly.

    Args:
        query: Backward-compatible shorthand used when arguments is omitted.
        server_command: Shell command to start the MCP server process.
        tool_name: Optional MCP tool name to call.
        arguments: Optional MCP tool arguments dict.
        timeout: Maximum seconds to wait for response.

    Returns:
        Tool text output, JSON-serialized MCP result, or an error string.
    """
    try:
        return asyncio.run(
            _call_mcp_tool(query, server_command, tool_name, arguments, timeout)
        )
    except TimeoutError:
        return f"Error: MCP server timed out after {timeout}s"
    except Exception as exc:
        return f"Error: {exc}"


DB_QUERY_DEFINITION: dict = {
    "type": "function",
    "function": {
        "name": "db_query",
        "description": "Execute an AQL query against ArangoDB",
        "parameters": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "AQL query to execute",
                }
            },
        },
    },
}

MCP_QUERY_DEFINITION: dict = {
    "type": "function",
    "function": {
        "name": "mcp_query",
        "description": "Call a tool on an MCP server over stdio transport",
        "parameters": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Backward-compatible query string when the target tool accepts a single query argument",
                },
                "tool_name": {
                    "type": "string",
                    "description": "Specific MCP tool name to call",
                },
                "arguments": {
                    "type": "object",
                    "description": "Explicit arguments dict for the MCP tool call",
                },
            },
        },
    },
}


# Aggregate list of all fleet tool definitions
FLEET_TOOL_DEFINITIONS: list[dict] = [
    GH_TOOL_DEFINITION,
    GIT_CMD_TOOL_DEFINITION,
    SKILL_INVOKE_DEFINITION,
    DB_QUERY_DEFINITION,
    MCP_QUERY_DEFINITION,
]
