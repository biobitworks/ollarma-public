# Current Task

## Scope

Implement and validate **PROMPT-OLLARMA-SWARM-001** — local-GPU predictive
persona swarm engine. N=50 personas x M=5 deliberative rounds on
qwen2.5-coder:7b via Ollama on M1 Pro Metal GPU. Sister to (not duplicate of)
the Antigence adjudicative swarm (N=15 x 1, no inter-agent visibility,
auto-fires when primary confidence < 0.7).

Reserved: EXP-OLLARMA-SWARM-001 (acceptance run on 5 pre-registered scenarios
incl. inert-control falsification check).

## Objectives

- [x] T1 — Register PROMPT-OLLARMA-SWARM-001 (MD + JSON + PROMPT_EXP_MAP) — `85b1a97` + `36ad5f9`
- [x] T2 — `src/ollarma/swarm/schemas.py` — `e2b5b1f`
- [x] T3 — `src/ollarma/swarm/persona_bank.py` — `e2b5b1f`
- [x] T4 — `src/ollarma/swarm/engine.py` — `101be41`
- [x] T5 — `src/ollarma/swarm/aggregator.py` — `101be41`
- [x] T6 — `src/ollarma/swarm/throttle.py` — `101be41`
- [x] T7 — `tests/swarm/test_engine_smoke.py` — `ec18940`
- [x] T8 — `tests/swarm/test_thermal_smoke.py` — `ec18940`
- [ ] **T9** — Pre-registered acceptance run on 5 benchmark scenarios (≤7.5h sequential against real Ollama; operator action — needs `qwen2.5-coder:7b` resident + thermal-stable M1 Pro)
- [ ] T10 — `/gsigmad-audit-output` on acceptance verdict bundle (after T9)
- [ ] T11 — Close PROMPT (flip status in PROMPT_EXP_MAP, commit audit receipt) (after T10)
- [ ] T12 — Surface API to consumers (README + `examples/`; cross-ref vitaology Phase 7) (after T11)
