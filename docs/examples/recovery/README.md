# Recovery Sample Artifacts

Durable reference copies of real recovery packets and block receipts, captured
during v4.4 Phase 48 dogfooding. All machine-specific paths have been redacted
(`<REPO_ROOT>`); timestamps and SHAs are frozen so these files don't drift.

| File | Captures |
|------|----------|
| `sample-packet.recovery-sweep-required.json` | Worst-case state: stranded sidecar worktree with uncommitted files **and** sidecar branch ahead of main (`recovery_sweep_required`). Shows full packet schema v1 including `next_fix_commands[]`, `artifacts_at_risk[]`, `ahead_commits[]`, `probable_reasoning_loss: true`. |
| `sample-block-receipt.jsonl` | One `RecoveryBlockReceipt` line that would be appended to `.ollarma/recovery_block_receipts.jsonl` when admission blocks a `route_prompt` request under the above state. |

These are **reference examples**, not runtime data. The live runtime equivalents
are gitignored:

- `.ollarma/incidents/*.json` (current packets — regenerated per scan)
- `.ollarma/incidents/latest.json` (pointer to newest — regenerated per scan)
- `.ollarma/recovery_block_receipts.jsonl` (block events — append-only)

See:
- `docs/RECOVERY_PROTOCOL.md` — full contract
- `docs/OPERATOR_RECOVERY_PLAYBOOK.md` — how to use these in practice
- `docs/LOCAL_ADOPTION.md` Recovery section — CLI/HTTP/MCP surface summary
