"""Resume Agent CLI.

Commands:
  resume-agent init       # one-time scaffold of personal data files
  resume-agent index      # (re)build the project library vector store
  resume-agent ingest     # pull jobs from configured ATS boards (no LLM)
  resume-agent run        # ingest + filter + tailor + validate + render + persist
  resume-agent status     # show the latest applications + statuses
  resume-agent doctor     # health-check ollama, configs, embeddings
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from config_loader import PROJECT_ROOT, load_preferences, load_settings
from filters import filter_jobs
from ingest.runner import fetch_all
from notify.telegram import notify
from orchestrator.graph import build_graph
from orchestrator.state import ApplicationState, ApplicationStatus
from retrieval import project_library
from store import db as store_db

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(rich_tracebacks=True, markup=False, show_path=False)],
    )


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command()
def init(
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing config files."),
):
    """Copy *.example.yaml -> *.yaml and create a starter master_resume.md."""
    cfg = PROJECT_ROOT / "config"
    pairs = [
        (cfg / "settings.example.yaml", cfg / "settings.yaml"),
        (cfg / "preferences.example.yaml", cfg / "preferences.yaml"),
        (cfg / "personal_facts.example.yaml", cfg / "personal_facts.yaml"),
    ]
    for src, dst in pairs:
        if dst.exists() and not overwrite:
            console.print(f"[yellow]skip[/] {dst.relative_to(PROJECT_ROOT)} (exists)")
            continue
        shutil.copyfile(src, dst)
        console.print(f"[green]wrote[/] {dst.relative_to(PROJECT_ROOT)}")

    master_dst = PROJECT_ROOT / "data" / "input" / "master_resume.md"
    master_src = PROJECT_ROOT / "data" / "input" / "master_resume.example.md"
    if master_src.exists() and (overwrite or not master_dst.exists()):
        shutil.copyfile(master_src, master_dst)
        console.print(f"[green]wrote[/] {master_dst.relative_to(PROJECT_ROOT)}")

    env_dst = PROJECT_ROOT / ".env"
    if not env_dst.exists():
        shutil.copyfile(PROJECT_ROOT / ".env.example", env_dst)
        console.print(f"[green]wrote[/] {env_dst.relative_to(PROJECT_ROOT)} (edit me)")

    console.print("\n[bold]Next:[/] edit config/*.yaml + data/input/master_resume.md, then run `resume-agent index`.")


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


@app.command()
def index(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Re-embed all projects in data/input/projects/*.md into the Chroma vector store."""
    _setup_logging(verbose)
    n = project_library.reindex()
    console.print(f"[green]Indexed {n} projects.[/]")


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


