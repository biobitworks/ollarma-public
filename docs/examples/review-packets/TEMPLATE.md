# Review request -> <reviewer>: <case slug>

**From:** <runtime/agent>
**Repo:** <absolute repo path or project key>
**Date:** <YYYY-MM-DD>
**Status:** requested
**Decision authority:** review-only unless the operator delegates more

## Problem

What failed, what kept happening, and why it matters.

## Minimal Conversation Slice

- User/operator instruction, redacted to the minimal relevant text.
- Agent/model answer or command, redacted to the minimal relevant text.
- Follow-up correction or observed loop, if needed.

## Runtime Evidence

- `.ollarma/bridge/events.jsonl` event ids / run ids:
- `.ollarma/session-log.jsonl` entries:
- gateway/admission/frontier receipt ids:
- run artifact paths and hashes:
- `~/.ollarma/swe_session/events.jsonl` handoff ids:

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
