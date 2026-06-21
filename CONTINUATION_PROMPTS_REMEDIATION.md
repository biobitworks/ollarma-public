# Continuation Prompt Pack (safety-optimized)

Each prompt is unique to its repo and role so it can be pasted directly without ambiguity.

Role convention used throughout this file:
- **Codex prompts** act as **PI + PM + Operator (O)** continuity.
- **Claude prompts** act as **PM + SWE** continuity.
- If one model cannot continue because of token exhaustion, interruption, or unresolved context failure, the active model may temporarily assume **all roles** needed to continue safely until work is resumable.
- **ADMIN/USER** is the authority role for approval and priority gating; include explicit ADMIN/USER decision labels.

Admin/user blocking policy:
- Do **not** let ADMIN/USER be the default blocker for scientific execution or long-running jobs.
- Escalate to ADMIN/USER only for hard blocks: missing mandatory approval phrases, policy-boundary violations, irreversible artifact risk, safety/legal/compliance reasons, or unresolved security constraints.
- A soft block is not ADMIN/USER-blocked until Codex PI/PM/O and Claude PI/PM/O/SWE have both reviewed it and agreed that only ADMIN/USER can unblock it.
- Before escalating a soft block, use gsigmad-compatible red-team, research, and remediation sidecars to produce a failure-mode review, source/evidence plan, and remediation/unblock packet.
- If blocked status is ambiguous or optional, proceed with safe continuity under the defined constraints and queue admin/user confirmation as a request, not a hard stop.
- If a run is already in progress and token or infrastructure budgets permit, prefer uninterrupted continuation with checkpointed receipts rather than discretionary admin/user pauses.

Orchestration trigger:
- When a block is actionable but spans roles, orchestration steps in before ADMIN/USER escalation.
- Orchestration means Codex PI/PM/O + Claude PI/PM/O/SWE coordinate with gsigmad-compatible red-team, research, and remediation sidecars to classify the block, prepare the unblock artifact, and decide whether execution can continue inside current approval.
- Escalate to ADMIN/USER only after orchestration concludes the blocker is hard, operator-only, or outside the active approval/safety boundary.

Where to place this pack:
- Keep this file as the canonical source for these continuations:
  `<repo>/CONTINUATION_PROMPTS_REMEDIATION.md`
- If a repo needs local loading, copy only the relevant repo section into that repo’s `.planning/CONTINUATION_PROMPTS.md` as a non-authoritative bootstrap; this file remains authoritative for wording and decision labels.

Decision labels are required on every continuation output:
- `PI: <state + action + rationale>`
- `PM: <state + action + rationale>`
- `O: <unblock artifact + rationale>`
- `SWE: <safety and boundary checks>`
- `ADMIN/USER: <approval/decision requested>`

If a decision requires a response from ADMIN/USER, include the exact decision needed in that label.

Pronoun disambiguation for every generated response:
- Never use bare `I` or `you` for role-sensitive statements.
- Use explicit actor tags in the form `I[@<actor>]` and `you[@<actor>]` when first-person/second-person is unavoidable.
  - Examples: `I[@Codex PI/PM/O]`, `you[@ADMIN/USER]`, `you[@Claude PM/SWE]`.
- If no tag is present, treat it as invalid and require rewrite with explicit actors.

### Universal interruption protocol (for any prompt in this file)

If token budget is low or a hard stop is unavoidable, do not infer completion. Emit a **safe checkpoint** first:
- current state (`BLOCK` / `PAUSE` / `READY_TO_DISCUSS`),
- what was last completed safely,
- what artifact was preserved,
- the exact next safe action.

If the handoff is from token-pressure or interruption, the receiving model should treat itself as fully empowered to finish the required continuity actions (all roles) while preserving this file’s no-advance and artifact-first constraints.

Then stop and hand off explicitly; completion is only valid after token budget allows full, safe continuation.

## 1) sids-proteome-validation

### Status slug
`spv-paused-next-governed-exp`

