"""EXP-OLLARMA-SWARM-002 inert-control calibration runner.

Pre-registered diagnostic driver for ``prompts/PROMPT_002_INERT_CONTROL_CALIBRATION.md``.

Phase 70 T9b returned PARTIAL because the inert control showed a max
round-over-round JSD of ``0.0412`` (fails the strict ``< 0.02`` cleanliness gate
but does NOT cross the ``0.05`` falsification threshold). The wobble was the
cold-start ``round0 -> round1`` transition out of unanimous-neutral. EXP-002 asks
whether that wobble *reproduces* across multiple inert prompt forms under the
exact same local-GPU swarm settings, or whether it was a single-prompt artifact.

This is a **separate** driver from ``scripts/swarm_acceptance.py`` on purpose: the
PROMPT requires that adapting the acceptance driver must not blur T9 acceptance
semantics. This script reuses ``ollarma.swarm.run_simulation`` unchanged and does
NOT touch production swarm logic or the pre-registered ``0.02`` acceptance
threshold. The ``0.02`` / ``0.05`` values below are inherited verbatim from
PROMPT-OLLARMA-SWARM-001 + PROMPT-002 MESI and are read-only here.

**This script does NOT fire automatically — it is the operator's tool.** The live
run is GPU-bound (~5 inert prompts x N=50 x M=5) and must be operator-scheduled
with single-writer ownership of ``runs/swarm_calibration_<UTC>/`` (PROMPT-002
§No-Overlap Guards).

Usage::

    # Smoke (N=2 x M=2 against a stub Ollama client; offline; no GPU)
    .venv/bin/python scripts/swarm_calibration.py --smoke

    # Full calibration run (LIVE; sequential against real Ollama; operator-gated)
    .venv/bin/python scripts/swarm_calibration.py

    # Resume an interrupted run (skip prompts with an existing summary.json)
    .venv/bin/python scripts/swarm_calibration.py --run-dir runs/swarm_calibration_<UTC>

Verdict taxonomy (PROMPT-002 §Post-Run Reporting):

- ``PASS``               -- all five inert prompts have max JSD ``< 0.02``.
- ``CALIBRATION_BASELINE`` -- one or more prompts are ``>= 0.02`` and all remain
                              ``<= 0.05``; route to EXP-003 null-model audit.
- ``FAIL``               -- one or more prompts exceed ``0.05``; engine-level
                              falsification; stop Phase 70 clean closeout.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ollarma.swarm import run_simulation  # noqa: E402
from ollarma.swarm.schemas import SimulationSummary  # noqa: E402


# ---------------------------------------------------------------------------
# Inert prompt classes — exactly the five from PROMPT-002 §Scope.
#
# All five are fixed string literals (deterministic, offline). Prompt #1 reuses
# the T9b inert seed verbatim so the cold-start comparison is apples-to-apples
# with the committed acceptance run.
# ---------------------------------------------------------------------------

INERT_PROMPTS: tuple[dict[str, str], ...] = (
    {
        "prompt_id": "inert_1_lorem",
        "label": "lorem ipsum control",
        "kind": "lorem",
        "text": "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
    },
    {
        "prompt_id": "inert_2_shuffled_neutral",
        "label": "shuffled neutral text",
        "kind": "shuffled_neutral",
        # Neutral English words in a fixed shuffled (non-grammatical, no-stance)
        # order. Deterministic literal — no runtime shuffling.
        "text": (
            "table window the morning quietly under paper folder between several "
            "the cabinet desk near a the shelf above and beside the corridor "
            "wooden chair the the the plain room a"
        ),
    },
    {
        "prompt_id": "inert_3_repeated_sentence",
        "label": "repeated neutral sentence",
        "kind": "repeated_sentence",
        "text": (
            "The folder is on the shelf. " * 8
        ).strip(),
    },
    {
        "prompt_id": "inert_4_random_tokens",
        "label": "random tokens with no semantic stance",
        "kind": "random_tokens",
        # Fixed pseudo-random token string — carries no claim, no stance.
        "text": (
            "qx7 vbn 41z mko 9 ppr tuv 03 hhk lll 2 we8 zzq nra 77 dcm xb1 oop "
            "55 grt yhn 12 sdf qaz wsx 8 edc rfv tgb 6 ujm ik9 ol0"
        ),
    },
    {
        "prompt_id": "inert_5_bland_factual",
        "label": "bland factual paragraph",
        "kind": "bland_factual",
        "text": (
            "Water is composed of two hydrogen atoms and one oxygen atom. The "
            "Earth completes one orbit of the Sun in approximately 365 days. A "
            "week has seven days. The freezing point of pure water at sea level "
            "is zero degrees Celsius. Most adult humans have thirty-two teeth."
        ),
    },
)


# ---------------------------------------------------------------------------
# Thresholds — INHERITED VERBATIM from PROMPT-OLLARMA-SWARM-001 / PROMPT-002 MESI.
#
# These are NOT to be retuned inside this run (PROMPT-002 §Constraints: "Do not
# retune the 0.02 threshold inside this run"). They are duplicated here only so
# the calibration verdict is self-describing; the production acceptance
# threshold lives independently in scripts/swarm_acceptance.py:MESI_THRESHOLDS.
# ---------------------------------------------------------------------------

CALIBRATION_THRESHOLDS: dict[str, float] = {
    "inert_clean_jsd_max": 0.02,            # strict cleanliness gate (PASS bar)
    "inert_falsification_threshold": 0.05,  # engine-falsification bar (FAIL bar)
}

MODEL_TAG = "qwen2.5-coder:7b"
SEED = 42
TEMPERATURE = 0.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def write_json_atomic(path: Path, obj: Any) -> None:
    """Atomic write via tmp+rename (POSIX atomic for same-filesystem)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(orjson.dumps(obj, option=orjson.OPT_INDENT_2))
    tmp.replace(path)


