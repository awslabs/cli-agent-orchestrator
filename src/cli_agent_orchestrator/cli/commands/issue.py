"""`cao project` and `cao issue` — the issue tracker's command-line surface.

These call the service directly rather than the REST API, matching `cao memory`
and for the same reason: filing an issue is most valuable exactly when
something is broken, and that is the moment a running cao-server is least
safe to assume. (`conduct issue` takes the HTTP path instead — it runs from a
different virtualenv and cannot import this package.)

Every command takes `--json` so an agent gets a parseable answer and a human
gets a readable one from the same code path.
"""

from __future__ import annotations

import json as jsonlib
import os
import sys
from typing import Any, Dict, List, Optional

import click

from cli_agent_orchestrator.clients.database import ensure_tracker_schema
from cli_agent_orchestrator.services import issue_tracker as tracker
from cli_agent_orchestrator.services.issue_tracker import TrackerError


def _fail(exc: TrackerError) -> None:
    """Report a refusal on stderr and exit non-zero, keeping its classification."""
    click.echo(f"error [{exc.code}]: {exc.message}", err=True)
    sys.exit(1)


def _emit(payload: Any, as_json: bool, renderer=None) -> None:
    if as_json or renderer is None:
        click.echo(jsonlib.dumps(payload, indent=2, sort_keys=True))
    else:
        renderer(payload)


def _issue_line(issue: Dict[str, Any]) -> str:
    severity = issue["severity"] if issue["severity"] != "unset" else "--"
    return (
        f"{issue['key']:<12} {severity:<5} {issue['status']:<12} "
        f"{(issue['component'] or '-'):<12} {issue['title']}"
    )


# --------------------------------------------------------------------------
# cao project
# --------------------------------------------------------------------------


@click.group()
def project():
    """Manage tracker projects (a name, its scopes, and its issue log)."""
    # No server means no lifespan, so the schema is this process's job.
    ensure_tracker_schema()


@project.command(name="list")
@click.option("--all", "include_archived", is_flag=True, help="include archived projects")
@click.option("--json", "as_json", is_flag=True)
def project_list(include_archived: bool, as_json: bool):
    """List projects."""
    rows = tracker.list_projects(include_archived=include_archived)

    def render(rows):
        if not rows:
            click.echo("no projects")
            return
        for row in rows:
            counts = row.get("counts", {})
            click.echo(
                f"{row['id']:<20} {row['status']:<9} "
                f"{counts.get('open', 0):>4} open / {counts.get('total', 0):>4} total   {row['name']}"
            )

    _emit(rows, as_json, render)


@project.command(name="create")
@click.argument("name")
@click.option("--id", "project_id", default=None, help="explicit slug (default: derived from NAME)")
@click.option("--prefix", default=None, help="issue key prefix, e.g. 'cond' for cond-0042")
@click.option("--description", default="")
@click.option("--path", "paths", multiple=True, help="absolute directory this project covers")
@click.option("--session", "sessions", multiple=True, help="tmux session name this project covers")
@click.option("--git-remote", "remotes", multiple=True, help="git remote this project covers")
@click.option("--json", "as_json", is_flag=True)
def project_create(name, project_id, prefix, description, paths, sessions, remotes, as_json):
    """Create a project spanning any number of paths, sessions and remotes."""
    scopes = (
        [{"kind": "path", "value": p} for p in paths]
        + [{"kind": "session", "value": s} for s in sessions]
        + [{"kind": "git_remote", "value": r} for r in remotes]
    )
    try:
        row = tracker.create_project(
            name=name,
            project_id=project_id,
            description=description,
            issue_prefix=prefix,
            scopes=scopes,
        )
    except TrackerError as exc:
        _fail(exc)
    _emit(row, as_json, lambda r: click.echo(f"created {r['id']} ({r['issue_prefix']}-NNNN)"))


