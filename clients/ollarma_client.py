#!/usr/bin/env python3
"""ollarma_client — zero-dependency local-AI access for any project or agent.

WHY THIS EXISTS
    Any project, script, cron job, or sub-agent should be able to call the local
    AI stack (Ollama models + Ollarma routing/orchestration) "as if it were a
    low-level helper script" — without installing Ollarma's full Python stack and
    without being a registered Ollarma adapter. This file is that access layer.

    It uses ONLY the Python standard library (urllib + json). Copy it anywhere,
    import it, or run it as a CLI. No `pip install`, no venv coupling.

THE SUBSTRATE
    Everything routes over local HTTP:
      - Ollama   http://127.0.0.1:11434   raw local LLM inference
      - Ollarma  http://127.0.0.1:8484    governed routing, KB-grounded answers
    Override with env vars OLLAMA_URL / OLLARMA_URL if your ports differ.

ACCESS TIERS (lowest → highest governance)
    ollama(prompt, model)        raw model call, no governance, no KB.        no adapter needed
    chat(prompt)                 Ollarma unscoped local helper (/chat).       no adapter needed
    route(project, prompt)       Ollarma project-grounded answer (/route).    project MUST have an adapter
    ask(prompt, project=...)     smart entry: route→chat→ollama auto-degrade. always answers if any lane is up

QUICK START (from any python script)
    import sys; sys.path.insert(0, "<repo>/clients")
    import ollarma_client as oc
    print(oc.ask("Summarize what Watchtower does.", project="Watchtower"))
    print(oc.ollama("Write a haiku about caches."))           # raw model
    print(oc.health())                                        # which services are up

QUICK START (CLI — also installed on PATH as ollarma_client.py)
    ollarma_client.py health
    ollarma_client.py ask "what is Ollarma?" --project Ollarma
    ollarma_client.py ollama "reply PONG" --model qwen3:1.7b
    ollarma_client.py models
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLARMA_URL = os.environ.get("OLLARMA_URL", "http://127.0.0.1:8484").rstrip("/")
ANTIGENCE_URL = os.environ.get("ANTIGENCE_URL", "http://127.0.0.1:5055").rstrip("/")

# Smallest always-resident model — safe default under memory pressure.
DEFAULT_MODEL = os.environ.get("OLLARMA_DEFAULT_MODEL", "qwen3:1.7b")

# Lanes that mean "Ollarma answered locally" vs "Ollarma punted to cloud/human".
_LOCAL_LANES = {"kb_direct", "grounded_local_synthesis", "grounded_local"}
_ESCALATION_LANE = "frontier_or_human"


class LocalAIError(RuntimeError):
    """Raised when a requested local service is unreachable or errors."""


# --------------------------------------------------------------------------- #
# transport (stdlib only)
# --------------------------------------------------------------------------- #
def _request(method: str, url: str, payload: dict | None = None, timeout: float = 120.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a JSON body
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        try:
            return json.loads(raw)
        except Exception:
            raise LocalAIError(f"{method} {url} -> HTTP {exc.code}: {raw[:200]}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise LocalAIError(f"{method} {url} unreachable: {exc}") from exc
    try:
        return json.loads(raw)
    except Exception as exc:
        raise LocalAIError(f"{method} {url} -> non-JSON response: {raw[:200]}") from exc


def _get(url: str, timeout: float = 10.0) -> dict:
    return _request("GET", url, None, timeout)


def _post(url: str, payload: dict, timeout: float = 120.0) -> dict:
    return _request("POST", url, payload, timeout)


# --------------------------------------------------------------------------- #
# tier 1 — raw Ollama (no governance, no adapter needed)
# --------------------------------------------------------------------------- #
def ollama(prompt: str, model: str = DEFAULT_MODEL, system: str | None = None,
           timeout: float = 180.0) -> str:
    """Raw local LLM call straight to Ollama. Returns the completion text."""
    payload = {"model": model, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    out = _post(f"{OLLAMA_URL}/api/generate", payload, timeout=timeout)
    if "error" in out:
        raise LocalAIError(f"ollama error: {out['error']}")
    return out.get("response", "")


def list_models() -> list[str]:
    """Names of models currently pulled in Ollama."""
    out = _get(f"{OLLAMA_URL}/api/tags")
    return sorted(m.get("name", "") for m in out.get("models", []))


# --------------------------------------------------------------------------- #
# tier 2 — Ollarma unscoped local helper (no adapter needed)
# --------------------------------------------------------------------------- #
def chat(prompt: str, model: str | None = None, timeout: float = 180.0) -> str:
    """Ollarma single-turn local helper (/chat). Returns the response text."""
    return chat_full(prompt, model=model, timeout=timeout).get("response", "")


def chat_full(prompt: str, model: str | None = None, timeout: float = 180.0) -> dict:
    """Ollarma /chat raw response: {response, model, status, reason_code, ...}."""
    payload: dict = {"message": prompt}
    if model:
        payload["model"] = model
    return _post(f"{OLLARMA_URL}/chat", payload, timeout=timeout)


# --------------------------------------------------------------------------- #
# tier 3 — Ollarma project-grounded routing (adapter required)
# --------------------------------------------------------------------------- #
def route(project: str, prompt: str, model: str | None = None, timeout: float = 240.0) -> dict:
    """Ollarma /route: KB-grounded, governed answer for a registered project.

    Returns the full RouteResult dict. Key fields:
      final_response, lane, reason_code, kb_status, selected_model, next_action.
    A `lane == "frontier_or_human"` result means Ollarma could not answer locally
    (e.g. no adapter, stale KB, swap pressure) and is deferring to cloud/human.
    """
    payload: dict = {"project": project, "prompt": prompt}
    if model:
        payload["model"] = model
    return _post(f"{OLLARMA_URL}/route", payload, timeout=timeout)


def kb_search(project: str, query: str, limit: int = 5) -> dict:
    """Bounded read-only KB hits for a project query (no LLM)."""
    return _post(f"{OLLARMA_URL}/kb/search", {"project": project, "query": query, "limit": limit})


def kb_status(project: str) -> dict:
    """KB freshness/build status for a project."""
    return _get(f"{OLLARMA_URL}/kb/status/{project}")


# --------------------------------------------------------------------------- #
# smart entry point — always returns a local answer if any lane is up
# --------------------------------------------------------------------------- #
def ask(prompt: str, project: str | None = None, model: str | None = None,
        timeout: float = 240.0) -> str:
    """Get a local answer with automatic degradation.

    Order: project-grounded route (if `project` given and it answers locally)
           → Ollarma unscoped /chat → raw Ollama. Raises LocalAIError only if
    every local lane is unreachable. This is the "just answer me locally,
    I don't care about the plumbing" call — the cheap default before any cloud.
    """
    # Tier 3: project-grounded, but only accept a genuinely-local lane.
    if project:
        try:
            r = route(project, prompt, model=model, timeout=timeout)
            if r.get("lane") in _LOCAL_LANES and r.get("final_response"):
                return r["final_response"]
            # lane == frontier_or_human (stale KB / no adapter / swap) → fall through
        except LocalAIError:
            pass
    # Tier 2: Ollarma unscoped helper.
    try:
        resp = chat(prompt, model=model, timeout=timeout)
        if resp:
            return resp
    except LocalAIError:
        pass
    # Tier 1: raw Ollama as the last local resort.
    return ollama(prompt, model=model or DEFAULT_MODEL, timeout=timeout)


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #
def health() -> dict:
    """Liveness of each local service. {service: bool} plus a `detail` map."""
    status: dict = {"ollama": False, "ollarma": False, "antigence": False, "detail": {}}
    try:
        tags = _get(f"{OLLAMA_URL}/api/tags", timeout=5)
        status["ollama"] = True
        status["detail"]["ollama_models"] = sorted(m.get("name", "") for m in tags.get("models", []))
    except LocalAIError as exc:
        status["detail"]["ollama"] = str(exc)[:160]
    try:
        h = _get(f"{OLLARMA_URL}/health", timeout=5)
        status["ollarma"] = True
        status["detail"]["ollarma"] = h.get("status", "up")
    except LocalAIError as exc:
        status["detail"]["ollarma"] = str(exc)[:160]
    try:
        # Antigence dashboard has no /health; root returns 200 HTML.
        urllib.request.urlopen(f"{ANTIGENCE_URL}/", timeout=5).read(64)
        status["antigence"] = True
    except Exception as exc:  # noqa: BLE001
        status["detail"]["antigence"] = str(exc)[:160]
    return status


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="ollarma_client", description="Local-AI access for any project/agent.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="check which local services are up")
    sub.add_parser("models", help="list pulled Ollama models")

    a = sub.add_parser("ask", help="smart local answer (route→chat→ollama)")
    a.add_argument("prompt")
    a.add_argument("--project", default=None)
    a.add_argument("--model", default=None)

    c = sub.add_parser("chat", help="Ollarma unscoped local helper")
    c.add_argument("prompt")
    c.add_argument("--model", default=None)

    r = sub.add_parser("route", help="Ollarma project-grounded routing")
    r.add_argument("project")
    r.add_argument("prompt")
    r.add_argument("--model", default=None)

    o = sub.add_parser("ollama", help="raw Ollama model call")
    o.add_argument("prompt")
    o.add_argument("--model", default=DEFAULT_MODEL)
    o.add_argument("--system", default=None)

    args = p.parse_args(argv)
    try:
        if args.cmd == "health":
            print(json.dumps(health(), indent=2))
        elif args.cmd == "models":
            print("\n".join(list_models()))
        elif args.cmd == "ask":
            print(ask(args.prompt, project=args.project, model=args.model))
        elif args.cmd == "chat":
            print(chat(args.prompt, model=args.model))
        elif args.cmd == "route":
            print(json.dumps(route(args.project, args.prompt, model=args.model), indent=2))
        elif args.cmd == "ollama":
            print(ollama(args.prompt, model=args.model, system=args.system))
    except LocalAIError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
