# Ollarma Test Prompt

Use this prompt when another local repo or agent needs to test `ollarma` without extra oral guidance.

```text
You are testing the local `ollarma` substrate from this machine.

Rules:
- Do not mutate files in the target project unless the operator explicitly asks you to.
- Capture exact commands, exact outputs, and exact failures.
- If a step fails, stop and report the failing command plus stderr/stdout instead of improvising.

1. Confirm command availability.
   - Run `ollarma --help`.
   - If project routing is required, run `ollarma projects` or `ollarma projects --adapters-dir <path>`.

2. Choose one access mode and say which one you picked.
   - MCP: register `ollarma mcp` and use the MCP tool surface, including `route_prompt` if project routing is needed.
   - HTTP: start `ollarma serve` and use `POST /chat` and `POST /route`.
   - CLI/project-routing: use `ollarma verify <run-id>` and, if needed, a routed project prompt through the CLI-facing workflow.

3. Run a minimum smoke workflow.
   - Verify the preserved baseline with `ollarma verify 2026-04-09T17:52:14Z`.
   - Run one chat/path check through your chosen access mode.
   - If project routing is in scope, run one routed prompt against a registered project.

4. Report results.
   - Include the exact mode used: MCP, HTTP, or CLI.
   - Include exact outputs for every command or request.
   - State whether the smoke path passed, failed, or was blocked by environment setup.

5. Keep boundaries intact.
   - Do not claim that Antigence is required for success.
   - Do not mutate sibling repos during the smoke test.
   - Do not replace exact outputs with summaries when the operator asked for evidence.
```