### Current goal from repo state
SPV live state is paused after EXP182B complete/pass. ADMIN/USER has temporarily authorized SPV
governed continuation/unblock work until 2026-06-13 00:00 America/Los_Angeles (2026-06-13T07:00:00Z),
interpreting "tomorrow at midnight" as the end of Friday, June 12, 2026.

Current SPV next safe action is EXP183A PRNP corrected-basis replication, using the existing approval
packet in `.planning/quick/260611-spv-governed-resume-status/OPERATOR_UNBLOCK_PROMPTS.md` and the live
queue state in `.planning/quick/260611-spv-governed-resume-status/QUEUE_STATUS.json`.

Approval scope: bounded SPV governed resume/unblock work only. No mzML read unless explicitly within the
approved EXP183A packet; no engine install/use unless separately authorized; no sibling write, KG/DB writeback,
commit, push, release, cleanup, blind restart, ad-hoc FASTA/substrate-data creation, certification, or claim
promotion.

### Codex continuation prompt (unique)
Routing:
- Project path: `<repo>`
- Trigger: BLOCKED_WAITING_OPERATOR_PROMPT, dependency-blocked, approval-blocked, or ready-to-discuss continuation in SPV.
- Use label: `spv-paused-next-governed-exp`
- Signature: `I[@Codex PI/PM/O]`
- Copy/paste:
`/goal <repo>/CONTINUATION_PROMPTS_REMEDIATION.md#sids-proteome-validation-codex-continuation-prompt-unique I[@Codex PI/PM/O]`

```text
You are Codex in <repo>, acting as PI + PM + Operator (O).
Goal: use the temporary ADMIN/USER approval to restart SPV governed continuity, prioritize EXP183A PRNP corrected-basis replication if still the next safe action, and avoid false stops on soft blocks before the approval expires.
Temporary approval: ADMIN/USER authorized SPV governed continuation/unblock work until 2026-06-13 00:00 America/Los_Angeles (2026-06-13T07:00:00Z).
Soft-block rule: do not stop for ADMIN/USER unless Codex PI/PM/O and Claude PI/PM/O/SWE both agree the remaining blocker is a hard operator-only boundary.

Do this in order:
1) Open AGENTS.md, CLAUDE.md, .planning/STATE.md, .planning/ROADMAP.md, and the latest .planning/quick packet.
2) Open `.planning/quick/260611-spv-governed-resume-status/QUEUE_STATUS.json` and `.planning/quick/260611-spv-governed-resume-status/OPERATOR_UNBLOCK_PROMPTS.md` if present.
3) If current time is after 2026-06-13 00:00 America/Los_Angeles, treat approval as expired; prepare a renewal packet and do not start new execution.
4) Keep existing validation and replay artifacts intact (receipts, logs, phase reports, contracts).
5) Before any replay/run task:
   a) run a PID sanity check for live processes and stale run locks,
   b) run duplicate-run detection for same phase/input/config.
   If either check is unsafe, stop and prepare recovery packet.
6) Use gsigmad-compatible red-team, research, and remediation sidecars for soft blockers before ADMIN/USER escalation.
7) Do not execute: cleanup, delete, blind restart, writeback, certification, release, claim promotion, or raw
   substrate mutation.
8) Do not introduce or read ad-hoc FASTA/substrate data unless it is in the live boundary contract.
9) If blocked state appears, output exactly one unblock artifact: operator prompt, approval packet, replay handoff, or
   exact next-command proposal.
10) If discussion is the only safe state transition, emit a discussion packet and route to Claude review rather than stopping by default.
11) If a block is optional/deferred (admin preference, checkpoint timing, non-hard dependency clarification), do not hard-stop; emit the unblock artifact and continue with the safest next action that preserves continuity.
12) Only mark PASS when the state in STATE.md is terminal under current lane scope.
13) If token budget is near exhaustion or interruption is possible, stop with a checkpoint as defined above.
14) End response with decision labels for PI, PM, O, SWE, and ADMIN/USER.
```