@app.command()
def ingest(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Pull jobs from every configured ATS board. Writes nothing; prints a summary."""
    _setup_logging(verbose)
    jobs = fetch_all()
    table = Table(title=f"Ingested {len(jobs)} jobs")
    table.add_column("Source"); table.add_column("Company"); table.add_column("Title"); table.add_column("Loc")
    for j in jobs[:50]:
        table.add_row(j.source.value, j.company, j.title, j.location or "-")
    console.print(table)
    if len(jobs) > 50:
        console.print(f"... and {len(jobs) - 50} more")


# ---------------------------------------------------------------------------
# run (the main pipeline)
# ---------------------------------------------------------------------------


@app.command()
def run(
    limit: int = typer.Option(5, "--limit", help="Max jobs to process this run."),
    dry_apply: bool = typer.Option(True, "--dry-apply/--apply", help="Render artifacts but don't submit (Phase 1 default)."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Full pipeline: ingest -> filter -> per-job (tailor -> validate -> render -> persist)."""
    _setup_logging(verbose)
    # CLI flag overrides settings.apply.dry_apply for this run.
    load_settings()["apply"]["dry_apply"] = bool(dry_apply)
    jobs = fetch_all()
    console.print(f"Ingested [bold]{len(jobs)}[/] raw jobs")
    kept = filter_jobs(jobs)
    console.print(f"Kept [bold]{len(kept)}[/] after filtering")

    if not kept:
        console.print("[yellow]Nothing to do.[/]")
        return

    graph = build_graph()
    processed = 0
    summary_rows: list[tuple[str, str, str]] = []
    for job, score in kept[:limit]:
        console.rule(f"[cyan]{job.company}[/] — {job.title} (semantic={score:.2f})")
        state = ApplicationState(job=job)
        result = graph.invoke(state)
        # LangGraph returns the final state as a dict; coerce back for nice printing.
        final = ApplicationState(**result) if isinstance(result, dict) else result
        store_db.upsert(final)  # belt-and-suspenders: persist even if a node skipped
        if final.status == ApplicationStatus.READY_TO_APPLY and dry_apply:
            console.print(f"[green]DRY-APPLY OK[/] -> {final.output_dir}")
        elif final.status == ApplicationStatus.NEEDS_REVIEW:
            console.print(f"[yellow]NEEDS REVIEW[/] -> {final.output_dir}")
            notify(
                f"NEEDS REVIEW: {job.company} — {job.title}\n"
                + "\n".join(final.validation_errors[:5]),
                level="warn",
            )
        elif final.status == ApplicationStatus.FAILED:
            console.print(f"[red]FAILED[/]: {[b.message for b in final.barriers]}")
        if final.estimates_present:
            notify(
                f"Estimates present in {job.company} — {job.title}. "
                f"Review {final.output_dir}/decisions.md",
                level="info",
            )
        summary_rows.append((job.company, job.title, final.status.value))
        processed += 1

    table = Table(title=f"Run summary ({processed} processed)")
    table.add_column("Company"); table.add_column("Title"); table.add_column("Status")
    for r in summary_rows:
        table.add_row(*r)
    console.print(table)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command()
def status(limit: int = typer.Option(20, "--limit")):
    """Show recent application records from the SQLite tracker."""
    rows = store_db.list_applications(limit=limit)
    table = Table(title="Recent applications")
    for col in ("Company", "Title", "Status", "ATS", "Output"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r.company,
            r.title,
            r.status,
            f"{(r.ats_coverage or 0):.0%}" if r.ats_coverage is not None else "-",
            (r.output_dir or "-").replace(str(PROJECT_ROOT) + "/", ""),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command("linkedin-login")
def linkedin_login():
    """Open a headed Patchright browser to LinkedIn, save storage_state once logged in.

    Re-run this whenever LinkedIn invalidates the session (typically every 30-90 days
    or after a security check). Required before LinkedIn ingest/apply work.
    """
    from ingest.linkedin import interactive_login

    path = interactive_login()
    console.print(f"[green]Saved storage_state to[/] {path}")


@app.command()
def doctor():
    """Check that Ollama, configs, and embeddings are working."""
    ok = True
    try:
        load_settings(); console.print("[green]ok[/] settings.yaml")
    except Exception as e:  # noqa: BLE001
        ok = False; console.print(f"[red]ERR[/] settings.yaml: {e}")
    try:
        load_preferences(); console.print("[green]ok[/] preferences.yaml")
    except Exception as e:  # noqa: BLE001
        ok = False; console.print(f"[red]ERR[/] preferences.yaml: {e}")

    try:
        from llm.router import get_router
        v = get_router().embed(["hello world"])[0]
        console.print(f"[green]ok[/] Ollama embeddings (dim={len(v)})")
    except Exception as e:  # noqa: BLE001
        ok = False
        console.print(f"[red]ERR[/] Ollama embeddings: {e}")
        console.print("    -> is `ollama serve` running and `ollama pull nomic-embed-text` done?")

    try:
        from llm.router import get_router
        # Cold model load can take a while; keep num_predict modest. For Qwen3,
        # think=False forces /no_think so short smoke replies are not eaten by
        # reasoning tokens. Other models (e.g. Gemma) ignore think.
        out, prov = get_router().chat(
            task="rerank",
            prompt="Reply with the single word OK.",
            max_tokens=64,
            think=False,
        )
        console.print(f"[green]ok[/] chat via {prov}: {out.strip()[:80]}")
    except Exception as e:  # noqa: BLE001
        ok = False
        console.print(f"[red]ERR[/] chat: {e}")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    app()
