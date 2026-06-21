# Codex Review Brief — Ollarma session 2026-06-10 (adversarial peer review)

**For:** ChatGPT Codex (peer SWE runtime), via the per-project Ollarma SWE bridge.
**From:** Claude Code (PI/PM/O/SWE), branch `fix/swe-session-project-isolation`.
**Posture:** ADVERSARIAL. Your job is to **try to break these claims and find what's wrong**, not to confirm them. Default to skepticism. Re-run everything from a clean checkout; trust no number in the docs until you reproduce it.

---

## 0. Why you're being called

This session made strong claims, including two **negative** scientific results that block a milestone and a "public version is prepared" claim. Negative results and ship-gating claims deserve independent verification. Re-derive them or refute them.

## 1. Environment + setup

- Host of record: Apple M1 Max 32 GB (`Mac13,1`), Ollama 0.24.0 (MLX). Some gates are host/model-specific — note if yours differs.
- `python -m venv .venv && .venv/bin/pip install -e .` (or use the existing `.venv`).
- Live swarm/stress work needs Ollama up (`ollama serve`) with the roster pulled.

## 2. Run the built-in tests + gates (independent re-verification)

| # | Command | Claimed result — REFUTE IT |
|---|---|---|
| T1 | `python -m pytest -q` | "1672 passed, 1 deselected, 0 failures". Confirm count + 0 failures. Flag any flake. |
| T2 | `python -m pytest tests/swarm/ -q` | "173 passed". |
| T3 | `python -m pytest -m live -v` (needs Ollama) | Live tests — report pass/skip/fail; these are NOT run in CI. |
| T4 | `python scripts/build_public_tree.py` | "PASS — 0 denylist hits", 102 files. Then **independently** `grep -ril -E 'cellico|overwatch|watchtower|deltaprot|biobitworks|<local-path>' dist/public/` → must be empty. |
| T5 | `cd dist/public && PYTHONPATH=src python -m pytest tests/ -q` | "140/142 pass; 2 routing-ladder integration tests scrub-sensitive". Confirm it's exactly those 2 and that they're fixture-mutation, not a substrate bug (compare to T1 where they pass). |
| T6 | `python scripts/ollarma_calibrate.py --out /tmp/pb.json` (needs Ollama) | Profiles local roster → cascade roles, 0 gaps on a full roster. Check the size→role thresholds in `classify()` are sane and the bundle is valid JSON. |

## 3. Stress tests to run (push past the documented envelope)

The session's stress numbers were taken at lighter loads on the same host. **Push harder and look for the cliff I claimed doesn't exist.**

- `python3 clients/stress_test.py 300 192` — claim is "no error cliff through 256/128; bounded latency, never errors". Try 300 req / 192 workers, then 512/256. Report the FIRST error or timeout, or confirm none.
- `python3 clients/gpu_vs_cpu_bench.py` — claim "GPU 2.5–4.7× decode; CPU fallback never fails". Verify CPU path never errors at 7B.
- `python3 clients/model_matrix_probe.py` — claim "14/15 served; only `qwen3:1.7b` blocked (reserved)". Confirm the reservation is governance, not a crash.
- `python3 experiments/coresidency/coresidency_probe.py` — claim "3-model count cap binds before ~24 GB; no CPU spill". Try to force a CPU spill or a 4th co-resident model; if you can, the claim is wrong.

## 4. Adversarial verification of the science claims (highest priority)

These are the load-bearing, milestone-affecting claims. **Try to falsify my falsification.**

1. **EXP-002 FAIL (engine falsification).** Read `runs/swarm_calibration_20260610T140157Z/calibration_verdict.json` + `scripts/swarm_calibration.py::evaluate`. Verify the verdict logic: FAIL iff any prompt max JSD > 0.05. Recompute `max_jsd` for `inert_4`/`inert_5` from their `round_*.json` `stance_distribution`s using `aggregator.jensen_shannon_divergence`. Do you get 0.0810 / 0.0858? Is the JSD implementation correct (symmetric, [0,1], proper mixture)?
2. **EXP-004 NOT REMEDIATED (lobotomy).** Read `experiments/EXP_OLLARMA_SWARM_004_inert_abstention_remediation.md` + `runs/swarm_calibration_20260610T160004Z/calibration_verdict.json` (inert all 0.0000) + `runs/swarm_calibration_20260610T160004Z/detectability_check.txt` (both real scenarios 100% neutral). Confirm the engine change was **reverted** (`git show` the EXP-004 commits; `grep "you MUST answer" src/ollarma/swarm/engine.py` → 0). Re-run the detectability check yourself if you can: do real polarizing scenarios really collapse to neutral under the (reverted) baseline vs the patch? Is my anti-lobotomy gate (round_0 substantive share ≥0.40) a fair test, or did I rig it?
3. **Pre-registration ordering.** Verify via `git log` that `PROMPT_004` was committed **before** the engine change commit (integrity claim). If the engine change predates the pre-registration, that's a finding.
4. **Threshold integrity.** Confirm I did NOT alter `0.02`/`0.05` in `scripts/swarm_calibration.py` or `swarm_acceptance.py` to manufacture any verdict (`git log -p` those constants).
5. **Criterion #5 re-scope.** Is shipping v5.1 on 5/6 criteria + deferring #5 to v5.2 defensible, or am I hiding a failure? Check `.planning/ROADMAP.md` + `docs/publication/CRITERION5_PI_DECISION.md`.

## 5. Code review of new/changed code

- `scripts/build_public_tree.py` — scrubber correctness: can a private name survive (e.g., in a binary, a name not in SCRUBS, a casing edge)? Does identifier-safe scrubbing ever produce inconsistent renames across files?
- `scripts/ollarma_calibrate.py` — `_params_billions` parsing, the size→role thresholds, the disk-size fallback (÷0.6) — are the role boundaries right? Edge: 0 models, unreachable Ollama, models with no `parameter_size`.
- `src/ollarma/swarm/engine.py` — confirm the revert left no dead/contradictory code; the NOTE comment is accurate.
- Publication docs (`docs/publication/`) — spot-check 3 claims in `CLAIMS_LEDGER.md` against the code/evidence. Any claim above its tier?

## 6. What to report back (via the SWE bridge or REVIEW.md)

For each section, one of: **CONFIRMED** / **REFUTED (evidence)** / **COULD-NOT-REPRODUCE**. Specifically:
- Test/gate counts (T1–T6) — exact numbers.
- Any stress cliff found (§3) with the load at which it appeared.
- Verdict-logic recomputation (§4.1/4.2) — your independent JSD numbers.
- Pre-registration ordering + threshold integrity (§4.3/4.4) — PASS/FAIL.
- Any bug in the new scripts (§5) with a repro.
- Top 3 risks you'd block a release on.

Log findings to the per-project SWE session (`~/.ollarma/swe_session/projects/ollarma/`) per `docs/CROSS_AGENT_REVIEW_LOGGING.md`, or write `REVIEW.md` at repo root. Be specific; cite file:line and commit hashes.

## 7. Hard stop rules (do not violate)
- Do NOT relabel any FAIL/PARTIAL as PASS. Do NOT change the `0.02`/`0.05` thresholds.
- Do NOT run a heavy GPU swarm and a stress test concurrently (contaminates timing; single-writer).
- Do NOT push, tag, or promote a release. Review only.
- If you fix anything, do it under a fresh pre-registration for swarm-engine changes — no silent patch-and-rerun.