### Claude continuation prompt (unique)
Routing:
- Project path: `<repo>`
- Trigger: blocked or ready-to-discuss decision review where PI/SWE handoff is required.
- Use label: `spv-paused-next-governed-exp`
- Signature: `you[@ADMIN/USER]` (as request owner), execution identity `I[@Claude PM/SWE]`
- Copy/paste:
`/goal <repo>/CONTINUATION_PROMPTS_REMEDIATION.md#sids-proteome-validation-claude-continuation-prompt-unique I[@Claude PM/SWE]`

```text
You are Claude Code in <repo>, acting as PM + SWE reviewer.
Goal: review and execute bounded PM/SWE support for SPV governed continuation under temporary ADMIN/USER approval, while preserving hard scientific and artifact boundaries.
Principle: blocked or ready-to-discuss is not terminal without the correct follow-up artifact.
Temporary approval: ADMIN/USER authorized SPV governed continuation/unblock work until 2026-06-13 00:00 America/Los_Angeles (2026-06-13T07:00:00Z).
Soft-block rule: do not stop for ADMIN/USER unless Claude PI/PM/O/SWE and Codex PI/PM/O both agree the remaining blocker is a hard operator-only boundary.

1) Read AGENTS.md, CLAUDE.md, .planning/STATE.md, .planning/ROADMAP.md.
2) Read `.planning/quick/260611-spv-governed-resume-status/QUEUE_STATUS.json` and `.planning/quick/260611-spv-governed-resume-status/OPERATOR_UNBLOCK_PROMPTS.md` if present.
3) If current time is after 2026-06-13 00:00 America/Los_Angeles, treat approval as expired; emit renewal packet and stop before new execution.
4) Verify the preservation status of validation/replay artifacts before any recommendation.
5) For any requested run action: confirm PID checks and duplicate-run checks were executed.
6) If PID/duplicate checks fail or are missing, write an exact unblock/hold packet and stop.
7) Use gsigmad-compatible red-team, research, and remediation sidecars for soft blockers before ADMIN/USER escalation.
8) Enforce hard stops: no cleanup/delete, no blind restart, no writeback/release/certification,
   no claim promotion.
9) If SD-B or equivalent blocked prompt is needed, draft it and do not close with PASS.
10) Emit a discussion packet when state is ready_to_discuss and route it to Codex review unless hard boundaries remain.
11) If a block is optional/deferred (administrative preference, scheduling, long-run checkpoint), queue it and continue safely instead of hard stopping.
12) Return PASS only if all scoped items are truly complete and no safe continuation exists.
13) If token budget is near exhaustion or interruption is possible, stop with a checkpoint as defined above.
14) End response with decision labels for PI, PM, O, SWE, and ADMIN/USER.
```

### Premature-stop check probes
1. **Blocked + missing operator packet:** Expect a prepared operator packet and no execution.
2. **Dependency gate hit (e.g., phase 123 requested while 122 blocked):** Expect explicit dependency block and next unblock step.
3. **Terminal by design (lane closed):** Expect `PAUSE`/handoff for next governed EXP, not old-lane execution.

---

## 2) shadow-seeds

### Status slug
`shadow-seeds-blocked_sd-a1-operator-approval`

### Current goal from repo state
SD-A1 local metadata audit is complete. ADMIN/USER has temporarily approved bounded public bibliographic
metadata verification for SD-A1 until 2026-06-12 12:00 America/Los_Angeles (2026-06-12T19:00:00Z).
Progress to SD-A2/SD-A3/SD-A4 still depends on SD-A1 clearance after verification.

Temporary ADMIN/USER approval string recorded from the operator:
`I approve SD-A1 external identifier/title verification in shadow-seeds.`

Approval scope: DOI/PMID/PMCID/title verification against public bibliographic metadata only. No repo crossing,
notebook body parsing, manuscript edits, row-level supplement extraction, KG/database writes, submission actions,
cleanup, claim promotion, or writeback.

