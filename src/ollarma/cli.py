"""local-model-bench -- benchmark CLI entry point.

Usage:
    bench run [--dry-run] [--models NAME] [--suites SUITE] [--trials N] [--num-ctx N]
    bench list
    bench report

Install:
    pip install -e .   # or: uv pip install -e .

CLI layer: Typer argument parsing + Rich formatting + file writes.
Business logic: ollarma.service (shared with MCP server and HTTP API).
"""
import datetime
import pathlib
from typing import Optional

import orjson
import typer
from rich.console import Console
from rich.table import Table

from ollarma import service, scribe_hooks
from ollarma.execution_policy import SelectionResolutionError, WorkloadClass
from ollarma.reserved_models import assert_model_not_reserved
from ollarma.service import (
    NoModelsError,
    NoResultsError,
)
from ollarma.guards import PreflightError

# Imports needed by unchanged commands (execute, chat, autopilot, continue-config)
from ollarma.agent import agent_loop, fleet_agent_loop, resolve_default_model, AgentResult
from ollarma.plan_parser import parse_plan_tasks, PlanTask, PlanMetadata
from ollarma.fleet import AdapterConfig, DEFAULT_ADAPTERS_DIR
from ollarma.autopilot import (
    run_autopilot,
    AutopilotReport,
    AssetResult,
)

app = typer.Typer(help="local-model-bench: benchmark local Ollama models on Biobitworks workloads")
console = Console()


@app.command()
def run(
    dry_run: bool = typer.Option(False, "--dry-run", help="Run one inference (real Ollama call), skip guards, write one JSONL row"),
    models_filter: Optional[list[str]] = typer.Option(None, "--models", help="Filter to these model names (repeat or comma-separate)"),
    suites_filter: Optional[list[str]] = typer.Option(None, "--suites", help="Filter to suites: code, science, swarm"),
    trials: int = typer.Option(3, "--trials", min=1, help="Trials per model x task pair"),
    num_ctx: Optional[int] = typer.Option(None, "--num-ctx", help="Override context window size (default: use task YAML value)"),
    num_predict: Optional[int] = typer.Option(None, "--num-predict", help="Override decode-token cap (default: task YAML value, else 1024). Bounds verbose/thinking models."),
    skip_preflight: bool = typer.Option(False, "--skip-preflight", help="Skip pre-flight checks (use when external tools keep models loaded)"),
) -> None:
    """Run benchmarks. With --dry-run: one model x one task, no guards, one result row."""
    try:
        result = service.run_benchmark(
            dry_run=dry_run,
            models_filter=models_filter,
            suites_filter=suites_filter,
            trials=trials,
            num_ctx=num_ctx,
            num_predict=num_predict,
            skip_preflight=skip_preflight,
            on_progress=console.print,
        )
    except NoModelsError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except NoResultsError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except PreflightError as exc:
        console.print(f"[red]Pre-flight failed:[/red] {exc}")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        return

    console.print(f"[green]Sealed:[/green] {result.sealed_path}")
    console.print(f"[green]Evidence chain:[/green] {result.evidence_path} ({result.receipt_count} receipts)")
    if result.dry_run and result.results:
        r = result.results[0]
        console.print(f"  prefill_tps={r.prefill_tps}  decode_tps={r.decode_tps:.1f}")
        console.print(f"  model_digest={r.model_digest[:24]}...")
        console.print(f"  schema_version={r.schema_version}  thinking_mode={r.thinking_mode}")


@app.command(name="list")
def list_cmd() -> None:
    """List available models and task suites."""
    try:
        data = service.list_models_and_tasks()
    except NoModelsError:
        console.print("[red]No models found in models.yml[/red]")
        raise typer.Exit(1)

    table = Table(title="Models")
    table.add_column("Name", style="cyan")
    table.add_column("Description")
    for m in data.models:
        table.add_row(m.name, m.description or "")
    console.print(table)

    table2 = Table(title="Tasks")
    table2.add_column("ID", style="cyan")
    table2.add_column("Suite")
    table2.add_column("num_ctx")
    for t in data.tasks:
        table2.add_row(t.id, t.suite, str(t.num_ctx))
    console.print(table2)


@app.command()
def report(
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Sealed run ID (default: latest)"),
) -> None:
    """Generate results.md and model_selection.md from sealed benchmark results."""
    results_dir = pathlib.Path("results")

    try:
        result = service.generate_report(run_id=run_id, results_dir=results_dir)
    except NoResultsError:
        console.print("[red]No sealed results found in results/ directory.[/red]")
        console.print("Run [bold]bench run[/bold] first to generate results.")
        raise typer.Exit(1)
    except FileNotFoundError as exc:
        console.print(f"[red]Sealed results not found:[/red] {exc}")
        raise typer.Exit(1)

    console.print(f"Loaded {result.rows_loaded} result rows")

    # Write presentation files (service returns strings, CLI writes to disk)
    results_path = results_dir / "results.md"
    results_path.write_text(result.results_md)
    console.print(f"[green]Written:[/green] {results_path}")

    selection_path = results_dir / "model_selection.md"
    selection_path.write_text(result.selection_md)
    console.print(f"[green]Written:[/green] {selection_path}")

    console.print(f"  stable_decision_hash: {result.artifact_hash[:16]}...")


@app.command()
def verify(
    run_id: str = typer.Argument(..., help="Run ID to verify (e.g., 2026-04-07T00:00:00Z)"),
) -> None:
    """Verify the evidence chain for a sealed benchmark run.

    Replays the full chain: genesis -> per-row receipts -> evidence root.
    If a .artifact.json exists, also verifies the selection artifact hash.
    Exits 0 on valid chain, 1 on tampered or incomplete chain.
    """
    try:
        result = service.verify_evidence(run_id=run_id)
    except FileNotFoundError:
        console.print(f"[red]Sealed results not found for run:[/red] {run_id}")
        raise typer.Exit(1)
    except ValueError as exc:
        console.print(f"[red]VERIFICATION FAILED:[/red] {exc}")
        raise typer.Exit(1)

    console.print(f"[green]Evidence chain valid[/green] ({result.receipt_count} receipts)")
    console.print(f"  evidence_root: {result.evidence_root[:16]}...")

    if result.artifact_valid is not None:
        console.print("[green]Selection artifact valid[/green]")
        console.print(f"  stable_decision_hash: {result.artifact_hash[:16]}...")


@app.command()
def sonify(
    run_id: str = typer.Argument(..., help="Run ID to sonify (e.g., 2026-04-07T00:00:00Z)"),
    compare: Optional[str] = typer.Option(None, "--compare", help="Second run ID for stereo comparison"),
) -> None:
    """Render evidence chain as audio WAV file.

    Single chain: writes run-{run-id}.sonify.wav (mono, ascending consonant scale).
    Tampered receipts produce dissonant tones at the tampered position.

    With --compare: writes compare-{a}-vs-{b}.wav (stereo, L=first chain, R=second).
    Concordant chains sound harmonic; discordant chains produce audible clash.
    """
    results_dir = pathlib.Path("results")

    try:
        result = service.sonify_evidence(run_id=run_id, compare_id=compare, results_dir=results_dir)
    except FileNotFoundError as exc:
        console.print(f"[red]Sealed results not found:[/red] {exc}")
        raise typer.Exit(1)

    if result.mode == "single":
        out_path = results_dir / f"run-{run_id}.sonify.wav"
        out_path.write_bytes(result.wav_bytes)

        console.print(f"[green]Sonified:[/green] {out_path}")
        console.print(f"  {result.receipt_count} receipts, {result.duration_s:.1f}s duration")
        if result.tampered_positions:
            console.print(f"  [red]Tampered positions:[/red] {result.tampered_positions}")
        else:
            console.print("  [green]All receipts valid[/green]")
    else:
        # Compare mode
        compare_id = compare
        out_path = results_dir / f"compare-{run_id}-vs-{compare_id}.wav"
        out_path.write_bytes(result.wav_bytes)

        console.print(f"[green]Comparison sonified:[/green] {out_path}")
        console.print(f"  Duration: {result.duration_s:.1f}s (stereo)")


@app.command()
def projects(
    adapters_dir: Optional[str] = typer.Option(
        None,
        "--adapters-dir",
        help="Path to gsigmad adapters directory (default: discovery precedence)",
    ),
) -> None:
    """List all registered projects from gsigmad adapter configs."""
    registry = service.list_projects(adapters_dir=adapters_dir)
    if not registry:
        console.print("[yellow]No adapters found[/yellow]")
        raise typer.Exit(1)

    table = Table(title="Fleet Projects")
    table.add_column("Project", style="cyan")
    table.add_column("Path", style="green")
    table.add_column("Source", style="dim")
    table.add_column("Type", style="dim")
    table.add_column("Tools", style="yellow")
    table.add_column("Antibodies", style="red")
    table.add_column("Exists", style="bold")

    for name, adapter in sorted(registry.items()):
        table.add_row(
            name,
            adapter.project_root,
            adapter.adapter_source,
            adapter.project_type[:30] if adapter.project_type else "",
            ", ".join(adapter.tools) if adapter.tools else "base",
            ", ".join(adapter.antibodies) if adapter.antibodies else "default",
            "yes" if adapter.root_exists else "[red]NO[/red]",
        )

    console.print(table)
    console.print(f"\n[dim]{len(registry)} projects registered[/dim]")


