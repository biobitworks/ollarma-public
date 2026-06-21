"""engine.py -- Predictive persona swarm round-loop runtime.

Implements PROMPT-OLLARMA-SWARM-001 task T4: ``run_simulation()``, the public
synchronous round-loop API for the deliberative swarm.

Sequential serving only. Single GPU. Never preloads or holds a second model
variant in memory. Quarantine policy: 1 retry on pydantic ``ValidationError``
or Ollama exception, then quarantine and continue. Aggregates exclude
quarantined responses.

Per-run on-disk layout (under ``run_dir``)::

    run_dir/
    |-- r0/
    |   |-- analyst_00.json          # StanceResponse (one per accepted persona)
    |   `-- ...
    |-- r1/...
    |-- round_0.json                 # RoundArtifact (per-round aggregate)
    |-- round_1.json
    |-- ...
    |-- summary.json                 # SimulationSummary (final)
    |-- quarantine.jsonl             # append-only quarantine log
    `-- throttle.jsonl               # append-only throttle decisions

All timing fields use ``time.perf_counter()`` for monotonic wall measurement.
``temperature=0.0``, ``seed=42``, ``num_ctx=1024``, ``num_predict=200``,
``num_gpu=999``, ``format="json"`` are pinned per CLAUDE.md and the PROMPT
GPU-thermal hygiene contract.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import orjson
from pydantic import ValidationError

from ollarma.swarm._runtime_contract import (
    DEFAULT_OLLAMA_OPTIONS as _SHARED_DEFAULT_OLLAMA_OPTIONS,
)
from ollarma.swarm.aggregator import (
    dissent_clusters,
    jensen_shannon_divergence,
    stance_distribution,
)
from ollarma.swarm.persona_bank import PersonaBank, PersonaPrompt
from ollarma.swarm.schemas import (
    RoundArtifact,
    SimulationSummary,
    StanceResponse,
)
from ollarma.swarm.throttle import AdaptiveSleeper


# ---------------------------------------------------------------------------
# Defaults pinned by CLAUDE.md and PROMPT lines 89-97 / 134-135.
#
# DEFAULT_OLLAMA_OPTIONS is sourced from ``ollarma.swarm._runtime_contract``
# (the shared single source of truth for Phase 66 + Phase 70 swarms). The
# alias here preserves the public name for any callers that import it from
# ``ollarma.swarm.engine`` directly.
# ---------------------------------------------------------------------------

DEFAULT_MODEL: Final[str] = "qwen2.5-coder:7b"
DEFAULT_OLLAMA_OPTIONS: Final[dict[str, Any]] = _SHARED_DEFAULT_OLLAMA_OPTIONS
"""Pinned options for every persona-round Ollama call.