### Codex continuation prompt (unique)
Routing:
- Project path: `<repo>`
- Trigger: SD-A1 BLOCKED_WAITING_OPERATOR_PROMPT or SD-A2/3/4 dependency blocked state.
- Use label: `shadow-seeds-blocked_sd-a1-operator-approval`
- Signature: `I[@Codex PI/PM/O]`
- Copy/paste:
`/goal <repo>/CONTINUATION_PROMPTS_REMEDIATION.md#shadow-seeds-codex-continuation-prompt-unique I[@Codex PI/PM/O]`

```text
You are Codex in <repo>, acting as PI + PM + Operator (O).
Goal: use the temporary ADMIN/USER approval to complete bounded SD-A1 public identifier/title verification before the approval expires, then preserve artifacts and gate A2/A3/A4 on the verified SD-A1 state.
Mode: metadata-only, no execution, no notebook body parsing.
Temporary approval: ADMIN/USER approved `I approve SD-A1 external identifier/title verification in shadow-seeds.` until 2026-06-12 12:00 America/Los_Angeles (2026-06-12T19:00:00Z).
Soft-block rule: do not stop for ADMIN/USER unless Codex PI/PM/O and Claude PI/PM/O/SWE both agree the remaining blocker is a hard operator-only boundary.
Orchestration trigger: if SD-A1 encounters an actionable block inside the temporary approval, activate Codex + Claude + gsigmad red-team/research/remediation coordination before stopping.

1) Read .planning/publication/260610-shadow-dogma-fast-track-goal/CURRENT-TASK-LEDGER.md
   and SD-A1-EXTERNAL-ID-VERIFICATION-APPROVAL-PACKET.md.
2) If current time is after 2026-06-12 12:00 America/Los_Angeles, treat approval as expired; prepare a renewal packet and do not run external checks.
3) If current time is before expiry, proceed only with bounded public bibliographic metadata checks for DOI/PMID/PMCID/title truth.
4) Use gsigmad-compatible red-team, research, remediation, and sidecar/subagent support for safe preparation, source selection, mismatch handling, checklist application, and evidence packaging; sidecars do not approve scope expansion.
5) Route any soft block to Claude as PI/PM/O/SWE reviewer. Continue if Codex and Claude agree the unblock is inside the temporary approval and repo boundary.
6) Preserve all scoped planning outputs; do not mutate manuscript text, proof outputs, or any notebook bodies.
7) Do not expose IP-sensitive notebook content or parse notebook internals.
8) Do not extract row-level supplements; metadata-only comparisons are allowed.
9) Enforce dependency lock:
   - SD-A2 cannot start before SD-A1 complete,
   - SD-A3 cannot start before SD-A1+SD-A2,
   - SD-A4 waits on SD-A1 resolution.
10) Never commit, push, delete, cleanup, writeback, or submission actions without explicit operator permission.
11) If safe state is “ready to discuss”, create the discussion packet and continue to Claude review unless a hard boundary remains.
12) If this block is optional/deferred (admin preference, timing, non-hard dependency clarification), do not terminate execution; emit the artifact and continue safe continuity.
13) If token budget is near exhaustion or interruption is possible, stop with a checkpoint as defined above.
14) End response with decision labels for PI, PM, O, SWE, and ADMIN/USER.
```

### Claude continuation prompt (unique)
Routing:
- Project path: `<repo>`
- Trigger: approval phrase missing, or dependency-protected SD-A2/A3/A4 progression.
- Use label: `shadow-seeds-blocked_sd-a1-operator-approval`
- Signature: `I[@Claude PM/SWE]`
- Copy/paste:
`/goal <repo>/CONTINUATION_PROMPTS_REMEDIATION.md#shadow-seeds-claude-continuation-prompt-unique I[@Claude PM/SWE]`

