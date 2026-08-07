"""`cao attachment` — see, sweep, and adjudicate provider-session claims.

A native session claim is exclusive: while one is held, no relaunch can
attach to that provider session. Until this command existed there was no
way to see one, and the store has no enumerator, so a claim that outlived
its owner was invisible and permanent at the same time.

These call the service directly rather than the REST API, matching
`cao issue` and `cao memory` and for the same reason: the claims most
worth resolving are the ones a dead or wedged server left behind, and
that is exactly when a running cao-server is least safe to assume.

No group callback touches the schema. The attachment table is created by
`init_db`'s migration ladder, and a callback that ran DDL here would run
it against the operator's real database before any test fixture could
intercept it.
"""

from __future__ import annotations

import hashlib
import json as jsonlib
import sys
from typing import Any

import click

from cli_agent_orchestrator.services import native_attachment as na
from cli_agent_orchestrator.services import native_attachment_recovery as recovery


def _fail(exc: na.NativeAttachmentError) -> None:
    """Report a refusal on stderr and exit non-zero, keeping its classification.

    Only the first line. A store failure wraps the driver's exception, and
    SQLAlchemy renders that as a readable sentence followed by the entire
    generated SQL — fourteen column aliases of it. The sentence is the part
    an operator can act on; burying it under the query is how a legible
    refusal becomes an unreadable one.
    """
    click.echo(f"error [{exc.code}]: {str(exc).splitlines()[0]}", err=True)
    sys.exit(1)


def _emit(payload: Any, as_json: bool, renderer=None) -> None:
    if as_json or renderer is None:
        click.echo(jsonlib.dumps(payload, indent=2, sort_keys=True))
    else:
        renderer(payload)


def _identity(record: dict) -> str:
    identity = record["owner"].get("process_identity") or {}
    pid = identity.get("pid")
    return f"pid {pid}" if pid else "no published pid"


def _row_line(record: dict) -> str:
    owner = record["owner"]
    return (
        f"{record['state']:<10} {record['provider']:<12} "
        f"{record['native_session_id']:<46} {owner['terminal_id']:<10} {_identity(record)}"
    )


@click.group()
def attachment():
    """Inspect and resolve provider-native session claims."""


@attachment.command(name="list")
@click.option(
    "--state",
    "states",
    multiple=True,
    type=click.Choice(sorted(na.ATTACHMENT_STATES)),
    help="Repeatable. Default: every state that still holds a session.",
)
@click.option("--provider", "providers", multiple=True, help="Repeatable.")
@click.option("--terminal", "owner_terminal_id", default=None, help="Owning terminal id.")
@click.option("--json", "as_json", is_flag=True)
def attachment_list(states, providers, owner_terminal_id, as_json):
    """List session claims.

    Defaults to the states that still hold a session, because a detached
    row is history and the question this command answers is what is
    currently blocked.
    """
    selected = frozenset(states) if states else na.LIVE_STATES | {na.AMBIGUOUS}
    try:
        records = na.list_attachments(
            states=selected,
            providers=frozenset(providers) if providers else None,
            owner_terminal_id=owner_terminal_id,
        )
    except na.NativeAttachmentError as exc:
        _fail(exc)
        return

    def render(rows):
        if not rows:
            click.echo("no attachments match")
            return
        for record in rows:
            click.echo(_row_line(record))
        click.echo(f"\n{len(rows)} attachment(s)")

    _emit(records, as_json, render)