@project.command(name="show")
@click.argument("project_id")
@click.option("--json", "as_json", is_flag=True)
def project_show(project_id, as_json):
    """Show a project with its scopes and issue counts."""
    try:
        row = tracker.get_project(project_id)
    except TrackerError as exc:
        _fail(exc)

    def render(row):
        click.echo(f"{row['id']}  {row['name']}  [{row['status']}]")
        if row["description"]:
            click.echo(f"  {row['description']}")
        click.echo(f"  keys: {row['issue_prefix']}-NNNN (next {row['next_issue_number']})")
        counts = row["counts"]
        click.echo(f"  issues: {counts['open']} open / {counts['total']} total")
        for status_name, count in sorted(counts.get("by_status", {}).items()):
            click.echo(f"    {status_name:<12} {count}")
        click.echo("  scopes:")
        for scope in row["scopes"]:
            click.echo(f"    [{scope['id']:>3}] {scope['kind']:<11} {scope['value']}")

    _emit(row, as_json, render)


@project.command(name="update")
@click.argument("project_id")
@click.option("--name", default=None)
@click.option("--description", default=None)
@click.option("--status", type=click.Choice(tracker.PROJECT_STATUSES), default=None)
@click.option("--prefix", default=None)
@click.option("--json", "as_json", is_flag=True)
def project_update(project_id, name, description, status, prefix, as_json):
    """Rename, re-describe, archive or re-prefix a project."""
    try:
        row = tracker.update_project(
            project_id, name=name, description=description, status=status, issue_prefix=prefix
        )
    except TrackerError as exc:
        _fail(exc)
    _emit(row, as_json, lambda r: click.echo(f"updated {r['id']}"))


@project.command(name="delete")
@click.argument("project_id")
@click.option("--force", is_flag=True, help="also delete the project's issues (irreversible)")
@click.option("--json", "as_json", is_flag=True)
def project_delete(project_id, force, as_json):
    """Delete a project. Refuses while it holds issues unless --force."""
    try:
        row = tracker.delete_project(project_id, force=force)
    except TrackerError as exc:
        _fail(exc)
    _emit(row, as_json, lambda r: click.echo(f"deleted {r['id']} ({r['issues_deleted']} issue(s))"))


@project.command(name="resolve")
@click.option("--cwd", default=None, help="directory to resolve (default: this one)")
@click.option("--session", default=None)
@click.option("--alias", default=None)
@click.option("--project", "project_id", default=None)
@click.option("--json", "as_json", is_flag=True)
def project_resolve(cwd, session, alias, project_id, as_json):
    """Answer which project an issue filed here would belong to."""
    try:
        got = tracker.resolve_project(
            project=project_id, session=session, alias=alias, cwd=cwd or os.getcwd()
        ).as_dict()
    except TrackerError as exc:
        _fail(exc)

    def render(got):
        if got["project_id"] is None:
            click.echo("no project registered for this filing site")
        else:
            click.echo(
                f"{got['project_id']}  (matched by {got['matched_by']}: {got['matched_value']})"
            )

    _emit(got, as_json, render)


@project.command(name="export")
@click.argument("project_id")
@click.option("--all", "include_closed", is_flag=True, help="include closed issues")
@click.option("-o", "--output", type=click.Path(), default=None, help="write to a file")
def project_export(project_id, include_closed, output):
    """Render the issue log as markdown."""
    try:
        text = tracker.render_markdown(project_id, open_only=not include_closed)
    except TrackerError as exc:
        _fail(exc)
    if output:
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(text)
        click.echo(f"wrote {output}")
    else:
        click.echo(text)


@project.group(name="scope")
def project_scope():
    """Manage the identifiers that resolve to a project."""


@project_scope.command(name="add")
@click.argument("project_id")
@click.option("--kind", type=click.Choice(tracker.SCOPE_KINDS), required=True)
@click.option("--value", required=True)
@click.option("--json", "as_json", is_flag=True)
def scope_add(project_id, kind, value, as_json):
    """Register one identifier as resolving to this project."""
    try:
        row = tracker.add_scope(project_id, kind=kind, value=value)
    except TrackerError as exc:
        _fail(exc)
    _emit(
        row,
        as_json,
        lambda r: click.echo(
            f"{'added' if r['created'] else 'already present'}: [{r['id']}] {r['kind']} {r['value']}"
        ),
    )