``format="json"`` is passed positionally to ``client.generate(...)`` rather
than via this dict so the call site reads the same as ``executor.py`` and
``service.py`` elsewhere in the project. ``temperature=0.0`` + ``seed=42``
guarantees byte-identical output for byte-identical prompts on Ollama 0.19+
with the MLX backend. The values live in ``_runtime_contract.py`` so the
execution-lane swarm (Phase 66+) imports the same dict.
"""

MAX_RETRIES: Final[int] = 1
"""1 retry on parse failure / Ollama exception, then quarantine."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_simulation(
    scenario: str,
    scenario_id: str,
    *,
    n_personas: int = 50,
    n_rounds: int = 5,
    model: str = DEFAULT_MODEL,
    seed: int = 42,
    run_dir: Path | None = None,
    ollama_client: Any | None = None,
) -> SimulationSummary:
    """Run a synchronous N-persona x M-round predictive swarm simulation.

    This is the H0 contract from PROMPT-OLLARMA-SWARM-001 line 33:

      > Ollarma exposes a ``swarm.run_simulation(scenario, personas, rounds,
      > stance_schema)`` Python API that spawns N=50 persona-instantiated
      > ``qwen2.5-coder:7b`` agents on a single local Ollama daemon, runs
      > M=5 synchronous rounds with each agent observing the prior round's
      > aggregated stance and posting one short response per round...

    Args:
        scenario: The seed claim / prompt under audience-test (free-form
            string; will be embedded verbatim in every persona prompt).
        scenario_id: Short filesystem-safe identifier for this scenario
            (e.g., ``"audience_1927_med"``); used in the auto-generated
            ``run_dir`` name and recorded in the summary.
        n_personas: N -- number of persona instances drawn from PersonaBank.
            Defaults to 50 per the H0 contract.
        n_rounds: M -- number of deliberation rounds. Defaults to 5.
        model: Ollama model tag. Defaults to ``qwen2.5-coder:7b``. The model
            MUST already be pulled and resident; this engine never calls
            ``ollama.pull()`` and never holds a second model variant.
        seed: Persona-bank rng seed; same seed reproduces the same persona draw.
            Note: Ollama generation seed is pinned at 42 in
            ``DEFAULT_OLLAMA_OPTIONS`` independently of this argument so the
            per-prompt model output is reproducible across runs even if the
            persona draw varies.
        run_dir: Optional explicit run directory. If ``None``, a fresh
            ``runs/swarm_<UTC>_<scenario_id>/`` directory is created under
            the current working directory.
        ollama_client: Optional pre-built ``ollama.Client`` instance. If
            ``None``, one is constructed lazily on first call. Allows tests
            to inject a mock without monkeypatching the ``ollama`` module.

    Returns:
        SimulationSummary: The full per-simulation summary, also persisted
        to ``<run_dir>/summary.json``. ``jsd_round_over_round[0]`` is fixed
        at 0.0; ``jsd_round_over_round[r]`` for r >= 1 is the JSD between
        the round (r-1) and round r stance distributions.
    """
    # 1. Materialise run_dir.
    if run_dir is None:
        utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = Path("runs") / f"swarm_{utc}_{scenario_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_dir.name

    # 2. Draw personas.
    personas = PersonaBank().draw(n_personas, seed)

    # 3. Lazily build an Ollama client if the caller didn't pass one.
    client = ollama_client if ollama_client is not None else _make_default_client()

    # 4. Per-run state.
    sleeper = AdaptiveSleeper(run_dir=run_dir)
    quarantine_path = run_dir / "quarantine.jsonl"
    rounds_out: list[RoundArtifact] = []
    # persona_id -> last accepted post text (round r-1's post, fed into round r)
    last_post_by_persona: dict[str, str] = {}
    total_quarantined: int = 0
    total_attempted: int = 0
    response_idx: int = 0

    sim_t0 = time.perf_counter()

    # 5. Main loop -- sequential by contract (single GPU; PROMPT MESI gate 5).
    for r in range(n_rounds):
        round_dir = run_dir / f"r{r}"
        round_dir.mkdir(parents=True, exist_ok=True)

        prior_distribution: dict[str, float] | None = (
            rounds_out[r - 1].stance_distribution if r > 0 else None
        )

        accepted: list[StanceResponse] = []
        quarantined_this_round: int = 0

        for persona in personas:
            total_attempted += 1
            prompt = _build_prompt(
                persona=persona,
                scenario=scenario,
                round_idx=r,
                prior_distribution=prior_distribution,
                prior_post=last_post_by_persona.get(persona.persona_id),
            )

            t_call = time.perf_counter()
            response = _generate_with_retry(
                client=client,
                model=model,
                prompt=prompt,
                persona=persona,
                round_idx=r,
                quarantine_path=quarantine_path,
            )
            wall_ms = (time.perf_counter() - t_call) * 1000.0

            # Throttle: record wall, sleep if anomaly above smoothed median.
            sleep_ms = sleeper.record(response_idx=response_idx, wall_ms=wall_ms)
            response_idx += 1
            if sleep_ms > 0:
                time.sleep(sleep_ms / 1000.0)

            if response is None:
                # Quarantined after retry. Engine continues; aggregates exclude.
                quarantined_this_round += 1
                continue

            # Persist accepted response.
            persona_path = round_dir / f"{persona.persona_id}.json"
            persona_path.write_bytes(
                orjson.dumps(
                    response.model_dump(mode="json"),
                    option=orjson.OPT_INDENT_2,
                )
            )

            accepted.append(response)
            # Carry the post into next round's prompt (last round only -- per
            # PROMPT pseudocode line 91 "p's prior posts (memory)").
            last_post_by_persona[persona.persona_id] = response.post

        total_quarantined += quarantined_this_round

        # Aggregate this round.
        distribution = stance_distribution(accepted)
        clusters = dissent_clusters(accepted, top_k=5)
        sample_posts = _select_sample_posts(accepted, top_k=5)

        artifact = RoundArtifact(
            run_id=run_id,
            round_idx=r,
            stance_distribution=distribution,
            dissent_clusters=clusters,
            sample_posts=sample_posts,
            quarantined_count=quarantined_this_round,
            n_personas=n_personas,
        )
        rounds_out.append(artifact)

        # Persist round artifact.
        (run_dir / f"round_{r}.json").write_bytes(
            orjson.dumps(
                artifact.model_dump(mode="json"),
                option=orjson.OPT_INDENT_2,
            )
        )

    wall_seconds = time.perf_counter() - sim_t0

    # 6. Round-over-round JSD (jsd[0] := 0.0 by convention).
    jsd_series: list[float] = [0.0]
    for r in range(1, n_rounds):
        jsd_series.append(
            jensen_shannon_divergence(
                rounds_out[r - 1].stance_distribution,
                rounds_out[r].stance_distribution,
            )
        )

    quarantine_rate = (
        total_quarantined / total_attempted if total_attempted > 0 else 0.0
    )

    summary = SimulationSummary(
        run_id=run_id,
        scenario_id=scenario_id,
        n_personas=n_personas,
        n_rounds=n_rounds,
        rounds=rounds_out,
        jsd_round_over_round=jsd_series,
        wall_seconds=wall_seconds,
        quarantine_rate=quarantine_rate,
    )

    (run_dir / "summary.json").write_bytes(
        orjson.dumps(
            summary.model_dump(mode="json"),
            option=orjson.OPT_INDENT_2,
        )
    )

    return summary


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _make_default_client() -> Any:
    """Construct a default ``ollama.Client``.

    Imported lazily so that ``import ollarma.swarm.engine`` doesn't hard-fail
    if the ``ollama`` Python SDK is missing in environments that only need
    the schema / aggregator surfaces (e.g., post-hoc analysis of an existing
    run_dir). The SDK is a top-level project dep in pyproject.toml so this
    branch should always succeed in normal use.
    """
    import ollama  # local import on purpose

    return ollama.Client()


