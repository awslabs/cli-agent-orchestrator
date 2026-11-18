# Remote CLI reference

`cao remote` stores local registrations for experimental Remote CAO boxes and
checks laptop and remote prerequisites. It never creates cloud resources,
starts a session, or changes a registered box during a check.

```bash
cao remote add dev-box \
  --instance-id i-0123456789abcdef \
  --region us-east-1 \
  --profile work
cao remote list
cao remote check dev-box
cao remote check dev-box --json
cao remote remove dev-box
```

`add` records the instance, AWS region and profile in CAO's configuration
directory with owner-only permissions. `--mode herdr` is the default; use
`--mode tmux` for a tmux-backed box.

`check` returns zero when every applicable check succeeds, one when something
needs fixing, and two for invalid input or an unavailable/corrupt registration.
It checks only: installed command versions; the selected AWS profile and
identity; EC2 and SSM status; local SSH key/config presence; and, after those
prerequisites pass, SSH connectivity and the box's version/health endpoint.
Its AWS and SSH calls are read-only. It does not run `aws sso login`,
`ssh-keygen`, `aws ssm start-session`, or any remote setup command.