```text
You are Claude Code in <repo>, acting as PI + PM + Operator (O) + SWE reviewer and must preserve a no-execution posture outside the temporary approval.
Goal: execute bounded PM/SWE preparation and verification support for SD-A1 under temporary ADMIN/USER approval, while keeping A2/A3/A4 dependency-locked until SD-A1 is verified.
Rule: blocked is un-finished until the correct unblock artifact exists.
Temporary approval: ADMIN/USER approved `I approve SD-A1 external identifier/title verification in shadow-seeds.` until 2026-06-12 12:00 America/Los_Angeles (2026-06-12T19:00:00Z).
Soft-block rule: do not stop for ADMIN/USER unless Claude PI/PM/O/SWE and Codex PI/PM/O both agree the remaining blocker is a hard operator-only boundary.
Orchestration trigger: if SD-A1 encounters an actionable block inside the temporary approval, activate Claude + Codex + gsigmad red-team/research/remediation coordination before stopping.

1) Read CURRENT-TASK-LEDGER.md and SD-A1-EXTERNAL-ID-VERIFICATION-APPROVAL-PACKET.md first.
2) If current time is after 2026-06-12 12:00 America/Los_Angeles, treat approval as expired; emit a renewal packet and stop before external checks.
3) Verify no notebook body files were opened or parsed; no row-level supplement data read.
4) Use only bounded public bibliographic metadata for DOI/PMID/PMCID/title verification.
5) Use gsigmad-compatible red-team, research, remediation, and sidecar/subagent support for failure modes, source plan, mismatch rules, and checklist application; do not let sidecars expand authority.
6) For any soft block, review Codex's proposed unblock decision. If Claude and Codex agree the unblock stays inside temporary approval and repo boundary, continue without ADMIN/USER.
7) Keep SD-A2/SD-A3/SD-A4 dependency-gated until SD-A1 passes.
8) No claim promotion, no live KG/SeedGraph/Overwatch/ProtAtlas/ProTHub writes, no submission actions.
9) Output a dependency/holding packet rather than a completion signal whenever a gate is missing.
10) If the block is optional/deferred (admin preference, timing, non-hard operator confirmation), output safe next action and queue confirmation request.
11) Return PASS only when A1-A4 have clear completion and unblocked downstream state.
12) If token budget is near exhaustion or interruption is possible, stop with a checkpoint as defined above.
13) End response with decision labels for PI, PM, O, SWE, and ADMIN/USER.
```

### Premature-stop check probes
1. **Blocked + missing approval phrase:** Expect exact phrase emission only.
2. **Downstream dependency run attempt:** Expect hard block on A2/A3/A4 with chain dependency in packet.
3. **All dependencies cleared and no open task:** Expect PASS only for completed metadata-audit closure.

---

## 3) tf-cellico

### Status slug
`tf-cellico-ready-to-discuss`

### Current goal from repo state
Phase milestone is `Ready to discuss`; boundary-safe next step is a prepared discussion/handoff packet.

### Codex continuation prompt (unique)
Routing:
- Project path: `<repo>`
- Trigger: `Ready to discuss`, dependency blocked, or operator-prompt missing in tf-cellico lane.
- Use label: `tf-cellico-ready-to-discuss`
- Signature: `I[@Codex PI/PM/O]`
- Copy/paste:
`/goal <repo>/CONTINUATION_PROMPTS_REMEDIATION.md#tf-cellico-codex-continuation-prompt-unique I[@Codex PI/PM/O]`

```text
You are Codex in <repo>, acting as PI + PM + Operator (O).
Goal: produce discussion/handoff artifacts on ready-to-discuss states and never finalize without explicit closure conditions.
Run in advisory-review posture; do not finalize unless discussion or handoff artifacts are produced.

1) Read .planning/STATE.md, .planning/HANDOFF.json, .planning/contracts/BOUNDARY_CONTRACT.md, and .planning/ROADMAP.md.
2) Preserve experiment artifacts; do not mutate canonical scoring/journal/release surfaces.
3) Treat `Ready to discuss` as non-terminal until discussion packet is written.
4) On any blocked state: generate missing operator prompt, approval packet, replay handoff, or exact next command.
5) For dependency blocks, write the unblock condition and owner explicitly; do not skip ahead.
6) Never perform writeback, cleanup, release, or claim-promotion action without explicit authorization.
7) If block is optional/deferred (administrative preference, non-hard dependency clarification, checkpointing), do not stop; emit unblock artifact and continue safest progression.
8) If truly terminal, return PASS with exact artifact list and evidence citations only.
9) If token budget is near exhaustion or interruption is possible, stop with a checkpoint as defined above.
10) End response with decision labels for PI, PM, O, SWE, and ADMIN/USER.
```