@app.command()
def workflow(
    project: str = typer.Option(..., "--project", help="Project name from fleet registry"),
    manifest_ref: str = typer.Option(
        ...,
        "--manifest-ref",
        help="Portable workflow manifest locator (stable id or repo-relative path)",
    ),
    step_id: str = typer.Option(
        ...,
        "--step-id",
        help="Manifest step_id to admit onto the workflow lane",
    ),
    model: Optional[str] = typer.Option(None, "--model", help="Ollama model override"),
    adapters_dir: Optional[str] = typer.Option(None, "--adapters-dir", help="Adapter directory override"),
) -> None:
    """Submit a manifest-backed validated workflow step to the queued workflow lane."""
    try:
        result = service.submit_workflow(
            project=project,
            manifest_ref=manifest_ref,
            step_id=step_id,
            model=model,
            adapters_dir=adapters_dir,
        )
    except (ValueError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if result.status == "accepted":
        console.print(f"[green]Workflow accepted[/green] for [bold]{result.project}[/bold]")
        console.print(f"  lane={result.lane} model={result.model} queue_depth={result.queue_depth}")
        console.print(f"  step_id={result.step_id} run_id={result.run_id}")
        return

    if result.status == "queued":
        console.print(f"[yellow]Workflow queued[/yellow] for [bold]{result.project}[/bold]")
        console.print(f"  lane={result.lane} model={result.model} queue_depth={result.queue_depth}")
        console.print(f"  step_id={result.step_id} run_id={result.run_id}")
        return

    console.print(f"[yellow]Workflow rejected[/yellow] for [bold]{result.project}[/bold]")
    console.print(f"  reason={result.reason_code}")
    if result.escalation_receipt:
        console.print_json(data=result.escalation_receipt)
    raise typer.Exit(1)


@app.command("kb-build")
def kb_build(
    project: str = typer.Option(..., "--project", help="Project name from fleet registry"),
    adapters_dir: Optional[str] = typer.Option(None, "--adapters-dir", help="Adapter directory override"),
) -> None:
    """Build deterministic repo-local KB artifacts for a project."""
    try:
        result = service.build_project_kb(
            project=project,
            adapters_dir=adapters_dir,
        )
    except (ValueError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]KB built[/green] for [bold]{result.project}[/bold]")
    console.print(
        f"  sources={result.source_count} documents={result.document_count} chunks={result.chunk_count}"
    )
    console.print(f"  manifest={result.manifest_ref['repo_relative']}")
    console.print(f"  receipt={result.receipt_ref['repo_relative']}")


@app.command("kb-status")
def kb_status(
    project: str = typer.Option(..., "--project", help="Project name from fleet registry"),
    adapters_dir: Optional[str] = typer.Option(None, "--adapters-dir", help="Adapter directory override"),
) -> None:
    """Show read-only KB status for a project."""
    try:
        result = service.get_project_kb_status(
            project=project,
            adapters_dir=adapters_dir,
        )
    except (ValueError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]{result.project}[/bold] KB status: [green]{result.status}[/green]")
    if result.reason_code:
        console.print(f"  reason={result.reason_code}")
    console.print(f"  search_db={result.search_db_path}")
    console.print(f"  documents={result.document_count} chunks={result.chunk_count}")
    if result.built_at:
        console.print(f"  built_at={result.built_at}")


@app.command("kb-search")
def kb_search(
    project: str = typer.Option(..., "--project", help="Project name from fleet registry"),
    query: str = typer.Option(..., "--query", help="Search text to match against KB chunks"),
    limit: int = typer.Option(5, "--limit", min=1, max=25, help="Maximum number of hits"),
    adapters_dir: Optional[str] = typer.Option(None, "--adapters-dir", help="Adapter directory override"),
) -> None:
    """Run bounded read-only KB search for a project."""
    try:
        result = service.search_project_kb(
            project=project,
            query=query,
            adapters_dir=adapters_dir,
            limit=limit,
        )
    except (ValueError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if result.status == "blocked":
        console.print(f"[yellow]KB search blocked[/yellow] for [bold]{result.project}[/bold]")
        if result.reason_code:
            console.print(f"  reason={result.reason_code}")
        raise typer.Exit(1)

    console.print(
        f"[green]KB search[/green] {result.project} status={result.status} hits={result.hit_count}"
    )
    if result.reason_code:
        console.print(f"  reason={result.reason_code}")
    for hit in result.hits:
        console.print(f"- {hit.path}#{hit.chunk_index} score={hit.score:.3f}")
        console.print(f"  authority={hit.authority} kind={hit.source_kind}")
        snippet = " ".join(hit.text.strip().split())
        console.print(f"  {snippet[:160]}")


@app.command()
def execute(
    plan_path: str = typer.Argument(..., help="Path to GSD PLAN.md file"),
    model: Optional[str] = typer.Option(None, "--model", help="Ollama model override (default: bench-selected)"),
    project: Optional[str] = typer.Option(None, "--project", help="Project name from fleet registry"),
) -> None:
    """Execute a GSD PLAN.md file via Ollama agent with tool calling.

    Reads the plan, parses XML tasks, sends each task's action to the
    selected Ollama model, executes tool calls (file read/write, bash, grep),
    and commits each task atomically.

    With --project: resolves project adapter, creates guardrail gate,
    and uses fleet_agent_loop with expanded tools.
    """
    import subprocess as _sp

    plan_file = pathlib.Path(plan_path)
    if not plan_file.exists():
        console.print(f"[red]Plan file not found:[/red] {plan_path}")
        raise typer.Exit(1)

    plan_text = plan_file.read_text()
    metadata, tasks = parse_plan_tasks(plan_text)

    try:
        effective_model = model or resolve_default_model(
            workload_class=WorkloadClass.VALIDATED_SCRIPT
        )
        assert_model_not_reserved(effective_model, context="execute")
    except SelectionResolutionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    project_root = str(pathlib.Path.cwd())

    # Resolve fleet project if --project provided
    fleet_adapter: Optional[AdapterConfig] = None
    fleet_guardrail = None
    if project:
        try:
            fleet_adapter = service.resolve_project_adapter(project)
        except ValueError:
            console.print(f"[red]Project '{project}' not found in fleet registry[/red]")
            raise typer.Exit(1)
        from ollarma.guardrail import GuardrailGate
        fleet_guardrail = GuardrailGate(fleet_adapter)
        project_root = fleet_adapter.project_root

    console.print(f"[bold]Executing plan:[/bold] {plan_path}")
    console.print(f"[bold]Model:[/bold] {effective_model}")
    if fleet_adapter:
        console.print(f"[bold]Project:[/bold] {fleet_adapter.project_name}")
    console.print(f"[bold]Tasks:[/bold] {len(tasks)}")
    console.print()

    for i, task in enumerate(tasks, 1):
        console.print(f"[bold cyan]--- Task {i}/{len(tasks)}: {task.name} ---[/bold cyan]")

        # Build system prompt with context
        system_prompt = (
            "You are a coding agent executing a plan task. "
            "Use the provided tools (read_file, edit_file, run_bash, grep_search) "
            "to complete the task. Work step by step."
        )

        # Build task prompt from action + files + acceptance criteria
        prompt_parts = [f"## Task: {task.name}\n"]
        if task.read_first:
            prompt_parts.append(f"Read these files first: {', '.join(task.read_first)}\n")
        prompt_parts.append(f"## Action\n{task.action}\n")
        if task.acceptance_criteria:
            prompt_parts.append("## Acceptance Criteria\n" + "\n".join(f"- {c}" for c in task.acceptance_criteria))

        prompt = "\n".join(prompt_parts)

        if fleet_adapter:
            result = fleet_agent_loop(
                prompt=prompt,
                model=effective_model,
                adapter=fleet_adapter,
                guardrail=fleet_guardrail,
                system_prompt=system_prompt,
            )
        else:
            result = agent_loop(
                prompt=prompt,
                model=effective_model,
                project_root=project_root,
                system_prompt=system_prompt,
            )

        console.print(f"\n[green]Tool calls:[/green] {result.tool_calls_count}")
        if result.final_response:
            # Print first 500 chars of response
            truncated = result.final_response[:500]
            if len(result.final_response) > 500:
                truncated += "..."
            console.print(f"[dim]{truncated}[/dim]")

        # Atomic git commit for this task (WR-03: detect actually-changed files)
        try:
            diff_result = _sp.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=project_root, capture_output=True, text=True, timeout=30,
            )
            untracked_result = _sp.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=project_root, capture_output=True, text=True, timeout=30,
            )
            changed_files = set(
                f for f in (diff_result.stdout.strip().split("\n")
                            + untracked_result.stdout.strip().split("\n"))
                if f
            )
            # Intersect with declared task files when available
            declared_files = set(task.files if task.files else metadata.files_modified)
            if declared_files:
                files_to_stage = sorted(changed_files & declared_files)
            else:
                files_to_stage = sorted(changed_files)

            if files_to_stage:
                _sp.run(["git", "add"] + files_to_stage, cwd=project_root,
                        capture_output=True, timeout=30)
                commit_msg = f"feat({metadata.phase}): {task.name}"
                commit_result = _sp.run(
                    ["git", "commit", "-m", commit_msg],
                    cwd=project_root, capture_output=True, text=True, timeout=30,
                )
                if commit_result.returncode == 0:
                    console.print(f"[green]Committed:[/green] {commit_msg}")
                else:
                    console.print("[yellow]Nothing to commit for this task[/yellow]")
            else:
                console.print("[yellow]No changed files to commit for this task[/yellow]")
        except (_sp.CalledProcessError, _sp.TimeoutExpired, OSError, FileNotFoundError) as exc:
            console.print(f"[yellow]Git commit skipped:[/yellow] {exc}")

        console.print()

    console.print(f"[bold green]Plan execution complete.[/bold green] {len(tasks)} tasks processed.")


