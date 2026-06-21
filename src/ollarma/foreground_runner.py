"""Foreground, resumable Ollama prompt runner.

This is small operational tooling for jobs such as relevance grading where the
failure mode is worse in detached/background runs: a killed prior process can
leave Ollama slots occupied by stale sockets, and a new long-lived client may
queue silently. The runner keeps the contract simple:

* health-probe ``/api/ps`` first;
* skip warmup when the requested model is already resident;
* call ``/api/generate`` once per prompt with bounded ``curl``;
* send ``Connection: close`` on every request;
* append one JSONL result per item so interrupted runs resume cleanly.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_KEEP_ALIVE: str | int = "5m"


class ForegroundRunnerError(RuntimeError):
    """Raised when a foreground runner operation fails."""


@dataclass(frozen=True)
class WarmStatus:
    """Result of ``ensure_warm``."""

    model: str
    resident_before: bool | None
    warmup_called: bool
    status: str


CurlRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


def _model_aliases(name: str) -> set[str]:
    aliases = {name}
    if ":" not in name:
        aliases.add(f"{name}:latest")
    if name.endswith(":latest"):
        aliases.add(name.removesuffix(":latest"))
    return aliases


def _models_match(left: str, right: str) -> bool:
    return bool(_model_aliases(left).intersection(_model_aliases(right)))


def _default_curl_runner(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def _json_curl_args(
    *,
    method: str,
    url: str,
    timeout_s: float,
    payload: Mapping[str, Any] | None = None,
) -> list[str]:
    args = [
        "curl",
        "-fsS",
        "--max-time",
        str(timeout_s),
        "-H",
        "Connection: close",
    ]
    if method.upper() == "POST":
        args.extend(["-X", "POST", "-H", "Content-Type: application/json"])
        args.extend(["-d", json.dumps(payload or {}, separators=(",", ":"), sort_keys=True)])
    args.append(url)
    return args


def _curl_json(
    *,
    method: str,
    url: str,
    timeout_s: float,
    payload: Mapping[str, Any] | None = None,
    curl_runner: CurlRunner = _default_curl_runner,
) -> dict[str, Any]:
    args = _json_curl_args(method=method, url=url, timeout_s=timeout_s, payload=payload)
    completed = curl_runner(args, timeout_s + 2.0)
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise ForegroundRunnerError(f"curl failed rc={completed.returncode}: {stderr[:300]}")
    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ForegroundRunnerError(f"response was not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ForegroundRunnerError("response JSON was not an object")
    return data


def resident_models(
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    timeout_s: float = 5.0,
    curl_runner: CurlRunner = _default_curl_runner,
) -> tuple[str, ...] | None:
    """Return model names from ``/api/ps`` or ``None`` when health probe fails."""

    try:
        data = _curl_json(
            method="GET",
            url=f"{ollama_url.rstrip('/')}/api/ps",
            timeout_s=timeout_s,
            curl_runner=curl_runner,
        )
    except ForegroundRunnerError:
        return None
    models = data.get("models", [])
    if not isinstance(models, list):
        return ()
    names: list[str] = []
    for raw in models:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("model") or raw.get("name") or "").strip()
        if name:
            names.append(name)
    return tuple(names)


def is_model_resident(
    model: str,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    timeout_s: float = 5.0,
    curl_runner: CurlRunner = _default_curl_runner,
) -> bool | None:
    """Return whether *model* is resident; ``None`` means the probe failed."""

    names = resident_models(ollama_url=ollama_url, timeout_s=timeout_s, curl_runner=curl_runner)
    if names is None:
        return None
    return any(_models_match(model, name) for name in names)


def generate_once(
    *,
    model: str,
    prompt: str,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    keep_alive: str | int = DEFAULT_KEEP_ALIVE,
    options: Mapping[str, Any] | None = None,
    response_format: str | None = None,
    curl_runner: CurlRunner = _default_curl_runner,
) -> dict[str, Any]:
    """Call ``/api/generate`` once using bounded foreground curl."""

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": keep_alive,
    }
    if options:
        payload["options"] = dict(options)
    if response_format:
        payload["format"] = response_format
    return _curl_json(
        method="POST",
        url=f"{ollama_url.rstrip('/')}/api/generate",
        timeout_s=timeout_s,
        payload=payload,
        curl_runner=curl_runner,
    )


def ensure_warm(
    model: str,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    timeout_s: float = 30.0,
    keep_alive: str | int = DEFAULT_KEEP_ALIVE,
    curl_runner: CurlRunner = _default_curl_runner,
) -> WarmStatus:
    """Warm *model* unless it is already resident according to ``/api/ps``."""

    resident = is_model_resident(
        model,
        ollama_url=ollama_url,
        timeout_s=min(timeout_s, 5.0),
        curl_runner=curl_runner,
    )
    if resident is True:
        return WarmStatus(
            model=model,
            resident_before=True,
            warmup_called=False,
            status="already_resident",
        )

    generate_once(
        model=model,
        prompt="ollarma foreground runner warmup",
        ollama_url=ollama_url,
        timeout_s=timeout_s,
        keep_alive=keep_alive,
        options={"num_predict": 1, "temperature": 0},
        curl_runner=curl_runner,
    )
    return WarmStatus(
        model=model,
        resident_before=resident,
        warmup_called=True,
        status="warmed",
    )


def _iter_jsonl(path: pathlib.Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ForegroundRunnerError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
            if not isinstance(value, dict):
                raise ForegroundRunnerError(f"{path}:{line_number}: JSONL row must be an object")
            yield value


def completed_ids(output_path: pathlib.Path, *, id_field: str = "id") -> set[str]:
    """Return successfully completed IDs from an existing output JSONL."""

    if not output_path.exists():
        return set()
    done: set[str] = set()
    for row in _iter_jsonl(output_path):
        if row.get("status") == "ok" and row.get(id_field) is not None:
            done.add(str(row[id_field]))
    return done


def run_jsonl_prompts(
    *,
    input_path: pathlib.Path,
    output_path: pathlib.Path,
    model: str,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    keep_alive: str | int = DEFAULT_KEEP_ALIVE,
    id_field: str = "id",
    prompt_field: str = "prompt",
    response_format: str | None = None,
    continue_on_error: bool = True,
    curl_runner: CurlRunner = _default_curl_runner,
) -> dict[str, Any]:
    """Run prompt rows from JSONL, appending resumable results to *output_path*."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    warm_status = ensure_warm(
        model,
        ollama_url=ollama_url,
        timeout_s=timeout_s,
        keep_alive=keep_alive,
        curl_runner=curl_runner,
    )
    seen = completed_ids(output_path, id_field=id_field)
    written = 0
    skipped = 0
    failed = 0
    with output_path.open("a", encoding="utf-8") as output:
        for row in _iter_jsonl(input_path):
            item_id_raw = row.get(id_field)
            if item_id_raw is None:
                raise ForegroundRunnerError(f"input row missing id field {id_field!r}")
            item_id = str(item_id_raw)
            if item_id in seen:
                skipped += 1
                continue
            prompt = str(row.get(prompt_field) or "").strip()
            if not prompt:
                raise ForegroundRunnerError(f"input row {item_id!r} missing prompt field {prompt_field!r}")
            started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                data = generate_once(
                    model=model,
                    prompt=prompt,
                    ollama_url=ollama_url,
                    timeout_s=timeout_s,
                    keep_alive=keep_alive,
                    response_format=response_format,
                    curl_runner=curl_runner,
                )
                record = {
                    id_field: item_id,
                    "status": "ok",
                    "model": model,
                    "started_at": started_at,
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "response": data.get("response", ""),
                    "raw": data,
                }
                seen.add(item_id)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                record = {
                    id_field: item_id,
                    "status": "error",
                    "model": model,
                    "started_at": started_at,
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "error": str(exc),
                }
                if not continue_on_error:
                    output.write(json.dumps(record, sort_keys=True) + "\n")
                    output.flush()
                    raise
            output.write(json.dumps(record, sort_keys=True) + "\n")
            output.flush()
            written += 1
    return {
        "status": "ok" if failed == 0 else "degraded",
        "model": model,
        "warm_status": warm_status.__dict__,
        "written": written,
        "skipped": skipped,
        "failed": failed,
        "output_path": str(output_path),
    }


def _parse_keep_alive(value: str) -> str | int:
    stripped = value.strip()
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a foreground resumable Ollama JSONL job.")
    parser.add_argument("--input", required=True, type=pathlib.Path, help="Input JSONL with id/prompt fields.")
    parser.add_argument("--output", required=True, type=pathlib.Path, help="Append-only output JSONL.")
    parser.add_argument("--model", required=True, help="Ollama model to use.")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--keep-alive", default=DEFAULT_KEEP_ALIVE)
    parser.add_argument("--id-field", default="id")
    parser.add_argument("--prompt-field", default="prompt")
    parser.add_argument("--format", default=None, help="Optional Ollama format, e.g. json.")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args(argv)
    result = run_jsonl_prompts(
        input_path=args.input,
        output_path=args.output,
        model=args.model,
        ollama_url=args.ollama_url,
        timeout_s=args.timeout_s,
        keep_alive=_parse_keep_alive(str(args.keep_alive)),
        id_field=args.id_field,
        prompt_field=args.prompt_field,
        response_format=args.format,
        continue_on_error=not args.fail_fast,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