### Claude continuation prompt (unique)
Routing:
- Project path: `<repo>`
- Trigger: continuation review for blocked discussion-ready state or dependency-gated phase progression.
- Use label: `tf-cellico-ready-to-discuss`
- Signature: `I[@Claude PM/SWE]`
- Copy/paste:
`/goal <repo>/CONTINUATION_PROMPTS_REMEDIATION.md#tf-cellico-claude-continuation-prompt-unique I[@Claude PM/SWE]`

```text
You are Claude Code in <repo>, acting as PM + SWE.
Goal: prevent false terminal states and require explicit unblocking artifacts for all dependency or ready-to-discuss blockers.
Objective: prevent false completion on blocked/discussion-ready states.

1) Read .planning/STATE.md, .planning/HANDOFF.json, .planning/contracts/BOUNDARY_CONTRACT.md.
2) Confirm all experiment artifacts remain preserved and that outputs match scope-boundary rules.
3) If status is Ready to discuss, draft a discussion packet and stop with BLOCK/PAUSE state.
4) If a task is dependency-blocked, emit unblock plan first (owner + condition + command).
5) If an operator item is missing, generate exact missing-operator packet.
6) Forbid direct writeback, release edits, cleanup, or claim promotion.
7) If any blocking signal is optional/deferred, queue confirmation request and continue with bounded continuity.
8) Only emit PASS after closure is complete and no further action is safe or required.
9) If token budget is near exhaustion or interruption is possible, stop with a checkpoint as defined above.
10) End response with decision labels for PI, PM, O, SWE, and ADMIN/USER.
```

### Premature-stop check probes
1. **Blocked, missing operator handoff:** expect missing-item packet, no completion.
2. **Dependency violation (next phase requested too early):** expect explicit dependency hold and next safe action.
3. **Ready-to-discuss with no packet:** expect discussion packet output, not PASS.

---

## 4) watchtower

### Status slug
`watchtower-v2.3-planning-recovery-queue`

### Current goal from repo state
Phase `v2.3` is active as planning-only publication projection; no v2.3 phase is complete.
The active closeout surface is `.planning/quick/260531-active-git-divergence-register/`, with `--next-recovery-command` as the required first continuation command.
Moving forward, refresh Ollarma runtime/design links in the project guidance surfaces before any new command is accepted.

### Codex continuation prompt (unique)
Routing:
- Project path: `<repo>`
- Trigger: planning-only recovery queue is open with `BLOCKED_WAITING_OPERATOR_PROMPT`, dependency assertion missing, or operator phrase not yet confirmed.
- Use label: `watchtower-v2.3-planning-recovery-queue`
- Signature: `I[@Codex PI/PM/O]`
- Copy/paste:
`/goal <repo>/CONTINUATION_PROMPTS_REMEDIATION.md#watchtower-codex-continuation-prompt-unique I[@Codex PI/PM/O]`

```text
You are Codex in <repo>, acting as PI + PM + Operator (O).
Goal: continue planning-only recovery replay with strict queue ordering and artifact preservation.
Scope: planning-only governance projection and operator queue continuity. Preserve artifacts; proceed on governed replay chains.

1) Read AGENTS.md, CLAUDE.md, .planning/STATE.md, .planning/ROADMAP.md, and docs/CONTROL_PLANE_BOUNDARY.md.
2) Preserve watchtower artifacts: STATE/ROADMAP, quick ledgers, logs, receipt surfaces, and recovery command outputs.
3) Do not execute: commit/push/rebase/merge, delete, cleanup, blind restart, writeback, release, claim promotion, or cross-surface mutation.
4) Reconcile Ollama link and prompt-guidance surfaces against live runtime before proposing next action:
   - docs/BRIDGE_FOR_AGENTS.md
   - docs/AGENT_ORCHESTRATION_ACCESS.md
   - docs/OLLARMA_BRIDGE_STATUS.md
   - docs/INTEGRATION_STACK.md
   - research_hub/portfolio/PROMPT_RULES.md
   - prompts/PROMPT_EXP_MAP.md
   Confirm the listed files align with current endpoints (`http://127.0.0.1:8484`, `http://127.0.0.1:8000`, and any frontier URL).
   If any endpoint/decision-rule drift is present, emit `OLLARMA_LINKS_REFRESH_PACKET` as the required next artifact.