@attachment.command(name="show")
@click.argument("provider")
@click.argument("native_session_id")
@click.option("--observe/--no-observe", default=True, help="Look for the owning process.")
@click.option("--json", "as_json", is_flag=True)
def attachment_show(provider, native_session_id, observe, as_json):
    """Show one claim, and by default look for the process that holds it.

    The observation is what an operator needs before adjudicating, and it
    is the part they cannot do by reading the row: the row records who
    took the claim, not whether that owner is still there.
    """
    try:
        record = na.get(provider, native_session_id)
    except na.NativeAttachmentError as exc:
        _fail(exc)
        return
    if record is None:
        click.echo(
            f"error [native-attachment-not-found]: no claim on {provider} "
            f"session {native_session_id}",
            err=True,
        )
        sys.exit(1)
    payload: dict[str, Any] = dict(record)
    if observe:
        payload["observation"] = recovery.observe_owner(record)

    def render(row):
        owner = row["owner"]
        click.echo(f"provider          {row['provider']}")
        click.echo(f"session           {row['native_session_id']}")
        click.echo(f"state             {row['state']}")
        click.echo(f"epoch             {row['epoch']}")
        click.echo(f"owner terminal    {owner['terminal_id']}")
        click.echo(f"owner generation  {owner['generation']}")
        click.echo(f"execution mode    {owner['execution_mode']}")
        click.echo(f"pane              {owner.get('pane_id') or '-'}")
        click.echo(f"process identity  {owner.get('process_identity') or '-'}")
        if row.get("ambiguity_reason"):
            click.echo(f"frozen because    {row['ambiguity_reason']}")
        if row.get("release_proof"):
            # A re-acquired row keeps the *previous* incarnation's proof —
            # deliberately, as evidence — so on a live claim this line
            # describes a different owner. Printing it unlabelled reads as
            # "this claim was released", which is the opposite of true.
            label = "released by" if row["state"] == na.DETACHED else "previously released by"
            click.echo(f"{label:<17} {row['release_proof'].get('schema')}")
        observation = row.get("observation")
        if observation:
            click.echo(f"\nowner is          {observation['disposition']}")
            click.echo(f"                  {observation['detail']}")

    _emit(payload, as_json, render)


@attachment.command(name="sweep")
@click.option(
    "--apply",
    "do_apply",
    is_flag=True,
    default=False,
    help="Release the claims whose owner is gone. The default is a pure-read dry run.",
)
@click.option("--json", "as_json", is_flag=True)
def attachment_sweep(do_apply, as_json):
    """Release every claim whose owning process is provably gone.

    Only an absent pid releases anything. A live owner, an owner that
    could not be checked, and a claim that never published a process
    identity are all held — the sweep reports each with its reason rather
    than guessing.
    """
    try:
        report = recovery.sweep(apply=do_apply)
    except na.NativeAttachmentError as exc:
        _fail(exc)
        return

    def render(result):
        mode = "apply" if result["applied"] else "dry-run"
        click.echo(f"mode={mode} examined={result['examined']}")
        for key in sorted(result["counts"]):
            click.echo(f"  {key:<24} {result['counts'][key]}")
        held = [o for o in result["outcomes"] if o["action"] not in ("released", "would-release")]
        if held:
            click.echo("\nheld:")
            for outcome in held:
                click.echo(
                    f"  {outcome['provider']:<12} {outcome['native_session_id']:<46} "
                    f"{outcome['reason']}"
                )

    _emit(report, as_json, render)


@attachment.command(name="freeze")
@click.argument("provider")
@click.argument("native_session_id")
@click.option("--operator", required=True, help="Who is making this call. Recorded permanently.")
@click.option("--detail", required=True, help="Why the owner cannot be determined.")
@click.option(
    "--pid-is-recycled",
    "attest_recycled",
    is_flag=True,
    help=(
        "Attest that the process now holding the recorded pid is NOT the recorded owner. "
        "Only accepted when the live start marker differs from the recorded one."
    ),
)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--json", "as_json", is_flag=True)
def attachment_freeze(provider, native_session_id, operator, detail, attest_recycled, yes, as_json):
    """Declare a claim's ownership unresolvable, so it can be adjudicated.

    Only needed for a claim the sweep can never settle — an owner the
    process table will not answer for. A provably-gone owner is released
    by the sweep and does not need this; a running one is refused.

    This is deliberately the first of two steps. Freezing says "I cannot
    determine this". Adjudicating says "I have decided anyway".
    """
    try:
        record = na.get(provider, native_session_id)
    except na.NativeAttachmentError as exc:
        _fail(exc)
        return
    if record is None:
        click.echo(
            f"error [native-attachment-not-found]: no claim on {provider} "
            f"session {native_session_id}",
            err=True,
        )
        sys.exit(1)

    if not yes:
        # Show the observation before asking. A cold-starting launch and a
        # stuck orphan render as the same line in `list`, and this is the
        # one place the difference is visible.
        observation = recovery.observe_owner(record)
        click.echo(f"{provider} session {native_session_id}")
        click.echo(f"  state          {record['state']}")
        click.echo(f"  owner is       {observation['disposition']} — {observation['detail']}")
        click.confirm(
            f"\nFreeze this claim as unresolvable, on {operator}'s name?",
            abort=True,
        )
    try:
        result = recovery.freeze_for_adjudication(
            provider=provider,
            native_session_id=native_session_id,
            operator=operator,
            detail=detail,
            attest_live_pid_is_not_the_owner=attest_recycled,
        )
    except na.NativeAttachmentError as exc:
        _fail(exc)
        return

    def render(row):
        click.echo(f"{row['provider']} session {row['native_session_id']} is now {row['state']}")
        click.echo(f"frozen because    {row['ambiguity_reason']}")

    _emit(result, as_json, render)


