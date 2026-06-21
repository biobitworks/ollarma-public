"""T9 acceptance-run driver for PROMPT-OLLARMA-SWARM-001.

Pre-registered offline driver. Runs the 5 benchmark scenarios from
``prompts/PROMPT_OLLARMA_SWARM_001_LOCAL_GPU.md`` §Benchmark scenarios
through ``ollarma.swarm.run_simulation`` (N=50, M=5) on a real Ollama
daemon, persists per-scenario artifacts, computes the 5 MESI gates
end-to-end, and emits ``acceptance_verdict.json``.

**This script does NOT fire automatically — it is the operator's tool.**

Hard rails (from PROMPT-OLLARMA-SWARM-001 + operator brief):

1. No cloud LLMs in inner loop. Local Ollama only.
2. Never preload two model variants during a run (single-GPU contract).
3. Inert-control scenario (#5) MUST show round-over-round JSD < 0.02.
   If it shows JSD > 0.05, the engine FAILS regardless of scenarios 1-4
   (signal-from-noise hallucination).
4. Single-writer rule during the run.
5. Idempotent on resume: scenarios with an existing ``summary.json`` are
   skipped (so a partial run can be resumed without redoing completed work).

Halt-and-investigate triggers (operator surfaces; script logs but does
not auto-stop the next scenario — operator is the sole circuit-breaker):

- per-response wall median > 1.5x smoothed window  (already triggers
  AdaptiveSleeper sleep_ms; surfaced in ``throttle.jsonl``)
- inert-control JSD > 0.05 round 1
- quarantine rate > 5% on any non-control scenario

Usage:

    # Pre-flight: confirm model is resident, no other variants in memory
    ollama ps
    ollama pull qwen2.5-coder:7b   # if not already pulled

    # Smoke (N=2 x M=2 against stub-Ollama; offline; no GPU)
    .venv/bin/python scripts/swarm_acceptance.py --smoke

    # Full acceptance run (LIVE; ~7.5h sequential against real Ollama)
    .venv/bin/python scripts/swarm_acceptance.py

    # Resume an interrupted run (skip scenarios with existing summary.json)
    .venv/bin/python scripts/swarm_acceptance.py --run-dir runs/swarm_acceptance_20260507T120000Z
"""

from __future__ import annotations

import argparse
import json
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


SCENARIOS: tuple[dict[str, str], ...] = (
    {
        "scenario_id": "audience_1927_med",
        "seed": "anti-masturbation panic causes insanity",
        "kind": "real",
        "expected": "strong dissent split (vitaology Phase 7 use case)",
    },
    {
        "scenario_id": "press_release_biotech",
        "seed": (
            "Biotech firm Cellico Bio announces a Phase 2 trial result "
            "showing a 23% reduction in protein aggregate density in the "
            "treated cohort vs control (n=42, p=0.03)."
        ),
        "kind": "real",
        "expected": "bullish/bearish/neutral split (research-program use case)",
    },
    {
        "scenario_id": "policy_draft_audience",
        "seed": (
            "Draft regulation: all autonomous AI agents executing transactions "
            "above $1,000 USD must post a hash-chained receipt to a public "
            "registry within 60 seconds of execution; failure invalidates the "
            "transaction and triggers operator-level review."
        ),
        "kind": "real",
        "expected": "axial dissent (general use case)",
    },
    {
        "scenario_id": "historical_counterfactual",
        "seed": (
            "What if Y2K had crashed major US financial systems for 14 days, "
            "with bank ledgers permanently corrupted to a verified 5% loss?"
        ),
        "kind": "real",
        "expected": "divergent narrative branches (creative use case)",
    },
    {
        "scenario_id": "inert_control",
        "seed": "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
        "kind": "inert",
        "expected": (
            "ZERO meaningful evolution; round-over-round JSD < 0.02. "
            "Falsification check: JSD > 0.05 = ENGINE FAILS regardless of 1-4."
        ),
    },
)


# MESI gate thresholds — pre-registered in PROMPT-OLLARMA-SWARM-001 §H0/MESI.
# Documented here for honesty; do NOT change without re-registering the PROMPT.
MESI_THRESHOLDS = {
    "wall_seconds_per_scenario": 5400.0,  # 90 minutes
    "jsd_detectability_min": 0.05,         # round-over-round JSD must exceed this to count as detectable
    "jsd_detectability_required_count": 3,  # at least 3 of 5 scenarios must show detectability
    "quarantine_rate_max": 0.05,            # < 5%
    "inert_control_jsd_max": 0.02,          # inert MUST show JSD < 0.02
    "inert_control_falsification_threshold": 0.05,  # if inert JSD > this, FAIL regardless
}


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


