# Remote CAO (experimental)

`cao remote` stores non-secret box metadata under CAO's configuration directory and
checks the laptop prerequisites for an SSM-only Remote CAO box. It creates no AWS
resources and `cao remote check` does not modify either machine.

```bash
cao remote add demo --instance-id i-0123456789abcdef --region us-east-1 --profile work
cao remote check demo
cao remote check demo --json
```

The experimental check verifies AWS CLI v2, the Session Manager plugin, SSH, the
SSO profile and login, instance/SSM status, the SSH key/config, then (when the
earlier checks pass) SSH, the selected attach-mode version, and the local CAO
health endpoint over SSH. `herdr` is the default mode; `tmux` uses `cao-tmux`.

This command intentionally does not yet create keys, edit SSH configuration, open
an SSO login, or connect to a session. Those actions belong to a later `connect`
command and require explicit confirmation.