# ---------------------------------------------------------------------------
# JSD split — cold-start (round0->round1) vs later-round steady state.
# ---------------------------------------------------------------------------

def split_jsd(jsd_round_over_round: list[float]) -> dict[str, float]:
    """Split a round-over-round JSD series into cold-start vs steady-state.

    ``jsd_round_over_round[0]`` is fixed at 0.0 by engine convention (no prior).
    ``jsd_round_over_round[1]`` is the cold-start ``round0 -> round1`` transition.
    Indices ``>= 2`` are the later-round steady state.

    Returns a dict with:

    - ``max_jsd``                 -- max over the whole series.
    - ``round0_to_round1_jsd``    -- the cold-start transition (index 1; 0.0 if absent).
    - ``max_later_round_jsd``     -- max over indices >= 2 (0.0 if absent).
    """
    series = list(jsd_round_over_round or [])
    max_jsd = max(series) if series else 0.0
    round0_to_round1 = series[1] if len(series) >= 2 else 0.0
    later = series[2:]
    max_later = max(later) if later else 0.0
    return {
        "max_jsd": max_jsd,
        "round0_to_round1_jsd": round0_to_round1,
        "max_later_round_jsd": max_later,
    }


# ---------------------------------------------------------------------------
# Verdict evaluation — pure function over prompts + summaries.
# ---------------------------------------------------------------------------

