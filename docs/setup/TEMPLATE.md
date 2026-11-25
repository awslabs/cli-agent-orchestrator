# Template: an agent-runnable setup guide

Distilled from the two guides that work today:
[Set up ephemeral agents and a decider](../ephemeral-agents-agent-setup.md) and
[Set up a fresh CAO box on EC2](../../examples/remote/ec2/AGENT-SETUP.md). Copy the skeleton
below for each new guide, and add it to the [index](README.md).

## What makes the two guides work

1. **A check ends every step.** Each step has a command and then "What you should see",
   with the exact output. A failure shows up at the step that caused it, not three steps
   later. That's the whole point, and the "Why a runbook" list says so up front.
2. **The silent failures are named first.** Before any step, the guide lists the quiet ways
   setup goes wrong (wrong Python environment, an upgrade that drops a package, a tier gap,
   a stale MCP connection). Every check later exists to catch one of them.
3. **Stop, don't guess.** "Stop at the first failed check." "Never guess a source." "If the
   paths differ, stop and ask." The agent never improvises past a refusal, and the refusal
   messages are quoted so the agent can match them.
4. **Consent points are explicit and named.** The rules list exactly which steps need the
   user's OK (installs into the server's environment, settings writes, restarts, anything
   that costs money). Logins, keys and consent prompts are the user's own steps: the agent
   hands over the exact command and waits. The ephemeral guide even notes the consent
   command refuses a non-interactive terminal, so no agent can drive it.
5. **Probe the build, not the version.** The version number can't tell you what a git
   install contains. The guide runs `--help` on each command it needs and checks for one
   JSON key, and stops if any is missing.
6. **Find the real target before touching it.** The server's Python comes from the process
   listening on the port (its command line only, never its environment). Processes are
   found by port, never by matching command-line text.
7. **Secrets never cross the agent.** No bare `env` or `printenv`, no echoing values, no
   `cat` of a key file, no `cao env list`. Report variable *names* only
   (`env | cut -d= -f1 | sort`). Receipts are printed with credentials in URLs redacted.
8. **Backup first, undo last.** Settings are backed up before the first write; an Undo
   section reverses every change and says which parts are the user's.
9. **Prove it end to end.** The smoke test reads the result in two places (the visible note
   and the stored record), because one can look right while the other shows a fallback.
10. **Plain enough for the user to read along.** Short sentences, one action per step, and
    a symptom-to-fix table at the end.

The EC2 guide adds three more that matter for any guide touching the outside world:
**"check before use"** marks commands the guide can't verify offline (vendor install links,
prices); **one question at a time**, written to an answers file so nothing is asked twice;
**no surprise root**, never piping a download into a shell.

## Shared snippets

Every guide that changes the server reuses these. They live once, in
[Shared setup snippets](snippets.md); link to them instead of copying them:

