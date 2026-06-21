"""conftest.py -- shared fixtures for tests/swarm/.

Provides:

* ``stub_ollama_client`` -- a factory fixture that returns a duck-typed
  Ollama client. The client exposes ``generate(model, prompt, format,
  options, stream)`` and returns a dict shaped like the real Ollama SDK
  response. Parameters:
    - ``responses``: list of canned ``response`` strings (cycled). If
      ``None``, a generic valid StanceResponse JSON is synthesized for each
      call from the persona_id embedded in the prompt.
    - ``wall_ms_per_response``: either a constant int/float (every call
      sleeps that long) or a callable ``(call_idx) -> ms`` for per-call
      schedules. Used by the thermal test.
    - ``invalid_json_every_n``: if set, every Nth *distinct prompt* (1-indexed)
      always returns garbage JSON -- so the engine's retry of the same prompt
      ALSO fails and the persona-round is quarantined. Retry-counts the same
      prompt as one logical "call" for the modulus, exercising the actual
      retry-then-quarantine path rather than retry-succeeds-on-second-attempt.

  The stub does NOT call ``time.sleep`` -- it advances a *virtual* clock
  via ``time.perf_counter`` monkeypatching done in the engine smoke / thermal
  tests. The ``wall_ms_per_response`` value is therefore the wall the engine
  *observes*, not real elapsed time.

* ``tmp_run_dir`` -- thin wrapper over pytest's ``tmp_path`` that returns
  a freshly-created directory the engine can use as ``run_dir=``. Kept
  separate from ``tmp_path`` so future fixtures can extend it (e.g., add
  a sentinel marker file) without breaking the test contract.

PROMPT-OLLARMA-SWARM-001 task T7/T8 (Phase 70 plan 70-03 wave 3).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_persona_id(prompt: str) -> str:
    """Pull persona_id back out of the engine-built prompt.

    The engine appends ``Set persona_id='<id>' and round_idx=<r>.`` near the
    end of every prompt; this helper recovers ``<id>`` so a synthesized
    response can echo the right persona_id (the StanceResponse schema has
    no required relationship between ``persona_id`` in the response and
    the prompt, but echoing it keeps the test data realistic).
    """
    marker = "persona_id="
    i = prompt.rfind(marker)
    if i < 0:
        return "unknown"
    rest = prompt[i + len(marker) :]
    # Format: 'analyst_00' and round_idx=...
    # Strip the leading quote and find the next quote.
    if rest.startswith("'"):
        end = rest.find("'", 1)
        return rest[1:end] if end > 0 else "unknown"
    return rest.split()[0].strip("'\"")


def _extract_round_idx(prompt: str) -> int:
    """Pull round_idx back out of the engine-built prompt."""
    marker = "round_idx="
    i = prompt.rfind(marker)
    if i < 0:
        return 0
    rest = prompt[i + len(marker) :]
    digits: list[str] = []
    for ch in rest:
        if ch.isdigit():
            digits.append(ch)
        else:
            break
    return int("".join(digits)) if digits else 0


def _synth_valid_response(prompt: str) -> str:
    """Synthesize a valid StanceResponse JSON for a given engine prompt."""
    pid = _extract_persona_id(prompt)
    rnd = _extract_round_idx(prompt)
    # Vary stance pseudo-randomly by hashing persona_id+round so different
    # personas don't all collapse to the same stance (which would make
    # round-over-round JSD trivially zero).
    stances = (
        "strongly_disagree",
        "disagree",
        "neutral",
        "agree",
        "strongly_agree",
        "refuse_to_engage",
    )
    h = hash((pid, rnd))
    stance = stances[h % len(stances)]
    return json.dumps(
        {
            "persona_id": pid,
            "round_idx": rnd,
            "stance": stance,
            "confidence": 0.7,
            "rationale": "stub rationale",
            "post": f"stub post from {pid} in round {rnd}",
        }
    )


# ---------------------------------------------------------------------------
# Stub client
# ---------------------------------------------------------------------------

class _StubOllamaClient:
    """Duck-typed stand-in for ``ollama.Client`` used by tests.

    Captures every call so tests can assert on call counts / args. The
    ``generate`` signature mirrors what ``engine._generate_with_retry`` calls.

    Virtual-clock model: the engine measures wall time via
    ``time.perf_counter()`` (called once before and once after
    ``client.generate``). Tests monkeypatch ``ollarma.swarm.engine.time``
    to use this stub's ``virtual_clock_seconds`` value. Every call to
    ``generate`` advances the virtual clock by ``wall_ms_per_response``
    milliseconds, so the engine sees a wall of exactly that many ms with
    zero real elapsed time.
    """

    def __init__(
        self,
        *,
        responses: list[str] | None,
        wall_ms_per_response: int | float | Callable[[int], int | float],
        invalid_json_every_n: int | None,
    ) -> None:
        self._responses = responses
        self._wall = wall_ms_per_response
        self._invalid_every = invalid_json_every_n
        self._call_idx = 0
        # Distinct-prompt index so retries of the same prompt share a modulus
        # bucket (i.e., retry of an invalid prompt is also invalid -> the
        # engine actually quarantines, instead of recovering on retry).
        self._distinct_prompts: dict[str, int] = {}
        self.calls: list[dict[str, Any]] = []
        # Virtual clock in seconds. Tests can read this via
        # ``client.virtual_clock_seconds`` (e.g., as the perf_counter side).
        self.virtual_clock_seconds: float = 0.0

    # ------------------------------------------------------------------
    # Public stub method
    # ------------------------------------------------------------------

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        options: dict[str, Any] | None = None,
        format: str | None = None,  # noqa: A002 -- match SDK kwarg name
        stream: bool = False,
    ) -> dict[str, Any]:
        idx = self._call_idx
        self._call_idx += 1

        # Compute the wall this call should *appear* to take (engine reads
        # this via time.perf_counter() which the tests monkeypatch).
        if callable(self._wall):
            wall_ms = float(self._wall(idx))
        else:
            wall_ms = float(self._wall)

        # Advance the virtual clock so the engine's perf_counter delta
        # equals exactly this wall_ms (after the test has patched perf_counter
        # to read ``self.virtual_clock_seconds``).
        self.virtual_clock_seconds += wall_ms / 1000.0

        # Decide payload. Bucket by distinct prompt so retries share the
        # modulus (retry of an invalid bucket -> still invalid -> quarantine).
        if prompt not in self._distinct_prompts:
            self._distinct_prompts[prompt] = len(self._distinct_prompts)
        prompt_idx = self._distinct_prompts[prompt]
        invalid = (
            self._invalid_every is not None
            and (prompt_idx + 1) % self._invalid_every == 0
        )
        if invalid:
            text = "{not valid json at all"
        elif self._responses is not None:
            text = self._responses[idx % len(self._responses)]
        else:
            text = _synth_valid_response(prompt)

        self.calls.append(
            {
                "idx": idx,
                "model": model,
                "prompt": prompt,
                "options": options,
                "format": format,
                "stream": stream,
                "wall_ms": wall_ms,
                "response": text,
            }
        )

        return {
            "model": model,
            "response": text,
            "done": True,
            "total_duration": int(wall_ms * 1_000_000),
            "eval_count": 42,
            "eval_duration": int(wall_ms * 900_000),
        }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_ollama_client() -> Callable[..., _StubOllamaClient]:
    """Factory fixture: build a stub client with the desired behaviour.

    Usage::

        def test_x(stub_ollama_client):
            client = stub_ollama_client(wall_ms_per_response=10)
            ... pass client to run_simulation(ollama_client=client) ...
    """

    def _build(
        *,
        responses: list[str] | None = None,
        wall_ms_per_response: int | float | Callable[[int], int | float] = 10,
        invalid_json_every_n: int | None = None,
    ) -> _StubOllamaClient:
        return _StubOllamaClient(
            responses=responses,
            wall_ms_per_response=wall_ms_per_response,
            invalid_json_every_n=invalid_json_every_n,
        )

    return _build


@pytest.fixture
def tmp_run_dir(tmp_path: Path) -> Path:
    """Per-test run_dir for the engine. Wraps pytest's tmp_path."""
    d = tmp_path / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d