@app.command()
def chat(
    model: Optional[str] = typer.Option(None, "--model", help="Ollama model override (default: bench-selected)"),
    project: Optional[str] = typer.Option(None, "--project", help="Project name from fleet registry"),
    namespace: Optional[str] = typer.Option(None, "--namespace", help="Namespace prefix for scoped receipts (e.g. 'ollarma-demo:')"),
) -> None:
    """Interactive chat REPL with tool execution via Ollama.

    Type messages, the model responds with optional tool calls executed locally.
    Type 'exit' or 'quit' to end. Ctrl+C also exits.

    With --project: resolves project adapter, creates guardrail gate,
    and uses fleet_agent_loop with expanded tools.
    """
    try:
        effective_model = model or resolve_default_model(
            workload_class=WorkloadClass.CHAT
        )
        assert_model_not_reserved(effective_model, context="chat")
    except SelectionResolutionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    project_root = str(pathlib.Path.cwd())

    # Resolve fleet project if --project provided
    fleet_adapter: Optional[AdapterConfig] = None
    fleet_guardrail = None
    if project:
        try:
            fleet_adapter = service.resolve_project_adapter(project)
        except ValueError:
            console.print(f"[red]Project '{project}' not found in fleet registry[/red]")
            raise typer.Exit(1)
        from ollarma.guardrail import GuardrailGate
        fleet_guardrail = GuardrailGate(fleet_adapter)
        project_root = fleet_adapter.project_root

    console.print(f"[bold]ollarma chat[/bold] -- model: {effective_model}")
    if fleet_adapter:
        console.print(f"[bold]Project:[/bold] {fleet_adapter.project_name}")
    console.print("[dim]Type 'exit' or 'quit' to end. Ctrl+C to interrupt.[/dim]")
    console.print()

    try:
        while True:
            try:
                user_input = input("ollarma> ").strip()
            except EOFError:
                break

            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                break

            if fleet_adapter:
                result = fleet_agent_loop(
                    prompt=user_input,
                    model=effective_model,
                    adapter=fleet_adapter,
                    guardrail=fleet_guardrail,
                    conversation_surface="cli_chat",
                )
            else:
                result = agent_loop(
                    prompt=user_input,
                    model=effective_model,
                    project_root=project_root,
                    conversation_surface="cli_chat",
                )

            if result.tool_calls_count > 0:
                console.print(f"[dim]({result.tool_calls_count} tool calls)[/dim]")

            console.print(result.final_response)
            console.print()

    except KeyboardInterrupt:
        console.print("\n[dim]Exiting chat.[/dim]")


@app.command(name="continue-config")
def continue_config(
    model: Optional[str] = typer.Option(None, "--model", help="Model override (default: bench-selected)"),
) -> None:
    """Generate .continuerc.json for Continue.dev VS Code integration.

    Creates a configuration file pointing Continue.dev at the local Ollama
    endpoint with the bench-selected best model for code tasks.
    """
    effective_model = model or resolve_default_model()
    try:
        assert_model_not_reserved(effective_model, context="continue config")
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    config = {
        "models": [
            {
                "title": f"Ollama ({effective_model})",
                "provider": "ollama",
                "model": effective_model,
                "apiBase": "http://localhost:11434",
            }
        ],
        "tabAutocompleteModel": {
            "title": f"Ollama Autocomplete ({effective_model})",
            "provider": "ollama",
            "model": effective_model,
            "apiBase": "http://localhost:11434",
        },
    }

    config_path = pathlib.Path(".continuerc.json")
    config_path.write_bytes(orjson.dumps(config, option=orjson.OPT_INDENT_2))

    console.print(f"[green]Written:[/green] {config_path}")
    console.print(f"[bold]Model:[/bold] {effective_model}")
    console.print("[dim]Open VS Code with Continue.dev extension to use.[/dim]")


@app.command()
def autopilot(
    project: str = typer.Argument(..., help="Project name from fleet registry"),
    run_assets: bool = typer.Option(False, "--run", help="Execute discovered assets (default: discovery only)"),
    fleet: bool = typer.Option(False, "--fleet", help="Run autopilot across all registered projects (AUTO-04)"),
    threshold: float = typer.Option(0.9, "--threshold", help="Pass rate threshold for model selection (D-09)"),
    include: Optional[list[str]] = typer.Option(None, "--include", help="Include patterns for asset discovery (D-02)"),
    exclude: Optional[list[str]] = typer.Option(None, "--exclude", help="Exclude patterns for asset discovery (D-02)"),
    adapters_dir: Optional[str] = typer.Option(
        None,
        "--adapters-dir",
        help="Path to gsigmad adapters directory (default: discovery precedence)",
    ),
) -> None:
    """Run autopilot asset discovery and optional execution for a project.

    Without --run: discovers assets and shows inventory.
    With --run: executes assets with tiered model assignment.
    With --fleet: iterates all registered projects (AUTO-04).
    """
    # 57.1-01 / DEBT-56.2 / F-01: CLI scribe parity with HTTP surface.
    # Wrap the full body in the same scribe-hook context manager used by
    # service.submit_autopilot so operator cron jobs and headless invocations
    # produce the same two-entry scribe trail as chat-session HTTP calls.
    if fleet:
        scribe_task = "autopilot:fleet"
    elif run_assets:
        scribe_task = f"autopilot:run:{project}"
    else:
        scribe_task = f"autopilot:discover:{project}"

    with scribe_hooks.hooked(
        "cli:autopilot",
        project=project,
        task=scribe_task,
        notes="",
    ):
        registry = service.list_projects(adapters_dir=adapters_dir)

        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        results_dir = pathlib.Path("results")
        results_dir.mkdir(exist_ok=True)

        if fleet:
            # Fleet mode (AUTO-04): iterate all registered projects
            if not registry:
                console.print("[yellow]No projects in fleet registry[/yellow]")
                raise typer.Exit(1)

            reports: list[AutopilotReport] = []
            for name, adapter in sorted(registry.items()):
                console.print(f"\n[bold cyan]--- Project: {name} ---[/bold cyan]")
                report = run_autopilot(
                    project_name=name,
                    project_root=adapter.project_root,
                    run=run_assets,
                    threshold=threshold,
                    include_patterns=include,
                    exclude_patterns=exclude,
                )
                reports.append(report)
                _render_autopilot_report(report)

            # Fleet summary table (D-16)
            fleet_table = Table(title="Fleet Summary")
            fleet_table.add_column("Project", style="cyan")
            fleet_table.add_column("Assets", justify="right")
            fleet_table.add_column("Passed", justify="right", style="green")
            fleet_table.add_column("Failed", justify="right", style="red")
            fleet_table.add_column("Escalate", justify="right", style="yellow")
            fleet_table.add_column("Tokens Local", justify="right")

            total_assets = 0
            total_passed = 0
            total_failed = 0
            total_escalation = 0
            total_tokens = 0

            for r in reports:
                fleet_table.add_row(
                    r.project_name,
                    str(r.total_assets),
                    str(r.passed),
                    str(r.failed),
                    str(r.escalation_needed),
                    str(r.tokens_consumed_local),
                )
                total_assets += r.total_assets
                total_passed += r.passed
                total_failed += r.failed
                total_escalation += r.escalation_needed
                total_tokens += r.tokens_consumed_local

            console.print()
            console.print(fleet_table)
            console.print(
                f"\nFleet: {len(reports)} projects, {total_assets} total assets, "
                f"{total_passed} passed, {total_failed} failed, "
                f"{total_escalation} escalation needed"
            )
            console.print(f"Total tokens served locally: {total_tokens}")

            # Write fleet report JSON
            fleet_data = [r.model_dump(mode="json") for r in reports]
            fleet_path = results_dir / f"autopilot-fleet-{timestamp}.json"
            fleet_path.write_bytes(orjson.dumps(fleet_data))
            console.print(f"\n[green]Fleet report:[/green] {fleet_path}")
        else:
            # Single project mode
            try:
                adapter = service.resolve_project_adapter(project, adapters_dir=adapters_dir)
            except ValueError:
                console.print(f"[red]Project '{project}' not found in fleet registry[/red]")
                raise typer.Exit(1)

            report = run_autopilot(
                project_name=adapter.project_name,
                project_root=adapter.project_root,
                run=run_assets,
                threshold=threshold,
                include_patterns=include,
                exclude_patterns=exclude,
            )
            _render_autopilot_report(report)

            # Write single project report JSON
            report_path = results_dir / f"autopilot-{project}-{timestamp}.json"
            report_path.write_bytes(orjson.dumps(report.model_dump(mode="json")))
            console.print(f"\n[green]Report:[/green] {report_path}")


