"""Phase 69 -- Target-agnostic Proof-Run Driver

Per PI direction (recorded in 69-CONTEXT.md), this driver ships INFRASTRUCTURE
ONLY. The actual proof-run is operator-gated. Phase 69 is marked
INFRASTRUCTURE-READY after smoke; BUILD-COMPLETE only after an operator-run
receipt against a real target repo is committed.

Sister to ``scripts/swarm_acceptance.py`` (Phase 70 T9a). Mirrors the
manifest+atomic-write+timestamped-out-dir pattern; does NOT import it
(independent driver).

Invocation:
    .venv/bin/python scripts/swarm_proof_run.py \\
        --target ./some-repo \\
        --task "Add a CLI subcommand foo with tests" \\
        --mode smoke --dry-run

Live operator run (sample):
    .venv/bin/python scripts/swarm_proof_run.py \\
        --target ./xenodisorder \\
        --task "Add `xenodisorder validate --strict` CLI flag" \\
        --mode proof --allow-target-writes \\
        --ollama-host http://localhost:11434

Hard rails:

1. No cloud LLM dependency at module load (the ``ollama`` SDK is imported
   lazily inside :func:`build_ollama_invoker`, and only ``--mode proof``
   without ``--dry-run`` ever calls it).
2. Driver writes only inside ``--out``. The ``--allow-target-writes`` flag
   is required IN ADDITION to ``--mode proof`` before any future target
   mutation is permitted; missing either refuses with exit 2.
3. Manifest is written FIRST (before any swarm work) so an interrupted run
   still leaves an audit trail of how it was invoked.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import orjson

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ollarma.swarm.lane import (  # noqa: E402
    LaneStore,
    LeaseManager,
    run_swarm_lane,
)


DRIVER_VERSION = "0.1.0"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def get_git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:
        return "<unknown>"


def write_json_atomic(path: Path, obj: Any) -> None:
    """Atomic write via tmp+rename (POSIX atomic for same-filesystem)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(orjson.dumps(obj, option=orjson.OPT_INDENT_2))
    tmp.replace(path)