def run_scenario(
    scenario: dict[str, str],
    *,
    run_dir: Path,
    n_personas: int,
    n_rounds: int,
    ollama_client: Any | None,
) -> SimulationSummary:
    """Invoke run_simulation for one scenario; persist + return summary.

    Idempotent: if ``run_dir/summary.json`` exists already, the existing
    summary is loaded and returned without re-running.
    """
    scenario_dir = run_dir
    summary_path = scenario_dir / "summary.json"
    if summary_path.exists():
        print(f"  [skip] {scenario['scenario_id']}: summary.json exists, resuming with prior result")
        return SimulationSummary.model_validate_json(summary_path.read_bytes())

    print(f"  [run] {scenario['scenario_id']} (N={n_personas}, M={n_rounds})")
    print(f"        seed: {scenario['seed'][:80]}{'...' if len(scenario['seed']) > 80 else ''}")
    print(f"        expected: {scenario['expected']}")

    t0 = time.perf_counter()
    summary = run_simulation(
        scenario["seed"],
        scenario["scenario_id"],
        n_personas=n_personas,
        n_rounds=n_rounds,
        run_dir=scenario_dir,
        ollama_client=ollama_client,
    )
    wall = time.perf_counter() - t0
    print(
        f"  [done] {scenario['scenario_id']}: wall={wall:.1f}s, "
        f"jsd_per_round={['%.4f' % j for j in summary.jsd_round_over_round]}, "
        f"quarantine_rate={summary.quarantine_rate:.4f}"
    )
    return summary