def _render_autopilot_report(report: AutopilotReport) -> None:
    """Render a single project's autopilot report to console."""
    # Asset Inventory table
    inv_table = Table(title=f"Asset Inventory -- {report.project_name}")
    inv_table.add_column("Type", style="cyan")
    inv_table.add_column("Count", justify="right")
    for asset_type, count in sorted(report.inventory.counts.items()):
        inv_table.add_row(asset_type, str(count))
    console.print(inv_table)

    # Tier Mappings table
    if report.tier_map:
        tier_table = Table(title="Tier Mappings")
        tier_table.add_column("Tier", style="cyan")
        tier_table.add_column("Model")
        tier_table.add_column("Size (B)", justify="right")
        tier_table.add_column("Quality", justify="right")
        tier_table.add_column("Degraded", style="yellow")
        for tier_name, mapping in sorted(report.tier_map.items()):
            tier_table.add_row(
                tier_name,
                mapping.model,
                f"{mapping.model_size_b:.1f}" if mapping.model_size_b is not None else "?",
                f"{mapping.quality_mean:.2f}" if mapping.quality_mean is not None else "?",
                "yes" if mapping.degraded_confidence else "",
            )
        console.print(tier_table)

    # Execution Results table (only if run was executed)
    if report.run_executed and report.results:
        exec_table = Table(title="Execution Results")
        exec_table.add_column("Asset", style="cyan")
        exec_table.add_column("Type")
        exec_table.add_column("Tier")
        exec_table.add_column("Model")
        exec_table.add_column("Exit", justify="right")
        exec_table.add_column("Duration", justify="right")
        exec_table.add_column("Escalated", style="yellow")
        for result in report.results:
            exec_table.add_row(
                pathlib.Path(result.asset_path).name,
                result.asset_type,
                result.task_tier,
                result.model_used,
                str(result.exit_code),
                f"{result.duration_s:.1f}s",
                "yes" if result.escalation_needed else "",
            )
        console.print(exec_table)

    # Summary line
    console.print(
        f"\n{report.total_assets} assets: {report.passed} passed, "
        f"{report.failed} failed, {report.escalation_needed} escalation needed"
    )
    if report.run_executed:
        console.print(f"Tokens served locally: {report.tokens_consumed_local}")


@app.command()
def escalate(
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Autopilot run ID (default: latest)"),
) -> None:
    """Generate escalation checklist from the latest autopilot run.

    Reads autopilot results, filters to assets needing escalation,
    and outputs both JSON and Markdown checklists (D-15, AUTO-05).
    """
    results_dir = pathlib.Path("results")

    try:
        result = service.generate_escalation(run_id=run_id, results_dir=results_dir)
    except FileNotFoundError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        console.print("Run [bold]bench autopilot <project> --run[/bold] first.")
        raise typer.Exit(1)

    if result.count == 0:
        console.print("[green]No assets need escalation[/green]")
        return

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # JSON output (D-15)
    json_path = results_dir / f"escalation-{timestamp}.json"
    json_path.write_bytes(orjson.dumps(result.items))

    # Markdown output (D-15)
    md_lines = [
        "# Escalation Checklist",
        "",
        f"{result.count} assets need frontier model or human attention.",
        "",
        "## Assets",
        "",
    ]
    for item in result.items:
        stderr_preview = item["stderr"][:200] if item["stderr"] else "(no stderr)"
        md_lines.append(f"- [ ] `{item['asset_path']}` ({item['asset_type']}, tier: {item['task_tier']})")
        md_lines.append(f"  - Last model tried: {item['last_model_tried']}")
        md_lines.append(f"  - Error: {stderr_preview}")

    md_path = results_dir / f"escalation-{timestamp}.md"
    md_path.write_text("\n".join(md_lines) + "\n")

    console.print(f"[bold]{result.count} assets need escalation[/bold]")
    console.print(f"[green]JSON:[/green] {json_path}")
    console.print(f"[green]Markdown:[/green] {md_path}")


@app.command()
def mcp() -> None:
    """Start MCP server over stdio for Claude Code integration.

    Exposes 7 service tools: list_models, run_benchmark, get_report,
    verify_evidence, list_projects, chat_with_model, route_prompt.

    Register with: claude mcp add ollarma -- ollarma mcp
    Requires: pip install ollarma[mcp]
    """
    try:
        from ollarma.mcp_server import mcp as mcp_server
    except ImportError:
        console.print("[red]MCP mode requires: pip install ollarma\\[mcp][/red]")
        raise typer.Exit(1)
    mcp_server.run(transport="stdio")


@app.command()
def start(
    host: str = typer.Option("127.0.0.1", help="Bind address (use 127.0.0.1 for security)"),
    port: int = typer.Option(8484, help="Bind port"),
    install: bool = typer.Option(False, "--install", help="Install launchd plist and load the agent"),
) -> None:
    """Start ollarma HTTP API server (alias for 'serve').

    Matches the 'start' verb convention used by Watchtower and Antigence.
    With --install, copies the launchd plist to ~/Library/LaunchAgents/ and loads it.
    """
    if install:
        _install_launchd()
        return
    serve(host=host, port=port)


_LAUNCHD_LABEL = "com.byron.ollarma"
_LAUNCHD_PLIST_FILENAME = "com.byron.ollarma.plist"
_LAUNCHCTL_TIMEOUT_S = 10


def _run_launchd_command(
    args: list[str],
    *,
    check: bool = True,
    tolerate_return_codes: tuple[int, ...] = (),
) -> "subprocess.CompletedProcess":
    """Run a plutil/launchctl subprocess with deterministic hardening.

    - ``shell=False`` always (avoid injection).
    - Finite ``timeout`` so a hung launchctl can't stall the CLI.
    - ``capture_output=True`` + ``text=True`` for deterministic stdout/stderr.
    - If ``check`` is True, non-zero return codes that are NOT in
      ``tolerate_return_codes`` cause a ``typer.Exit(1)`` with a clear message.
    """
    import subprocess  # noqa: PLC0415 -- lazy per repo convention
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        shell=False,
        timeout=_LAUNCHCTL_TIMEOUT_S,
    )
    if check and result.returncode != 0 and result.returncode not in tolerate_return_codes:
        console.print(
            f"[red]{args[0]} {args[1]} failed (exit {result.returncode}):[/red] "
            f"{(result.stderr or result.stdout).strip()}"
        )
        raise typer.Exit(1)
    return result