def _build_prompt(
    *,
    persona: PersonaPrompt,
    scenario: str,
    round_idx: int,
    prior_distribution: dict[str, float] | None,
    prior_post: str | None,
) -> str:
    """Assemble the per-call prompt: persona + scenario + (prior round state).

    Output is plain text concatenation; we rely on ``format="json"`` on the
    Ollama side plus the StanceResponse schema for output structure rather
    than encoding the schema in the prompt (Ollama 0.19+ honours the schema
    via constrained decoding when ``format="json"`` is set).
    """
    parts: list[str] = [persona.prompt]
    parts.append("")
    parts.append(f"Scenario under discussion: {scenario}")
    # NOTE (EXP-OLLARMA-SWARM-004, 2026-06-10): a blanket abstention instruction
    # was tested here to fix the EXP-002 inert confabulation. It drove inert JSD
    # to 0.0000 (PASS) but FAILED the anti-lobotomy detectability gate — real
    # contestable scenarios (policy draft, biotech trial) also collapsed to 100%
    # neutral with qwen2.5-coder:7b. Reverted: the blunt instruction cannot
    # distinguish noise from a debatable claim. A discriminating abstention
    # (substantive-vs-noise pre-classification, or a non-code judge model) is the
    # v5.2 remediation hypothesis. See prompts/PROMPT_004_INERT_ABSTENTION_REMEDIATION.md.

    if prior_distribution is not None:
        # Render a compact, deterministic summary of the prior round so two
        # identical (persona, scenario, prior_distribution) tuples produce
        # byte-identical prompts (which, with seed=42 + temp=0, give
        # byte-identical responses).
        items = sorted(prior_distribution.items())
        prior_str = ", ".join(f"{k}={v:.3f}" for k, v in items)
        parts.append(
            f"Prior round (r={round_idx - 1}) stance distribution: {prior_str}"
        )

    if prior_post is not None:
        parts.append(f"Your prior post: {prior_post}")

    parts.append("")
    parts.append(
        "Respond with a JSON object matching this schema: "
        "{persona_id: string, round_idx: integer, "
        "stance: one of [strongly_disagree, disagree, neutral, agree, "
        "strongly_agree, refuse_to_engage], "
        "confidence: number in [0, 1], "
        "rationale: string (<=30 words), "
        "post: string (<=280 chars)}."
    )
    parts.append(
        f"Set persona_id={persona.persona_id!r} and round_idx={round_idx}."
    )
    return "\n".join(parts)