@project_scope.command(name="rm")
@click.argument("project_id")
@click.argument("scope_id", type=int)
@click.option("--json", "as_json", is_flag=True)
def scope_rm(project_id, scope_id, as_json):
    """Drop one scope."""
    try:
        row = tracker.remove_scope(project_id, scope_id)
    except TrackerError as exc:
        _fail(exc)
    _emit(row, as_json, lambda r: click.echo(f"removed scope {r['id']}"))


# --------------------------------------------------------------------------
# cao issue
# --------------------------------------------------------------------------


@click.group()
def issue():
    """File, search and edit issues."""
    ensure_tracker_schema()


@issue.command(name="file")
@click.option("--title", required=True)
@click.option("--body", default=None)
@click.option("--body-file", type=click.Path(exists=True), default=None)
@click.option("--project", "project_id", default=None, help="explicit project (skips resolution)")
@click.option("--cwd", default=None, help="filing site (default: this directory)")
@click.option("--session", "session_name", default=None)
@click.option(
    "--alias", default=None, help="a project_id-kind scope value, e.g. a conductor campaign name"
)
@click.option("--severity", type=click.Choice(tracker.SEVERITIES), default="unset")
@click.option("--status", type=click.Choice(tracker.STATUSES), default="open")
@click.option("--component", default=None)
@click.option("--reporter", default=None)
@click.option("--assignee", default=None)
@click.option("--label", "labels", multiple=True)
@click.option("--command", "failing_command", default=None, help="the failing command")
@click.option("--evidence", default=None, help="absolute path to a log or run dir")
@click.option("--key", default=None, help="explicit issue key (migration only)")
@click.option("--json", "as_json", is_flag=True)
def issue_file(
    title,
    body,
    body_file,
    project_id,
    cwd,
    session_name,
    alias,
    severity,
    status,
    component,
    reporter,
    assignee,
    labels,
    failing_command,
    evidence,
    key,
    as_json,
):
    """File an issue against a project."""
    if body_file:
        with open(body_file, "r", encoding="utf-8") as handle:
            body = handle.read()
    try:
        row = tracker.create_issue(
            project_id=project_id,
            title=title,
            body=body or "",
            status=status,
            severity=severity,
            component=component,
            reporter=reporter,
            assignee=assignee,
            labels=list(labels),
            failing_command=failing_command,
            evidence=evidence,
            session_name=session_name,
            source_path=cwd or os.getcwd(),
            cwd=cwd or os.getcwd(),
            alias=alias,
            key=key,
            origin="cli",
        )
    except TrackerError as exc:
        _fail(exc)
    _emit(row, as_json, lambda r: click.echo(f"{r['key']}  filed against {r['project_id']}"))


@issue.command(name="list")
@click.option("--project", "project_id", default=None)
@click.option("--status", "statuses", multiple=True, type=click.Choice(tracker.STATUSES))
@click.option("--severity", "severities", multiple=True, type=click.Choice(tracker.SEVERITIES))
@click.option("--component", default=None)
@click.option("--assignee", default=None)
@click.option("--reporter", default=None)
@click.option("--label", default=None)
@click.option("-q", "--query", default=None, help="search title, body, key and failing command")
@click.option("--open", "open_only", is_flag=True, help="exclude closed/wontfix/duplicate")
@click.option("--limit", default=100, type=int)
@click.option("--offset", default=0, type=int)
@click.option(
    "--order",
    type=click.Choice(["created_desc", "created_asc", "updated_desc", "severity", "key"]),
    default="created_desc",
)
@click.option("--json", "as_json", is_flag=True)
def issue_list(
    project_id,
    statuses,
    severities,
    component,
    assignee,
    reporter,
    label,
    query,
    open_only,
    limit,
    offset,
    order,
    as_json,
):
    """List issues."""
    try:
        page = tracker.list_issues(
            project_id=project_id,
            status=list(statuses) or None,
            severity=list(severities) or None,
            component=component,
            assignee=assignee,
            reporter=reporter,
            label=label,
            query=query,
            open_only=open_only,
            limit=limit,
            offset=offset,
            order=order,
        )
    except TrackerError as exc:
        _fail(exc)

    def render(page):
        for row in page["issues"]:
            click.echo(_issue_line(row))
        shown = len(page["issues"])
        if shown < page["total"]:
            click.echo(
                f"-- showing {page['offset'] + 1}-{page['offset'] + shown} of {page['total']}"
            )
        else:
            click.echo(f"-- {page['total']} issue(s)")

    _emit(page, as_json, render)


