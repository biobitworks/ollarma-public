# CAFA Antigence-lane handoff (single starting file)

You are the **Antigence lane (PI/PM/O/SWE)** for the CAFA-6 protein-function
verification pipeline, running inside the **ollarma repo** so you connect to the
live SWE bridge as the peer of the Ollarma lane (Claude Code).

## Do this in order

1. **Connect first.** Run the `ollarma-swe-session-connect` skill (project=ollarma,
   your runtime = **codex**) so the Ollarma lane sees you as the live peer; then
   keep it updated (`ollarma-swe-session-autoupdate`). Confirm `peer_present` with
   claude-code.
2. **Read** `prompts/PROMPT_CAFA_ANTIGENCE_LANE.md` (your full brief) and
   `docs/CAFA_SUBSTRATE_STATUS.md` (the Ollarma substrate that is already built).
3. **Consume the Ollarma substrate** (already committed, importable here):
   ```python
   from ollarma.cafa import screen_text_claim, verify_go_submission, GoDag, Prediction
   from ollarma.cafa.citation import resolve_citations, LocalAbstractCorpus
   from ollarma.cafa.distillation import DistillationCollector
   from ollarma.cafa.oracle import measure_boundary
   ```
   `screen_text_claim(target, text).escalation_request` →
   `{to_lane:"antigence", antibodies:["claim_entailment","open_world_overclaim"],
   target, text, cited_pmids, frontier_allowed:False}` — this is your input.

## Local-first (hard rule)
No frontier model and no online database until **absolutely necessary**. Use
**local Ollama models only** (localhost:11434). MCP/RAG is a grading oracle, not a
data path. A clean run makes zero frontier/online calls.

## First deliverable (bounded — do this, commit, post a bridge event, then stop)
Build the **`claim_entailment`** antibody as a **local** cell: given a claim + its
locally-resolved abstract, return SUPPORT / REFUTE / NOINFO using a local model
(e.g. qwen3.5:9b or deepseek-r1:14b load-on-demand, json-constrained). Add unit
tests (fixture claim+abstract pairs; no network). Commit atomically. Append a
bridge event (state e.g. `ANTIGENCE_CLAIM_ENTAILMENT_V0`) summarizing what you
built and what you need from the Ollarma lane next (e.g. real SciFact corpus for
calibration). Then pre-register the calibration EXP (`gsigmad-create-prompt`)
before computing any MCC — do NOT tune to a target.

Coordinate with the Ollarma lane (claude-code) via `~/.ollarma/swe_session/projects/ollarma/`.
