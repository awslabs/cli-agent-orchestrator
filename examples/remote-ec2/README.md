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
  `PATH`, `LANG` and `TERM` in its environment. No unit that agents can
  reach holds a token. The optional relay runs as its own user.
- After a reboot, the herdr unit moves the saved layout aside before herdr
  starts, and cao-server starts only once that herdr's socket exists, so CAO
  never starts a herdr of its own. CAO then relaunches agents with the right
  flags. Phase 0 checks that herdr really comes up empty.

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
| Docker | Off (`INSTALL_DOCKER=yes` turns it on) | Membership of the `docker` group is root on the box, for every agent. |

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
keep only the models you use. Read [`iam/README.md`](iam/README.md): the ARN
forms are marked **check before use**. The policy replaces the managed
`AmazonSSMManagedInstanceCore` with the few SSM actions Session Manager
needs, because the managed policy would let every agent read Parameter
Store.

```bash
aws iam create-role --role-name <cao-box-role> \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
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
aws s3api put-bucket-policy --bucket <backup-bucket> \
  --policy file://iam/backup-bucket-policy.json.example     # TLS only, this KMS key only
```

The backups hold CAO's database, and that includes message text. Treat the
bucket like the box.

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

Write down the data volume's id (`vol-...`). `bootstrap.sh` finds the disk
by that id, never by device name:

```bash
aws ec2 describe-instances --region ap-southeast-2 --instance-ids <instance-id> \
  --query 'Reservations[].Instances[].BlockDeviceMappings[?DeviceName==`/dev/sdf`].Ebs.VolumeId' --output text
```

### 6. First login and your SSH key

Session Manager gets you a shell before SSH is set up:

```bash
aws ssm start-session --region ap-southeast-2 --target <instance-id>
```