def _install_launchd() -> None:
    """Install and load the ollarma launchd agent.

    Hardened bootstrap (Plan 51-01 PERSIST-01):
      1. Copy repo plist to ``~/Library/LaunchAgents/`` (or validate existing).
      2. ``plutil -lint`` the installed plist before touching launchctl.
      3. Create ``~/Library/Logs/ollarma/`` (Standard{Out,Err}Path).
      4. ``launchctl bootout gui/<uid>/<label>`` — best-effort (tolerates 113).
      5. ``launchctl bootstrap gui/<uid> <plist>`` — loud on failure.
      6. ``launchctl enable gui/<uid>/<label>``.
      7. ``launchctl kickstart -kp gui/<uid>/<label>``.
      8. ``launchctl list <label>`` + ``launchctl print gui/<uid>/<label>``
         as verification output for the operator.
    """
    import os
    import shutil

    source = pathlib.Path(__file__).resolve().parent.parent.parent / _LAUNCHD_PLIST_FILENAME
    target = pathlib.Path.home() / "Library" / "LaunchAgents" / _LAUNCHD_PLIST_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)

    if source.exists():
        shutil.copy2(str(source), str(target))
        console.print(f"[green]Installed:[/green] {target}")
    elif target.exists():
        console.print(f"[bold]Plist already at {target}[/bold]")
    else:
        console.print(
            f"[red]Plist not found at {source}.[/red]\n"
            "[dim]This command requires an editable install (pip install -e .).[/dim]"
        )
        raise typer.Exit(1)

    # Step 2: validate the installed plist. We lint the *target* because that's
    # the file launchctl will actually read.
    _run_launchd_command(["plutil", "-lint", str(target)])

    # Step 3: ensure log directory exists before launchctl tries to write.
    log_dir = pathlib.Path.home() / "Library" / "Logs" / "ollarma"
    log_dir.mkdir(parents=True, exist_ok=True)

    uid = os.getuid()
    domain_target = f"gui/{uid}/{_LAUNCHD_LABEL}"
    domain = f"gui/{uid}"

    def _bootout_and_wait() -> None:
        """Best-effort bootout, then poll until the service is fully gone.

        launchctl bootout returns quickly even when KeepAlive=true — the child
        process may still be alive. Polling launchctl list until it returns
        non-zero ensures bootstrap won't hit EIO (5) from a still-loaded service.
        """
        import time  # noqa: PLC0415

        _run_launchd_command(
            ["launchctl", "bootout", domain_target],
            check=False,
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            probe = _run_launchd_command(
                ["launchctl", "list", _LAUNCHD_LABEL],
                check=False,
            )
            if probe.returncode != 0:
                break  # service is gone from the domain
            import time as _t  # noqa: PLC0415
            _t.sleep(0.1)

    # Step 4: bootout — best-effort; poll until fully unloaded.
    _bootout_and_wait()

    # Step 5: bootstrap — hard failure if this fails.
    # One retry on EIO (5): bootout race where KeepAlive child hasn't fully exited.
    result = _run_launchd_command(
        ["launchctl", "bootstrap", domain, str(target)],
        check=False,
    )
    if result.returncode == 5:
        _bootout_and_wait()
        _run_launchd_command(["launchctl", "bootstrap", domain, str(target)])
    console.print(f"[green]Loaded:[/green] {_LAUNCHD_LABEL} via launchctl")

    # Step 6: enable (idempotent).
    _run_launchd_command(["launchctl", "enable", domain_target])

    # Step 7: kickstart -kp (force restart; bring up if not running).
    # Non-fatal timeout: bootstrap already succeeded; kickstart may time out if
    # the service is still in its initial ramp-up window.  The service WILL
    # start — launchd owns it via KeepAlive.  Treat TimeoutExpired as a warning
    # rather than a hard failure so the operator is not blocked by a cosmetic
    # step that provides no correctness guarantee after a successful bootstrap.
    try:
        import subprocess as _sp  # noqa: PLC0415
        _run_launchd_command(["launchctl", "kickstart", "-kp", domain_target])
    except _sp.TimeoutExpired:
        console.print(
            "[yellow]Warning:[/yellow] launchctl kickstart timed out — bootstrap "
            "already succeeded; the service will start under launchd control. "
            "Run `ollarma startup-smoke` in a few seconds to verify."
        )

    # Step 8: verification output for the operator.
    _run_launchd_command(["launchctl", "list", _LAUNCHD_LABEL], check=False)
    _run_launchd_command(["launchctl", "print", domain_target], check=False)
    console.print(
        f"[dim]Verify: launchctl print {domain_target}[/dim]\n"
        f"[dim]Logs:   ~/Library/Logs/ollarma/stdout.log[/dim]"
    )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address (use 127.0.0.1 for security)"),
    port: int = typer.Option(8484, help="Bind port"),
) -> None:
    """Start HTTP API server.

    Exposes REST endpoints mirroring CLI commands: /models, /run, /report,
    /verify, /projects, plus /chat and /route on 127.0.0.1:8484.
    OpenAPI docs at /docs. Binds to localhost only by default.

    Requires: pip install ollarma[http]
    """
    try:
        import uvicorn
    except ImportError:
        console.print("[red]HTTP mode requires: pip install ollarma\\[http][/red]")
        raise typer.Exit(1)
    if host not in {"127.0.0.1", "localhost"}:
        console.print(
            "[red]HTTP service mode is localhost-only by default. "
            "Use 127.0.0.1 or localhost.[/red]"
        )
        raise typer.Exit(1)
    console.print(f"[bold]Starting ollarma HTTP API on {host}:{port}[/bold]")
    console.print(f"[dim]OpenAPI docs: http://{host}:{port}/docs[/dim]")
    console.print(f"[dim]Operator dashboard: http://{host}:{port}/dashboard[/dim]")
    uvicorn.run("ollarma.http_api:app", host=host, port=port, log_level="info")


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", help="Expected dashboard host"),
    port: int = typer.Option(8484, help="Expected dashboard port"),
) -> None:
    """Print the local dashboard URL served by `ollarma serve`."""
    if host not in {"127.0.0.1", "localhost"}:
        console.print("[red]Dashboard surface is localhost-only.[/red]")
        raise typer.Exit(1)
    console.print(f"[bold]Dashboard:[/bold] http://{host}:{port}/dashboard")
    console.print("[dim]Start `ollarma serve` first if the HTTP API is not already running.[/dim]")


# ---------------------------------------------------------------------------
# pipeline subcommand group (Plan 33-02)
# ---------------------------------------------------------------------------

pipeline_app = typer.Typer(name="pipeline", help="Model pipeline control.")
app.add_typer(pipeline_app)


@pipeline_app.command("status")
def pipeline_status_cmd():
    """Show model pipeline status."""
    snapshot = service.get_pipeline_status()
    typer.echo(f"State: {snapshot.state}  Swap: {snapshot.swap_used_mb}MB")
    for m in snapshot.models:
        pin_info = f" [PINNED x{m.pin_count}]" if m.pinned else ""
        typer.echo(f"  {m.name}{pin_info}")


@pipeline_app.command("warmup")
def pipeline_warmup_cmd(model: str = typer.Argument(...)):
    """Warm up a model (keep_alive=5m)."""
    try:
        receipt = service.pipeline_warmup(model)
        typer.echo(f"[OK] warmed up {model} ({receipt.receipt_hash[:8]})")
    except ValueError as exc:
        typer.echo(f"[ERROR] {exc}", err=True)
        raise typer.Exit(1)


@pipeline_app.command("pin")
def pipeline_pin_cmd(model: str = typer.Argument(...)):
    """Pin a model (increment refcount)."""
    try:
        receipt = service.pipeline_pin(model)
        typer.echo(f"[OK] pinned {model} ({receipt.receipt_hash[:8]})")
    except ValueError as exc:
        typer.echo(f"[ERROR] {exc}", err=True)
        raise typer.Exit(1)


@pipeline_app.command("evict")
def pipeline_evict_cmd(model: str = typer.Argument(...)):
    """Evict a model from Ollama memory."""
    try:
        receipt = service.pipeline_evict(model)
        typer.echo(f"[OK] evicted {model} ({receipt.receipt_hash[:8]})")
    except ValueError as exc:
        typer.echo(f"[ERROR] {exc}", err=True)
        raise typer.Exit(1)


