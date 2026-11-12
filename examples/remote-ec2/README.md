# CAO on one always-on EC2 box, reached from your laptop's herdr

When your laptop restarts, your agents stop. The herdr server goes down, every
pane process goes with it, and so do cao-server and any Telegram bot. This
sample moves all of that onto one always-on Linux box in AWS. Everything there
runs under systemd. Your laptop keeps only the herdr window, which shows the
box as a saved SSH machine and reconnects by itself.

It's a sample: scripts, unit files and notes for you to copy and adapt. It is
the small, one-box version of the cluster ideas in issue #1238, in the same
spirit as the super-agent sample (#1300).

> **Nothing here creates AWS resources by itself.** Every command that creates
> or deletes something in your account is written out in this README for you
> to run. `bootstrap.sh` runs on the box and only sets up the box.

## Contents

- [What you get](#what-you-get)
- [Before you start](#before-you-start)
- [Quick start](#quick-start)
- [The phases](#the-phases)
- [Design notes](#design-notes)
- [Backups and restore](#backups-and-restore)
- [Checking it works](#checking-it-works)
- [FAQ](#faq)
- [Teardown and cost](#teardown-and-cost)
- [Files](#files)
- [Testing this sample](#testing-this-sample)

## What you get

```
laptop                                   EC2 box (Ubuntu 24.04 arm64, Graviton)
------                                   -------------------------------------
herdr window  --ssh over SSM---------->  herdr-cao.service   herdr --session cao-<instance-id> server
                                           panes: agents, hx, yazi, shells
browser       --ssh -L 19889:9889----->  cao-server.service  127.0.0.1:9889 only
Telegram app  <---- Telegram API ---->   cao-telegram-relay.service (optional, second bot)
                                         cao-backup.timer    nightly SQLite backup -> S3
```

- No inbound port. SSH runs inside an SSM Session Manager session.
- cao-server listens on `127.0.0.1` only. You reach the dashboard through an
  SSH tunnel.
- The herdr server, and so every pane, starts with only `HOME`, `USER`,
  `PATH`, `LANG` and `TERM` in its environment. There are no tokens in any
  unit except the optional relay's.
- After a reboot, herdr starts with an empty layout and CAO relaunches agents
  with the right flags. No bare-shell pane gets an inbox message.

## Before you start

On the laptop:

- the AWS CLI v2 and the
  [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html);
- credentials that may create the resources below, and call `ssm:StartSession`
  on the instance;
- herdr, at the version you'll pin on the box (`herdr --version`);
- an SSH key pair (only the public half goes to the box).

Decisions this sample has already made for you, all of which you can change:

| Choice | Default | Why |
|---|---|---|
| Region | Sydney, `ap-southeast-2` | Close to the user, so typing feels fast. See [Bedrock in Sydney](#bedrock-in-sydney). |
| Box | `m8g.4xlarge` (Graviton4, 16 vCPU, 64 GiB) | 4 GiB per vCPU suits several agents plus builds. Alternatives: `c8g.4xlarge` (32 GiB), `r8g.4xlarge` (128 GiB), `m7g.4xlarge` (Graviton3) as the fallback. |
| OS | Ubuntu 24.04 LTS, arm64 | glibc 2.39. It's the OS that Docker's, Claude Code's, VS Code's and Kiro's install docs name. Security updates run to 2029. |
| Home path | Your laptop's home path, for example `/Users/<you>` | Absolute paths in your kits, prompts and memory keep working unchanged. |
| Editing | `hx` and `yazi` in herdr panes | Nothing to sync. VS Code Remote-SSH is optional. |

## Quick start

Replace every `<...>` placeholder with your own value.

### 1. Check the box type is offered in Sydney

```bash
aws ec2 describe-instance-type-offerings --region ap-southeast-2 \
  --location-type availability-zone \
  --filters Name=instance-type,Values=m8g.4xlarge,m7g.4xlarge \
  --query 'InstanceTypeOfferings[].[InstanceType,Location]' --output table
```

If `m8g.4xlarge` isn't listed in your zone, use `m7g.4xlarge`.

### 2. Find the Ubuntu 24.04 arm64 image

```bash
aws ssm get-parameter --region ap-southeast-2 \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
  --query Parameter.Value --output text
```

### 3. Make the instance role (narrow, see [Secrets and the role](#secrets-and-the-instance-role))

Edit [`iam/instance-role-policy.json.example`](iam/instance-role-policy.json.example)
first: fill in `<account-id>`, `<backup-bucket>` and `<backup-key-id>`, and
keep only the models you use.

```bash
aws iam create-role --role-name <cao-box-role> \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name <cao-box-role> \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam put-role-policy --role-name <cao-box-role> --policy-name cao-box \
  --policy-document file://iam/instance-role-policy.json.example
aws iam create-instance-profile --instance-profile-name <cao-box-profile>
aws iam add-role-to-instance-profile --instance-profile-name <cao-box-profile> --role-name <cao-box-role>
```

### 4. Make the backup bucket (versioned, private, encrypted)

```bash
aws s3api create-bucket --bucket <backup-bucket> --region ap-southeast-2 \
  --create-bucket-configuration LocationConstraint=ap-southeast-2
aws s3api put-public-access-block --bucket <backup-bucket> \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket <backup-bucket> --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket <backup-bucket> --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms","KMSMasterKeyID":"<backup-key-arn>"}}]}'
```

### 5. Launch the box

The security group has **no inbound rules**. IMDSv2 is required, with a hop
limit of 1. The data volume is encrypted and survives termination.

```bash
aws ec2 create-security-group --region ap-southeast-2 --vpc-id <vpc-id> \
  --group-name <cao-box-sg> --description "CAO box: no inbound"
aws ec2 run-instances --region ap-southeast-2 \
  --image-id <ami-from-step-2> --instance-type m8g.4xlarge \
  --subnet-id <subnet-id> --security-group-ids <sg-id> \
  --iam-instance-profile Name=<cao-box-profile> \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled \
  --block-device-mappings '[
    {"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":50,"VolumeType":"gp3","Encrypted":true}},
    {"DeviceName":"/dev/sdf","Ebs":{"VolumeSize":200,"VolumeType":"gp3","Encrypted":true,"DeleteOnTermination":false}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=cao-box}]'
```

The subnet needs outbound internet (a NAT gateway, or a public IP with no
inbound rules) or VPC endpoints for SSM, Bedrock and S3.

### 6. First login and your SSH key

Session Manager gets you a shell before SSH is set up:

```bash
aws ssm start-session --region ap-southeast-2 --target <instance-id>
```

Copy the sample to the box, for example with `git clone` of this repository.
Then run a dry run (it prints every step and changes nothing), and after that
the real thing. On Nitro instances the data volume shows up as an NVMe device:
check with `lsblk`.

```bash
cd cli-agent-orchestrator/examples/remote-ec2
lsblk
sudo BOX_USER=<you> BOX_HOME=/Users/<you> DATA_DEVICE=/dev/nvme1n1 \
     HERDR_VERSION=<laptop-herdr-version> CAO_REF=<cao-commit-sha> \
     BACKUP_S3_URI=s3://<backup-bucket>/cao-box/ \
     bash bootstrap.sh --dry-run
sudo BOX_USER=<you> ... bash bootstrap.sh      # same settings, no --dry-run
```

Then add your laptop's **public** key to `/Users/<you>/.ssh/authorized_keys`
(mode 0600, owned by `<you>`).

### 7. Connect the laptop

Add [`ssh/config.snippet`](ssh/config.snippet) to the laptop's
`~/.ssh/config`, then:

```bash
ssh cao-box true                                  # proves SSH over SSM works
herdr machine add cao-box --remote-session cao-<instance-id> --label cao-box
```

**If `herdr machine add` offers to stop the remote server, answer No** (that's
the default). The session name is in `/etc/cao-box/box.env` on the box.

### 8. Start the services, log in, check

On the box, as `<you>`:

```bash
sudo systemctl start herdr-cao.service cao-server.service
curl -fsS http://127.0.0.1:9889/health
claude          # Bedrock through the instance role, see config/claude-settings.box.json.example
codex login --device-auth
gh auth login
```

## The phases

Take it one phase at a time. Each phase leaves your laptop setup running, so
you can stop at any point.

**Phase 0: prove herdr over SSH (a toy session).**
Do quick-start steps 1–7, then start only `herdr-cao.service`. From the
laptop's herdr, open a pane on the box and run `top`.
- Reboot the laptop. The box's session should be listed again and `top` still
  running.
- Reboot the box. The unit should come back by itself with an empty layout,
  and the old layout should be in `restore-archive/`.
- Check `kiro-cli` here if you use Kiro: its docs don't confirm a
  linux-arm64 build. Claude Code and Codex work without it.

**Phase 1: CAO and one agent.**
Start `cao-server.service` (installed from the pinned `CAO_REF`). Log in to
Claude Code and launch one agent with `cao launch`.
- Reboot the box. Both units come back, `cao-<instance-id>` is listed, no old
  pane text is replayed, and a fresh launch works.
- In the cao-server log, look for the startup cleanup of old terminal rows.
- Run [`bin/box-resume.sh`](bin/box-resume.sh) for the read-only checks.

**Phase 2 (optional): move your kit and Telegram.**
Copy your conductor-style kit, prompts and memory to the same paths on the
box. Leave out secrets and runtime folders (`fifos/`, `locks/`, `tmp/`,
`logs/`). Set up the [second bot](telegram/README.md).
- *Cutover:* stop the laptop's long-running agents and start the box's.
- *Rollback:* stop the box's agents and the relay, and start the laptop's
  again. Nothing on the laptop was removed, so this is quick.

**Phase 3: backups and hardening.**
- Check `cao-backup.timer` (`systemctl list-timers cao-backup.timer`).
- Do a [restore drill](#backups-and-restore) once.
- Add a budget alarm and a CloudWatch status-check alarm.
- Check that `unattended-upgrades` is on for security updates.
- Optionally turn on `CAO_AUTH_LOCAL_TOKEN` (see the FAQ).

## Design notes

### The box

Graviton, at least 16 vCPU. The default is `m8g.4xlarge` (16 vCPU, 64 GiB).
The alternatives are `c8g.4xlarge` (32 GiB), `r8g.4xlarge` (128 GiB), and
`m7g.4xlarge` (Graviton3) as the fallback when Graviton4 isn't offered. The
sizes come from the AWS instance-type pages. Availability in Sydney isn't
assumed: run quick-start step 1.

### The OS

Ubuntu 24.04 arm64. `bootstrap.sh` refuses to run anywhere else.
- Amazon Linux 2 has been out of support since 2026-06-30.
- Amazon Linux 2023 is supported to 2029-06-30, but this sample doesn't test
  it.

### Tools on linux-arm64

herdr, Claude Code, Codex CLI, `gh`, AWS CLI v2, SAM CLI, Docker, `yazi` and
`hx` all publish arm64 builds. `bootstrap.sh` installs the non-agent tools at
pinned versions (`HERDR_VERSION`, `GH_VERSION`, `YAZI_VERSION`,
`HELIX_VERSION`). Docker comes from Ubuntu's `docker.io` package; use
Docker's own apt repository if you need a newer one.
- Asset names change now and then. If a download 404s, check the release page
  and adjust the URL in `bootstrap.sh`.
- kiro-cli on linux-arm64 is **not confirmed** by Kiro's docs. Try it in
  phase 0.

### Bedrock in Sydney

There are no in-Region model ids in `ap-southeast-2`. Use the Australia
profiles, `au.anthropic.claude-<model>-5-5`, which route to Sydney and
Melbourne, or the global ones, `global.anthropic.claude-<model>-5-5`, which
cost about 10% less but may run anywhere. `us.` profiles aren't offered in
`ap-southeast-2`. Check which profiles you can use:

```bash
aws bedrock list-inference-profiles --region ap-southeast-2 \
  --query "inferenceProfileSummaries[?starts_with(inferenceProfileId, 'au.')].inferenceProfileId"
```

Change every `us.` id in your Claude Code settings and in CAO's
`model_tiers` (see [`config/`](config/)). Set `AWS_REGION=ap-southeast-2`.
Use the instance role, not a profile: the box has no `~/.aws/credentials` and
no `AWS_PROFILE`.

### Access

SSH runs over SSM (`AWS-StartSSHSession`), so there's no inbound port. The
fallback is plain SSH from your own `/32` only. Session Manager doesn't log
the contents of SSH sessions, so don't rely on it as an audit trail of what
you typed.

### cao-server

It listens on loopback only (`--host 127.0.0.1` in the unit, on purpose) and
is never public. The dashboard goes through `ssh -N cao-box-dash` and
`http://127.0.0.1:19889`. There's exactly one cao-server per CAO home, so
never point the laptop's CAO at the box's files.

### Secrets and the instance role

- The herdr unit starts with a minimal environment (`HOME`, `USER`, `PATH`,
  `LANG`, `TERM`), and [`bin/herdr-start.sh`](bin/herdr-start.sh) runs herdr
  under `env -i`. Panes inherit the herdr server's environment, so that's all
  an agent sees from systemd.
- The Telegram token sits in a mode 0600 file that only the relay unit reads.
- Agent CLIs log in on the box, with their own device flows. Don't copy login
  files from the laptop.
- **Every agent can reach the instance role** through the instance metadata
  service. So keep the role narrow: Bedrock invoke on the profiles you use,
  `s3:PutObject` and `s3:GetObject` on one prefix, the SSM core policy, and
  nothing for IAM, EC2 or Secrets Manager. IMDSv2 with hop limit 1 keeps
  containers on the box from reaching it. The exact Bedrock resource ARNs for
  cross-Region profiles are worth checking against the Bedrock docs before
  you rely on the example policy.

### herdr

- Pin one version on both the box and the laptop. `herdr-linux-aarch64` is
  published for v0.9.3 and v0.9.5. A CLI and server version mismatch has
  broken every CAO call before.
- After an upgrade, check `herdr status`.
- Answer No if `herdr machine add` offers to stop the remote server.

### Reboot recovery

On a reboot, processes die. herdr would restore the saved layout as bare
shells, or as agents without CAO's flags. So the herdr unit's start script
moves the saved layout into a dated `restore-archive/` folder first, and
[`config/herdr-config.toml.snippet`](config/herdr-config.toml.snippet) sets
`resume_agents_on_restore = false`. CAO then relaunches agents with the right
flags. herdr's own `session-snapshots/` are kept.

The session name is `cao-<instance-id>`. It's read once from the instance
metadata (IMDSv2) at bootstrap and kept in `/etc/cao-box/box.env`. It's
never recomputed, so it doesn't drift, and it survives stop and start.

### Editing

Edit on the box, in herdr panes, with `hx` and `yazi`. VS Code Remote-SSH is
optional (Ubuntu 24.04 meets its requirements). There's no `sshfs` and no
two-way sync, so there are never two copies to reconcile.

### Telegram

Use a second bot just for the box, and set it up one step at a time: see
[`telegram/README.md`](telegram/README.md).

### Data

- CAO's SQLite database lives on the encrypted EBS data volume, **never** on
  EFS or NFS: SQLite locking isn't safe over network file systems.
- Leave CAO's journal mode as it is.
- Every night, the backup job runs `sqlite3 .backup`, checks the copy with
  `PRAGMA integrity_check`, and uploads it to a versioned S3 prefix.
- Add EBS snapshots of the data volume (Data Lifecycle Manager, for example 7
  daily and 4 weekly) for whole-box restores.

### The home path

`bootstrap.sh` creates the user with `BOX_HOME` as their real home and mounts
the data volume at its parent (`DATA_MOUNT`, for example `/Users`). Set
`BOX_HOME` to your laptop's home path, and absolute paths in your kits keep
working.

- **Why not a symlink?** `/Users/<you> -> /home/<you>` lets literal paths
  work. But git worktrees, path guards and anything that resolves real paths
  sees `/home/...` and may disagree.
- **No kit paths to keep?** Use the ordinary layout instead:
  `BOX_HOME=/home/<you> DATA_MOUNT=/home/<you>`. That mounts the volume as the
  home itself and leaves the default `ubuntu` user alone.

## Backups and restore

[`backup.sh`](backup.sh) runs nightly from `cao-backup.timer`. It keeps 14
local copies in `~/backups/cao/` and uploads each one to `BACKUP_S3_URI`.

```bash
# A drill: restore into a temp folder, check it, touch nothing live. Monthly.
/usr/local/lib/cao-box/restore.sh --drill s3://<backup-bucket>/cao-box/cao-<stamp>.db.gz

# The real thing: stop cao-server, swap the DB (keeping the old one), start, wait for /health.
/usr/local/lib/cao-box/restore.sh --live ~/backups/cao/cao-<stamp>.db.gz --yes
```

`--live` doesn't touch herdr, so running panes keep running. It refuses to
write over a database that still has a `-journal`, `-wal` or `-shm` file next
to it.

**Whole-box restore:**
1. Make a volume from a snapshot and attach it to a fresh instance.
2. Run `bootstrap.sh` with the same settings. It finds the filesystem, skips
   formatting, and keeps the existing `box.env`, so the session name stays
   the same.

## Checking it works

These are the issue's acceptance checks.

| Check | How |
|---|---|
| The laptop reboots | The box's session is listed in herdr again, and the agent's pane is still running. |
| The box reboots | `bin/box-resume.sh` reports all ok. `cao-<instance-id>` is listed with an empty layout, and a fresh `cao launch` works. |
| No open ports | `aws ec2 describe-security-groups --group-ids <sg-id> --query 'SecurityGroups[].IpPermissions'` prints `[[]]` (or only 22 from your `/32`). On the box, `ss -ltn` shows `127.0.0.1:9889` and no `0.0.0.0:9889`. |
| Backups restore | `restore.sh --drill` says `integrity_check ok`. `restore.sh --live` brings `/health` back. |
| Telegram | A prompt sent to the box's bot reaches the agent, and the reply comes back. |
| No secrets in panes | In a pane started by herdr, run `env \| cut -d= -f1 \| sort`. That prints **names only**; never run a bare `env`. None of the names should be a token, key or secret. |

## FAQ

**Why not just keep the laptop awake?**
Restarts still happen: updates, crashes, a flat battery. On the box, a laptop
restart is a non-event, because herdr reconnects by itself.

**Why not Docker?**
It's possible, but it adds a layer between herdr, the agents and the files
you edit, and it isn't the main path here. Docker is installed for your
projects' own use.

**Can I stop the box at night to save money?**
Yes. Stopping is like a reboot: the units come back on start, and the session
name stays the same. `Persistent=true` runs a missed backup at boot. You
still pay for the EBS volumes and snapshots while it's stopped.

**Does cao-server restarting kill my agents?**
No. The panes belong to herdr, and cao-server reconnects to herdr's socket.
Restarting herdr does end the panes. That's why the units are separate and
herdr is never `PartOf=` cao-server.

**How do I turn on `CAO_AUTH_LOCAL_TOKEN`?**
Put it in a root-owned 0600 file, for example `/etc/cao-box/cao-auth.env`.
Then add it with `sudo systemctl edit cao-server`:

```ini
[Service]
EnvironmentFile=/etc/cao-box/cao-auth.env
```

It's all-or-nothing, and CAO passes it to every agent pane. Read
[the configuration docs](../../docs/configuration.md) first.

**Which CAO version does the box run?**
The commit in `CAO_REF`. To upgrade, re-run `bootstrap.sh` with a new
`CAO_REF`, then run `sudo systemctl restart cao-server`.

**Where are herdr's logs?**
In `~/.config/herdr/sessions/cao-<instance-id>/`, plus
`journalctl -u herdr-cao`.

**Kiro?**
kiro-cli on linux-arm64 isn't confirmed. Try it in phase 0. The other
providers don't depend on it.

## Teardown and cost

Most of the bill is the instance. After that come the EBS volumes,
snapshots, S3, KMS and Bedrock usage. This sample quotes no prices: check
the EC2, EBS and Bedrock pricing pages for Sydney. Set a budget alarm
before you start.

To remove everything (this deletes data; download what you want to keep
first):

```bash
aws ec2 terminate-instances --region ap-southeast-2 --instance-ids <instance-id>
aws ec2 delete-volume --region ap-southeast-2 --volume-id <data-volume-id>      # it survives termination
aws ec2 delete-snapshot --region ap-southeast-2 --snapshot-id <snapshot-id>     # each one; and the DLM policy
aws ec2 delete-security-group --region ap-southeast-2 --group-id <sg-id>
aws s3api delete-bucket --bucket <backup-bucket>   # after deleting every object version
aws iam remove-role-from-instance-profile --instance-profile-name <cao-box-profile> --role-name <cao-box-role>
aws iam delete-instance-profile --instance-profile-name <cao-box-profile>
aws iam delete-role-policy --role-name <cao-box-role> --policy-name cao-box
aws iam detach-role-policy --role-name <cao-box-role> --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam delete-role --role-name <cao-box-role>
```

Then remove the `cao-box` entries from the laptop's `~/.ssh/config`, run
`herdr machine remove cao-box` (or remove it from herdr's sidebar), and send
`/deletebot` to BotFather.

## Files

| File | What it does |
|---|---|
| [`bootstrap.sh`](bootstrap.sh) | Sets up the box. Idempotent, with `--dry-run`. Ubuntu 24.04 arm64 only. Enables units but never starts them. |
| [`backup.sh`](backup.sh), [`restore.sh`](restore.sh) | Nightly backup; restore drill and live restore. |
| [`bin/box-session-name.sh`](bin/box-session-name.sh) | Prints `cao-<instance-id>` from IMDSv2. |
| [`bin/herdr-start.sh`](bin/herdr-start.sh), [`bin/herdr-clean-layout.sh`](bin/herdr-clean-layout.sh) | Start herdr with the minimal environment, after moving the saved layout aside. |
| [`bin/box-resume.sh`](bin/box-resume.sh) | Read-only checks after a reboot. |
| [`bin/resource-probe-linux.sh`](bin/resource-probe-linux.sh), [`resource-gate-linux.md`](resource-gate-linux.md) | A Linux memory-pressure probe, and why a macOS-only gate stalls. |
| [`systemd/`](systemd/) | `herdr-cao.service`, `cao-server.service`, `cao-backup.service` and `.timer`, and the optional relay unit. |
| [`config/`](config/) | Box versions of the herdr, CAO and Claude Code settings (Sydney model ids). |
| [`iam/instance-role-policy.json.example`](iam/instance-role-policy.json.example) | A narrow instance-role policy to adapt. |
| [`ssh/config.snippet`](ssh/config.snippet) | The laptop's SSH entries, plus the `herdr machine add` command. |
| [`telegram/README.md`](telegram/README.md) | The second bot, step by step. |

See also [herdr in CAO](../../docs/herdr.md).

## Testing this sample

The tests need no network, no AWS, no root and no real servers.

```bash
uv run pytest examples/remote-ec2/tests -q --no-cov
```

They cover:
- `bash -n` and `shellcheck` (skipped if it isn't installed);
- the unit files (`systemd-analyze verify` where it exists, otherwise a
  structural parse);
- the herdr environment allowlist and the loopback bind;
- a `bootstrap.sh --dry-run` in a fake root that must change nothing, and
  its refusal on the wrong OS;
- the session name from a mocked IMDSv2;
- a backup and restore round trip with `integrity_check`;
- the clean-layout step and the resource probe;
- a leak grep (no local paths, account numbers or `us.` model ids).