def evaluate_calibration(
    prompts: tuple[dict[str, str], ...],
    summaries: list[SimulationSummary],
    *,
    model: str,
    ollama_evidence: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    """Compute the EXP-002 calibration verdict against per-prompt summaries.

    Verdict logic (PROMPT-002 §Post-Run Reporting), evaluated in priority order:

    1. If ANY prompt's max JSD ``> 0.05`` -> ``FAIL`` (engine falsification).
    2. Else if ALL prompts' max JSD ``< 0.02`` -> ``PASS``.
    3. Else -> ``CALIBRATION_BASELINE`` (one or more ``>= 0.02``, all ``<= 0.05``).
    """
    if len(prompts) != len(summaries):
        raise ValueError(
            f"prompt count {len(prompts)} != summary count {len(summaries)}"
        )

    clean_max = CALIBRATION_THRESHOLDS["inert_clean_jsd_max"]
    falsify = CALIBRATION_THRESHOLDS["inert_falsification_threshold"]

    per_prompt: list[dict[str, Any]] = []
    for meta, summary in zip(prompts, summaries):
        split = split_jsd(summary.jsd_round_over_round)
        per_prompt.append(
            {
                "prompt_id": meta["prompt_id"],
                "label": meta["label"],
                "kind": meta["kind"],
                "run_id": summary.run_id,
                "max_jsd": split["max_jsd"],
                "round0_to_round1_jsd": split["round0_to_round1_jsd"],
                "max_later_round_jsd": split["max_later_round_jsd"],
                "quarantine_rate": summary.quarantine_rate,
                "wall_seconds": summary.wall_seconds,
                "jsd_round_over_round": summary.jsd_round_over_round,
                "passes_strict_clean": split["max_jsd"] < clean_max,
                "falsified": split["max_jsd"] > falsify,
            }
        )

    any_falsified = any(p["falsified"] for p in per_prompt)
    all_clean = all(p["passes_strict_clean"] for p in per_prompt)

    if any_falsified:
        verdict = "FAIL"
        verdict_reason = (
            "ENGINE FALSIFICATION — at least one inert prompt exceeded the "
            f"{falsify} falsification threshold. The swarm is generating signal "
            "from noise; stop Phase 70 clean closeout."
        )
    elif all_clean:
        verdict = "PASS"
        verdict_reason = (
            f"All {len(per_prompt)} inert prompts have max round-over-round JSD "
            f"< {clean_max}. Inert behavior is clean across prompt forms."
        )
    else:
        verdict = "CALIBRATION_BASELINE"
        over = [p["prompt_id"] for p in per_prompt if not p["passes_strict_clean"]]
        verdict_reason = (
            f"{len(over)}/{len(per_prompt)} inert prompts at or above {clean_max} "
            f"but all <= {falsify}: {', '.join(over)}. Route to EXP-003 aggregator "
            "null-model audit before any metric or engine change."
        )

    # Threshold-amendment vs engine-remediation indication.
    #
    # If every prompt's *steady-state* (later-round) JSD is already clean
    # (< 0.02) and the only over-threshold signal is the cold-start
    # round0->round1 transition, the observed distribution is consistent with a
    # *measurement-definition* amendment (exclude/handle the cold-start step)
    # rather than an engine change. If steady-state itself wobbles, that points
    # toward generation/engine remediation. This is an INDICATION for EXP-003 to
    # confirm, NOT a decision — claim ceiling stays HYPOTHESIS_STAGE_1.
    steady_state_all_clean = all(
        p["max_later_round_jsd"] < clean_max for p in per_prompt
    )
    coldstart_is_sole_driver = all(
        (p["passes_strict_clean"]) or (p["max_later_round_jsd"] < clean_max)
        for p in per_prompt
    )
    if verdict == "PASS":
        supports_threshold_amendment = False
        amendment_rationale = (
            "Not applicable — all prompts already pass the strict gate; no "
            "amendment needed."
        )
    elif verdict == "FAIL":
        supports_threshold_amendment = False
        amendment_rationale = (
            "Not applicable — falsification fired; this is engine-level, not a "
            "threshold question."
        )
    elif steady_state_all_clean and coldstart_is_sole_driver:
        supports_threshold_amendment = True
        amendment_rationale = (
            "Steady-state (rounds 1+ JSD) is clean for every prompt; the only "
            "over-threshold signal is the cold-start round0->round1 transition "
            "out of unanimous-neutral. This is consistent with a measurement "
            "amendment (cold-start handling) rather than engine remediation. "
            "INDICATION ONLY — EXP-003 must confirm generation vs aggregation."
        )
    else:
        supports_threshold_amendment = False
        amendment_rationale = (
            "Steady-state JSD also exceeds the strict gate for at least one "
            "prompt; the wobble is not confined to the cold-start step. This "
            "points toward generation/engine remediation, not a threshold move. "
            "EXP-003 must localize the source before any change."
        )

    all_passed_strict = all(p["passes_strict_clean"] for p in per_prompt)

    return {
        "prompt_id": "PROMPT-002",
        "parent_prompt_id": "PROMPT-OLLARMA-SWARM-001",
        "exp_id": "EXP-OLLARMA-SWARM-002",
        "decision_packet": (
            ".gsigmad/decision_packets/"
            "DP-20260531T210716Z-exp-ollarma-swarm-002.json"
        ),
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "evaluated_at_utc": utc_now().isoformat(),
        "mode": mode,
        "model_tag": model,
        "settings": {
            "n_personas": summaries[0].n_personas if summaries else None,
            "n_rounds": summaries[0].n_rounds if summaries else None,
            "seed": SEED,
            "temperature": TEMPERATURE,
        },
        "thresholds": CALIBRATION_THRESHOLDS,
        "claim_ceiling": "HYPOTHESIS_STAGE_1",
        "ollama_runtime_evidence": ollama_evidence,
        "per_prompt": per_prompt,
        "summary_metrics": {
            "n_prompts": len(per_prompt),
            "every_prompt_passed_strict_clean": all_passed_strict,
            "max_jsd_across_prompts": max(
                (p["max_jsd"] for p in per_prompt), default=0.0
            ),
            "max_coldstart_jsd_across_prompts": max(
                (p["round0_to_round1_jsd"] for p in per_prompt), default=0.0
            ),
            "max_later_round_jsd_across_prompts": max(
                (p["max_later_round_jsd"] for p in per_prompt), default=0.0
            ),
            "max_quarantine_rate": max(
                (p["quarantine_rate"] for p in per_prompt), default=0.0
            ),
            "total_wall_seconds": sum(p["wall_seconds"] for p in per_prompt),
            "supports_threshold_amendment": supports_threshold_amendment,
            "amendment_rationale": amendment_rationale,
        },
        "next_gated_action": _next_action(verdict),
        "governance_notes": [
            "Claim ceiling is HYPOTHESIS_STAGE_1 — this run decides the next "
            "remediation step; it does not validate Phase 70 generally.",
            "Does NOT close T11/T12 and does NOT authorize a threshold change "
            "without EXP-003/EXP-004 or explicit PI disposition.",
            "Production swarm logic and the 0.02 acceptance threshold are "
            "unchanged by this run.",
        ],
    }


def _next_action(verdict: str) -> str:
    if verdict == "PASS":
        return (
            "Report PASS to PI. Proceed to EXP-003 only if PI still wants "
            "aggregation null-model confirmation before T9c."
        )
    if verdict == "CALIBRATION_BASELINE":
        return (
            "Prepare EXP-003 aggregator null-model audit (Decision Packet + "
            "PROMPT) to localize generation vs aggregation sensitivity. Do NOT "
            "change the metric or engine yet."
        )
    return (
        "Engine-level falsification. Stop Phase 70 clean closeout. Escalate to "
        "PI; do not proceed to EXP-003/004 remediation as if the engine is sound."
    )


# ---------------------------------------------------------------------------
# Per-prompt run (idempotent).
# ---------------------------------------------------------------------------

def run_inert_prompt(
    prompt: dict[str, str],
    *,
    prompt_dir: Path,
    n_personas: int,
    n_rounds: int,
    ollama_client: Any | None,
) -> SimulationSummary:
    """Run one inert prompt through run_simulation; idempotent on resume."""
    summary_path = prompt_dir / "summary.json"
    if summary_path.exists():
        print(
            f"  [skip] {prompt['prompt_id']}: summary.json exists, "
            "resuming with prior result"
        )
        return SimulationSummary.model_validate_json(summary_path.read_bytes())

    print(f"  [run] {prompt['prompt_id']} (N={n_personas}, M={n_rounds})")
    print(f"        label: {prompt['label']}")
    print(
        f"        text:  {prompt['text'][:80]}"
        f"{'...' if len(prompt['text']) > 80 else ''}"
    )

    t0 = time.perf_counter()
    summary = run_simulation(
        prompt["text"],
        prompt["prompt_id"],
        n_personas=n_personas,
        n_rounds=n_rounds,
        run_dir=prompt_dir,
        ollama_client=ollama_client,
    )
    wall = time.perf_counter() - t0
    split = split_jsd(summary.jsd_round_over_round)
    print(
        f"  [done] {prompt['prompt_id']}: wall={wall:.1f}s, "
        f"max_jsd={split['max_jsd']:.4f}, "
        f"coldstart={split['round0_to_round1_jsd']:.4f}, "
        f"max_later={split['max_later_round_jsd']:.4f}, "
        f"quarantine_rate={summary.quarantine_rate:.4f}"
    )
    return summary


# ---------------------------------------------------------------------------
# Manifest + ollama evidence.
# ---------------------------------------------------------------------------

def build_manifest(
    *,
    run_dir: Path,
    mode: str,
    n_personas: int,
    n_rounds: int,
    model: str,
    ollama_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Build the calibration run manifest."""
    return {
        "schema_version": 1,
        "exp_id": "EXP-OLLARMA-SWARM-002",
        "prompt_id": "PROMPT-002",
        "parent_prompt_id": "PROMPT-OLLARMA-SWARM-001",
        "decision_packet": (
            ".gsigmad/decision_packets/"
            "DP-20260531T210716Z-exp-ollarma-swarm-002.json"
        ),
        "generated_at_utc": utc_now().isoformat(),
        "mode": mode,
        "run_dir": str(run_dir),
        "model_tag": model,
        "settings": {
            "n_personas": n_personas,
            "n_rounds": n_rounds,
            "seed": SEED,
            "temperature": TEMPERATURE,
        },
        "settings_match_t9b": (
            mode == "live"
            and model == MODEL_TAG
            and n_personas == 50
            and n_rounds == 5
        ),
        "thresholds": CALIBRATION_THRESHOLDS,
        "claim_ceiling": "HYPOTHESIS_STAGE_1",
        "single_writer": (
            "Operator owns runs/swarm_calibration_<UTC>/ for the duration of "
            "the run (PROMPT-002 §No-Overlap Guards)."
        ),
        "prompts": [
            {
                "prompt_id": p["prompt_id"],
                "label": p["label"],
                "kind": p["kind"],
                "text": p["text"],
            }
            for p in INERT_PROMPTS
        ],
        "ollama_runtime_evidence": ollama_evidence,
    }


def gather_ollama_evidence(is_smoke: bool) -> dict[str, Any]:
    """Capture local Ollama runtime evidence (version + resident models).

    In smoke mode no live daemon is contacted. In live mode this queries the
    local Ollama daemon; failures are recorded rather than raised so the run
    receipt always carries an evidence block.
    """
    if is_smoke:
        return {
            "mode": "smoke",
            "note": "stub Ollama client; no live daemon contacted",
        }
    evidence: dict[str, Any] = {"mode": "live", "captured_at_utc": utc_now().isoformat()}
    try:
        import ollama  # local import on purpose

        client = ollama.Client()
        ps = client.ps()
        # ps() returns a typed object or dict depending on SDK version.
        models = getattr(ps, "models", None)
        if models is None and isinstance(ps, dict):
            models = ps.get("models", [])
        resident = []
        for m in models or []:
            name = getattr(m, "model", None) or (
                m.get("model") if isinstance(m, dict) else None
            )
            size_vram = getattr(m, "size_vram", None) or (
                m.get("size_vram") if isinstance(m, dict) else None
            )
            resident.append({"model": name, "size_vram": size_vram})
        evidence["resident_models"] = resident
        evidence["model_under_test_resident"] = any(
            (r.get("model") or "").startswith(MODEL_TAG.split(":")[0])
            and MODEL_TAG in (r.get("model") or "")
            for r in resident
        )
    except Exception as exc:  # noqa: BLE001 -- evidence-capture must not abort the run
        evidence["error"] = f"{type(exc).__name__}: {exc}"
    return evidence


def build_smoke_stub_client():
    """Return a stub Ollama client for --smoke mode (no GPU).

    Mirrors ``scripts/swarm_acceptance.py:build_smoke_stub_client`` so smoke
    behavior matches the acceptance-driver contract. The canned responses are
    near-frozen (mostly neutral) to imitate inert behavior, but smoke mode is
    only for exercising the end-to-end plumbing — never for evidence.
    """
    from itertools import cycle

    base_responses = [
        '{"persona_id":"p1","round_idx":0,"stance":"neutral","confidence":0.5,"rationale":"no stance","post":"Nothing to react to here."}',
        '{"persona_id":"p2","round_idx":0,"stance":"neutral","confidence":0.5,"rationale":"no stance","post":"This text has no claim."}',
        '{"persona_id":"p1","round_idx":1,"stance":"neutral","confidence":0.5,"rationale":"unchanged","post":"Still nothing to react to."}',
        '{"persona_id":"p2","round_idx":1,"stance":"neutral","confidence":0.5,"rationale":"unchanged","post":"No change."}',
    ]

    class _StubClient:
        def __init__(self):
            self._cycle = cycle(base_responses)

        def generate(self, **_kwargs):
            return {
                "model": MODEL_TAG,
                "created_at": utc_now().isoformat(),
                "response": next(self._cycle),
                "done": True,
                "total_duration": 10_000_000,
                "eval_count": 24,
                "eval_duration": 5_000_000,
            }

    return _StubClient()


def build_provisional_audit_receipt(
    verdict: dict[str, Any], run_dir: Path
) -> dict[str, Any]:
    """Provisional audit receipt — closeout contract is DRAFT/unapproved.

    ``docs/EXP_CLOSEOUT_CONTRACT.md`` exists only as a DRAFT (T10 not resolved).
    Until the PI adopts it, no authoritative audit receipt can be issued, so this
    run emits a PROVISIONAL receipt that records the verdict and explicitly flags
    its non-authoritative status.
    """
    return {
        "schema_version": 1,
        "status": "PROVISIONAL",
        "reason": (
            "docs/EXP_CLOSEOUT_CONTRACT.md is DRAFT/unapproved (Phase 70 T10 not "
            "resolved). No authoritative audit receipt can be issued until the PI "
            "adopts or replaces the closeout contract."
        ),
        "exp_id": "EXP-OLLARMA-SWARM-002",
        "prompt_id": "PROMPT-002",
        "verdict": verdict["verdict"],
        "verdict_reason": verdict["verdict_reason"],
        "mode": verdict["mode"],
        "model_tag": verdict["model_tag"],
        "claim_ceiling": "HYPOTHESIS_STAGE_1",
        "calibration_verdict_path": str(run_dir / "calibration_verdict.json"),
        "manifest_path": str(run_dir / "manifest.json"),
        "generated_at_utc": utc_now().isoformat(),
        "authoritative": False,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EXP-OLLARMA-SWARM-002 inert-control calibration runner"
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Smoke mode (N=2 x M=2, stub Ollama client, no GPU). Default off.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Resume/target an existing run-dir (default: create "
        "runs/swarm_calibration_<UTC>/).",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=int,
        default=30,
        help="Sleep between prompts for thermal cooldown. Default 30s; 0 in smoke.",
    )
    args = parser.parse_args(argv)

    is_smoke = args.smoke
    n_personas = 2 if is_smoke else 50
    n_rounds = 2 if is_smoke else 5
    cooldown = 0 if is_smoke else args.cooldown_seconds
    mode = "smoke" if is_smoke else "live"

    ollama_client = build_smoke_stub_client() if is_smoke else None

    if args.run_dir is None:
        run_dir = REPO_ROOT / "runs" / f"swarm_calibration_{utc_stamp()}"
    else:
        run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    ollama_evidence = gather_ollama_evidence(is_smoke)

    manifest = build_manifest(
        run_dir=run_dir,
        mode=mode,
        n_personas=n_personas,
        n_rounds=n_rounds,
        model=MODEL_TAG,
        ollama_evidence=ollama_evidence,
    )
    write_json_atomic(run_dir / "manifest.json", manifest)

    print("EXP-OLLARMA-SWARM-002 inert-control calibration runner")
    print(f"  mode:     {'SMOKE' if is_smoke else 'LIVE'}")
    print(f"  run_dir:  {run_dir}")
    print(f"  model:    {MODEL_TAG}")
    print(f"  N x M:    {n_personas} x {n_rounds}  (seed={SEED}, temp={TEMPERATURE})")
    print(f"  cooldown: {cooldown}s between prompts")
    print(f"  prompts:  {len(INERT_PROMPTS)} inert classes")
    print()

    summaries: list[SimulationSummary] = []
    for i, prompt in enumerate(INERT_PROMPTS):
        prompt_dir = run_dir / f"prompt_{i + 1}"
        prompt_dir.mkdir(parents=True, exist_ok=True)

        print(f"Prompt {i + 1}/{len(INERT_PROMPTS)}: {prompt['prompt_id']}")
        try:
            summary = run_inert_prompt(
                prompt,
                prompt_dir=prompt_dir,
                n_personas=n_personas,
                n_rounds=n_rounds,
                ollama_client=ollama_client,
            )
        except Exception as exc:  # noqa: BLE001 -- preserve partial results
            print(f"  [error] {prompt['prompt_id']}: {type(exc).__name__}: {exc}")
            print("  Halting calibration run; partial results preserved on disk.")
            return 2

        summaries.append(summary)

        if i < len(INERT_PROMPTS) - 1 and cooldown > 0:
            print(f"  [cooldown] sleeping {cooldown}s before next prompt...")
            time.sleep(cooldown)
        print()

    print("All inert prompts complete. Evaluating calibration verdict...")
    verdict = evaluate_calibration(
        INERT_PROMPTS,
        summaries,
        model=MODEL_TAG,
        ollama_evidence=ollama_evidence,
        mode=mode,
    )

    write_json_atomic(run_dir / "calibration_verdict.json", verdict)
    write_json_atomic(
        run_dir / "audit_output_receipt.json",
        build_provisional_audit_receipt(verdict, run_dir),
    )

    print(f"\nVerdict: {verdict['verdict']}")
    print(f"  reason: {verdict['verdict_reason']}")
    print(
        "  every_prompt_passed_strict_clean: "
        f"{verdict['summary_metrics']['every_prompt_passed_strict_clean']}"
    )
    print(
        "  supports_threshold_amendment: "
        f"{verdict['summary_metrics']['supports_threshold_amendment']}"
    )
    print(f"  next: {verdict['next_gated_action']}")
    print(f"  written: {run_dir / 'calibration_verdict.json'}")

    if is_smoke:
        print(
            "\n[smoke] This was a plumbing-only run with a stub client. "
            "It is NOT EXP-002 evidence. The live, operator-scheduled run "
            "is the only valid calibration evidence."
        )

    if verdict["verdict"] == "FAIL":
        return 3
    if verdict["verdict"] == "CALIBRATION_BASELINE":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
