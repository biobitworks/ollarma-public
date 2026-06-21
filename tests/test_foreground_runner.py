"""Offline tests for the foreground Ollama runner."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ollarma.foreground_runner import (
    ensure_warm,
    generate_once,
    main,
    run_jsonl_prompts,
)


class FakeCurl:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, timeout_s))
        data = self.responses.pop(0)
        return subprocess.CompletedProcess(args, 0, json.dumps(data), "")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def test_ensure_warm_skips_generate_when_model_is_resident() -> None:
    fake = FakeCurl([{"models": [{"model": "qwen3.5:2b"}]}])

    status = ensure_warm("qwen3.5:2b", curl_runner=fake)

    assert status.resident_before is True
    assert status.warmup_called is False
    assert status.status == "already_resident"
    assert len(fake.calls) == 1
    assert "/api/ps" in fake.calls[0][0][-1]


def test_ensure_warm_calls_bounded_generate_when_model_is_not_resident() -> None:
    fake = FakeCurl([{"models": []}, {"response": "ok", "done": True}])

    status = ensure_warm("qwen3.5:2b", timeout_s=12.0, curl_runner=fake)

    assert status.resident_before is False
    assert status.warmup_called is True
    assert len(fake.calls) == 2
    generate_args = fake.calls[1][0]
    assert "/api/generate" in generate_args[-1]
    assert "Connection: close" in generate_args
    assert "--max-time" in generate_args
    assert "12.0" in generate_args


def test_generate_once_uses_connection_close_and_nonstreaming_payload() -> None:
    fake = FakeCurl([{"response": "{\"label\":\"PASS\"}", "done": True}])

    generate_once(
        model="qwen3.5:2b",
        prompt="grade this",
        timeout_s=7.0,
        response_format="json",
        curl_runner=fake,
    )

    args = fake.calls[0][0]
    payload = json.loads(args[args.index("-d") + 1])
    assert "Connection: close" in args
    assert "--max-time" in args
    assert "7.0" in args
    assert payload["stream"] is False
    assert payload["format"] == "json"


def test_run_jsonl_prompts_resumes_completed_rows(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    _write_jsonl(
        input_path,
        [
            {"id": "a", "prompt": "already done"},
            {"id": "b", "prompt": "needs grading"},
        ],
    )
    _write_jsonl(output_path, [{"id": "a", "status": "ok", "response": "old"}])
    fake = FakeCurl(
        [
            {"models": [{"model": "qwen3.5:2b"}]},
            {"response": "{\"label\":\"BLOCK\"}", "done": True},
        ]
    )

    result = run_jsonl_prompts(
        input_path=input_path,
        output_path=output_path,
        model="qwen3.5:2b",
        response_format="json",
        curl_runner=fake,
    )

    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert result["skipped"] == 1
    assert result["written"] == 1
    assert [row["id"] for row in rows] == ["a", "b"]
    assert rows[-1]["status"] == "ok"
    assert rows[-1]["response"] == "{\"label\":\"BLOCK\"}"


def test_cli_keep_alive_parser_preserves_indefinite_numeric_value(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    _write_jsonl(input_path, [{"id": "a", "prompt": "hello"}])
    captured: dict[str, object] = {}

    def fake_run_jsonl_prompts(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "model": kwargs["model"]}

    monkeypatch.setattr("ollarma.foreground_runner.run_jsonl_prompts", fake_run_jsonl_prompts)

    code = main(
        [
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--model",
            "nomic-embed-text",
            "--keep-alive",
            "-1",
        ]
    )

    assert code == 0
    assert captured["keep_alive"] == -1
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
