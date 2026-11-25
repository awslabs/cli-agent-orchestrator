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