@issue.command(name="show")
@click.argument("issue_key")
@click.option("--json", "as_json", is_flag=True)
def issue_show(issue_key, as_json):
    """Show one issue with its comments, links and audit trail."""
    try:
        row = tracker.get_issue(issue_key)
    except TrackerError as exc:
        _fail(exc)

    def render(row):
        severity = f"[{row['severity']}] " if row["severity"] != "unset" else ""
        click.echo(f"{row['key']} — {severity}{row['title']}")
        click.echo(f"  project:   {row['project_id']}")
        click.echo(f"  status:    {row['status']}")
        for field in (
            "component",
            "reporter",
            "assignee",
            "failing_command",
            "evidence",
            "resolution",
            "duplicate_of",
        ):
            if row.get(field):
                click.echo(f"  {field + ':':<12}{row[field]}")
        if row["labels"]:
            click.echo(f"  labels:    {', '.join(row['labels'])}")
        click.echo(f"  filed:     {row['created_at']}")
        if row["closed_at"]:
            click.echo(f"  closed:    {row['closed_at']}")
        if row["body"]:
            click.echo("")
            click.echo(row["body"].rstrip())
        for link in row["links"]:
            other = link["to_key"] if link["from_key"] == row["key"] else link["from_key"]
            click.echo(f"  link: {link['kind']} {other}")
        for comment in row["comments"]:
            click.echo("")
            click.echo(f"  --- {comment['author'] or 'unknown'} at {comment['created_at']}")
            click.echo(f"  {comment['body']}")

    _emit(row, as_json, render)


@issue.command(name="edit")
@click.argument("issue_key")
@click.option("--title", default=None)
@click.option("--body", default=None)
@click.option("--body-file", type=click.Path(exists=True), default=None)
@click.option("--status", type=click.Choice(tracker.STATUSES), default=None)
@click.option("--severity", type=click.Choice(tracker.SEVERITIES), default=None)
@click.option("--component", default=None)
@click.option("--assignee", default=None)
@click.option("--reporter", default=None)
@click.option("--label", "labels", multiple=True, help="replaces the whole label set")
@click.option("--command", "failing_command", default=None)
@click.option("--evidence", default=None)
@click.option("--resolution", default=None)
@click.option("--duplicate-of", default=None)
@click.option(
    "--actor", default=None, help="who is making this change (recorded in the audit trail)"
)
@click.option("--json", "as_json", is_flag=True)
def issue_edit(issue_key, body_file, labels, actor, as_json, **fields):
    """Change one or more fields. Only the options you pass are applied."""
    changes = {name: value for name, value in fields.items() if value is not None}
    if body_file:
        with open(body_file, "r", encoding="utf-8") as handle:
            changes["body"] = handle.read()
    if labels:
        changes["labels"] = list(labels)
    if not changes:
        click.echo("nothing to change", err=True)
        sys.exit(1)
    try:
        row = tracker.update_issue(issue_key, actor=actor, **changes)
    except TrackerError as exc:
        _fail(exc)
    _emit(row, as_json, lambda r: click.echo(f"{r['key']}  {r['status']}  {r['title']}"))


@issue.command(name="close")
@click.argument("issue_key")
@click.option("--resolution", default=None, help="how it was resolved")
@click.option(
    "--as",
    "final_status",
    type=click.Choice(["closed", "wontfix", "duplicate", "resolved"]),
    default="closed",
)
@click.option("--actor", default=None)
@click.option("--json", "as_json", is_flag=True)
def issue_close(issue_key, resolution, final_status, actor, as_json):
    """Close an issue."""
    changes: Dict[str, Any] = {"status": final_status}
    if resolution:
        changes["resolution"] = resolution
    try:
        row = tracker.update_issue(issue_key, actor=actor, **changes)
    except TrackerError as exc:
        _fail(exc)
    _emit(row, as_json, lambda r: click.echo(f"{r['key']} -> {r['status']}"))