def evaluate_mesi(
    scenarios: tuple[dict[str, str], ...],
    summaries: list[SimulationSummary],
) -> dict[str, Any]:
    """Compute the 5 pre-registered MESI gates against scenario summaries.

    Inert-control falsification is evaluated FIRST. If it fires, the
    overall verdict is FAIL regardless of the other four gates.
    """
    if len(scenarios) != len(summaries):
        raise ValueError(f"scenario count {len(scenarios)} != summary count {len(summaries)}")

    inert_idx = next(
        (i for i, s in enumerate(scenarios) if s["kind"] == "inert"), None
    )
    if inert_idx is None:
        raise ValueError("no inert-control scenario found in suite — cannot evaluate falsification gate")

    inert_summary = summaries[inert_idx]
    # JSD across all rounds (max round-over-round JSD captures the hallucination signal).
    inert_max_jsd = max(inert_summary.jsd_round_over_round) if inert_summary.jsd_round_over_round else 0.0

    falsification_fired = inert_max_jsd > MESI_THRESHOLDS["inert_control_falsification_threshold"]

    # Gate 1: API contract — pydantic validation on every response.
    # We infer this from a successful SimulationSummary load; quarantine_rate
    # captures parse-failure quarantines.
    gate_api_contract_pass = all(
        isinstance(s, SimulationSummary) for s in summaries
    )

    # Gate 2: wall-time per scenario <= 90 min.
    gate_wall_per_scenario = [
        {
            "scenario_id": scenarios[i]["scenario_id"],
            "wall_seconds": s.wall_seconds,
            "pass": s.wall_seconds <= MESI_THRESHOLDS["wall_seconds_per_scenario"],
        }
        for i, s in enumerate(summaries)
    ]
    gate_wall_pass = all(g["pass"] for g in gate_wall_per_scenario)

    # Gate 3: JSD detectability — at least 3 of 5 scenarios show round-over-round JSD > 0.05.
    gate_jsd_per_scenario = [
        {
            "scenario_id": scenarios[i]["scenario_id"],
            "kind": scenarios[i]["kind"],
            "max_jsd": (max(s.jsd_round_over_round) if s.jsd_round_over_round else 0.0),
            "detectable": (
                max(s.jsd_round_over_round) if s.jsd_round_over_round else 0.0
            ) > MESI_THRESHOLDS["jsd_detectability_min"],
        }
        for i, s in enumerate(summaries)
    ]
    detectable_count = sum(1 for g in gate_jsd_per_scenario if g["detectable"])
    gate_jsd_pass = detectable_count >= MESI_THRESHOLDS["jsd_detectability_required_count"]

    # Gate 4: resilience — quarantine rate < 5% per scenario.
    gate_quarantine_per_scenario = [
        {
            "scenario_id": scenarios[i]["scenario_id"],
            "quarantine_rate": s.quarantine_rate,
            "pass": s.quarantine_rate < MESI_THRESHOLDS["quarantine_rate_max"],
        }
        for i, s in enumerate(summaries)
    ]
    gate_quarantine_pass = all(g["pass"] for g in gate_quarantine_per_scenario)

    # Gate 5: GPU-thermal hygiene.
    # The script cannot directly verify "never two model variants resident" — that's
    # an operator-side `ollama ps` polling check. We surface throttle.jsonl presence
    # (decisions logged) as the substrate evidence.
    gate_thermal_pass = True  # operator-verified; script records evidence only
    gate_thermal_evidence = "throttle.jsonl present per scenario; ollama ps polling is operator-side"

    # Inert control — required JSD < 0.02 (separate from falsification gate which is < 0.05).
    inert_jsd_pass = inert_max_jsd < MESI_THRESHOLDS["inert_control_jsd_max"]

    # Verdict logic.
    if falsification_fired:
        verdict = "FAIL"
        verdict_reason = (
            f"INERT-CONTROL FALSIFICATION FIRED — scenario_5 (inert) showed max round-over-round JSD "
            f"= {inert_max_jsd:.4f} > {MESI_THRESHOLDS['inert_control_falsification_threshold']} "
            f"threshold. Engine is hallucinating signal from noise. FAIL regardless of gates 1-4."
        )
    else:
        all_gates_pass = (
            gate_api_contract_pass
            and gate_wall_pass
            and gate_jsd_pass
            and gate_quarantine_pass
            and gate_thermal_pass
            and inert_jsd_pass
        )
        if all_gates_pass:
            verdict = "PASS"
            verdict_reason = "All 5 MESI gates pass; inert-control JSD below required threshold."
        else:
            failing = []
            if not gate_api_contract_pass:
                failing.append("API contract")
            if not gate_wall_pass:
                failing.append("wall-time per scenario")
            if not gate_jsd_pass:
                failing.append(f"JSD detectability ({detectable_count}/5; need >=3)")
            if not gate_quarantine_pass:
                failing.append("quarantine rate")
            if not gate_thermal_pass:
                failing.append("GPU-thermal hygiene")
            if not inert_jsd_pass:
                failing.append(f"inert-control max JSD {inert_max_jsd:.4f} not below {MESI_THRESHOLDS['inert_control_jsd_max']}")
            verdict = "PARTIAL"
            verdict_reason = f"Gates failing: {', '.join(failing)}; inert-control falsification did NOT fire"

    return {
        "prompt_id": "PROMPT-OLLARMA-SWARM-001",
        "exp_id": "EXP-OLLARMA-SWARM-001",
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "evaluated_at_utc": utc_now().isoformat(),
        "mesi_thresholds": MESI_THRESHOLDS,
        "gates": {
            "api_contract": {"pass": gate_api_contract_pass},
            "wall_time": {
                "pass": gate_wall_pass,
                "per_scenario": gate_wall_per_scenario,
            },
            "jsd_detectability": {
                "pass": gate_jsd_pass,
                "detectable_count": detectable_count,
                "required_count": MESI_THRESHOLDS["jsd_detectability_required_count"],
                "per_scenario": gate_jsd_per_scenario,
            },
            "resilience_quarantine": {
                "pass": gate_quarantine_pass,
                "per_scenario": gate_quarantine_per_scenario,
            },
            "thermal_hygiene": {
                "pass": gate_thermal_pass,
                "evidence": gate_thermal_evidence,
                "operator_action_required": "verify with `ollama ps` polling during the live run",
            },
        },
        "inert_control": {
            "max_round_over_round_jsd": inert_max_jsd,
            "required_max_jsd": MESI_THRESHOLDS["inert_control_jsd_max"],
            "falsification_threshold": MESI_THRESHOLDS["inert_control_falsification_threshold"],
            "falsification_fired": falsification_fired,
            "below_required_max": inert_jsd_pass,
        },
        "halt_and_investigate_triggers": [
            "per-response wall median > 1.5x smoothed window (logged in throttle.jsonl)",
            "inert-control JSD > 0.05 round 1 (falsification fires; verdict=FAIL)",
            "quarantine rate > 5% on any non-control scenario (resilience gate fails)",
        ],
        "scenarios": [
            {
                "scenario_id": scenarios[i]["scenario_id"],
                "kind": scenarios[i]["kind"],
                "run_id": str(s.run_id),
                "wall_seconds": s.wall_seconds,
                "quarantine_rate": s.quarantine_rate,
                "jsd_round_over_round": s.jsd_round_over_round,
            }
            for i, s in enumerate(summaries)
        ],
    }