@attachment.command(name="adjudicate")
@click.argument("provider")
@click.argument("native_session_id")
@click.option("--operator", required=True, help="Who is making this call. Recorded permanently.")
@click.option(
    "--detail",
    required=True,
    help="Why the owner is believed gone. Recorded permanently. Max 500 characters.",
)
@click.option(
    "--evidence",
    "evidence_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="File whose sha256 is recorded as the evidence digest.",
)
@click.option(
    "--evidence-sha256",
    default=None,
    help="The digest itself, when the evidence is not a local file.",
)
@click.option(
    "--pid-is-recycled",
    "attest_recycled",
    is_flag=True,
    help=(
        "Attest that the process still bearing the recorded pid is somebody else. "
        "Required to decide past a live pid, and accepted only when its start marker "
        "was read and genuinely differs from the recorded one."
    ),
)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--json", "as_json", is_flag=True)
def attachment_adjudicate(
    provider,
    native_session_id,
    operator,
    detail,
    evidence_path,
    evidence_sha256,
    attest_recycled,
    yes,
    as_json,
):
    """Resolve a frozen claim, on your name and your evidence.

    A frozen claim is one whose ownership the system could not resolve,
    and it is deliberately terminal for automation. This is the only way
    out, and it is not automation: it records who decided, what they
    looked at, and what the system could still see at that moment.

    An owner that is still observably alive is refused here exactly as it
    is everywhere else.
    """
    if (evidence_path is None) == (evidence_sha256 is None):
        raise click.UsageError("supply exactly one of --evidence or --evidence-sha256")
    if evidence_path is not None:
        with open(evidence_path, "rb") as handle:
            evidence_sha256 = hashlib.sha256(handle.read()).hexdigest()

    try:
        record = na.get(provider, native_session_id)
    except na.NativeAttachmentError as exc:
        _fail(exc)
        return
    if record is None:
        click.echo(
            f"error [native-attachment-not-found]: no claim on {provider} "
            f"session {native_session_id}",
            err=True,
        )
        sys.exit(1)

    if not yes:
        preview = recovery.observe_owner(record)
        click.echo(f"{provider} session {native_session_id}")
        click.echo(f"  state          {record['state']}")
        click.echo(f"  frozen because {record.get('ambiguity_reason') or '-'}")
        click.echo(
            f"  owner          {record['owner']['terminal_id']} "
            f"generation {record['owner']['generation']}"
        )
        click.echo(f"  owner is       {preview['disposition']} — {preview['detail']}")
        click.confirm(
            f"\nRecord {operator} as having judged this owner gone, and release the session?",
            abort=True,
        )
        # Re-read after the prompt. The pause is unbounded, and the record
        # read before it describes a row that may have been released,
        # re-claimed and re-frozen since. The epoch below is what actually
        # refuses that, but observing the row we are about to act on rather
        # than the one we showed is the honest way to build the evidence.
        try:
            record = na.get(provider, native_session_id)
        except na.NativeAttachmentError as exc:
            _fail(exc)
            return
        if record is None:
            click.echo(
                f"error [native-attachment-not-found]: {provider} session "
                f"{native_session_id} disappeared while you were deciding",
                err=True,
            )
            sys.exit(1)
    observation = recovery.observe_owner(record)

    try:
        result = na.adjudicate(
            provider=provider,
            native_session_id=native_session_id,
            record=na.adjudication(
                outcome=na.ADJUDICATION_OUTCOME_OWNER_GONE,
                evidence_sha256=evidence_sha256,
                detail=detail,
                operator=operator,
                observed_at=observation["observed_at"],
                observation=observation,
                attests_live_process_is_not_the_owner=attest_recycled,
            ),
            live_survivors=recovery.survivors_blocking_adjudication(observation),
            observed_epoch=record["epoch"],
        )
    except na.NativeAttachmentError as exc:
        _fail(exc)
        return

    def render(row):
        click.echo(f"{row['provider']} session {row['native_session_id']} is now {row['state']}")
        click.echo(f"adjudicated by {operator}; the owner and the freeze reason are preserved")

    _emit(result, as_json, render)
