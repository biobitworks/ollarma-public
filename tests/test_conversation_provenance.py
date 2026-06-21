from __future__ import annotations

import json

import pytest

from ollarma.conversation_provenance import (
    ConversationProvenanceError,
    build_antigence_review_candidate,
    extract_tool_call_artifacts,
    not_implemented_turn,
    record_conversation_turn,
    stable_hash,
)


def _jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_conversation_store_writes_index_raw_and_redacted_refs(tmp_path) -> None:
    turn = record_conversation_turn(
        repo_root=tmp_path,
        surface="http_chat",
        prompt_text="summarize <local-path> with token=abcdefgh12345678",
        response_text="answer at <local-path>",
        model="qwen2.5-coder:7b",
        metadata={"messages": [{"role": "user", "content": "token=abcdefgh12345678"}]},
    )

    base = tmp_path / ".ollarma" / "conversations"
    assert (base / "events.jsonl").exists()
    assert (base / "index.jsonl").exists()
    assert turn.raw_ref == f".ollarma/conversations/raw/{turn.conversation_id}.jsonl"
    assert turn.redacted_ref == f".ollarma/conversations/redacted/{turn.conversation_id}.jsonl"
    assert turn.prompt_hash == stable_hash(
        "summarize <local-path> with token=abcdefgh12345678"
    )
    assert turn.response_hash == stable_hash("answer at <local-path>")
    assert turn.contains_secret is True

    raw = _jsonl(tmp_path / turn.raw_ref)[0]
    redacted = _jsonl(tmp_path / turn.redacted_ref)[0]
    assert "token=abcdefgh12345678" in raw["payload"]["prompt_text"]
    assert "<local-path>" in raw["payload"]["response_text"]
    assert "token=abcdefgh12345678" not in redacted["payload"]["prompt_text"]
    assert "<local-path>" not in redacted["payload"]["response_text"]
    assert "token=abcdefgh12345678" not in json.dumps(redacted["payload"]["metadata"])


def test_not_implemented_surface_is_explicit_and_do_not_train(tmp_path) -> None:
    turn = not_implemented_turn(
        repo_root=tmp_path,
        surface="swarm",
        reason="raw persona lane transcripts are not wired yet",
    )

    assert turn.surface == "swarm"
    assert turn.capture_status == "not_implemented"
    assert turn.training_eligibility == "do_not_train"
    assert turn.redaction_status == "not_applicable"


def test_tool_call_artifacts_hash_arguments_results_and_mutation() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "edit_file", "arguments": {"path": "a.py"}}}
            ],
        },
        {"role": "tool", "content": "[BLOCKED by guardrail: no writes]"},
    ]

    artifacts = extract_tool_call_artifacts(messages)

    assert len(artifacts) == 1
    assert artifacts[0].tool_name == "edit_file"
    assert artifacts[0].arguments_hash == stable_hash({"path": "a.py"})
    assert artifacts[0].result_hash == stable_hash("[BLOCKED by guardrail: no writes]")
    assert artifacts[0].guardrail_status == "blocked"
    assert artifacts[0].mutation_flag is True


def test_antigence_export_refuses_unlabeled_or_unredacted_records(tmp_path) -> None:
    turn = record_conversation_turn(
        repo_root=tmp_path,
        surface="http_chat",
        prompt_text="hello",
        response_text="world",
        model="qwen2.5-coder:7b",
    )

    with pytest.raises(ConversationProvenanceError, match="label"):
        build_antigence_review_candidate(
            turn=turn,
            redacted_prompt="hello",
            redacted_response="world",
            labels=(),
        )

    raw_turn = turn.model_copy(update={"redaction_status": "raw_local"})
    with pytest.raises(ConversationProvenanceError, match="unredacted"):
        build_antigence_review_candidate(
            turn=raw_turn,
            redacted_prompt="hello",
            redacted_response="world",
            labels=("safe_helpful",),
        )


def test_antigence_export_contains_hashes_and_source_hash(tmp_path) -> None:
    turn = record_conversation_turn(
        repo_root=tmp_path,
        surface="http_route",
        prompt_text="what evidence?",
        response_text="grounded answer",
        project="overwatch",
        model="qwen2.5-coder:7b",
        lane="kb_direct",
        reason_code="KB_DIRECT_ANSWER",
        bridge_event_refs=("evt-1",),
        kb_evidence_refs=("doc-1",),
        training_eligibility="training_candidate",
    )

    export = build_antigence_review_candidate(
        turn=turn,
        redacted_prompt="what evidence?",
        redacted_response="grounded answer",
        labels=("safe_helpful",),
        split="train_candidate",
    )

    assert export["source_repo"] == "ollarma"
    assert export["prompt_hash"] == stable_hash("what evidence?")
    assert export["response_hash"] == stable_hash("grounded answer")
    assert export["bridge_event_refs"] == ["evt-1"]
    assert export["route_evidence_refs"] == ["doc-1"]
    assert export["source_export_hash"].startswith("sha256:")