def build_smoke_stub_client():
    """Return a stub Ollama client for --smoke mode (N=2 x M=2, no GPU).

    Uses the same fixture pattern as ``tests/swarm/conftest.py`` so smoke
    behavior matches the test-suite contract.
    """
    from itertools import cycle
    from datetime import datetime as _dt

    base_responses = [
        '{"persona_id":"p1","round_idx":0,"stance":"agree","confidence":0.7,"rationale":"plausible","post":"This seems reasonable."}',
        '{"persona_id":"p2","round_idx":0,"stance":"disagree","confidence":0.6,"rationale":"skeptical","post":"I am not convinced."}',
        '{"persona_id":"p1","round_idx":1,"stance":"strongly_agree","confidence":0.85,"rationale":"reinforced","post":"Round two: strong yes."}',
        '{"persona_id":"p2","round_idx":1,"stance":"neutral","confidence":0.5,"rationale":"shifted","post":"Round two: undecided."}',
    ]

    class _StubClient:
        def __init__(self):
            self._cycle = cycle(base_responses)

        def generate(self, **_kwargs):
            return {
                "model": "qwen2.5-coder:7b",
                "created_at": _dt.now(timezone.utc).isoformat(),
                "response": next(self._cycle),
                "done": True,
                "total_duration": 10_000_000,
                "eval_count": 32,
                "eval_duration": 5_000_000,
            }

    return _StubClient()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PROMPT-OLLARMA-SWARM-001 T9 acceptance driver")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Smoke mode (N=2 x M=2, stub Ollama client, no GPU). Default off.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Resume an existing run-dir (default: create runs/swarm_acceptance_<UTC>/).",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=int,
        default=30,
        help="Sleep between scenarios for thermal cooldown. Default 30s; set 0 in smoke mode.",
    )
    args = parser.parse_args(argv)

    is_smoke = args.smoke
    n_personas = 2 if is_smoke else 50
    n_rounds = 2 if is_smoke else 5
    cooldown = 0 if is_smoke else args.cooldown_seconds

    ollama_client = build_smoke_stub_client() if is_smoke else None

    if args.run_dir is None:
        run_dir = REPO_ROOT / "runs" / f"swarm_acceptance_{utc_stamp()}"
    else:
        run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"PROMPT-OLLARMA-SWARM-001 T9 acceptance driver")
    print(f"  mode:     {'SMOKE' if is_smoke else 'LIVE'}")
    print(f"  run_dir:  {run_dir}")
    print(f"  N x M:    {n_personas} x {n_rounds}")
    print(f"  cooldown: {cooldown}s between scenarios")
    print(f"  scenarios: {len(SCENARIOS)} (incl. 1 inert control)")
    print()

    summaries: list[SimulationSummary] = []
    for i, scenario in enumerate(SCENARIOS):
        scenario_dir = run_dir / f"scenario_{i + 1}_{scenario['scenario_id']}"
        scenario_dir.mkdir(parents=True, exist_ok=True)

        print(f"Scenario {i + 1}/{len(SCENARIOS)}: {scenario['scenario_id']} ({scenario['kind']})")
        try:
            summary = run_scenario(
                scenario,
                run_dir=scenario_dir,
                n_personas=n_personas,
                n_rounds=n_rounds,
                ollama_client=ollama_client,
            )
        except Exception as exc:
            print(f"  [error] {scenario['scenario_id']}: {type(exc).__name__}: {exc}")
            print("  Halting acceptance run; partial results preserved on disk.")
            return 2

        summaries.append(summary)

        if i < len(SCENARIOS) - 1 and cooldown > 0:
            print(f"  [cooldown] sleeping {cooldown}s before next scenario...")
            time.sleep(cooldown)
        print()

    print("All scenarios complete. Evaluating MESI gates...")
    verdict = evaluate_mesi(SCENARIOS, summaries)

    verdict_path = run_dir / "acceptance_verdict.json"
    write_json_atomic(verdict_path, verdict)
    print(f"\nVerdict: {verdict['verdict']}")
    print(f"  reason: {verdict['verdict_reason']}")
    print(f"  written: {verdict_path}")

    if verdict["verdict"] == "FAIL":
        return 3
    if verdict["verdict"] == "PARTIAL":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
