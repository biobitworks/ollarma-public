# Push / Review Bundle — prepared 2026-06-02

**Status: PREPARED, NOT PUSHED. Awaiting operator approval.**
Nothing has been pushed, committed, amended, or rebased. All local commits are as-is.

---

## 1. Ollarma — branch `main` (origin/main → 5 commits behind local)

### Requested commits (4)
| Commit | Type | Summary |
|--------|------|---------|
| `07854d8` | fix(http) | Reserved-model denials → HTTP 403 (`RESERVED_MODEL_ANTIGENCE_SENTINEL`); cover dashboard missing-root degrade (`PROJECT_ROOT_MISSING`) |
| `b532bca` | fix(bench) | Skip roster models absent from `ollama list` instead of aborting the whole run |
| `e2d5d48` | fix(bench) | Per-model backstop — one un-runnable model (e.g. embedding `nomic-embed-text`) can't abort the run |
| `6478d9a` | chore(gsigmad) | Untrack `.gsigmad/degradation.json` (runtime ops state, not evidence); now gitignored |

Files touched (all within `src/ollarma/`, `tests/`, `.planning/quick/`, `.gitignore`, `.gsigmad/`):
`http_api.py`, `executor.py`, `service.py`, `test_http_api.py`, `test_executor.py`,
`test_benchmark_guard.py`, two `.planning/quick/260531-vge-*` notes, `.gitignore`,
removal of tracked `.gsigmad/degradation.json`. **No research artifacts, no cellico paths.**

### ⚠️ Operator decision required — interleaved 5th commit
`git push origin main` pushes the **whole branch**, not a cherry-picked subset.
A 5th unpushed commit sits *between* the requested ones in history:

```
e2d5d48  fix(bench): per-model backstop                      ← requested
6478d9a  chore(gsigmad): untrack degradation.json            ← requested
b532bca  fix(bench): skip absent roster models               ← requested
228b1cc  docs(review): define cross-agent logging packets    ← NOT in your list
07854d8  fix(http): reserved-model 403 + dashboard degrade   ← requested
```

Because `228b1cc` is in the middle, the 4 requested commits **cannot** be pushed
without it while keeping history as-is. Options:
- **(A) Push all 5** — simplest, history untouched. `228b1cc` is a docs-only commit
  (`docs/CROSS_AGENT_REVIEW_LOGGING.md`, review-packet templates/examples) — low risk.
- **(B) Hold the push** until you confirm `228b1cc` is also cleared for release.

I recommend **(A)** unless `228b1cc` is intentionally being held back.

---

## 2. gettingsciencedone — branch `codex/pause-after-phase-06`

### Requested commit (1)
| Commit | Type | Summary |
|--------|------|---------|
| `08a6084` | fix(degradation) | Disambiguate KG `client_not_installed` vs `server_unreachable`; add state TTL (default 3600s) so a frozen `KG_DEGRADED` flag is downgraded to `UNKNOWN`+`stale=true` |

Files touched: `src/gsigmad/.../degradation.py`-domain + `monitoring`, `tests/test_degradation_state.py`.
This is the upstream counterpart to Ollarma `6478d9a` (both kill the stale-health-flag misread).

### ⚠️ Operator decision required — branch carries 8 unpushed commits
`08a6084` is **HEAD** of `codex/pause-after-phase-06`, which is **8 ahead / 129 behind**
its upstream. Pushing the branch carries all 8 (freeze/harden/recovery-docs/backup), not
just `08a6084`. There is also a **dirty working tree** (16 modified files — see §4) that
is **not** part of this commit and would **not** be pushed.
→ Confirm whether you want the full 8-commit branch pushed, or `08a6084` isolated
  (would require a separate branch / cherry-pick — a history change, so held pending your call).

---

## 3. Verification summary

| Suite | Result |
|-------|--------|
| Ollarma — targeted (`test_http_api`, `test_executor`, `test_benchmark_guard`) | ✅ 91 passed |
| Ollarma — **full suite** | ✅ 1627 passed, 1 deselected (134s) |
| gsigmad — `test_degradation_state.py` (the `08a6084` test file) | ✅ 11 passed |
| gsigmad — **full suite** | ⚠️ 1 failed, 1193 passed, 12 skipped, 3 xfailed, 10 xpassed |

Note: `08a6084`'s message says "12 new tests"; collection shows **11**. Cosmetic
count discrepancy only — all 11 pass; no missing/failing test.

---

## 4. Known UNRELATED gsigmad failure (does not block this bundle)

```
FAILED tests/test_skill_bundle.py::test_source_skills_are_mirrored_into_bundle
```

- **Cause:** uncommitted working-tree drift — source `skills/*/SKILL.md` were edited but
  their `src/gsigmad/skill_bundle/skills/*` mirrors are out of sync.
- **Unrelated to `08a6084`:** that commit touches **no** skill/bundle files (verified).
- **Not in this bundle:** the drift lives in uncommitted files (§5), which are excluded.
- The mirror-sync fix is a separate task; flagged, not addressed here.

Uncommitted gsigmad files (excluded — informational only):
`.gsigmad/degradation.json`, `.gsigmad/ledger/governance.jsonl`,
`docs/CONTROL_PLANE_BRIDGE_CONTRACT.md`, `docs/GOVERNED_AUTORUN_PROMPT.md`,
4× `skills/*/SKILL.md`, 3× `src/gsigmad/skill_bundle/skills/*/SKILL.md`,
3× `src/gsigmad/governance/CANON-CORE*`, `tests/test_canon_core.py`, `uv.lock`.

---

## 5. Untracked research artifacts — EXPLICITLY EXCLUDED

Per the goal, these are **not** part of the bundle and will **not** be pushed/committed:

```
clients/STRESS_FINDINGS.md
clients/gpu_vs_cpu_bench.py
clients/model_matrix_probe.py
experiments/coresidency/
```

No cellico changes are involved in any commit in this bundle.

---

## 6. Proposed push commands (run ONLY after approval)

```bash
# Ollarma (Option A — pushes 5 commits incl. docs 228b1cc):
git -C <repo> push origin main

# gettingsciencedone (pushes the 8-commit branch as-is):
git -C <repo> push origin codex/pause-after-phase-06
```

Awaiting your go / no-go (and your call on the two ⚠️ decisions above).
