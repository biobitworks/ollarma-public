# RTB-06 Antigence Backbone Proof Bundle Plan

**Parent:** `docs/ANTIGENCE_REALTIME_BACKBONE_PLAN.md`
**Status:** proof plan, not executed proof.
**Scope:** define the localhost-only proof bundle that demonstrates the planned
real-time bridge, reviewed chat, KG navigation, distillation, and blocked
verdict behavior.

## Proof Inputs

- A tiny fixture adapter/project.
- A tiny KB with at least one canonical source and one reference source.
- A reviewed `/chat` request.
- A reviewed `/route` request with citations.
- A `/navigator/query` request.
- A distillation over chat + route + navigator outputs.
- A forced Antigence `block` verdict case.
- A provisional/open-calibration antigen-bank verdict case.
- A sequential escalation case showing big-model rungs are not fanned out.
- A JSON-router case that fails closed without grammar and passes with grammar.
- A project with an empty antibody manifest, reported as a pack-population gap.
- An Antigence-unavailable case.

## Bundle Layout

```text
runs/bridge/<UTC>/
  README.md
  manifest.json
  bridge_events.jsonl
  review_packets.jsonl
  distillation_receipts.jsonl
  navigator_results.jsonl
  verification.json
  no_write_audit.json
```

## Verification

`verification.json` must prove:

- event replay reconstructs the sequence;
- review packets reference prompt/response hashes;
- antibody lane provenance is present;
- navigator citations preserve authority labels;
- distillation source refs match stored output hashes;
- block verdict prevents downstream mutation;
- open-calibration antigen-bank findings are downgraded or held advisory;
- escalation ladder receipts show sequential execution for larger models;
- router/verdict receipts show strict-output mode;
- empty project antibody manifests are visible as gaps, not silent acceptance;
- Antigence-unavailable posture is explicit;
- no external KG/EXP/Antigence/Watchtower/Overwatch state was written.

## Tests

- proof driver dry-run creates bundle structure;
- proof driver verifies event chain;
- proof driver detects missing review packet;
- proof driver detects distillation source hash mismatch;
- proof driver detects unexpected external writes;
- proof driver records blocked verdict behavior.
- proof driver records calibration-open downgrade behavior.
- proof driver records sequential escalation-ladder behavior.
- proof driver records JSON grammar enforcement for router/verdict lanes.
- proof driver records per-project antibody manifest coverage.

## Acceptance Gate

RTB-06 is complete only when:

- all RTB-01 through RTB-05 tests pass;
- proof fixture run creates `runs/bridge/<UTC>/`;
- verification output is machine-readable and exits nonzero on tampering;
- README states which surfaces are live and which remain operator-gated.
