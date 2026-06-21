from __future__ import annotations

import json
from pathlib import Path


DEMO_ID = "seedgraph-handoff-demo"
HANDOFF_REF = "artifacts/ollarma/demo/handoff.json"
NARRATIVE_REF = "artifacts/ollarma/demo/judge_narrative.md"


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    artifact_dir = project_root / "artifacts" / "ollarma" / "demo"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    handoff_payload = {
        "demo_id": DEMO_ID,
        "status": "ready_for_handoff",
        "execution_substrate": "ollarma-autopilot",
        "mode": "local-first",
        "restart_safe": True,
        "bounded_execution": True,
        "upstream_risk": "seedgraph_science_ops_flow_may_be_too_risky_for_event_timing",
        "happy_path": [
            "build_demo_kb",
            "confirm_workflow_catalog",
            "run_autopilot_demo",
            "handoff_to_downstream_operator",
        ],
        "handoff_targets": [
            "seedgraph",
            "watchtower",
            "gettingsciencedone",
        ],
        "artifact_ref": HANDOFF_REF,
        "narrative_ref": NARRATIVE_REF,
        "offline_fallback": "rerun_autopilot_and_show_existing_handoff_artifact",
        "deferred": [
            "live sibling-repo workflow execution",
            "broad multi-project orchestration",
            "public packaging",
        ],
    }

    narrative = "\n".join(
        [
            "# Judge Narrative",
            "",
            "`ollarma` is acting as the reliable local execution substrate.",
            "It can build a local KB, expose a bounded workflow catalog, run one validated local task,",
            "and leave deterministic artifacts plus restart-safe receipts before handing off to broader systems.",
        ]
    )

    (artifact_dir / "handoff.json").write_text(
        json.dumps(handoff_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "judge_narrative.md").write_text(narrative + "\n", encoding="utf-8")

    print("OLLARMA_DEMO_OK")
    print(f"artifact_ref={HANDOFF_REF}")
    print(f"narrative_ref={NARRATIVE_REF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