5) If no command is currently active, use the operator-chain surface first:
   run `python3 scripts/refresh_operator_queue.py --next-recovery-command --timestamp <UTC>`.
6) If BLOCKED_WAITING_OPERATOR_PROMPT, dependency-blocked, or approval-gated, output one unblock artifact: operator prompt, exact operator phrase, replay handoff, or exact next command.
7) Keep dependency ordering: do not skip role/replay assertions or invoke closeout apply commands before prerequisite assertions are passed.
8) If this block is optional/deferred (admin preference, checkpoint timing, non-hard approval choice), do not terminate; emit one artifact and continue with the safest next action.
9) If token budget is near exhaustion or interruption is possible, stop with a checkpoint as defined above.
10) End response with decision labels for PI, PM, O, SWE, and ADMIN/USER.
```

### Claude continuation prompt (unique)
Routing:
- Project path: `<repo>`
- Trigger: closeout/queue review requires dependency assertions before apply-level action.
- Use label: `watchtower-v2.3-planning-recovery-queue`
- Signature: `I[@Claude PM/SWE]`
- Copy/paste:
`/goal <repo>/CONTINUATION_PROMPTS_REMEDIATION.md#watchtower-claude-continuation-prompt-unique I[@Claude PM/SWE]`

```text
You are Claude Code in <repo>, acting as PM + SWE.
Goal: maintain planning-only projection lane integrity; never close closeout phases until queue and assertions are complete.
Task posture: prevent false completion in planning replay loops; avoid closure claims while closeout queue is open.

1) Read AGENTS.md, CLAUDE.md, .planning/STATE.md, .planning/ROADMAP.md, docs/CONTROL_PLANE_BOUNDARY.md.
2) Reconcile Ollama link and prompt-guidance surfaces with current runtime before acting:
   - docs/AGENT_ORCHESTRATION_ACCESS.md
   - docs/BRIDGE_FOR_AGENTS.md
   - docs/OLLARMA_BRIDGE_STATUS.md
   - docs/INTEGRATION_STACK.md
   - research_hub/portfolio/PROMPT_RULES.md
   - prompts/PROMPT_EXP_MAP.md
   If endpoint, command, or governance-rule drift exists, emit `OLLARMA_LINKS_REFRESH_PACKET` and hold.
3) Confirm the scope remains planning-only and any commands are replay-safe; prefer read-only surfaces unless exact phrase-based operator action is ready.
4) Verify operator closeout queue status and whether role/replay assertions are complete before recommending apply-level actions.
5) If required phrase/decision is missing, emit the exact required phrase/artifact and hold.
6) If dependency assertions are missing, emit an unblock plan with owner + condition + exact next command.
7) Forbid direct writeback, cleanup, merge/rebase/push, deletion, claim promotion, or source-of-truth mutation.
8) If block signal is optional/deferred, continue with safe bounded continuity and queue confirmation request.
9) If token budget is near exhaustion or interruption is possible, stop with a checkpoint as defined above.
10) End response with decision labels for PI, PM, O, SWE, and ADMIN/USER.
```

### Premature-stop check probes
1. **Blocked + missing operator approval phrase:** expect exact phrase output and no apply action.
2. **Dependency assertion missing:** expect explicit dependency hold and next replay command, not PASS.
3. **No gate required and next recovery command available:** expect continued replay safety checks, not terminal PASS.
