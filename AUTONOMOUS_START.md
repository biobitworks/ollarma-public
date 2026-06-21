# Autonomous Start

This repo is currently at **v4.0 complete**. Treat this file as the first stop when entering the project.

## Start Here

1. Read `.planning/STATE.md` to confirm the current milestone, phase, and completion state.
2. Read `.planning/ROADMAP.md` to see whether any new milestone phases have been added.
3. Choose the GSD entry point:
   - `$gsd-next` for the normal handoff when you want the next logical action from the current state.
   - `$gsd-autonomous` when there is an active milestone with remaining phases and you want the full discuss -> plan -> red-team -> execute loop until milestone closeout.

## Exact Behavior

- If `.planning/STATE.md` says the milestone is complete and no new phase is active, use `$gsd-next` first to detect whether a new milestone or follow-on phase has been added.
- If the current milestone is complete and the roadmap already defines a next
  incomplete milestone, do not stop at the handoff. Activate the next milestone
  and continue.
- If `$gsd-next` finds unfinished roadmap work, continue with the next incomplete phase.
- If `$gsd-autonomous` is used, it owns the remaining phases for this repo only
  and proceeds phase by phase with sidecars across discuss -> research -> plan
  -> red-team -> remediation -> execute -> validate, then milestone completion,
  audit, and cleanup.
- Use `--from N` only when you intentionally want to start autonomous execution from a later phase number.

## Boundaries

- Stay within `<repo>`.
- Do not revert or rewrite unowned changes.
- Keep writes aligned with the existing planning system under `.planning/`.
- This repo is local-first; do not broaden the startup surface into a general control plane.