def _generate_with_retry(
    *,
    client: Any,
    model: str,
    prompt: str,
    persona: PersonaPrompt,
    round_idx: int,
    quarantine_path: Path,
) -> StanceResponse | None:
    """Call Ollama; on failure retry once; on second failure quarantine.

    Returns the parsed StanceResponse or None if quarantined.
    Handles two failure modes:
      1. Any exception from ``client.generate`` (Ollama timeout, network
         error, JSON-format failure when the model produces unparseable
         output, etc.).
      2. ``pydantic.ValidationError`` when the response JSON does not match
         the StanceResponse schema (out-of-bounds confidence, unknown stance,
         oversized post, etc.).

    Both modes get 1 retry with the same prompt. Retry rationale: temp=0 +
    seed=42 makes the *same* call deterministic, so a retry only helps if the
    failure was non-deterministic (network / Ollama daemon flake). For
    schema-violation failures the retry will produce the same bad output;
    that's fine -- we then quarantine, which is the documented behaviour.
    """
    last_error: str = ""
    for attempt in range(MAX_RETRIES + 1):  # initial + MAX_RETRIES
        try:
            resp = client.generate(
                model=model,
                prompt=prompt,
                options=DEFAULT_OLLAMA_OPTIONS,
                format="json",
                stream=False,
            )
            # ``ollama.Client.generate`` returns an object with a ``.response``
            # attribute (the model's text output). Be defensive about dict-vs-
            # attribute access so this works against both the SDK type and a
            # dict-shaped mock used in tests.
            text = getattr(resp, "response", None)
            if text is None and isinstance(resp, dict):
                text = resp.get("response")
            if text is None:
                last_error = f"missing 'response' field on attempt {attempt}"
                continue

            return StanceResponse.model_validate_json(text)

        except ValidationError as exc:
            last_error = f"ValidationError on attempt {attempt}: {exc!s}"
            continue
        except Exception as exc:  # noqa: BLE001 -- 1-retry-then-quarantine contract
            # Catches OllamaError, httpx timeout, JSON decode error, etc.
            last_error = (
                f"{type(exc).__name__} on attempt {attempt}: {exc!s}"
            )
            continue

    # Exhausted retries -> quarantine and continue.
    _append_quarantine(
        quarantine_path,
        {
            "ts": time.time(),
            "persona_id": persona.persona_id,
            "round_idx": round_idx,
            "attempts": MAX_RETRIES + 1,
            "last_error": last_error,
        },
    )
    return None


def _append_quarantine(path: Path, record: dict) -> None:
    """Append one JSON line to the quarantine ledger."""
    line = orjson.dumps(record, option=orjson.OPT_SORT_KEYS)
    with path.open("ab") as fh:
        fh.write(line)
        fh.write(b"\n")


def _select_sample_posts(
    accepted: list[StanceResponse],
    *,
    top_k: int = 5,
) -> list[str]:
    """Pick up to ``top_k`` representative posts -- one per stance, max-conf.

    Mirrors the ``dissent_clusters`` selection rule (highest confidence per
    stance bucket) so the per-round artifact's ``sample_posts`` and
    ``dissent_clusters[*].sample_post`` agree on which post represents each
    stance. Tie-broken by post text for determinism.
    """
    if not accepted:
        return []

    by_stance: dict[str, StanceResponse] = {}
    for r in accepted:
        cur = by_stance.get(r.stance)
        if cur is None or (r.confidence, r.post) > (cur.confidence, cur.post):
            by_stance[r.stance] = r

    # Sort by member count (descending) so the most-supported stances come
    # first. We don't have counts here, but stance order in the prompt is
    # also the schema's Literal order, which is a reasonable stable tiebreak.
    chosen = sorted(
        by_stance.values(),
        key=lambda r: (-r.confidence, r.stance),
    )
    return [r.post for r in chosen[:top_k]]