Copy the sample to the box, for example with `git clone` of this repository.
Put the settings in a file, so every run uses the same ones. Every download
is checked against a sha256, and a missing or wrong checksum stops the run.
Copy each value from the release's published checksums (see
[Tools](#tools-on-linux-arm64)).

```bash
cd cli-agent-orchestrator/examples/remote-ec2
cat > box-settings.env <<'EOF'
BOX_USER=<you>
BOX_UID=<uid>                 # 1000-59999 (a macOS uid such as 501 is refused); pick one and never change it
BOX_HOME=/Users/<you>
DATA_VOLUME_ID=<vol-id>
HERDR_VERSION=<laptop-herdr-version>
HERDR_SHA256=<sha256 of herdr-linux-aarch64>
CAO_REF=<cao-commit-sha>
BACKUP_S3_URI=s3://<backup-bucket>/cao-box/
BACKUP_KMS_KEY_ARN=<backup-key-arn>
AWS_CLI_VERSION=<version>
AWS_CLI_SHA256=<sha256>
SAM_VERSION=<version>
SAM_SHA256=<sha256>
YAZI_SHA256=<sha256>
HELIX_SHA256=<sha256>
UV_VERSION=<version>
UV_SHA256=<sha256>
EOF
sudo env $(cat box-settings.env | sed 's/ *#.*//') bash bootstrap.sh --dry-run --format-new-volume   # first run: shows the format step too
sudo env $(cat box-settings.env | sed 's/ *#.*//') bash bootstrap.sh --format-new-volume   # first run only
```

Keep the settings file to these names. `bootstrap.sh` refuses to run for
real if any `BOX_TEST_*` variable is set (those are hooks for its dry-run
tests).

The data-volume step formats **only** a disk that matches `DATA_VOLUME_ID`'s
NVMe serial, is a whole disk, isn't the root disk, has no partitions, holders
or mounts, has no signatures (`wipefs -n` is empty and `blkid -p` exits 2),
**and** you passed `--format-new-volume`. Any other outcome stops the run.
A disk that already has ext4 or xfs is mounted as it is. Leave the flag off
on later runs.

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
the default). The session name is in `/etc/cao-box/box.env` on the box (and
a copy on the data volume, see [Backups and restore](#backups-and-restore)).

### 8. Start the services, log in, check

From your admin shell (the Session Manager shell; `<you>` has no sudo, and
shouldn't):

```bash
sudo systemctl start herdr-cao.service
sudo systemctl start cao-server.service     # waits for herdr's socket; fails if it never appears
sudo systemctl start cao-backup.timer       # bootstrap only enables it; a reboot would start it too
# cao-server takes a few seconds to listen after systemd says it started:
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:9889/health && break; sleep 1; done
sudo -u <you> -i /usr/local/lib/cao-box/box-resume.sh   # as <you>: herdr state is per user
```

Then as `<you>` (over SSH, or in a herdr pane):

```bash
claude          # Bedrock through the instance role, see config/claude-settings.box.json.example
codex login --device-auth
gh auth login
```

Claude Code asks "do you trust this folder?" the first time it opens a
folder, and an agent that CAO launches waits on that question until the
launch times out. On your laptop your folders are already trusted; on a fresh
box they aren't. So trust each folder you'll launch agents in, once, as
`<you>`: either open `claude` there and answer yes, or record it directly
(replace the paths with your own):

```bash
for dir in "$HOME" "$HOME/work/my-repo"; do
  jq --arg d "$dir" '.projects[$d].hasTrustDialogAccepted = true' ~/.claude.json > ~/.claude.json.tmp \
    && chmod 600 ~/.claude.json.tmp && mv ~/.claude.json.tmp ~/.claude.json
done
```

`~/.claude.json` exists once `claude` has run once. Only trust folders whose
contents you'd run yourself.

## The phases

Take it one phase at a time. Each phase leaves your laptop setup running, so
you can stop at any point.

**Phase 0: prove herdr over SSH (a toy session).**
Do quick-start steps 1–7, then start only `herdr-cao.service`. From the
laptop's herdr, open a pane on the box and run `top`.
- Reboot the laptop. The box's session should be listed again and `top` still
  running.
- Reboot the box. The unit should come back by itself, the old layout
  should be in `restore-archive/`, and herdr should show **no** old panes.
  This sample assumes herdr starts empty when `session.json` is missing; it
  wasn't verified that herdr never falls back to `session-snapshots/`. If it
  does, stop here and report it.
- Check `kiro-cli` here if you use Kiro: its docs don't confirm a
  linux-arm64 build. Claude Code and Codex work without it.

**Phase 1: CAO and one agent.**
Start `cao-server.service` (installed from the pinned `CAO_REF`). Log in to
Claude Code and launch one agent with `cao launch`.
- Reboot the box. Both units come back, `cao-<instance-id>` is listed, no old
  pane text is replayed, and a fresh launch works.
- CAO can keep the terminal records of sessions from before the reboot (their
  status reads `unknown`). Remove each old session with
  `cao shutdown --session <name>` before you relaunch.
- Run [`bin/box-resume.sh`](bin/box-resume.sh) as `<you>` for the read-only
  checks: `sudo -u <you> -i /usr/local/lib/cao-box/box-resume.sh`.

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
pinned versions, and checks each download before it runs or installs it:

| Tool | Pin | Checksum |
|---|---|---|
| herdr | `HERDR_VERSION` | `HERDR_SHA256`, required |
| gh | `GH_VERSION` | read from the release's published `gh_<version>_checksums.txt` |
| AWS CLI v2 | `AWS_CLI_VERSION` (a versioned zip) | `AWS_CLI_SHA256`. AWS also signs the zip with PGP: verify the signature by hand first if you want more than a checksum. |
| SAM CLI | `SAM_VERSION` | `SAM_SHA256` |
| yazi | `YAZI_VERSION` | `YAZI_SHA256` |
| hx (helix) | `HELIX_VERSION` | `HELIX_SHA256` |
| uv | `UV_VERSION` (the release tarball, not the install script) | `UV_SHA256` |

A missing or wrong checksum stops the run. A checksum taken from the same
release page proves the file wasn't damaged or swapped in transit, not who
made it; that's the trust you'd give the project anyway.
- Docker is optional and off by default (`INSTALL_DOCKER=yes`), from Ubuntu's
  `docker.io` package. Adding the user to the `docker` group makes every
  agent root on the box (`docker run -v /:/host ...`). Prefer rootless Docker
  if you need containers.
- Asset names change now and then. If a download 404s, check the release page
  and adjust the URL in `bootstrap.sh`.
- kiro-cli on linux-arm64 is **not confirmed** by Kiro's docs. Try it in
  phase 0.

### Bedrock in Sydney

There are no in-Region model ids in `ap-southeast-2`. Use the Australia
profiles, `au.anthropic.claude-<model>-5-5`, which route to Sydney and
Melbourne, or the global ones, `global.anthropic.claude-<model>-5-5`, which
cost about 10% less but may run anywhere. `us.` profiles aren't offered in
`ap-southeast-2`. Check which profiles you can use **from your laptop**, with
your own admin credentials. The box's instance role can only invoke models;
it's denied `bedrock:ListInferenceProfiles` on purpose:

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
is never public.

**It must never start its own herdr.** When CAO finds no herdr socket for its
session, it starts a herdr server itself. That herdr would skip the
clean-layout step and the minimal environment, and would live in
cao-server's cgroup, so restarting cao-server would kill every pane. So the
unit has `Requires=` and `After=herdr-cao.service`, and an `ExecStartPre`
gate ([`bin/wait-herdr-socket.sh`](bin/wait-herdr-socket.sh)) that waits for
herdr's socket and fails if it never appears.

The same is true of any `cao` command you run by hand: if herdr is down, it
starts a herdr of its own. So check `systemctl is-active herdr-cao` before
running `cao` commands after a problem. A CAO setting to turn the auto-start
off is a planned follow-up. The dashboard goes through `ssh -N cao-box-dash` and
`http://127.0.0.1:19889`. There's exactly one cao-server per CAO home, so
never point the laptop's CAO at the box's files.

### Secrets and the instance role

- The herdr unit starts with a minimal environment (`HOME`, `USER`, `PATH`,
  `LANG`, `TERM`), and [`bin/herdr-start.sh`](bin/herdr-start.sh) runs herdr
  under `env -i`. Panes inherit the herdr server's environment, so that's all
  an agent sees from systemd.
- The Telegram relay runs as its own system user, `cao-relay`. Its token
  file belongs to that user, mode 0600, so agents can read neither the file
  nor the relay's process environment.
- Agent CLIs log in on the box, with their own device flows. Don't copy login
  files from the laptop.
- **Every agent can reach the instance role** through the instance metadata
  service. So keep the role narrow: Bedrock invoke on the profiles you use,
  `s3:PutObject` and `s3:GetObject` on one prefix, the SSM core policy, and
  nothing for IAM, EC2, Secrets Manager or Parameter Store. IMDSv2 with hop
  limit 1 keeps containers on a bridged network from reaching it (a
  `--network host` container still can). The ARN forms in
  [`iam/`](iam/README.md) are marked "check before use".
- **What's left.** Any agent can read anything the box user owns: other
  CLIs' login files, your kit, memory. Never give the box user `sudo` or the
  `docker` group unless you accept that every agent is root.

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
flags. herdr's own `session-snapshots/` are kept. That herdr then comes up
empty is an assumption that phase 0 checks.

The session name is `cao-<instance-id>`. It's read once from the instance
metadata (IMDSv2) at bootstrap and kept in `.cao-box/box.env` on the data
volume, with a copy in `/etc/cao-box/box.env` for the units. It's never
recomputed, so it doesn't drift, and it survives stop and start and a
whole-box restore.

### Editing

Edit on the box, in herdr panes, with `hx` and `yazi`. VS Code Remote-SSH is
optional (Ubuntu 24.04 meets its requirements). There's no `sshfs` and no
two-way sync, so there are never two copies to reconcile.

### Telegram

Use a second bot just for the box, run its relay as its own user, and set it
up one step at a time: see [`telegram/README.md`](telegram/README.md).

### Data

- CAO's SQLite database lives on the encrypted EBS data volume, **never** on
  EFS or NFS: SQLite locking isn't safe over network file systems.
- Leave CAO's journal mode as it is.
- Every night, the backup job runs `sqlite3 .backup`, checks the copy with
  `PRAGMA integrity_check`, and uploads it to a versioned S3 prefix.
- Add EBS snapshots of the data volume (Data Lifecycle Manager, for example 7
  daily and 4 weekly) for whole-box restores.

### The home path

`bootstrap.sh` creates the user with `BOX_HOME` as their real home, uid and
gid `BOX_UID`, and mounts the data volume at its parent (`DATA_MOUNT`, for
example `/Users`). Set
`BOX_HOME` to your laptop's home path, and absolute paths in your kits keep
working.

- **Why not a symlink?** `/Users/<you> -> /home/<you>` lets literal paths
  work. But git worktrees, path guards and anything that resolves real paths
  sees `/home/...` and may disagree.
- **No kit paths to keep?** Use the ordinary layout instead:
  `BOX_HOME=/home/<you> DATA_MOUNT=/home/<you>`. That mounts the volume as the
  home itself and leaves the default `ubuntu` user alone. `bootstrap.sh`
  gives the freshly mounted folder to `<you>` and copies `/etc/skel` into it.
  In this layout the volume has no root-only folder, so `box.env` lives only
  in `/etc/cao-box/`, and nothing on the volume is ever read for it. Copy it
  by hand before a whole-box restore.

## Backups and restore

[`backup.sh`](backup.sh) runs nightly from `cao-backup.timer`. It keeps 14
local copies in `~/backups/cao/` (folder 0700, files 0600) and uploads each
one to `BACKUP_S3_URI`, asking for SSE-KMS with `BACKUP_KMS_KEY_ARN`. The
backups hold message text: inbox, handoff results, launch records and
memory metadata.

```bash
# A drill, as <you>: restore into a temp folder, check it, touch nothing live. Monthly.
/usr/local/lib/cao-box/restore.sh --drill s3://<backup-bucket>/cao-box/cao-<stamp>.db.gz

# The real thing, as root from the admin shell (<you> has no sudo):
sudo /usr/local/lib/cao-box/restore.sh --live /Users/<you>/backups/cao/cao-<stamp>.db.gz --yes
```

- A drill refuses a `--to` folder inside the live CAO home, or any folder
  that isn't empty.
- `--live` runs only as root. It reads the box user from
  `/etc/cao-box/box-user.env` (written by `bootstrap.sh`, root's), finds the
  database in that user's home, and refuses if the database isn't the box
  user's.
  - Root does only that read and `systemctl`. Every file operation in the
    box user's tree runs **as the box user**: reading the backup, keeping a
    copy and swapping in the restored database. It refuses any symlink on
    the database's path, on the backup, and on the files it creates. So a
    link an agent plants can't make root copy a root-only file.
  - It checks the backup, then stops cao-server.
  - It keeps the current database as `<db>.pre-restore-<stamp>`.
  - It puts the backup in its place, in the live database's mode.
  - `--dry-run` runs every check and prints the plan without changing
    anything.
  - It starts cao-server, waits for `/health`, and checks that the live
    database has the backup's tables.
  - It refuses to write over a database that still has a `-journal`, `-wal`
    or `-shm` file next to it.
  - It doesn't touch herdr, so running panes keep running.
- Don't run `cao` CLI commands during a live restore: some open the database
  directly.

**Whole-box restore:**
1. Make a volume from a snapshot and attach it to a fresh instance.
2. Run `bootstrap.sh` with the same settings, the same `BOX_UID` and the new
   `DATA_VOLUME_ID`, without `--format-new-volume`.
3. It finds the filesystem and mounts it, checks the home belongs to
   `BOX_UID`, and reads `.cao-box/box.env` from the volume. The folder must
   be root's, and the file must be exactly the two session lines with equal
   values. It then writes those two lines afresh into `/etc/cao-box/box.env`.
   So the session name stays the old one, and it matches CAO's `settings.json`
   and the laptop's saved machine. It stops if `settings.json` and `box.env`
   disagree.

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
No. The panes belong to herdr's own unit, and cao-server reconnects to
herdr's socket. That holds because cao-server never starts a herdr of its
own: its start waits for herdr-cao's socket and fails without it (see
[cao-server](#cao-server)). Restarting herdr does end the panes, and because
of `Requires=` it restarts cao-server too.

**Can I use Docker on the box?**
Yes: `INSTALL_DOCKER=yes`. But the `docker` group is root-equivalent, so every
agent becomes root on the box. Rootless Docker avoids that.

**How do I turn on `CAO_AUTH_LOCAL_TOKEN`?**
Put it in a root-owned 0600 file, for example `/etc/cao-box/cao-auth.env`.
Then add it with `sudo systemctl edit cao-server`:

```ini
[Service]
EnvironmentFile=/etc/cao-box/cao-auth.env
```

It's all-or-nothing, and CAO passes it to every agent pane. If the box user
is in the `docker` group, agents can read the file too. Read
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
aws iam delete-role --role-name <cao-box-role>
```

Then remove the `cao-box` entries from the laptop's `~/.ssh/config`, run
`herdr machine remove cao-box` (or remove it from herdr's sidebar), and send
`/deletebot` to BotFather.

## Files

| File | What it does |
|---|---|
| [`bootstrap.sh`](bootstrap.sh) | Sets up the box. Idempotent, with `--dry-run`. Ubuntu 24.04 arm64 only. Formats only a verified blank data volume, and only with `--format-new-volume`. Checks every download's sha256. Enables units but never starts them. |
| [`backup.sh`](backup.sh), [`restore.sh`](restore.sh) | Nightly backup; a restore drill (as the box user) and a live restore (root only). |
| [`bin/box-session-name.sh`](bin/box-session-name.sh) | Prints `cao-<instance-id>` from IMDSv2. |
| [`bin/herdr-start.sh`](bin/herdr-start.sh), [`bin/herdr-clean-layout.sh`](bin/herdr-clean-layout.sh) | Start herdr with the minimal environment, after moving the saved layout aside. |
| [`bin/wait-herdr-socket.sh`](bin/wait-herdr-socket.sh) | cao-server's start gate: waits for herdr's socket, fails without it. |
| [`bin/lib-cao-box.sh`](bin/lib-cao-box.sh) | Shared helpers: the checksum check, the strict two-line `box.env` parser, and the root-only ownership checks. |
| [`bin/box-resume.sh`](bin/box-resume.sh) | Read-only checks after a reboot. |
| [`bin/resource-probe-linux.sh`](bin/resource-probe-linux.sh), [`resource-gate-linux.md`](resource-gate-linux.md) | A Linux memory-pressure probe, and why a macOS-only gate stalls. |
| [`systemd/`](systemd/) | `herdr-cao.service`, `cao-server.service`, `cao-backup.service` and `.timer`, and the optional relay unit. |
| [`config/`](config/) | Box versions of the herdr, CAO and Claude Code settings (Sydney model ids). |
| [`iam/`](iam/README.md) | A narrow instance-role policy and a backup-bucket policy, with what to check before use. |
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
- `bootstrap.sh --dry-run` against stubbed system commands, through every
  step: the data-volume guard (blank, partition table only, blkid 4 and 8,
  the root disk, a wrong serial, partitions, holders, no flag), user and
  home ownership, pins and checksums, and `systemctl enable` without
  `--now`;
- a `--root` dry run that must change nothing, a real `--root` run (file
  modes, no IMDS call), and the refusal on the wrong OS;
- the cao-server socket gate;
- the session name from a mocked IMDSv2;
- a backup and restore round trip with `integrity_check`;
- the clean-layout step and the resource probe;
- a leak grep (no local paths, account numbers or `us.` model ids).
