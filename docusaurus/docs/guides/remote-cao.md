---
sidebar_position: 3
---

# Remote CAO: run CAO on an always-on box

Running CAO on a laptop ties your agents to its battery, sleep cycle and available memory. An always-on Linux box lets long-running agents continue while your laptop is closed. You attach from the laptop to watch or work with them; the box runs the agents, terminal server and CAO services.

This page introduces the [EC2 sample](https://github.com/awslabs/cli-agent-orchestrator/tree/main/examples/remote/ec2/). For the actual resource-creation commands and setup procedure, follow the [full Remote CAO guide](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/remote.md). The sample is a reference to adapt, not a cloud provisioning tool.

:::tip[Start with the full guide]
Read the [step-by-step setup](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/remote.md#the-steps) before creating anything in AWS. It explains IAM, networking, the instance, SSH host keys, backups, and teardown. This page is a map, not a replacement for those steps.
:::

## How it fits together

```text
laptop                                   always-on EC2 box
------                                   -----------------
herdr window -- SSH over SSM ---------> herdr-cao.service -> agent panes
browser      -- SSH tunnel -----------> cao-server.service (loopback only)
                                         cao-backup.timer -> optional S3 backup
```

The sample uses Ubuntu 24.04 on arm64. SSH travels through AWS Systems Manager Session Manager (SSM), so the security group needs no inbound port. `cao-server` listens on the box's loopback address, not on the public network. The instance's systemd services keep the remote terminal server and CAO running independently of your laptop.

The guide's main path uses **herdr**, which provides a saved machine and reconnecting terminal UI. The [sample also supports tmux](https://github.com/awslabs/cli-agent-orchestrator/blob/main/examples/remote/ec2/README.md#tmux-mode) when you prefer a plain SSH terminal. Choose a mode before bootstrapping the box; the two modes use different terminal services.

## Quick start

1. **Prepare the laptop.** Install AWS CLI v2, the Session Manager plugin, OpenSSH and herdr. Use a short-lived AWS login with the permissions described in the [guide](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/remote.md#1-prepare-the-laptop). Match the herdr version on the laptop and box.
2. **Create the AWS resources.** Follow the [sample's quick start](https://github.com/awslabs/cli-agent-orchestrator/blob/main/examples/remote/ec2/README.md#quick-start) to create a scoped instance role, a security group with no inbound rules, and an EC2 instance. Backups require additional bucket and key setup. The sample does not provision these resources for you.
3. **Bootstrap the box.** Run the sample's `bootstrap.sh` as root from a checkout outside the agent user's home. Follow the [box setup steps](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/remote.md#3-set-up-the-box) for the data volume, configuration and service checks.
4. **Set up SSH over SSM.** Install your public key on the box and configure SSH with the SSM proxy, a pinned host key and agent forwarding disabled. See [Connect: SSH inside SSM](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/remote.md#4-connect-ssh-inside-ssm). Do not copy your laptop's AWS credentials to the box.
5. **Check services and attach.** Verify the services and log in to the agent CLIs on the box as described in [the guide](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/remote.md#5-check-the-services-and-log-in). Then attach with herdr or tmux as described below.

The box incurs cloud charges while it runs. Read the [teardown and cost instructions](https://github.com/awslabs/cli-agent-orchestrator/blob/main/examples/remote/ec2/README.md#teardown-and-cost) before you start.

## Experimental `cao remote` commands

`cao remote` registers a box **on your laptop** and checks prerequisites. These commands are experimental: they do not create cloud resources, install CAO on the box, or set up SSH for you. For options, exit codes and check scope, see the [Remote CLI reference](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/remote-cli.md).

With the guide's SSH alias `cao-box`, register the name `box`: `check` connects to the SSH host `cao-<box>`. Substitute your instance ID (8 or 17 hexadecimal digits after `i-`), AWS region and profile. The example region is illustrative; use the region where your box actually runs.

Set up the SSH alias before checking the box; a registration alone is not an SSH configuration.

```bash
cao remote add box \
  --instance-id i-<hex-digits> \
  --region us-east-1 \
  --profile work
cao remote list
cao remote check box
cao remote check box --json
cao remote remove box
```

`add` saves a local registration; it defaults to herdr mode. To register a tmux box, add `--mode tmux`; `--user` records a Linux user for display but does not change the checks. `list` shows your registered boxes, and `remove` deletes only the local registration. `check` reports laptop, AWS, SSH and box-health prerequisites; it does not perform setup. It may start an SSH connection through your SSM configuration but does not mutate the box.

:::note[Connecting is still manual]
`cao remote connect` is coming in [PR #1414](https://github.com/awslabs/cli-agent-orchestrator/pull/1414); it is not available on main. Use the SSH and attach steps in the full guide instead.
:::

## Attach to your agents

### Herdr mode

After completing the guide's SSH configuration, register the remote session with herdr on the laptop. Replace the instance ID with yours:

```bash
herdr machine add cao-box --remote-session cao-<instance-id> --label cao-box
herdr
```

The sample names the session `cao-<instance-id>`. If herdr offers to stop the remote server while saving the machine, answer **No**. You can also [attach directly or check its status](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/remote.md#6-attach-from-the-laptop). The laptop's herdr window is a view into the server and agents that remain on the box.

### Tmux mode

If you chose the sample's tmux mode, use its [tmux-specific bootstrap instructions](https://github.com/awslabs/cli-agent-orchestrator/blob/main/examples/remote/ec2/README.md#tmux-mode). From the laptop, attach over your configured SSH alias:

```bash
ssh -t cao-box tmux attach
```

Do not expect the herdr machine command to attach to a tmux-mode box. The sample runs tmux in its own systemd service, separate from `cao-server`.

### Dashboard

The guide's optional `cao-box-dash` SSH alias forwards laptop port 19889 to the box's loopback port 9889. With that alias configured, open the tunnel and browse to `http://127.0.0.1:19889`:

```bash
ssh -N cao-box-dash
```

Do not expose the dashboard on a public interface. The [full attach instructions](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/remote.md#6-attach-from-the-laptop) show the SSH configuration.

## If you cannot attach

Start with the [guide's service checks](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/remote.md#5-check-the-services-and-log-in), rather than changing the instance or restarting agents blindly.

1. Check your laptop's AWS login and the region of the instance. An expired SSO session prevents SSH-over-SSM even when the instance is healthy.
2. Confirm that the box is running and registered with SSM. SSH-over-SSM depends on the Session Manager plugin and the box's SSM agent.
3. Check that the SSH alias resolves to the intended instance and uses the pinned host key. The Remote CLI reference explains why the local registration name and SSH alias differ.
4. On the box, verify that the terminal server and `cao-server` services are active. Herdr-mode and tmux-mode boxes have different terminal services; use the checks for your selected mode.
5. If only the dashboard is unreachable, verify the optional tunnel alias and its forwarded ports. A working herdr session does not imply that a separate SSH tunnel is open.

The [full guide's FAQ](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/remote.md#faq) covers reconnects, service restarts and Session Manager's Run As setting. Use the [sample's restore procedure](https://github.com/awslabs/cli-agent-orchestrator/blob/main/examples/remote/ec2/README.md#backups-and-restore) if the data volume or service state needs recovery.

:::note[Checks cannot fix a box]
`cao remote check box` reports what it can observe, but leaves configuration untouched. A failed check does not authorize it to log in, install tools, alter the instance role or change a service. Fix the reported prerequisite using the full guide, then check again.
:::

## Security and limits

- **Keep the network closed.** Use SSM for SSH, no inbound security-group rules, a pinned SSH host key and `ForwardAgent no`. Session Manager does not record commands typed inside an SSH session.
- **Limit cloud authority.** Every agent on the box can use the instance role, so scope it to the resources it needs. Restrict `ssm:SendCommand`: it runs as root on the instance.
- **Treat the agent user as untrusted.** Agents can read that user's files and stored CLI logins. The sample's agent user has no `sudo` or Docker group membership. Keep laptop credentials on the laptop.
- **Budget for operations.** You create, pay for, update and eventually tear down the AWS resources yourself. Backups and restores need deliberate configuration; a stopped instance cannot run agents.
- **Know what checks mean.** `cao remote check` is a prerequisite check, not a provisioning, security-audit or repair tool. A successful check does not authorize broader cloud access or verify your backups.

For the detailed threat model and operational procedures, read [Security in short](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/remote.md#security-in-short), the [sample design notes](https://github.com/awslabs/cli-agent-orchestrator/blob/main/examples/remote/ec2/README.md#design-notes) and the [CLI reference](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/remote-cli.md).
