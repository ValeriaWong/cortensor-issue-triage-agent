"""CLI: issue-triage triage | probe | eval | approve.

- ``triage`` never writes to GitHub. It prints the verdict + draft and
  saves a pending snapshot for a human to approve.
- ``approve --yes`` is the only path that writes, and only with
  ``GITHUB_TOKEN``; without a token it prints exact ``gh api`` commands
  for the human to run instead of pretending it succeeded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from .approval import AlreadyConsumedError, NotPendingError, SnapshotStore
from .config import (
    fix_proxy_env,
    github_token,
    load_dotenv,
    min_interval_seconds,
    reasoning_effort_default,
    resolve_provider,
)
from .eval import run_eval, save_eval_result
from .github_client import GitHubClient, parse_issue_url, parse_repo
from .llm_client import DirectClient
from .memory import RepoMemory
from .models import Snapshot, TriageDecision, utcnow_iso
from .probe import PROBE_NAMES, run_probes, write_building_blocks_md
from .triage import TriageEngine, snapshot_id_for

app = typer.Typer(help="Single-issue deep triage agent (Cortensor Hackathon #5).")


def _setup(preset: str | None) -> tuple[DirectClient, GitHubClient, RepoMemory]:
    fix_proxy_env()
    load_dotenv()
    provider = resolve_provider(preset)
    llm = DirectClient(
        base_url=provider.base_url,
        model=provider.model,
        api_key=provider.api_key,
        min_interval_sec=min_interval_seconds(),
        reasoning_effort=reasoning_effort_default(),
    )
    return llm, GitHubClient(token=github_token()), RepoMemory()


def _print_verdict(result: dict) -> None:
    verdict = result["verdict"]
    draft = result["draft"]
    typer.echo(f"\n== {result['repo']}#{result['issue_number']}: {result['title']}")
    typer.echo(f"   type      : {verdict.issue_type}")
    typer.echo(f"   severity  : {verdict.severity}")
    typer.echo(f"   component : {verdict.component}")
    typer.echo(f"   labels    : {', '.join(verdict.suggested_labels) or '(none)'}")
    typer.echo(f"   assignee  : {verdict.assignee or '(none suggested)'}")
    typer.echo(f"   route     : {verdict.route}")
    typer.echo(f"   confidence: {verdict.confidence:.2f}")
    typer.echo(f"   rationale : {verdict.rationale}")
    if verdict.evidence:
        typer.echo(f"   evidence  : {'; '.join(verdict.evidence[:3])}")
    if verdict.duplicate_of:
        typer.echo(f"   duplicate : #{verdict.duplicate_of}")
    dropped = result["classify_meta"].get("dropped_labels") or []
    if dropped:
        typer.echo(f"   [dropped invented labels: {', '.join(dropped)}]")
    typer.echo(f"   [classify: {result['classify_meta']['channel']}, "
               f"{result['classify_meta']['latency_ms']} ms; "
               f"draft: {result['draft_meta']['channel']}, "
               f"{result['draft_meta']['latency_ms']} ms; "
               f"total {result['total_ms']} ms]")
    typer.echo("\n-- draft reply (NOT sent) --")
    typer.echo(draft.draft)


@app.command()
def triage(
    repo: Optional[str] = typer.Option(None, "--repo", help="owner/name"),
    issue: Optional[int] = typer.Option(None, "--issue", help="issue number"),
    issue_url: Optional[str] = typer.Option(None, "--issue-url", help="full issue URL"),
    limit: Optional[int] = typer.Option(None, "--limit", help="batch: triage N open issues"),
    fixture: Optional[str] = typer.Option(None, "--fixture", help="local raw issue JSON file"),
    preset: Optional[str] = typer.Option(None, "--preset", help="provider preset"),
) -> None:
    """Deep-triage one issue (or, with --limit, N open issues as a batch)."""
    if issue_url:
        repo, issue = parse_issue_url(issue_url)
    if fixture:
        if not repo:
            typer.echo("error: --fixture needs --repo owner/name", err=True)
            raise typer.Exit(2)
        raw = json.loads(Path(fixture).read_text(encoding="utf-8"))
        issue = raw.get("number")
    elif limit is None and (not repo or issue is None):
        typer.echo("error: need --repo owner/name --issue N, --issue-url, or --limit", err=True)
        raise typer.Exit(2)
    if repo:
        parse_repo(repo)

    llm, gh, memory = _setup(preset)
    engine = TriageEngine(llm=llm, github=gh, memory=memory)
    store = SnapshotStore()
    try:
        if limit is not None:
            if not repo:
                typer.echo("error: --limit needs --repo owner/name", err=True)
                raise typer.Exit(2)
            issues = [i for i in gh.list_issues(repo, state="open", per_page=limit)
                      if "pull_request" not in i][:limit]
            typer.echo(f"batch: {len(issues)} open issues in {repo} "
                       f"(~1 model call each + draft, paced >=45 s apart)")
            repo_cache = engine.fetch_repo_cache(repo)
            for item in issues:
                ctx = engine.fetch_context(repo, item["number"], repo_cache=repo_cache)
                result = engine.run(ctx)
                _print_verdict(result)
                snap = _save_snapshot(store, result)
                typer.echo(f"   [snapshot: {snap.id} (pending)]")
            return
        assert repo is not None and issue is not None
        if fixture:
            ctx = TriageEngine.context_from_fixture(repo, raw)
        else:
            ctx = engine.fetch_context(repo, issue)
        result = engine.run(ctx)
        _print_verdict(result)
        snap = _save_snapshot(store, result)
        typer.echo(f"\n[snapshot saved: {snap.id} — pending approval]")
        typer.echo(f"approve: issue-triage approve --snapshot {snap.id} --yes")
        typer.echo(f"reject : issue-triage approve --snapshot {snap.id} --no")
    finally:
        llm.close()
        gh.close()


def _save_snapshot(store: SnapshotStore, result: dict) -> Snapshot:
    snap = Snapshot(
        id=snapshot_id_for(result["repo"], result["issue_number"]),
        repo=result["repo"],
        issue_number=result["issue_number"],
        issue_title=result["title"],
        verdict=result["verdict"],
        draft=result["draft"],
    )
    store.save(snap)
    return snap


@app.command()
def approve(
    snapshot: str = typer.Option(..., "--snapshot", help="snapshot id or file path"),
    yes: bool = typer.Option(False, "--yes", help="approve and execute once"),
    no: bool = typer.Option(False, "--no", help="reject and block"),
    note: str = typer.Option("", "--note", help="note for the decision"),
) -> None:
    """Approve (execute once) or reject a pending triage snapshot."""
    if yes == no:
        typer.echo("error: pass exactly one of --yes / --no", err=True)
        raise typer.Exit(2)
    fix_proxy_env()
    load_dotenv()
    store = SnapshotStore()
    memory = RepoMemory()
    if no:
        snap = store.reject(snapshot, note=note)
        typer.echo(f"rejected snapshot {snap.id} (issue #{snap.issue_number}); nothing was sent.")
        return
    # --yes: approve, then execute exactly once
    try:
        snap = store.approve_once(snapshot)
    except NotPendingError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    token = github_token()
    if not token:
        typer.echo("No GITHUB_TOKEN: nothing was executed. Run these yourself:")
        if snap.verdict.suggested_labels:
            typer.echo(
                f"  gh api repos/{snap.repo}/issues/{snap.issue_number}/labels "
                f"-f 'labels[]={snap.verdict.suggested_labels[0]}'"
                + "".join(f" -f 'labels[]={l}'" for l in snap.verdict.suggested_labels[1:])
            )
        typer.echo(
            f"  gh api repos/{snap.repo}/issues/{snap.issue_number}/comments "
            f"-f body=@- <<'EOF'\n{snap.draft.draft}\nEOF"
        )
        typer.echo(f"\nSnapshot {snap.id} stays 'approved' (not consumed) — re-run with a "
                   "token to execute, or reject it with --no.")
        return
    gh = GitHubClient(token=token)
    try:
        if snap.verdict.suggested_labels:
            applied = gh.apply_labels(snap.repo, snap.issue_number, snap.verdict.suggested_labels)
            typer.echo(f"labels applied: {', '.join(applied)}")
        comment = gh.post_comment(snap.repo, snap.issue_number, snap.draft.draft)
        typer.echo(f"comment posted: {comment.get('html_url', '')}")
        store.consume(snap.id)
        memory.mark_decided(snap.repo, snap.issue_number)
        typer.echo(f"snapshot {snap.id} consumed (single use — a second approve is refused).")
    except AlreadyConsumedError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    finally:
        gh.close()


@app.command()
def probe(
    probe_name: Optional[str] = typer.Option(None, "--probe", help="run one probe only"),
    offline: bool = typer.Option(False, "--offline", help="skip probes needing model calls"),
    preset: Optional[str] = typer.Option(None, "--preset", help="provider preset"),
    fixture: Optional[str] = typer.Option(
        None, "--fixture", help="raw issue JSON for the subagent probe"
    ),
) -> None:
    """Run the building-block probe suite; regenerates docs/BUILDING_BLOCKS_TESTED.md."""
    fix_proxy_env()
    load_dotenv()
    provider = resolve_provider(preset)
    fixture_issue = None
    if fixture:
        fixture_issue = json.loads(Path(fixture).read_text(encoding="utf-8"))
    selected = [probe_name] if probe_name else None
    if selected and selected[0] not in PROBE_NAMES:
        typer.echo(f"unknown probe {probe_name!r}; choose from: {', '.join(PROBE_NAMES)}", err=True)
        raise typer.Exit(2)
    typer.echo(f"probes: {', '.join(selected or PROBE_NAMES)} "
               f"(preset={provider.preset}, model={provider.model}, offline={offline})")
    results = run_probes(selected=selected, offline=offline, provider=provider,
                         fixture_issue=fixture_issue)
    for r in results:
        typer.echo(f"[{r.status.upper():7}] {r.name} ({r.duration_ms} ms)\n"
                   f"         {r.evidence}")
    path = write_building_blocks_md(results, provider)
    typer.echo(f"\nwrote {path}")
    if any(r.status == "fail" for r in results):
        raise typer.Exit(1)


@app.command(name="eval")
def eval_cmd(
    repo: str = typer.Option("openminion/openminion", "--repo", help="owner/name"),
    limit: int = typer.Option(10, "--limit", help="issues to evaluate"),
    preset: Optional[str] = typer.Option(None, "--preset", help="provider preset"),
) -> None:
    """Evaluate the classifier on N real labelled issues (ground truth =
    maintainer labels). One model call per issue, paced >=45 s."""
    parse_repo(repo)
    llm, gh, memory = _setup(preset)
    engine = TriageEngine(llm=llm, github=gh, memory=memory)
    try:
        typer.echo(f"eval: {repo}, limit={limit} "
                   f"(~{limit} model calls, paced >=45 s apart — allow ~{limit} min)")
        result = run_eval(engine, repo, limit)
        path = save_eval_result(result)
        typer.echo(json.dumps(result["metrics"], indent=2))
        typer.echo(f"\nskipped (unlabelled/ambiguous): "
                   f"{result['skipped_unlabelled_or_ambiguous']}")
        if result.get("errors"):
            typer.echo(f"errors (issue failed, counted separately): {len(result['errors'])}")
            for err in result["errors"]:
                typer.echo(f"  #{err['issue_number']}: {err['error']}")
        typer.echo(f"saved {path}")
    finally:
        llm.close()
        gh.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
