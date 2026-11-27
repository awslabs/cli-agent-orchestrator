# Remote CLI reference

`cao remote` stores local registrations for experimental Remote CAO boxes and
checks laptop and remote prerequisites. It never creates cloud resources or
changes a registered box during a check. The SSH stage uses the user's SSH
configuration, which can open SSM sessions (up to three during a full check).
`check` always uses the SSH host `cao-<box>`; with the guide's `Host cao-box`
configuration, register the box as `box`.

```bash
cao remote add box \
  --instance-id i-0123456789abcdef \
  --region us-east-1 \
  --profile work
cao remote list
cao remote check box
cao remote check box --json
cao remote remove box
```

`add` records the instance, AWS region and profile in CAO's configuration
directory with owner-only permissions. `--mode herdr` is the default; use
`--mode tmux` for a tmux-backed box.
`--user` optionally records the box's Linux user in the registration and
displays it in `list`; the current checks do not use it.

`check` returns zero when every applicable check succeeds, one when something
needs fixing, and two for invalid input or an unavailable/corrupt registration.
It checks only: installed command versions; the selected AWS profile and
identity; EC2 and SSM status; local SSH key/config presence; and, after those
prerequisites pass, SSH connectivity and the box's version/health endpoint.
Its AWS and SSH calls are read-only. It does not run `aws sso login`,
`ssh-keygen`, or any remote setup command directly.

## Connect (experimental)

`cao remote connect <box>` first prints the same checks as `check`, then asks
before it runs AWS SSO login, creates a missing SSH key, writes CAO-owned SSH
config blocks, or pins a host key obtained through SSM. The shared dashboard host
inherits the same pinned key, CAO known-hosts file and `ForwardAgent no` setting;
managed blocks precede the user's `Host` and `Match` entries. Malformed markers
fail closed without modifying the config. `--yes` accepts those
prompts; `--no-attach` stops after setup and prints the attach and dashboard
commands. A changed pinned host key stops safely; verify it and use `--rekey`
to replace it. `connect` never changes text outside its marked SSH blocks and
does not alter the user's normal `known_hosts`. If an existing box rejects the
key, it prints the exact public-key environment value needed for manual setup,
then can install that public key through SSM after confirmation. The remote
script runs as the box user and appends an absent key only.

`cao remote remove <box>` unregisters the box. If its managed SSH blocks or
CAO host-key entry exist, it first offers to remove only those entries; `--yes`
accepts that cleanup.