@pipeline_app.command("drain-swap")
def pipeline_drain_swap_cmd(
    old_model: str = typer.Argument(...),
    new_model: str = typer.Argument(...),
    timeout: float = typer.Option(30.0, "--timeout", "-t"),
):
    """Soft-drain in-flight requests then swap to new model."""
    try:
        receipt = service.pipeline_drain_swap(old_model, new_model, timeout_s=timeout)
        fallback = receipt.payload.get("fallback", False)
        mode = "hard-swap (timeout)" if fallback else "soft-drain"
        typer.echo(f"[OK] {mode}: {old_model} -> {new_model} ({receipt.receipt_hash[:8]})")
    except ValueError as exc:
        typer.echo(f"[ERROR] {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# macfind subcommand group
# ---------------------------------------------------------------------------

macfind_app = typer.Typer(name="macfind", help="Semantic file search on approved Mac directories.")
app.add_typer(macfind_app)


@macfind_app.command("query")
def macfind_query_cmd(
    query: str = typer.Argument(..., help="Search query text"),
    namespace: Optional[str] = typer.Option(None, "--namespace", "-n", help="Namespace prefix"),
    max_results: int = typer.Option(10, "--max-results", "-m", help="Maximum number of hits"),
) -> None:
    """Search approved Mac directories semantically."""
    try:
        receipt = service.find_on_mac(query, namespace, max_results=max_results)
        if receipt.status == "no_match":
            typer.echo(f"[NO MATCH] {receipt.no_match_reason}")
        elif receipt.status in ("index_missing", "not_configured"):
            typer.echo(f"[{receipt.status.upper()}] {receipt.no_match_reason}")
        else:
            for hit in receipt.hits:
                typer.echo(f"{hit.path}  (score={hit.hybrid_score:.3f})")
                typer.echo(f"  {hit.snippet}")
                typer.echo("")
    except ValueError as exc:
        typer.echo(f"[ERROR] {exc}", err=True)
        raise typer.Exit(1)


@macfind_app.command("reindex")
def macfind_reindex_cmd() -> None:
    """Rebuild the macfind index from approved directories."""
    try:
        stats = service.reindex_macfind()
        typer.echo(f"[OK] Indexed {stats.get('files', 0)} files, {stats.get('chunks', 0)} chunks.")
        if stats.get("errors"):
            typer.echo(f"[WARN] {len(stats['errors'])} errors during indexing.")
    except ValueError as exc:
        typer.echo(f"[ERROR] {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# agent subcommand group (Plan 35-02)
# ---------------------------------------------------------------------------

agent_app = typer.Typer(help="Typed agent commands.")
app.add_typer(agent_app, name="agent")


@agent_app.command("run")
def agent_run_cmd(
    name: str = typer.Argument(..., help="Agent name: helper, executor, macfind"),
    prompt: str = typer.Argument(..., help="Prompt text"),
    project: str = typer.Option("", "--project", "-p"),
    namespace: str = typer.Option("", "--namespace", "-n"),
    tool: str = typer.Option("", "--tool", "-t", help="Tool name to invoke"),
):
    """Run a typed agent by name and print the AgentReceipt."""
    try:
        receipt = service.run_agent(
            name,
            prompt,
            project=project or None,
            namespace=namespace or None,
            tool_name=tool or None,
        )
        typer.echo(f"[OK] {receipt.agent_name}: model={receipt.model_selected}, retries={receipt.retry_count}")
        typer.echo(receipt.output.get("result", ""))
    except ValueError as exc:
        typer.echo(f"[ERROR] {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Startup smoke (v4.5 Phase 51 PERSIST-01 + PERSIST-03)
# ---------------------------------------------------------------------------

def _smoke_run_launchctl_list(label: str) -> tuple[bool, str]:
    """Return ``(present, detail)`` — launchd service presence by label."""
    import subprocess  # noqa: PLC0415
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, shell=False, timeout=_LAUNCHCTL_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return False, f"launchctl list failed: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    # Output shape: "<pid-or-dash>\t<exit>\t<label>"; dash means loaded-but-stopped.
    out = result.stdout.strip()
    if not out or label not in out:
        return False, "service not loaded"
    return True, out


def _smoke_http_get(base_url: str, path: str, *, timeout: float = 5.0) -> tuple[bool, str, bytes]:
    """Return ``(ok, detail, body)`` for a single HTTP GET."""
    import http.client  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415
    url = f"{base_url.rstrip('/')}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            body = resp.read()
            return True, f"GET {path} ok", body
    except urllib.error.URLError as exc:
        return False, f"GET {path} failed: {exc}", b""
    except (TimeoutError, OSError, ValueError, http.client.HTTPException) as exc:
        return False, f"GET {path} error: {type(exc).__name__}: {exc}", b""


@app.command("startup-smoke")
def startup_smoke_cmd(
    base_url: str = typer.Option(
        "http://127.0.0.1:8484", "--base-url",
        help="HTTP base URL for /health, /startup/readiness, /models/status, /metrics",
    ),
) -> None:
    """Inspect the persistent ollarma service post-login/restart.

    Inspection-only — does NOT mutate launchd state. Probes:
      - launchctl list com.byron.ollarma
      - <base>/health
      - <base>/startup/readiness
      - <base>/models/status
      - <base>/metrics

    Exits 0 for ready/degraded startup posture. Exits 1 only for:
      - launchd service missing
      - HTTP service unreachable
      - readiness status == blocked
    """
    import json as _json  # noqa: PLC0415

    exit_code = 0
    console.print("[bold]ollarma startup smoke[/bold]")

    # 1. launchd presence
    present, detail = _smoke_run_launchctl_list(_LAUNCHD_LABEL)
    if present:
        console.print(f"[green]launchctl:[/green] {detail}")
    else:
        console.print(f"[red]launchctl:[/red] {detail}")
        exit_code = 1

    # 2. HTTP endpoints
    readiness_status: str | None = None
    for path in ("/health", "/startup/readiness", "/models/status", "/metrics"):
        ok, detail, body = _smoke_http_get(base_url, path)
        if not ok:
            console.print(f"[red]{path}:[/red] {detail}")
            exit_code = 1
            continue
        # Parse readiness to extract status
        if path in ("/health", "/startup/readiness") and body:
            try:
                parsed = _json.loads(body.decode())
                if path == "/startup/readiness":
                    readiness_status = parsed.get("status")
                elif path == "/health" and readiness_status is None:
                    # Fall back to /health's folded status
                    readiness_status = parsed.get("status")
                console.print(f"[green]{path}:[/green] status={parsed.get('status', '?')}")
                continue
            except (ValueError, UnicodeDecodeError):
                pass
        console.print(f"[green]{path}:[/green] {len(body)}B")

    # 3. Readiness status gate
    if readiness_status == "blocked":
        console.print("[red]Startup readiness is BLOCKED[/red]")
        exit_code = 1
    elif readiness_status == "degraded":
        console.print("[yellow]Startup readiness is DEGRADED (non-fatal)[/yellow]")
    elif readiness_status == "ready":
        console.print("[green]Startup readiness is READY[/green]")
    else:
        console.print(f"[yellow]Readiness status unknown: {readiness_status!r}[/yellow]")

    # 4. Log path hints
    console.print(
        "[dim]Logs: ~/Library/Logs/ollarma/stdout.log | "
        "~/Library/Logs/ollarma/stderr.log[/dim]"
    )

    if exit_code:
        raise typer.Exit(exit_code)


# ---------------------------------------------------------------------------
# Recovery subcommand group (v4.3)
# ---------------------------------------------------------------------------

recover_app = typer.Typer(
    help="Scan and report on interrupted agent/model work (stranded worktrees, sidecar branches, scribe state)",
    no_args_is_help=True,
)
app.add_typer(recover_app, name="recover")


def _render_recovery_summary(packet: dict, *, title: str) -> None:
    """Render a recovery packet as a Rich summary table."""
    from rich.table import Table  # noqa: PLC0415

    state = packet.get("state", "unknown")
    blocker = packet.get("blocker_code", "?")
    worktrees = packet.get("worktrees", []) or []
    ahead = packet.get("ahead_commits", []) or []
    artifacts = packet.get("artifacts_at_risk", []) or []
    resume = packet.get("resume_present", False)
    log = packet.get("session_log_present", False)

    t = Table(title=title, show_lines=False)
    t.add_column("Field", style="bold")
    t.add_column("Value")
    t.add_row("State", state)
    t.add_row("Blocker", blocker)
    t.add_row("Resume artifact", "yes" if resume else "no")
    t.add_row("Session log", "yes" if log else "no")
    t.add_row("Worktrees seen", str(len(worktrees)))
    t.add_row("Sidecar branches ahead", str(len(ahead)))
    t.add_row("Artifacts at risk", str(len(artifacts)))
    console.print(t)

    fixes = packet.get("next_fix_commands", []) or []
    if fixes:
        console.print("\n[bold]Next fix commands:[/bold]")
        for f in fixes:
            console.print(f"  [dim]$[/dim] {f}")

    notes = packet.get("notes", []) or []
    if notes:
        console.print("\n[bold]Notes:[/bold]")
        for n in notes:
            console.print(f"  - {n}")


@recover_app.command("scan")
def recover_scan_cmd(
    project_root: Optional[str] = typer.Option(
        None, "--project-root", help="Repo root to scan (default: cwd)",
    ),
    base_branch: str = typer.Option(
        "main", "--base", help="Base branch to compare sidecar branches against",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit raw JSON packet to stdout (no summary)",
    ),
) -> None:
    """Scan the repo for stranded work and write a recovery packet."""
    import subprocess  # noqa: PLC0415
    try:
        packet = service.recover_scan(
            project_root=project_root, source="cli", base_branch=base_branch,
        )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        typer.echo(f"[ERROR] {exc}", err=True)
        raise typer.Exit(2)

    if json_out:
        import orjson  # noqa: PLC0415
        typer.echo(orjson.dumps(packet, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode())
    else:
        _render_recovery_summary(packet, title="Recovery Scan")

    # Non-zero exit on any non-clean state so shell scripts can react.
    if packet.get("state") != "clean":
        raise typer.Exit(1)


@recover_app.command("report")
def recover_report_cmd(
    project_root: Optional[str] = typer.Option(
        None, "--project-root", help="Repo root (default: cwd)",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit raw JSON packet to stdout (no summary)",
    ),
) -> None:
    """Print the latest recovery packet without running a new scan."""
    packet = service.recover_latest(project_root=project_root)
    if packet is None:
        typer.echo(
            "No recovery packet found. Run `ollarma recover scan` first.",
            err=True,
        )
        raise typer.Exit(2)

    if json_out:
        import orjson  # noqa: PLC0415
        typer.echo(orjson.dumps(packet, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode())
    else:
        _render_recovery_summary(packet, title="Recovery Report (latest)")

    # Report command also exits non-zero on non-clean state so CI can branch.
    if packet.get("state") != "clean":
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Receipts subcommand group (Phase 57.1, OBS-59, F-04)
# ---------------------------------------------------------------------------

receipts_app = typer.Typer(
    help="Inspect receipt chains (Phase 57.1).",
    no_args_is_help=True,
)
app.add_typer(receipts_app, name="receipts")


@receipts_app.command("trace")
def receipts_trace_cmd(
    escalation_receipt_id: str = typer.Argument(
        ..., help="Escalation receipt id (form: er-<16 hex>)",
    ),
    repo_root: Optional[str] = typer.Option(
        None, "--repo-root", help="Repo root containing .ollarma/ (default: cwd)",
    ),
) -> None:
    """Walk escalation -> admission -> frontier chain for a given id.

    Reads only. Exits 0 when chain is intact (or escalation is legitimately
    external), exits 1 on HASH_MISMATCH / MISSING_ADMISSION / MISSING_FRONTIER.
    """
    from ollarma import receipts_trace  # noqa: PLC0415 -- keep CLI import cheap
    import sys  # noqa: PLC0415

    root = pathlib.Path(repo_root) if repo_root else pathlib.Path.cwd()
    result = receipts_trace.trace(escalation_receipt_id, root)
    use_tty = sys.stdout.isatty()
    rendered = receipts_trace.render_table(result, use_tty=use_tty)
    typer.echo(rendered)
    if result.exit_code != 0:
        raise typer.Exit(result.exit_code)


# ---------------------------------------------------------------------------
# Gateway operator CLI (Phase 59: live-smoke entry point)
# ---------------------------------------------------------------------------

gateway_app = typer.Typer(
    help="Frontier gateway operator commands (Phase 59+).",
    no_args_is_help=True,
)
app.add_typer(gateway_app, name="gateway")


@gateway_app.command("smoke-anthropic")
def gateway_smoke_anthropic_cmd(
    prompt: str = typer.Option(
        "Reply with OK.", "--prompt", help="Prompt to send.",
    ),
    model: str = typer.Option(
        "claude-haiku-4-5-latest", "--model", help="Anthropic model id.",
    ),
    virtual_key_id: str = typer.Option(
        "vk_anthropic", "--virtual-key-id", help="Virtual-key registry id.",
    ),
    repo_root: Optional[str] = typer.Option(
        None, "--repo-root", help="Repo containing .planning/config.json.",
    ),
) -> None:
    """Live-smoke the Anthropic adapter against the operator Keychain entry.

    Preconditions (D-59-07):
      - ``features.gateway.enabled`` is ``true`` in ``.planning/config.json``.
      - The escalation_receipt's project ("smoke") is in the allowlist.
      - ``virtual_key_id`` resolves to a registry entry whose
        ``keychain_service`` points at the Anthropic Keychain credential
        (default: ``ollarma-anthropic``).

    On Keychain miss: prints the exact install command so the operator can
    activate the live path without guessing.
    """
    from ollarma.escalation import ReasonCode, build_escalation_receipt  # noqa: PLC0415
    from ollarma.gateway_admission import resolve_virtual_key  # noqa: PLC0415
    from ollarma.gateway_client import GatewayClient  # noqa: PLC0415
    from ollarma.providers import get_provider  # noqa: PLC0415

    root = pathlib.Path(repo_root) if repo_root else pathlib.Path.cwd()
    service_name_default = "ollarma-anthropic"
    key_bytes = resolve_virtual_key(service_name_default)
    if key_bytes is None:
        console.print(
            f"[red]KEYCHAIN_MISS[/red] service={service_name_default!r} "
            "returned no credential.\n"
            "Install with:\n"
            f"  [bold]security add-generic-password -s {service_name_default} "
            "-a $USER -w[/bold]",
        )
        raise typer.Exit(2)

    escalation = build_escalation_receipt(
        project="smoke",
        lane="local",
        task_class="generic",
        reason_code=ReasonCode.SWAP_DEGRADED,
        reason_detail="gateway smoke-anthropic live smoke",
        next_action="frontier_or_human",
    )
    adapter = get_provider("anthropic")
    client = GatewayClient(root)
    receipt = client.submit(
        escalation,
        dry_run=False,
        provider="anthropic",
        model_id=model,
        prompt=prompt,
        provider_adapter=adapter,
        virtual_key_bytes=key_bytes,
    )
    # Clear local reference to key bytes before rendering.
    del key_bytes
    console.print(
        f"[green]smoke-anthropic[/green] status={receipt.status} "
        f"model_id={receipt.model_id} "
        f"prompt_tokens={receipt.prompt_tokens} "
        f"response_tokens={receipt.response_tokens} "
        f"cost_usd={receipt.cost_usd} latency_ms={receipt.latency_ms}"
    )
    if receipt.status != "succeeded":
        raise typer.Exit(1)


@gateway_app.command("smoke-openai")
def gateway_smoke_openai_cmd(
    prompt: str = typer.Option(
        "Reply with OK.", "--prompt", help="Prompt to send.",
    ),
    model: str = typer.Option(
        "gpt-4o-mini", "--model", help="OpenAI model id.",
    ),
    virtual_key_id: str = typer.Option(
        "vk_openai", "--virtual-key-id", help="Virtual-key registry id.",
    ),
    repo_root: Optional[str] = typer.Option(
        None, "--repo-root", help="Repo containing .planning/config.json.",
    ),
) -> None:
    """Live-smoke the OpenAI adapter against the operator Keychain entry.

    Preconditions (mirror smoke-anthropic):
      - ``features.gateway.enabled`` is ``true`` in ``.planning/config.json``.
      - The escalation_receipt's project ("smoke") is in the allowlist.
      - ``virtual_key_id`` resolves to a registry entry whose
        ``keychain_service`` points at the OpenAI Keychain credential
        (default: ``ollarma-openai``).

    On Keychain miss: prints the exact install command so the operator can
    activate the live path without guessing.
    """
    from ollarma.escalation import ReasonCode, build_escalation_receipt  # noqa: PLC0415
    from ollarma.gateway_admission import resolve_virtual_key  # noqa: PLC0415
    from ollarma.gateway_client import GatewayClient  # noqa: PLC0415
    from ollarma.providers import get_provider  # noqa: PLC0415

    root = pathlib.Path(repo_root) if repo_root else pathlib.Path.cwd()
    service_name_default = "ollarma-openai"
    key_bytes = resolve_virtual_key(service_name_default)
    if key_bytes is None:
        console.print(
            f"[red]KEYCHAIN_MISS[/red] service={service_name_default!r} "
            "returned no credential.\n"
            "Install with:\n"
            f"  [bold]security add-generic-password -s {service_name_default} "
            "-a $USER -w[/bold]",
        )
        raise typer.Exit(2)

    escalation = build_escalation_receipt(
        project="smoke",
        lane="local",
        task_class="generic",
        reason_code=ReasonCode.SWAP_DEGRADED,
        reason_detail="gateway smoke-openai live smoke",
        next_action="frontier_or_human",
    )
    adapter = get_provider("openai")
    client = GatewayClient(root)
    receipt = client.submit(
        escalation,
        dry_run=False,
        provider="openai",
        model_id=model,
        prompt=prompt,
        provider_adapter=adapter,
        virtual_key_bytes=key_bytes,
    )
    # Clear local reference to key bytes before rendering.
    del key_bytes
    console.print(
        f"[green]smoke-openai[/green] status={receipt.status} "
        f"model_id={receipt.model_id} "
        f"prompt_tokens={receipt.prompt_tokens} "
        f"response_tokens={receipt.response_tokens} "
        f"cost_usd={receipt.cost_usd} latency_ms={receipt.latency_ms}"
    )
    if receipt.status != "succeeded":
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# SWE-bench Lite subcommand group (Phase 61-01; EXP-1.2 substrate)
# ---------------------------------------------------------------------------

swe_bench_app = typer.Typer(
    help="SWE-bench Lite harness (Phase 61+; EXP-1.2 substrate).",
    no_args_is_help=True,
)
app.add_typer(swe_bench_app, name="swe-bench")


@swe_bench_app.command("fetch")
def swe_bench_fetch_cmd(
    dataset: str = typer.Option(
        "swe-bench-lite", "--dataset", help="Dataset identifier.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Redownload even if cache is valid.",
    ),
    repo_root: Optional[str] = typer.Option(
        None, "--repo-root", help="Repo root containing .ollarma/ (default: cwd).",
    ),
) -> None:
    """Download + cache SWE-bench Lite with a pinned SHA checksum."""
    from ollarma import swe_bench  # noqa: PLC0415

    root = pathlib.Path(repo_root) if repo_root else pathlib.Path.cwd()
    try:
        path = swe_bench.fetch_dataset(root, dataset=dataset, force=force)
    except ValueError as exc:
        console.print(f"[red]fetch failed:[/red] {exc}")
        raise typer.Exit(2)
    console.print(f"[green]fetched[/green] {path}")


@swe_bench_app.command("run")
def swe_bench_run_cmd(
    lane: str = typer.Option(
        "local", "--lane", help="Lane to run: local | frontier.",
    ),
    subset: Optional[str] = typer.Option(
        None, "--subset",
        help="Required: first-N | ids=ID1,ID2,... | random-seed=SEED,count=N",
    ),
    project: str = typer.Option(
        "swe-bench-demo", "--project", help="Project name passed to autopilot.",
    ),
    dataset: str = typer.Option(
        "swe-bench-lite", "--dataset", help="Dataset identifier.",
    ),
    repo_root: Optional[str] = typer.Option(
        None, "--repo-root", help="Repo root containing .ollarma/ (default: cwd).",
    ),
    timeout_s: int = typer.Option(
        300, "--timeout-s", min=1, help="Per-problem sandbox timeout (seconds).",
    ),
    provider: str = typer.Option(
        "anthropic", "--provider",
        help="Frontier provider to dispatch to (frontier lane only).",
    ),
    model: str = typer.Option(
        "claude-haiku-4-5-latest", "--model",
        help="Provider model id (frontier lane only).",
    ),
    virtual_key_id: Optional[str] = typer.Option(
        None, "--virtual-key-id",
        help="Virtual key id resolved by the admission policy (frontier lane).",
    ),
    live: bool = typer.Option(
        False, "--live/--no-live",
        help=(
            "Execute against a real provider HTTP endpoint. Default --no-live "
            "short-circuits before the provider call and exits; the real live "
            "run is an operator session gated by this flag."
        ),
    ),
) -> None:
    """Iterate a subset of SWE-bench problems through the chosen lane.

    Substrate only: local-lane tests mock the autopilot dispatch; frontier-lane
    tests exercise ``--no-live`` which does NOT reach a real provider. The
    operator live-run must pass ``--live`` and have a real virtual key
    configured.
    """
    from ollarma import swe_bench  # noqa: PLC0415

    if lane not in ("local", "frontier"):
        console.print(f"[red]unknown lane:[/red] {lane}")
        raise typer.Exit(2)
    if not subset:
        console.print(
            "[red]--subset is required[/red] "
            "(first-N | ids=ID1,ID2,... | random-seed=SEED,count=N)"
        )
        raise typer.Exit(2)

    root = pathlib.Path(repo_root) if repo_root else pathlib.Path.cwd()
    try:
        problems = swe_bench.load_problems(root, dataset=dataset)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    except ValueError as exc:
        console.print(f"[red]load failed:[/red] {exc}")
        raise typer.Exit(2)

    try:
        selected = swe_bench.resolve_subset(problems, subset)
    except ValueError as exc:
        console.print(f"[red]subset error:[/red] {exc}")
        raise typer.Exit(2)

    if lane == "local":
        runner = swe_bench.LocalLaneRunner(
            repo_root=root,
            dataset=dataset,
            autopilot_dispatch=_cli_autopilot_dispatch,
        )
        rows: list[dict[str, object]] = []
        for problem in selected:
            record = runner.run(problem, project, timeout_s=timeout_s)
            rows.append({
                "instance_id": record.instance_id,
                "status": record.status,
                "duration_s": round(record.duration_s, 3),
                "reason_code": record.reason_code or "",
            })
        _render_swe_bench_table(runner.run_id, rows)
        console.print(f"receipts: {runner.receipts_path}")
        return

    # Frontier lane (SWE-03).
    if not virtual_key_id:
        console.print(
            "[red]--virtual-key-id is required for --lane frontier[/red]"
        )
        raise typer.Exit(2)
    if not live:
        console.print(
            "[yellow]--no-live[/yellow]: frontier-lane live dispatch "
            "skipped. Pass --live to run against the real provider (operator "
            "session only). Phase 62-01 substrate: no HTTP in test contexts."
        )
        raise typer.Exit(0)

    # --live path. Import lazily so --no-live never pulls httpx.
    from ollarma.gateway_admission import (  # noqa: PLC0415
        AdmissionPolicy, _load_gateway_config, resolve_virtual_key,
    )
    from ollarma.gateway_client import GatewayClient  # noqa: PLC0415

    config = _load_gateway_config(root / ".planning" / "config.json")
    policy = AdmissionPolicy(config)
    vk_entry = policy._find_vk(virtual_key_id)  # noqa: SLF001
    if vk_entry is None:
        console.print(
            f"[red]unknown virtual_key_id:[/red] {virtual_key_id}"
        )
        raise typer.Exit(2)
    vk_bytes = resolve_virtual_key(vk_entry["keychain_service"])
    if vk_bytes is None:
        console.print(
            f"[red]keychain miss for virtual_key_id:[/red] {virtual_key_id}"
        )
        raise typer.Exit(2)

    client = GatewayClient(root)
    runner = swe_bench.FrontierLaneRunner(
        repo_root=root,
        gateway_client=client,
        admission_policy=policy,
        dataset=dataset,
        virtual_key_bytes=vk_bytes,
    )
    rows = []
    for problem in selected:
        record = runner.run(
            problem, project,
            virtual_key_id=virtual_key_id,
            model=model,
            timeout_s=timeout_s,
        )
        rows.append({
            "instance_id": record.instance_id,
            "status": record.status,
            "duration_s": round(record.duration_s, 3),
            "reason_code": record.reason_code or "",
        })
    _render_swe_bench_table(runner.run_id, rows)
    console.print(f"receipts: {runner.receipts_path}")


def _render_swe_bench_table(run_id: str, rows: list[dict]) -> None:
    table = Table(title=f"SWE-bench run {run_id}")
    table.add_column("instance_id")
    table.add_column("status")
    table.add_column("duration_s", justify="right")
    table.add_column("reason_code")
    for row in rows:
        table.add_row(
            str(row["instance_id"]), str(row["status"]),
            str(row["duration_s"]), str(row["reason_code"]),
        )
    console.print(table)


@swe_bench_app.command("leaderboard")
def swe_bench_leaderboard_cmd(
    run_id: str = typer.Argument(
        ..., help="Run id previously passed to build_leaderboard().",
    ),
    dataset: str = typer.Option(
        "swe-bench-lite", "--dataset", help="Dataset identifier.",
    ),
    repo_root: Optional[str] = typer.Option(
        None, "--repo-root", help="Repo root containing results/ + .ollarma/.",
    ),
    frontier_run_id: Optional[str] = typer.Option(
        None, "--frontier-run-id",
        help="Frontier-lane run id (defaults to --run-id when both lanes share a dir).",
    ),
) -> None:
    """Print a leaderboard artifact + verify the receipt chain."""
    from ollarma import swe_bench  # noqa: PLC0415

    root = pathlib.Path(repo_root) if repo_root else pathlib.Path.cwd()
    try:
        artifact = swe_bench.load_leaderboard(run_id, root, dataset=dataset)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)

    console.print(f"[bold]SWE-bench leaderboard[/bold] {artifact.run_id}")
    console.print(f"subset: {artifact.subset_spec}")
    console.print(f"total_problems: {artifact.total_problems}")
    console.print("")

    table = Table(title="pass@1 by lane")
    table.add_column("lane")
    table.add_column("pass@1")
    table.add_column("cost_usd")
    for lane in ("local", "frontier"):
        table.add_row(
            lane,
            artifact.per_lane_pass_at_1.get(lane, "0"),
            artifact.per_lane_cost_usd.get(lane, "0"),
        )
    console.print(table)

    result = swe_bench.verify_leaderboard_chain(
        run_id, root, dataset=dataset, frontier_run_id=frontier_run_id,
    )
    if result.passed:
        console.print("[green]chain verify: clean[/green]")
        raise typer.Exit(0)
    console.print(
        f"[red]chain verify: broken[/red] {result.first_break_detail}"
    )
    raise typer.Exit(1)


@swe_bench_app.command("status")
def swe_bench_status_cmd(
    dataset: str = typer.Option(
        "swe-bench-lite", "--dataset", help="Dataset identifier.",
    ),
    repo_root: Optional[str] = typer.Option(
        None, "--repo-root", help="Repo root containing .ollarma/ (default: cwd).",
    ),
) -> None:
    """Show dataset cache state and the most recent run summary."""
    from ollarma import swe_bench  # noqa: PLC0415

    root = pathlib.Path(repo_root) if repo_root else pathlib.Path.cwd()
    jsonl = root / ".ollarma" / "benchmarks" / dataset / "problems.jsonl"
    sha = root / ".ollarma" / "benchmarks" / dataset / "problems.sha256"
    runs_dir = root / ".ollarma" / "benchmarks" / dataset / "runs"

    if jsonl.exists() and sha.exists():
        console.print(f"[green]cache:[/green] {jsonl} (sha={sha.read_text().strip()[:16]}...)")
    else:
        console.print(f"[yellow]cache:[/yellow] not fetched ({jsonl})")

    if runs_dir.exists():
        runs = sorted(p.name for p in runs_dir.iterdir() if p.is_dir())
    else:
        runs = []
    if runs:
        console.print(f"runs: {len(runs)} total; latest={runs[-1]}")
    else:
        console.print("runs: none")
    # Reference swe_bench for static import validation.
    _ = swe_bench.DATASET_DEFAULT


def _cli_autopilot_dispatch(problem, project):  # noqa: ANN001, ARG001 -- private thunk
    """Thin wrapper around ``service.submit_autopilot`` for the CLI run path.

    The test suite does NOT exercise this thunk (tests inject their own
    dispatch). The operator live-run reads its result and acts on the
    escalation receipt iff swap is degraded.
    """
    from ollarma import service  # noqa: PLC0415
    from ollarma.escalation import (  # noqa: PLC0415
        ReasonCode, build_escalation_receipt,
    )

    try:
        report = service.submit_autopilot(project=project, run_assets=True)
    except (OSError, ValueError) as exc:
        receipt = build_escalation_receipt(
            project=project,
            lane="local",
            task_class="swe-bench",
            reason_code=ReasonCode.SWAP_DEGRADED,
            reason_detail=f"autopilot dispatch error: {exc}",
            next_action="frontier_or_human",
        )
        return (None, receipt)
    return (report, None)


if __name__ == "__main__":
    app()