- [Find the server's Python](snippets.md#find-the-servers-python) and check the `cao` on
  `PATH` is its CLI.
- [Back up `settings.json`](snippets.md#back-up-settingsjson), and restore it.
- [Restart the server the way it was started, and wait for `/health`](snippets.md#restart-the-server-and-wait-for-health).
- [Reconnect your own MCP connection](snippets.md#reconnect-your-mcp-connection) and confirm
  the new tool is listed.
- [Check an add-on survives `cao update`](snippets.md#check-an-add-on-is-in-uvs-receipt): is
  it in uv's receipt?

---

## The skeleton

````markdown
---
feature: <short-id>                     # e.g. mcp-apps; matches the index and the command
title: Set up <feature>
applies_to: <CAO build probes>          # the commands and keys step 2 checks for
user_steps: [<step numbers>]            # steps only the user may do
consent_before: [<step numbers>]        # steps that need the user's OK first
touches: [settings.json, server env, provider config, cloud]   # what it changes
undo: yes
---

# Set up <feature>: a runbook an agent can follow

This page is for the case where you ask your agent to "<the user's words>". It is written so
the agent can follow it step by step, and plain enough for you to read along.

**Why a runbook.** Most of the ways this setup goes wrong are silent:

- <silent failure 1, in one line>;
- <silent failure 2>;
- <silent failure 3>.

Each step below ends with a check, so a failure shows up at that step.

For what <feature> is, see [<feature doc>](<link>). This page only covers getting it set up.

## Before you start

- **<Experimental or security warning, quoted from the feature doc>.** Tell the user and get
  their explicit OK before step <n>.
- **You need a CAO build with <commands>.** Step 2 probes for them; the version number
  can't tell you.
- **Prerequisites:** <accounts, keys, OS packages>. If one is missing, stop and ask.
- **macOS or Linux.** On anything else, stop and ask.

### Rules for the agent

1. **Stop at the first failed check** and report it. Don't guess past a refusal.
2. **Ask for the user's explicit OK before** <installs into the server's environment, writes
   to settings.json, restarts, anything that costs money>: steps <list>.
3. **Logins, keys and consent are the user's.** Never type at their prompts or handle a
   token. Hand them the exact command (steps <list>).
4. **Never print secrets.** No bare `env` or `printenv`, no echoed values, no `cat` of a key
   file, no `cao env list`. Report variable names only.
5. **Use exact names** from this page or from the user, never a short name you heard.
6. **Ask one question at a time**, and don't ask again for something you already have.

> **User step.** Marks a step you hand to the user. Wait for them to say it's done.

**Check before use** marks a command this page can't verify offline (a vendor's install
link, a price). Check it against the vendor's current docs first.

## The steps at a glance

1. Back up.
2. Preflight: find the server, probe the build.
3. <install>.
4. <configure>.
5. <user's step>.
6. Restart and reconnect.
7. Smoke test.
8. Undo, if needed.

## 1. Back up

Follow [Back up `settings.json`](snippets.md#back-up-settingsjson). Keep the backup path for
the undo step.

## 2. Preflight

Follow [Find the server's Python](snippets.md#find-the-servers-python). Keep the `ps` line.

Probe the build for what this guide uses:

```bash
<cmd> --help      # exit 0; the list includes <subcommand>
<cmd> status      # exit 0; JSON with a "<key>" key
```

If any probe fails, this build is too old. Report it and stop.

## 3. <Action>

```bash
<command>
```

**What you should see:** <exact output, or the line that matters>.

- `<exact error text>` means <cause>. <Fix>, then run the check again.
- Anything else: stop and report.

## <n>. <User's step>

> **User step.** Give the user this block to run in their own terminal:
>
> ```bash
> <command>    # what it asks for, and what to type
> ```

**What you may check yourself:** `<read-only command>` (shows state, never the secret).

## <n>. Restart and reconnect

Follow [Restart the server and wait for `/health`](snippets.md#restart-the-server-and-wait-for-health),
then [Reconnect your MCP connection](snippets.md#reconnect-your-mcp-connection).

<what proves the feature loaded: a status key, a listed tool; not a log line that only echoes
the settings>

## <n>. Smoke test

Do one real thing with the feature. Read the result in two places:

1. <the visible result>;
2. <the stored record or log line>.

Both must agree before you call the setup done.

## <n>. Undo

Each of these writes settings or restarts the server, so get the user's OK.

1. <reverse each change, newest first>.
2. Restore the step 1 backup, or remove the file this setup created.

These are **the user's** steps: <revoke consent, delete keys, uninstall>.

## Symptom → fix

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| <what the user or agent sees> | <cause> | <fix, naming the step> |

## FAQ

**Can the agent do the whole thing?** Everything except <the user's steps>, and the OKs.

**What leaves this machine?** <nothing, or exactly what and where>.

## Report to the user

When done, tell the user in plain words: what was set up, every check that passed, every
user step they did, any warning they must remember (for example "re-check after each
`cao update`"), and how to undo.
````

## Rules for writing a guide

- **One feature per guide**, named for what the user asks for. Link the feature doc; don't
  repeat it.
- **Every step ends with a check that a wrong setup would fail.** "It printed something" is
  not a check.
- **Quote error text exactly**, so the agent can match it.
- **Probe commands, don't trust versions.**
- **Env var names only, never values.** Placeholders are `<...>`.
- **Name the consent points** in the rules and in the front matter.
- **Every write has an undo.**
- **Link the shared snippets**; don't paste a private copy that can drift.
- **Front matter is required.** The keys are the ones in the skeleton: `feature`, `title`,
  `applies_to`, `user_steps`, `consent_before`, `touches` and `undo`. Step numbers match the
  guide's headings.
- **Keep it testable.** Every command in a check block must run on a machine without the
  user's credentials, or be marked as a user step, so the guides can be tested in CI.