def build_ollama_invoker(model: str = "qwen2.5-coder:7b"):
    """Build a real Ollama-backed on_role_invoke.

    Used ONLY in ``--mode proof`` AND not ``--dry-run``. The ``ollama``
    import is local so smoke / dry-run paths don't require the SDK to be
    installed.
    """
    # Local import: smoke / dry-run paths do not require ollama.
    import ollama  # type: ignore[import-not-found]

    from ollarma.swarm.lane.schemas import (
        ExecutorOutput,
        PlannerOutput,
        ReviewerOutput,
        SynthesizerOutput,
    )

    role_output_map = {
        "planner": PlannerOutput,
        "executor": ExecutorOutput,
        "reviewer": ReviewerOutput,
        "synthesizer": SynthesizerOutput,
    }

    client = ollama.Client()

    def _invoke(role, lane_run):
        prompt = _build_prompt_for_role(role, lane_run)
        response = client.generate(model=model, prompt=prompt, format="json")
        try:
            payload = json.loads(response.get("response", "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Ollama returned invalid JSON for role {role}: {exc}"
            )
        cls = role_output_map[role]
        return cls(
            run_id=lane_run.run_id,
            lane_id=lane_run.lane_id,
            **_coerce_payload(role, payload),
        )

    return _invoke


def _build_prompt_for_role(role, lane_run) -> str:
    """Minimal role prompt; operator can tune."""
    upstream_summary = "\n".join(
        f"{o.role}: "
        f"{getattr(o, 'final_summary', None) or getattr(o, 'rationale', '') or ''}"
        for o in lane_run.upstream_outputs
    )
    return (
        f"You are the {role} in a 4-role execution swarm. "
        f"Task: {lane_run.routing_decision or {}}. "
        f"Upstream context:\n{upstream_summary}\n\n"
        f"Return a JSON object matching the {role} output schema."
    )


def _coerce_payload(role, payload):
    """Provide minimum required fields for each role output if Ollama missed them."""
    if role == "planner":
        return {
            "plan_steps": payload.get("plan_steps", []),
            "rationale": payload.get("rationale", ""),
        }
    if role == "executor":
        return {
            "actions_taken": payload.get("actions_taken", []),
            "artifacts_written": payload.get("artifacts_written", []),
            "success": payload.get("success", True),
            "error_message": payload.get("error_message"),
        }
    if role == "reviewer":
        return {
            "findings": payload.get("findings", []),
            "severity": payload.get("severity", "pass"),
            "summary": payload.get("summary", ""),
        }
    if role == "synthesizer":
        return {
            "final_summary": payload.get("final_summary", ""),
            "decisions": payload.get("decisions", []),
            "next_actions": payload.get("next_actions", []),
        }
    raise ValueError(f"unknown role {role!r}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 69 swarm-lane proof-run driver"
    )
    p.add_argument(
        "--target",
        required=True,
        help="Target repo path or identifier (arbitrary string).",
    )
    p.add_argument(
        "--task",
        required=True,
        help="Task description for the swarm.",
    )
    p.add_argument(
        "--mode",
        choices=["smoke", "proof"],
        default="smoke",
        help="smoke = stub on_role_invoke (no Ollama); proof = real Ollama.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Use stub on_role_invoke (no Ollama) regardless of --mode.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default runs/swarm_proof/<UTC>/).",
    )
    p.add_argument(
        "--allow-target-writes",
        action="store_true",
        help=(
            "Required IN ADDITION to --mode proof for any "
            "target-repo mutation."
        ),
    )
    p.add_argument(
        "--ollama-host",
        default=None,
        help="Optional Ollama daemon host (default localhost:11434).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Safety gate: real proof-run (proof + not dry-run) requires explicit
    # --allow-target-writes. Belt-and-suspenders. --mode proof --dry-run is
    # allowed (no LLM, no writes; bundle smoke).
    if args.mode == "proof" and not args.dry_run and not args.allow_target_writes:
        print(
            "ERROR: --mode proof requires --allow-target-writes to permit any "
            "target-repo mutation. Pass it explicitly OR add --dry-run for a "
            "no-LLM bundle smoke.",
            file=sys.stderr,
        )
        return 2

    out_dir = args.out or (REPO_ROOT / "runs" / "swarm_proof" / utc_stamp())
    out_dir.mkdir(parents=True, exist_ok=True)

    # Manifest first -- captures invocation even if subsequent work fails.
    manifest = {
        "driver_version": DRIVER_VERSION,
        "invoked_at_utc": utc_now().isoformat(),
        "git_head": get_git_head(),
        "args": {
            "target": args.target,
            "task": args.task,
            "mode": args.mode,
            "dry_run": args.dry_run,
            "out": str(out_dir),
            "allow_target_writes": args.allow_target_writes,
            "ollama_host": args.ollama_host,
        },
        "phase": "69-proof-run-and-verification-bundle",
        "status_note": (
            "INFRASTRUCTURE-ONLY driver per PI direction; "
            "operator-run receipt closes Phase 69."
        ),
    }
    write_json_atomic(out_dir / "manifest.json", manifest)
    print(f"manifest written: {out_dir / 'manifest.json'}")

    # LaneStore + LeaseManager rooted at out_dir.
    store = LaneStore(out_dir)
    lease_manager = LeaseManager(store)

    # Choose invoker.
    if args.mode == "smoke" or args.dry_run:
        invoker = None  # Phase 66 default no-op outputs
        print(
            f"running in {'smoke' if args.mode == 'smoke' else 'dry-run'} mode "
            f"(stub invoker; no Ollama)"
        )
    else:
        invoker = build_ollama_invoker()
        print("running in proof mode against real Ollama")

    run_id = uuid4()
    print(f"run_id: {run_id}")

    summary = run_swarm_lane(
        scenario=args.task,
        store=store,
        lease_manager=lease_manager,
        run_id=run_id,
        on_role_invoke=invoker,
        metadata={"target": args.target, "mode": args.mode, "task": args.task},
    )

    print(
        f"\nSwarmRun status: {summary.status}, "
        f"reason_code: {summary.reason_code}"
    )

    # README for the bundle.
    _write_bundle_readme(out_dir, summary, args)

    if summary.status == "completed":
        return 0
    if summary.status == "blocked":
        return 1
    return 3  # failed


def _write_bundle_readme(out_dir: Path, summary, args) -> None:
    content = f"""# swarm_proof_run bundle -- {out_dir.name}

**Driver:** Phase 69 INFRASTRUCTURE-ONLY proof-run
**Mode:** {args.mode}{' (dry-run)' if args.dry_run else ''}
**Target:** {args.target}
**Task:** {args.task}
**SwarmRun status:** {summary.status}
**Reason code:** {summary.reason_code}
**Run ID:** {summary.run_id}

## Files

- `manifest.json` -- driver invocation, args, git HEAD
- `runs/<run_id>/lane_transitions.jsonl` -- hash-chained transitions (Phase 66 receipt chain)
- `runs/<run_id>/checkpoint.json` -- final Checkpoint (Phase 67)
- `runs/<run_id>/lane_outputs/<lane_id>.json` -- per-lane typed pydantic outputs
- `runs/<run_id>/quarantine.jsonl` -- present if any quarantined responses
- `runs.sqlite` -- LaneStore SQLite index (runs + leases tables)

## Replay

```bash
.venv/bin/python -c "
from pathlib import Path
from ollarma.swarm.lane import LaneStore
store = LaneStore(Path('{out_dir}'))
run_dir = Path('{out_dir}') / 'runs' / '{summary.run_id}'
transitions = store.load_transitions(run_dir)
print(f'transitions: {{len(transitions)}}')
for t in transitions:
    print(f'  {{t.to_role}} ({{t.outcome}}) hash={{t.transition_hash[:12]}}...')
"
```

## Phase 69 status

**This bundle alone does NOT close Phase 69.** Per PI direction, Phase 69 is
INFRASTRUCTURE-READY until an operator-run receipt against a real target repo
is committed. To produce one:

```bash
.venv/bin/python scripts/swarm_proof_run.py \\
    --target ./<your-real-repo> \\
    --task "<your real task>" \\
    --mode proof --allow-target-writes
```

Commit the resulting `runs/swarm_proof/<UTC>/` directory and update STATE.md
`progress.phase_69_complete=true`.
"""
    (out_dir / "README.md").write_text(content)


if __name__ == "__main__":
    sys.exit(main())
