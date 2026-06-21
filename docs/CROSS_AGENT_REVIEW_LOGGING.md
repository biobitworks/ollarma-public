# Cross-Agent Review Logging

**Status:** current-state contract plus gap statement.
**Audience:** operators, Codex/Claude Code lanes, Antigence reviewers, and
sibling-project owners using Ollarma as a local bridge.

## Answer

Ollarma keeps ongoing local evidence when other agents and projects use it, but
it does **not** currently keep a complete raw back-and-forth conversation corpus
by default.

That distinction is intentional:

- Runtime evidence should be durable, replayable, and redacted enough for
  audit.
- Raw chat transcripts can contain secrets, private operator context, unrelated
  project state, and high-noise model text.
- Bug recreation needs a smaller artifact: the prompt/response slices,
  commands, receipts, hashes, environment, and expected-vs-actual behavior that
  caused the failure.

The current posture is therefore:

```text
local agent/project use
  -> Ollarma receipts and bridge events
  -> optional SWE session handoff/review request
  -> redacted review packet for Antigence/Ollarma when a case matters
```

## Evidence Streams Today

| Stream | Path | Captures | Current limit |
|---|---|---|---|
| Scribe session log | `.ollarma/session-log.jsonl` | Progress notes, task state, artifacts, decisions, next action | Not a full transcript; entries depend on callers/hooks writing useful notes |
| Resume summary | `.ollarma/RESUME.md` | Human-readable summary regenerated from scribe entries | Summary only; regenerable |
| Bridge event spine | `.ollarma/bridge/events.jsonl` | `chat_started`, `chat_done`, `route_*`, blocked/escalated events, payload hashes, bounded previews, receipt refs | Redacted/previews only; does not persist full model text |
| Gateway receipts | `.ollarma/gateway/admissions.jsonl`, `.ollarma/gateway/receipts.jsonl` | Admission and frontier-call receipt chain | Gateway only; no raw API keys or provider payload dumps |
| Recovery/incidents | `.ollarma/incidents/*.json`, `.ollarma/recovery_block_receipts.jsonl` | Stranded work, admission blocks, recovery state | Recovery state, not conversation history |
| Agent run receipts | `.ollarma/agent_receipts.json` | Per-agent run provenance where available | Run-level metadata, not full back-and-forth |
| Shared SWE session log | `~/.ollarma/swe_session/events.jsonl` | Cross-agent handoffs, operator-visible guidance, lane status | Event/handoff oriented; outside repo; not a full transcript |
| Review requests | `~/.ollarma/swe_session/review-requests/*.md` | Redacted case files for Codex/Claude/Antigence review | Manual or hook-created; not yet enforced as the default bug packet |

All `.ollarma/` state is local-only and gitignored. It is evidence for the
machine where the event happened, not a source tree artifact.

## What Is Missing

The missing piece is not "save every conversation." The missing piece is a
standard **review packet** that captures enough of a problematic exchange for
Antigence and Ollarma to review, fix bugs, and recreate tests.

Without a review packet, a later reviewer may see:

- a route or chat happened;
- a model was selected;
- a request was blocked or answered;
- a selection or gateway condition was degraded.

But they may not have enough to know:

- which exact user/agent instruction triggered the bug;
- which response text or command caused the bad behavior;
- which file or API surface should receive the regression test;
- which reproduction command proves the fix.

## Review Packet Contract

When an Ollarma interaction exposes a bug, recurring agent failure, unsafe
behavior, or research-relevant model behavior, create a redacted review packet
under:

```text
~/.ollarma/swe_session/review-requests/YYYY-MM-DD-<slug>.md
```

For committed examples or stable protocol fixtures, add a redacted copy under:

```text
docs/examples/review-packets/
```

Use this structure:

```markdown
# Review request -> <reviewer>: <case slug>

**From:** <runtime/agent>
**Repo:** <absolute repo path or project key>
**Date:** <YYYY-MM-DD>
**Status:** requested | staged | reviewed | closed
**Decision authority:** review-only unless an operator explicitly delegates more

## Problem

What failed, what kept happening, and why it matters.

## Minimal Conversation Slice

- User/operator instruction, redacted to the minimal relevant text.
- Agent/model answer or command, redacted to the minimal relevant text.
- Follow-up correction or observed loop, if needed.

Do not paste unrelated transcript history.

## Runtime Evidence

- `.ollarma/bridge/events.jsonl` event ids / run ids
- `.ollarma/session-log.jsonl` entries
- gateway/admission/frontier receipt ids, if any
- run artifact paths and hashes
- relevant `~/.ollarma/swe_session/events.jsonl` handoff ids

## Reproduction

Exact command, HTTP request, fixture, or test that reproduces the issue.

## Expected vs Actual

Expected:
Actual:

## Safety / Governance Notes

Secrets redacted? Raw provider payloads omitted? Claim ceiling?
Does this require Antigence, gsigmad, Watchtower, Overwatch, or operator review?

## Requested Review

Specific questions for Ollarma/Antigence/Codex/Claude.

## Verdict

Reviewer appends findings, required fixes, tests added, and verification.
```

## Antigence/Ollarma Review Path

Antigence review of Ollarma chat/route output is planned in
`docs/ANTIGENCE_RTB_03_REVIEWED_CHAT_PLAN.md`; it is not shipped behavior until
that RTB slice is implemented and verified.

Until then:

- Ollarma should emit receipts and bridge events for runtime evidence.
- Agents should create review packets for failures or research-worthy cases.
- Antigence can review the redacted packet and local evidence, but it should not
  be treated as an automatic live reviewer for every `/chat` or `/route` call.
- Mutating work should remain governed by the existing deterministic execution,
  workflow, gateway, and operator-review gates.

## What Operators Should Do

Use full raw transcripts only as local, operator-controlled forensic material.
For normal cross-agent research and bug work, preserve this smaller evidence
bundle:

1. The minimal prompt/response/command slice.
2. The bridge event ids and receipt refs.
3. The exact reproduction command or HTTP body.
4. The expected-vs-actual behavior.
5. The regression test or fixture path added after the fix.

That gives future Ollarma and Antigence reviewers enough context to fix the
bug and recreate tests without turning the local runtime into a broad transcript
surveillance store.