@issue.command(name="comment")
@click.argument("issue_key")
@click.option("--body", default=None)
@click.option("--body-file", type=click.Path(exists=True), default=None)
@click.option("--author", default=None)
@click.option("--json", "as_json", is_flag=True)
def issue_comment(issue_key, body, body_file, author, as_json):
    """Add a comment."""
    if body_file:
        with open(body_file, "r", encoding="utf-8") as handle:
            body = handle.read()
    if not body:
        click.echo("a comment needs --body or --body-file", err=True)
        sys.exit(1)
    try:
        row = tracker.add_comment(issue_key, body=body, author=author)
    except TrackerError as exc:
        _fail(exc)
    _emit(row, as_json, lambda r: click.echo(f"comment {r['id']} on {r['issue_key']}"))


@issue.command(name="link")
@click.argument("issue_key")
@click.option("--to", "to_key", required=True)
@click.option("--kind", type=click.Choice(tracker.LINK_KINDS), default="relates")
@click.option("--actor", default=None)
@click.option("--json", "as_json", is_flag=True)
def issue_link(issue_key, to_key, kind, actor, as_json):
    """Relate two issues."""
    try:
        row = tracker.add_link(issue_key, to_key=to_key, kind=kind, actor=actor)
    except TrackerError as exc:
        _fail(exc)
    _emit(row, as_json, lambda r: click.echo(f"{r['from_key']} {r['kind']} {r['to_key']}"))


@issue.command(name="rm")
@click.argument("issue_key")
@click.option("--yes", is_flag=True, help="skip the confirmation prompt")
@click.option("--json", "as_json", is_flag=True)
def issue_rm(issue_key, yes, as_json):
    """Delete an issue and everything attached to it."""
    if not yes:
        click.confirm(f"delete {issue_key} and all its comments, events and links?", abort=True)
    try:
        row = tracker.delete_issue(issue_key)
    except TrackerError as exc:
        _fail(exc)
    _emit(row, as_json, lambda r: click.echo(f"deleted {r['key']}"))


@issue.command(name="stats")
@click.option("--project", "project_id", default=None)
@click.option("--json", "as_json", is_flag=True)
def issue_stats(project_id, as_json):
    """Aggregate counts for a project, or the whole install."""
    try:
        row = tracker.stats(project_id)
    except TrackerError as exc:
        _fail(exc)

    def render(row):
        click.echo(f"{row['open']} open / {row['total']} total")
        for heading, key in (
            ("status", "by_status"),
            ("severity", "by_severity"),
            ("component", "by_component"),
        ):
            click.echo(f"  by {heading}:")
            for name, count in sorted(row[key].items(), key=lambda kv: (-kv[1], kv[0])):
                click.echo(f"    {name:<14} {count}")

    _emit(row, as_json, render)


@issue.command(name="import-ledger")
@click.argument("ledger", type=click.Path(exists=True))
@click.option("--project", "project_id", required=True)
@click.option(
    "--default-status",
    type=click.Choice(tracker.STATUSES),
    default="open",
    help="status for entries whose ledger text does not state one",
)
@click.option("--component", default=None, help="component to stamp on every imported entry")
@click.option("--dry-run", is_flag=True, help="parse and report without writing")
@click.option("--json", "as_json", is_flag=True)
def issue_import_ledger(ledger, project_id, default_status, component, dry_run, as_json):
    """Import a markdown issue ledger, preserving its ids and filing dates."""
    from cli_agent_orchestrator.services import issue_ledger_import

    try:
        report = issue_ledger_import.import_ledger(
            ledger,
            project_id=project_id,
            default_status=default_status,
            component=component,
            dry_run=dry_run,
        )
    except TrackerError as exc:
        _fail(exc)

    def render(report):
        click.echo(
            f"{'would import' if dry_run else 'imported'} {report['imported']} of "
            f"{report['parsed']} parsed entr(ies); {report['skipped']} skipped"
        )
        for note in report["notes"]:
            click.echo(f"  {note}")

    _emit(report, as_json, render)
