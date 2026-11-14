# Set up a fresh CAO box on EC2: a runbook an agent can follow

This page is for the case where a user asks their agent to "set up the remote EC2 box for me".
It is written so the agent can follow it step by step, and plain enough for the user to read
along. It sets up **one fresh box**. People run CAO in very different ways, so moving an
existing conductor or kit onto the box (a cutover) is out of scope here.

Everything below comes from this sample's [`README.md`](README.md), [`bootstrap.sh`](bootstrap.sh)
and the files next to them. Each step says where. A step marked **check before use** is one this
sample can't check offline, or one the README doesn't cover; check it against the current AWS or
tool docs before you rely on it.

**Why a runbook.** Most of the ways this setup goes wrong are quiet: a box type that isn't
offered in the zone, model ids that don't exist in the region, a checkout that root refuses to
run from, a herdr version that doesn't match the laptop, or an agent stuck on a trust prompt.
Each step ends with a check, so a failure shows up at that step instead of three steps later.

## Contents

- [Rules for the agent](#rules-for-the-agent)
- [The steps at a glance](#the-steps-at-a-glance)
- [0. A new laptop: install the tools](#0-a-new-laptop-install-the-tools)
- [1. Collect the inputs](#1-collect-the-inputs)
- [2. Laptop preflight](#2-laptop-preflight)
- [3. Create the AWS resources](#3-create-the-aws-resources)
- [4. SSH over SSM, as the admin user](#4-ssh-over-ssm-as-the-admin-user)
- [5. On the box: checkout, settings, bootstrap](#5-on-the-box-checkout-settings-bootstrap)
- [6. Start the services and connect herdr](#6-start-the-services-and-connect-herdr)
  - [6e. Attaching to the remote session](#6e-attaching-to-the-remote-session)
- [7. The user's logins](#7-the-users-logins)
- [8. The first agent](#8-the-first-agent)
- [9. Optional: the CAO TUI](#9-optional-the-cao-tui)
- [10. The reboot drill](#10-the-reboot-drill)
- [11. Optional: backups to S3](#11-optional-backups-to-s3)
- [12. Troubleshooting](#12-troubleshooting)
- [13. Teardown and cost](#13-teardown-and-cost)
- [14. The final report](#14-the-final-report)
- [15. Daily use: stop, start, re-attach](#15-daily-use-stop-start-re-attach)
- [16. The end-to-end check on a new machine](#16-the-end-to-end-check-on-a-new-machine)

## Rules for the agent

1. **Get the user's OK before you create anything in AWS.** Every resource costs money. Before
   step 3, show the user the instance type and its hourly on-demand price in the chosen region,
   plus the volumes, and wait for a clear yes. This sample quotes no prices
   ([`README.md`](README.md#teardown-and-cost)): take the number from the EC2 and EBS pricing
   pages or the AWS Price List API, say where it came from, and never make one up.
2. **Logins belong to the user.** `gh auth login`, the Claude Code, Codex and Kiro logins, and
   Bedrock model access are the user's steps. Hand them the exact command; don't type at their
   prompts. You never handle a token.
3. **Never print secrets or env values.** Never run a bare `env` or `printenv`, never echo a
   variable's value, and never `cat` a file that holds a key or token. Report variable
   **names** only. To list what a pane sees, use `env | cut -d= -f1 | sort`
   ([`README.md`](README.md#checking-it-works), "No secrets in panes").
4. **Ask one question at a time**, and write each answer into the answers file (step 1) as you
   go. Don't ask again for something it already has.
5. **No surprise root.** Never pipe a download into a shell as root (`curl ... | sh`). Root runs
   only `bootstrap.sh`, `systemctl`, `apt-get` and the few commands this page names.
6. **The checkout lives outside the box user's home, owned by root.** Use
   `/opt/cao-box-src`, and pass the settings with `--settings`. As root, `bootstrap.sh` refuses
   any other place, because every agent can write in the box user's home
   ([`bootstrap.sh`](bootstrap.sh) header; [`README.md`](README.md), "Secrets and the instance
   role").
7. **Stop at the first failed check** and tell the user. Don't guess past a refusal from
   `bootstrap.sh`: its messages say what to change.
8. **Never give the box user `sudo`**, and never add them to the `docker` group without the
   user's explicit OK. Either makes every agent root on the box ([`README.md`](README.md),
   "Secrets and the instance role", "What's left").

**Placeholders.** `<...>` marks a value you fill in from the answers file: for example
`<region>`, `<instance-id>`, `<vol-id>`, `<you>` (the box user). Add `--profile <aws-profile>`
to every `aws` command on the laptop; it is left out below to keep the lines short.

**User steps** are marked like this:

> **User step.** Hand this to the user and wait for them to say it's done.

## The steps at a glance

0. A new laptop: install the tools, and the user signs in to AWS.
1. Collect the inputs.
2. Laptop preflight.
3. Create the AWS resources (after the user's OK).
4. SSH over SSM, as the admin user.
5. On the box: the checkout, the settings file, `bootstrap.sh`, the user's key.
6. Start the services, check them, and connect herdr.
7. The user's logins.
8. The first agent.
9. Optional: the CAO TUI.
10. The reboot drill.
11. Optional: backups to S3.
12. Troubleshooting.
13. Teardown and cost.
14. The final report.
15. Daily use: stop, start, re-attach.
16. The end-to-end check on a new machine.

## 0. A new laptop: install the tools

Skip this step on a laptop that already passes step 2. On a brand-new laptop, assume **nothing**
is installed. For each tool: run its check first, install only if the check fails, then run
the check again. The install commands are **check before use**: this sample can't test them
offline, and download links and package names change. Take them from each tool's own install
page, linked below. Installs that need `sudo` on the laptop, or a package manager the user
hasn't chosen, are the user's call: show them the command and let them run it (rule 5 applies
here too: never pipe a download into a shell).

**0a. A package manager (macOS).** Homebrew is the usual one; it isn't on a new Mac. Linux
laptops use their own (`apt-get` on Ubuntu or Debian, below).

```bash
brew --version
```

> **User step.** If it's missing and the user wants it, they install Homebrew from
> [brew.sh](https://brew.sh), reading the script before running it. Without Homebrew, use each
> tool's own installer below.

**0b. `ssh`, `ssh-keygen` and `git`.**

```bash
command -v ssh ssh-keygen git
```

**Check:** three paths. macOS ships `ssh` and `ssh-keygen`; `git` comes with Apple's command
line tools (`xcode-select --install`, a user step: it opens a dialog) or `brew install git`. On
Ubuntu or Debian: `sudo apt-get install -y openssh-client git` (**check before use**).

**0c. The AWS CLI v2** ([install page](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)).

```bash
aws --version          # must say aws-cli/2.
```

If it's missing (**check before use**, take the current commands from the install page):

- macOS: AWS's `.pkg` installer from that page, or `brew install awscli`;
- Linux: AWS's zip for the laptop's CPU (`uname -m` says `x86_64` or `aarch64`), unpacked, then
  `sudo ./aws/install`. The install page shows how to check the zip's PGP signature first.

An `aws-cli/1.` version, or an `aws` from a distribution's Python package, is too old: install
v2 and make sure `command -v aws` finds it first.

**0d. The Session Manager plugin** ([install page](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)).
SSH to the box runs inside a Session Manager session (step 4), so this is required.

```bash
session-manager-plugin --version
```

If it's missing (**check before use**): on macOS, the page's signed `.pkg` for the Mac's CPU
(or `brew install --cask session-manager-plugin`); on Ubuntu or Debian, the page's `.deb`, then
`sudo dpkg -i <file>.deb`. An old plugin can make SSH over SSM fail (step 6f): if the check
prints a version, compare it with the page's release notes.

**0e. herdr, pinned.** The laptop and the box must run the **same** herdr version
([`README.md`](README.md), "herdr"); a mismatch has broken every CAO call before. Pick the
version for both now, before step 2c records it:

- herdr's home is [herdr.dev](https://herdr.dev); its releases are on GitHub at
  [`herdrdev/herdr`](https://github.com/herdrdev/herdr/releases);
- the box needs that release's `herdr-linux-aarch64` asset. It is published for v0.9.3 and
  v0.9.5 (README, "herdr"); for any other version, check the release lists it;
- the laptop needs that release's asset for its own OS and CPU (**check before use**: read the
  asset names on the release page).

With `gh` (0f) you can list the versions and assets:

```bash
gh api repos/herdrdev/herdr/releases --jq '.[].tag_name' | head -n 10
gh api repos/herdrdev/herdr/releases/tags/<herdr-version> --jq '.assets[] | .name + " " + .digest'
```

Install the laptop's asset into a folder on the user's `PATH`, for example `~/.local/bin`, and
check its sha256 against the release's `digest` before you run it (the same way step 5b does
for the box; **check before use**):

```bash
shasum -a 256 <downloaded-herdr-asset>      # Linux: sha256sum; must equal the digest without "sha256:"
install -m 0755 <downloaded-herdr-asset> ~/.local/bin/herdr
herdr --version
```

**Check:** `herdr --version` prints the version you picked. A package manager's herdr (for
example Homebrew's) installs whatever version is current, which may not be the box's: use it
only if `herdr --version` matches. Don't run `herdr update` or upgrade herdr's package on just
one side; to move, pin the new version on both (step 12, the herdr row).

**0f. Optional: `gh`** ([cli.github.com](https://cli.github.com)). Step 5b and 0e use it to read
release digests; without it, read the same `digest` field from
`https://api.github.com/repos/<owner>/<repo>/releases/tags/<tag>` with `curl`.

```bash
gh --version
```

If it's missing (**check before use**): `brew install gh` on macOS; on Linux, GitHub's own
package repository, as cli.github.com describes. Reading public releases needs no login; if
the user wants to log in on the laptop, `gh auth login` is their step (rule 2).

**0g. An SSH key pair.** Step 1 asks which key to use.

```bash
ls ~/.ssh/*.pub 2>/dev/null
```

> **User step.** If there is none, or the user wants one just for the box, they make it, and
> choose its passphrase themselves:
>
> ```bash
> ssh-keygen -t ed25519 -f ~/.ssh/<your-key> -C cao-box
> ```

**Check:** `~/.ssh/<your-key>.pub` exists. Only the `.pub` half ever leaves the laptop.

**0h. The user signs in to AWS.** This is the **user's** step: you never see, type or store an
access key or a password.

> **User step.** The user signs in with the AWS CLI, in the way their organization uses, for
> example IAM Identity Center:
>
> ```bash
> aws configure sso                          # once: makes a named profile
> aws sso login --profile <aws-profile>      # again whenever the sign-in expires
> ```
>
> or an existing named profile. They tell you the profile's **name**; step 1 records it.

**Check** (the account id is printed masked, so it never lands in a log):

```bash
aws sts get-caller-identity --profile <aws-profile> --query Account --output text \
  | sed -E 's/^(..).*(....)$/\1******\2/'
```

It prints something like `12******3456`. An error means the sign-in failed or expired: the
user runs `aws sso login` again.

## 1. Collect the inputs

Make an answers file on the laptop, in a folder of the user's choosing, readable only by them:

```bash
install -m 0600 /dev/null <answers-folder>/cao-box-answers.env
```

It holds names and choices only, never a secret. Ask for each item below, one question at a
time, and add a `KEY=value` line for each answer.

| Answer | What to ask | Default and limits (source) |
|---|---|---|
| `REGION` | Which AWS region? | `ap-southeast-2`. The sample's files are written for Sydney: [`ssh/config.snippet`](ssh/config.snippet), the IAM ARNs and the [`config/`](config/) model ids all name it. Another region means editing those too (**check before use**). |
| `INSTANCE_TYPE` | Which box size? | `m8g.4xlarge` (Graviton4, 16 vCPU, 64 GiB). Alternatives: `c8g.4xlarge` (32 GiB), `r8g.4xlarge` (128 GiB), and `m7g.4xlarge` (Graviton3) as the fallback. Graviton, 16 vCPU or more ([`README.md`](README.md), "The box"). |
| `AWS_PROFILE_NAME` | Which AWS CLI profile on the laptop may create these resources? | No default; on a new laptop, the one from step 0h. Store the profile's **name** only. |
| `VPC_ID`, `SUBNET_ID` | Which VPC and subnet? | The subnet needs outbound internet (a NAT gateway, or a public IP with no inbound rules) or VPC endpoints for SSM, Bedrock and S3 (README quick start, step 5). |
| `BOX_USER` | The Linux user name for the agents? | A plain Linux name: `^[a-z_][a-z0-9_-]{0,31}$` (`bootstrap.sh` step 0). |
| `BOX_UID` | Its uid? | A number from 1000 to 59999. A macOS uid such as 501 is refused. Pick one and never change it: rebuilds must reuse it so the data volume's files keep their owner (`bootstrap.sh` header and step 0). |
| `BOX_HOME` | The home path? | The laptop's home path (`/Users/<you>`), so absolute paths in kits keep working, or the ordinary `/home/<you>`. With `/home/<you>`, also set `DATA_MOUNT=/home/<you>` ([`README.md`](README.md), "The home path"). |
| `INSTALL_DOCKER` | Docker on the box? | `no`. Say this before they answer: with `yes`, the box user joins the `docker` group, and that makes every agent root on the box (`docker run -v /:/host ...`). Rootless Docker avoids that ([`README.md`](README.md), "Tools on linux-arm64"). |
| `BACKUPS` | Nightly backups to S3? | Optional; see step 11. The backups hold message text, so treat the bucket like the box ([`README.md`](README.md), "Backups and restore"). |
| `SSH_KEY` | Which SSH key pair on the laptop? | A path under `~/.ssh/` (step 0g). Only the **public** half ever goes to AWS or the box (README, "Before you start"). |

**Check:** the answers file has a line for every row, and `stat -f %Lp` (macOS) or
`stat -c %a` (Linux) on it prints `600`. Read the file back to the user and ask them to confirm
it.

## 2. Laptop preflight

Run each check. Stop at the first one that fails.

**2a. The AWS CLI and the Session Manager add-on.**

```bash
aws --version                  # must say aws-cli/2.
session-manager-plugin --version
```

**Check:** both print a version. If the second is missing, the user installs AWS's Session
Manager add-on for the AWS CLI (README, "Before you start", links the install page).

**2b. The credentials work.**

```bash
aws sts get-caller-identity --query Arn --output text
```

**Check:** it prints an ARN. Don't print the account number to anyone but the user.

**2c. The laptop's herdr version.** The box must run the same one
([`README.md`](README.md), "herdr").

```bash
herdr --version
```

**Check:** it prints a version, and you write it into the answers file as `HERDR_VERSION` in the
form `vX.Y.Z` (that is the form `bootstrap.sh` step 0 accepts). `herdr-linux-aarch64` is
published for v0.9.3 and v0.9.5 (README, "herdr"); for any other version, check the herdr
release page has that asset (step 5c).

**2d. The box type is offered in the region** (README quick start, step 1):

```bash
aws ec2 describe-instance-type-offerings --region <region> \
  --location-type availability-zone \
  --filters Name=instance-type,Values=<instance-type>,m7g.4xlarge \
  --query 'InstanceTypeOfferings[].[InstanceType,Location]' --output table
```

**Check:** the chosen type is listed in the subnet's availability zone. If not, offer the user
`m7g.4xlarge` and record the new answer.

**2e. The Bedrock model ids that exist in the region** (README, "Bedrock in Sydney"). Run this
from the laptop: the box's role is denied `bedrock:ListInferenceProfiles` on purpose.

```bash
aws bedrock list-inference-profiles --region <region> \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'anthropic.claude')].inferenceProfileId" \
  --output text
```

**Check:** the list has the ids you'll use. In `ap-southeast-2` there are no in-region ids:
use the `au.` profiles (Sydney and Melbourne) or the `global.` ones (cheaper, but may run
anywhere); `us.` profiles aren't offered there. Write the chosen ids into the answers file. If
the list is empty or a model is missing, the user may need to turn on model access.

> **User step.** If a model the user wants isn't usable yet, they turn on Bedrock model access
> for it in the console. You don't do this for them.

**2f. The Ubuntu image** (README quick start, step 2):

```bash
aws ssm get-parameter --region <region> \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
  --query Parameter.Value --output text
```

**Check:** it prints an `ami-...` id. Record it as `AMI_ID`. `bootstrap.sh` runs only on
Ubuntu 24.04 arm64 (README, "The OS").

## 3. Create the AWS resources

**Stop here and get the user's OK** (rule 1). Show them: the instance type and its hourly price
in the region, the 50 GiB root and 200 GiB data volumes (gp3), and that the data volume
**survives termination** and keeps costing until it is deleted on purpose (step 13). Record the
OK, with the time, in the answers file.

Write down every id you create in the answers file as you go; step 13 and step 14 need them.

**3a. The instance role** (README quick start, step 3; [`iam/README.md`](iam/README.md)).

Copy [`iam/instance-role-policy.json.example`](iam/instance-role-policy.json.example) to a
working file outside the checkout, and fill it in:

- `<account-id>`, from step 2b;
- keep only the model ids the user picked in step 2e;
- with backups (step 11): `<backup-bucket>` and `<backup-key-id>`. Without backups: delete the
  `WriteAndReadBackupsOnly` and `BackupKey` statements;
- in another region: change the region in every ARN (**check before use**).

The ARN forms in this file are marked **check before use** in [`iam/README.md`](iam/README.md):
they were not tested against a live account. Show the filled-in policy to the user and get
their OK before you apply it.

```bash
aws iam create-role --role-name <cao-box-role> \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam put-role-policy --role-name <cao-box-role> --policy-name cao-box \
  --policy-document file://<filled-policy.json>
aws iam create-instance-profile --instance-profile-name <cao-box-profile>
aws iam add-role-to-instance-profile --instance-profile-name <cao-box-profile> --role-name <cao-box-role>
```

**Check:**

```bash
aws iam get-role-policy --role-name <cao-box-role> --policy-name cao-box --query PolicyName --output text
aws iam get-instance-profile --instance-profile-name <cao-box-profile> \
  --query 'InstanceProfile.Roles[].RoleName' --output text
```

The first prints `cao-box`, the second the role name. The role has no managed policies: the
inline one replaces `AmazonSSMManagedInstanceCore`, which would let every agent read Parameter
Store (README quick start, step 3).

**3b. A security group with no inbound rules** (README quick start, step 5):

```bash
aws ec2 create-security-group --region <region> --vpc-id <vpc-id> \
  --group-name <cao-box-sg> --description "CAO box: no inbound"
```

**Check:** it prints a `GroupId`; record it as `SG_ID`. Then:

```bash
aws ec2 describe-security-groups --region <region> --group-ids <sg-id> \
  --query 'SecurityGroups[].IpPermissions'
```

It prints `[[]]` (README, "Checking it works", "No open ports"). Add no inbound rule.

**3c. The SSH key in AWS** (for the admin user's first SSH, step 4). The README's own first
login uses `aws ssm start-session` instead; this key is an addition, **check before use**.
Only the public half is sent:

```bash
aws ec2 import-key-pair --region <region> --key-name <cao-box-key> \
  --public-key-material fileb://~/.ssh/<your-key>.pub
```

**Check:** `aws ec2 describe-key-pairs --region <region> --key-names <cao-box-key> --query 'KeyPairs[].KeyName' --output text`
prints the name.

**3d. Launch the box** (README quick start, step 5). IMDSv2 is required with a hop limit of 1;
both volumes are encrypted; the data volume has `DeleteOnTermination` false.

```bash
aws ec2 run-instances --region <region> \
  --image-id <ami-id> --instance-type <instance-type> \
  --subnet-id <subnet-id> --security-group-ids <sg-id> \
  --iam-instance-profile Name=<cao-box-profile> \
  --key-name <cao-box-key> \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled \
  --block-device-mappings '[
    {"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":50,"VolumeType":"gp3","Encrypted":true}},
    {"DeviceName":"/dev/sdf","Ebs":{"VolumeSize":200,"VolumeType":"gp3","Encrypted":true,"DeleteOnTermination":false}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=cao-box}]' \
  --query 'Instances[].InstanceId' --output text
```

(`--key-name` and `--query` are additions to the README's command, from step 3c.)

**Check:** it prints an instance id; record it as `INSTANCE_ID`. Then find the data volume's id
(record it as `DATA_VOLUME_ID`; `bootstrap.sh` finds the disk by this id, never by device name):

```bash
aws ec2 describe-instances --region <region> --instance-ids <instance-id> \
  --query 'Reservations[].Instances[].BlockDeviceMappings[?DeviceName==`/dev/sdf`].Ebs.VolumeId' --output text
aws ec2 describe-instances --region <region> --instance-ids <instance-id> \
  --query 'Reservations[].Instances[].MetadataOptions.[HttpTokens,HttpPutResponseHopLimit]' --output text
```

The first prints a `vol-...` id; the second prints `required` and `1`.

**3e. Wait for SSM to show Online** (**check before use**: these two commands aren't in the
README):

```bash
aws ec2 wait instance-running --region <region> --instance-ids <instance-id>
aws ssm describe-instance-information --region <region> \
  --filters Key=InstanceIds,Values=<instance-id> \
  --query 'InstanceInformationList[].PingStatus' --output text
```

**Check:** the second command prints `Online`. It can take a few minutes after the instance is
running; ask again every 30 seconds, for up to 10 minutes. If it never does, check the subnet's
outbound route (step 1) and the `SessionManagerOnly` statement in the role
([`iam/README.md`](iam/README.md) notes newer SSM Agents may need less).

## 4. SSH over SSM, as the admin user

Add the two hosts from [`ssh/config.snippet`](ssh/config.snippet) to the laptop's
`~/.ssh/config`, plus a third one for the admin user, so you can work as admin before the box
user exists. Show the user the lines before you add them, and back the file up first:

```bash
cp ~/.ssh/config ~/.ssh/config.bak.$(date +%s) 2>/dev/null || true
```

Fill in `<instance-id>`, `<box-user>` and `<your-key>`. In another region, change
`--region ap-southeast-2` in each `ProxyCommand`. If the user's AWS profile isn't the default,
add `--profile <aws-profile>` to each `ProxyCommand` (**check before use**). The admin entry is
the `cao-box` entry with a different user. Ubuntu's images log in as `ubuntu`
(**check before use**):

```text
Host cao-box-admin
  HostName <instance-id>
  User ubuntu
  IdentityFile ~/.ssh/<your-key>
  ProxyCommand sh -c "aws ssm start-session --region <region> --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p'"
  ForwardAgent no
```

**Check:**

```bash
ssh cao-box-admin 'uname -m; . /etc/os-release; echo "$ID $VERSION_ID"'
```

It prints `aarch64` and `ubuntu 24.04`. The first connection asks to confirm the host key: that
is the user's call.

If SSH fails but SSM is Online, the fallback admin shell is
`aws ssm start-session --region <region> --target <instance-id>` (README quick start, step 6).

## 5. On the box: checkout, settings, bootstrap

All of step 5 runs as the admin user (`ssh cao-box-admin`), with `sudo`.

**5a. The checkout, owned by root, outside the home** (README quick start, step 6):

```bash
sudo install -d -m 0755 /opt/cao-box-src
sudo git clone https://github.com/awslabs/cli-agent-orchestrator.git /opt/cao-box-src/cli-agent-orchestrator
```

The sample must be in that checkout. While the sample is still in an open pull request, fetch
and check out the commit that has it (**check before use**):

```bash
sudo git -C /opt/cao-box-src/cli-agent-orchestrator fetch origin <sample-ref>
sudo git -C /opt/cao-box-src/cli-agent-orchestrator checkout --detach <sample-commit>
```

**Check:** the folder is root's, and nothing in it is writable by group or others. That is the
rule `bootstrap.sh` enforces as root (its `safe_checkout`). This prints nothing:

```bash
sudo find /opt/cao-box-src ! -type l \( ! -user root -o -perm -g+w -o -perm -o+w \) -print | head -n 1
ls /opt/cao-box-src/cli-agent-orchestrator/examples/remote-ec2/bootstrap.sh
```

**5b. Find each tool's pin and sha256.** `bootstrap.sh` checks every download against a sha256,
and a missing or wrong one stops the run (README, "Tools on linux-arm64"). It asks for a pin
only for a tool the box doesn't already have, and a dry run (step 5d) names any that are
missing. Work these out on the laptop:

| Setting | Asset `bootstrap.sh` downloads (step 4) | Where to read the sha256 |
|---|---|---|
| `HERDR_VERSION`, `HERDR_SHA256` | `herdr-linux-aarch64` from the herdrdev/herdr release `<HERDR_VERSION>` | The GitHub release API (below). |
| `GH_VERSION` (default in the script) | `gh_<version>_linux_arm64.deb` | Not needed: `bootstrap.sh` reads gh's published `gh_<version>_checksums.txt` itself. |
| `YAZI_VERSION` (default in the script), `YAZI_SHA256` | `yazi-aarch64-unknown-linux-gnu.zip` from sxyazi/yazi | The GitHub release API. |
| `HELIX_VERSION` (default in the script), `HELIX_SHA256` | `helix-<version>-aarch64-linux.tar.xz` from helix-editor/helix | The GitHub release API. |
| `UV_VERSION`, `UV_SHA256` | `uv-aarch64-unknown-linux-gnu.tar.gz` from astral-sh/uv | The GitHub release API. |
| `SAM_VERSION`, `SAM_SHA256` | `aws-sam-cli-linux-arm64.zip` from aws/aws-sam-cli, tag `v<version>` | The GitHub release API. |
| `AWS_CLI_VERSION`, `AWS_CLI_SHA256` | `awscli-exe-linux-aarch64-<version>.zip` from awscli.amazonaws.com | Not on GitHub. AWS signs the zip with PGP; verify the signature as AWS's install docs say, then take its sha256 (**check before use**). |
| `CAO_REF` | CAO itself, through `uv tool install git+<repo>@<CAO_REF>` (`bootstrap.sh` step 5) | A full commit sha is best (`bootstrap.sh` header). |

GitHub's release API lists a `digest` for each asset, in the form `sha256:<hex>`
(**check before use**: older releases may have none). For example:

```bash
gh api repos/herdrdev/herdr/releases/tags/<HERDR_VERSION> \
  --jq '.assets[] | select(.name == "herdr-linux-aarch64") | .digest'
```

Use the same shape for the other GitHub tools, with their repository, tag and asset name from
the table. Strip the `sha256:` prefix; `bootstrap.sh` wants 64 lowercase hex characters
(step 0 checks `HERDR_SHA256` this way). If a release has no `digest`, use the checksum file the
release publishes; if there is none, tell the user and ask before you go on. A checksum from the
same release page proves the file wasn't damaged or swapped in transit, not who made it
(README, "Tools on linux-arm64").

**Check:** each sha256 is 64 hex characters, and each asset name matches the table. If a
download later 404s, the asset name changed: check the release page (README, "Tools on
linux-arm64").

**5c. Write the settings file** (README quick start, step 6). It must be root's, mode 0600, not a
symlink, and outside `BOX_HOME`. `bootstrap.sh` parses it one `KEY=VALUE` line at a time, never
sources it, and refuses any name that isn't one of its settings:

```bash
sudo install -m 0600 /dev/null /opt/cao-box-src/box-settings.env
sudo tee /opt/cao-box-src/box-settings.env > /dev/null <<'EOF'
BOX_USER=<you>
BOX_UID=<uid>
BOX_HOME=<box-home>
DATA_VOLUME_ID=<vol-id>
HERDR_VERSION=<herdr-version>
HERDR_SHA256=<sha256>
CAO_REF=<cao-commit-sha>
INSTALL_DOCKER=<yes-or-no>
UV_VERSION=<version>
UV_SHA256=<sha256>
YAZI_SHA256=<sha256>
HELIX_SHA256=<sha256>
SAM_VERSION=<version>
SAM_SHA256=<sha256>
AWS_CLI_VERSION=<version>
AWS_CLI_SHA256=<sha256>
EOF
```

- With `BOX_HOME=/home/<you>`, add `DATA_MOUNT=/home/<you>` ([`README.md`](README.md), "The home
  path").
- Leave out `BACKUP_S3_URI` and `BACKUP_KMS_KEY_ARN` until step 11. `bootstrap.sh` wants both or
  neither (step 0).
- Leave out a tool's pins if the dry run says that tool is already present.
- Values may use only letters, digits and `. _ : / @ + , = -`; no quotes, no spaces, no inline
  `<...>` left over (step 0 of `bootstrap.sh`; the tests refuse a placeholder).

**Check:**

```bash
sudo stat -c '%U %a' /opt/cao-box-src/box-settings.env     # root 600
sudo grep -c '<' /opt/cao-box-src/box-settings.env          # 0: no placeholder left
```

**5d. The dry run, then the real run** (README quick start, step 6). A fresh data volume is
blank, so the first run needs `--format-new-volume`:

```bash
cd /opt/cao-box-src/cli-agent-orchestrator/examples/remote-ec2
sudo bash bootstrap.sh --settings /opt/cao-box-src/box-settings.env --dry-run --format-new-volume
```

**Check:** it ends with the `done` step and no `ERROR:` line. Read the plan to the user,
especially the format step for `<vol-id>`. It formats **only** a disk that matches the volume's
NVMe serial, is a whole disk, isn't the root disk, and has no partitions, holders, mounts or
signatures (README quick start, step 6). Then, with the user's OK:

```bash
sudo bash bootstrap.sh --settings /opt/cao-box-src/box-settings.env --format-new-volume
```

**Check:** it ends with `Next steps (you run these; see README.md):`. Then:

```bash
findmnt -no SOURCE,TARGET <data-mount>                         # the data volume, at BOX_HOME's parent or DATA_MOUNT
id <you>                                                       # uid=<uid> gid=<uid>
cat /etc/cao-box/box.env                                       # two non-secret lines: the session, cao-<instance-id>
systemctl is-enabled herdr-cao.service cao-server.service cao-backup.timer   # enabled x3
```

`bootstrap.sh` only enables the units; it never starts them (its header). On later runs, leave
`--format-new-volume` off.

**5e. The user's SSH key for the box user** (README quick start, step 6). Add the laptop's
**public** key to the box user's `~/.ssh/authorized_keys`, mode 0600, owned by the box user.
Do it as the box user, so root never writes in the home:

```bash
sudo -u <you> sh -c 'install -d -m 0700 "$1/.ssh" && cat >> "$1/.ssh/authorized_keys" && chmod 0600 "$1/.ssh/authorized_keys"' \
  sh <box-home> < <file-with-the-public-key>
```

**Check:** `sudo -u <you> stat -c '%U %a' <box-home>/.ssh/authorized_keys` prints `<you> 600`, and the
file has the one public key line (a public key is not a secret, but don't paste private keys
anywhere).

## 6. Start the services and connect herdr

**6a. Start the units, then check `/health`** (README quick start, step 8). From the admin shell:
the box user has no sudo, and shouldn't.

```bash
sudo systemctl start herdr-cao.service
sudo systemctl start cao-server.service     # waits for herdr's socket; fails if it never appears
sudo systemctl start cao-backup.timer       # bootstrap only enables it; a reboot would start it too
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:9889/health && break; sleep 1; done
```

**Check:** the last command prints the health answer. cao-server takes a few seconds to listen
after systemd says it started, which is why the loop retries. Also check it listens on loopback
only (README, "Checking it works"):

```bash
ss -ltn | grep 9889     # 127.0.0.1:9889, never 0.0.0.0:9889
```

**6b. The read-only resume check, as the box user** (README, "The phases", phase 1). herdr keeps
its state per user, so run it as `<you>`:

```bash
sudo -u <you> -i /usr/local/lib/cao-box/box-resume.sh
```

**Check:** every line starts with `ok`, and it ends with `All up.` (it exits 1 on any `FAIL`;
[`bin/box-resume.sh`](bin/box-resume.sh)).

**6c. Switch SSH to the box user.** From the laptop (README quick start, step 7):

```bash
ssh cao-box true
```

**Check:** it exits 0 with no password prompt. From now on, do box-user work over `ssh cao-box`,
and admin work over `ssh cao-box-admin`.

**6d. Save the box as a herdr machine** (README quick start, step 7). The session name is in
`/etc/cao-box/box.env` (step 5d's check):

```bash
herdr machine add cao-box --remote-session cao-<instance-id> --label cao-box
```

**If it offers to stop the remote server, answer No** (that's the default). Saying yes would end
the box's herdr and every pane.

**Check:** the laptop's herdr shows `cao-box` with the box's session, and a pane opened there
runs on the box (`uname -m` prints `aarch64`).

### 6e. Attaching to the remote session

Once step 6d has saved the machine, there are three ways to reach the box's herdr session
(`cao-<instance-id>`). They were checked against herdr 0.9.3's `--help`; on another version,
check `herdr --help` says the same (**check before use**). `cao-box` here is the SSH host from
step 4 and the machine label from step 6d.

1. **Plain `herdr` on the laptop.** The saved machine `cao-box` appears in the sidebar and
   reconnects by itself, for example after the laptop wakes up. This is the everyday way.
   ```bash
   herdr
   ```
2. **Straight to the remote session, over SSH:**
   ```bash
   herdr --remote cao-box --session cao-<instance-id>
   ```
3. **From an SSH shell on the box**, as the box user: log in with `ssh cao-box`, then run
   ```bash
   herdr session attach cao-<instance-id>
   ```
   If `herdr` isn't found there, fix the login `PATH` first (step 8a).

To look at the saved machine without attaching:

```bash
herdr machine list                          # the saved machines, cao-box among them
herdr machine status cao-box                # reachable or not; it never prompts for a login
herdr --machine cao-box workspace list      # the box's workspaces, run through the saved machine
```

**Check:** `herdr machine status cao-box` says it is reachable, and
`herdr --machine cao-box workspace list` answers. A machine that has to sign in again can be
reconnected in a terminal with `herdr machine reconnect cao-box`.

### 6f. If herdr says the box isn't reachable

herdr reaches the box through the same SSH entry, so test SSH on its own first:

```bash
ssh cao-box true
```

- **It fails:** the cause is below herdr. The box may be **stopped** (start it, step 15), SSM
  may not be `Online` yet (step 3e), the user's AWS sign-in may have **expired** (they run
  `aws sso login` again, step 0h), or the Session Manager plugin may be **old** (update it,
  step 0d). Fix that, then `ssh cao-box true` again.
- **It works, but herdr still can't reach the box:** check the versions match on both sides
  (`herdr --version` on the laptop, `ssh cao-box 'bash -lc "herdr --version"'` on the box; step
  12, the herdr row), and that the box's herdr is up (`ssh cao-box systemctl is-active herdr-cao`).

## 7. The user's logins

All of these run **as the box user** on the box (`ssh cao-box`, or a herdr pane on the box).
Agent CLIs log in on the box with their own flows; don't copy login files from the laptop
(README, "Secrets and the instance role").

**7a. GitHub.**

> **User step.** In a shell on the box, as the box user:
>
> ```bash
> gh auth login --scopes repo,workflow,read:org
> gh auth setup-git
> git config --global user.name "<their name>"
> git config --global user.email "<their email>"
> ```

**Check (you may run these; they show no token):**

```bash
gh auth status 2>&1 | grep -E 'Logged in|scopes'     # shows the account and scopes, not the token
git config --global --get user.name
```

**7b. Claude Code on Bedrock, through the instance role.** The sample doesn't install Claude
Code: the box user installs it with Anthropic's documented installer, as the box user, never as
root (**check before use**). `bootstrap.sh` step 7 put
[`config/claude-settings.box.json.example`](config/claude-settings.box.json.example) in
`~/.claude/settings.json` if there was none. It turns on Bedrock and sets the region and the
model ids.

- There is no `AWS_PROFILE` and no `~/.aws/credentials` on the box: Claude Code uses the instance
  role (README, "Bedrock in Sydney"). Never add either.
- Use the model ids from step 2e. In Sydney the file already has `au.` ids. In another region,
  edit the ids and `AWS_REGION` in `~/.claude/settings.json` and in CAO's
  `~/.aws/cli-agent-orchestrator/settings.json` (`model_tiers`; from
  [`config/cao-settings.json.snippet`](config/cao-settings.json.snippet)). `bootstrap.sh` keeps
  either file if it already exists.

**Check:** the settings keys are there, without printing values you don't need:

```bash
jq -r '.env | keys[]' ~/.claude/settings.json      # names only: CLAUDE_CODE_USE_BEDROCK, AWS_REGION, ANTHROPIC_*
jq -r '.model_tiers.claude_code' ~/.aws/cli-agent-orchestrator/settings.json   # model ids, not secrets
```

> **User step.** The user runs `claude` once on the box, answers its first-run questions, and
> sends one short prompt. A reply proves Bedrock works through the role.

**7c. Codex and Kiro (optional).** Neither is needed for Claude Code agents.

> **User step.** For Codex: install it as the box user from its own docs, then
> `codex login --device-auth` (README quick start, step 8). For Kiro: kiro-cli on linux-arm64
> is **not confirmed** by Kiro's docs; try it, and stop if it doesn't run (README, "Tools on
> linux-arm64").

**Check:** for each one the user installed, `command -v codex` or `command -v kiro-cli` prints a
path.

## 8. The first agent

**8a. `cao` and `uv` on the PATH.** `bootstrap.sh` installs uv, herdr and CAO into the box
user's `~/.local/bin` (steps 4 and 5). The units and herdr's panes have that folder on their
`PATH` ([`systemd/herdr-cao.service`](systemd/herdr-cao.service),
[`systemd/cao-server.service`](systemd/cao-server.service)). An SSH login shell may not: Ubuntu's
default `~/.profile` adds `~/.local/bin` only if it existed at login (**check before use**).

```bash
command -v cao uv herdr
```

**Check:** it prints three paths under `~/.local/bin`. If not, add one line to the box user's
`~/.profile`, then log in again:

```bash
printf '%s\n' 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile
```

**8b. Check herdr first.** If herdr is down, any `cao` command starts a herdr of its own, which
skips the clean layout and the minimal environment (README, "cao-server"):

```bash
systemctl is-active herdr-cao     # active
```

**8c. Pre-trust the work folders.** Claude Code asks "do you trust this folder?" the first time
it opens one, and an agent that CAO launches waits on that question until the launch times out
(README quick start, step 8). For each folder the user names (only ones whose contents they'd
run themselves), as the box user:

```bash
for dir in "$HOME" "<work-folder>"; do
  jq --arg d "$dir" '.projects[$d].hasTrustDialogAccepted = true' ~/.claude.json > ~/.claude.json.tmp \
    && chmod 600 ~/.claude.json.tmp && mv ~/.claude.json.tmp ~/.claude.json
done
```

`~/.claude.json` exists once `claude` has run once (step 7b).

**Check:** `jq -r '.projects | to_entries[] | select(.value.hasTrustDialogAccepted) | .key' ~/.claude.json`
lists each folder.

**8d. Pick a Claude Code profile.** CAO's default provider is `kiro_cli`
(`src/cli_agent_orchestrator/constants.py`), so name the provider. CAO's own profiles (for
example `developer`) set no provider. A profile pinned to another provider's model failed in a
real run on a box without that provider, even with `--provider claude_code`
(**check before use**). So pick a Claude Code profile, or copy one and change its
provider and model ([`docs/agent-profile.md`](../../docs/agent-profile.md)).

**8e. Launch.** As the box user, in a herdr pane on the box:

```bash
cao launch --agents <profile> --provider claude_code --working-directory <work-folder>
```

`cao launch` shows what it will run and asks to confirm. Show that to the user; don't add
`--yolo`.

**Check:** the agent reaches idle (no trust prompt on screen), and `cao` lists its session.
Tell the user to wait until it's idle before typing to it (`bin/box-resume.sh`'s closing note).

## 9. Optional: the CAO TUI

`cao tui` runs a compiled `cao-tui` binary. A git install of CAO builds it only when cargo is on
the `PATH` at install time. Without cargo the install still works, but `cao tui` says the binary
is missing ([`scripts/hatch_build_tui_tag.py`](../../scripts/hatch_build_tui_tag.py),
`src/cli_agent_orchestrator/cli/commands/tui.py`). `bootstrap.sh` installs CAO with a `PATH` of
only `~/.local/bin` and the system folders (its `USER_PATH`), so its install has no TUI.

1. **A C toolchain (admin).** Get the user's OK, then from the admin shell:
   `sudo apt-get install -y build-essential`.
   **Check:** `cc --version` prints a version.
2. **Rust, as the box user, never as root.** Download `rustup-init` for
   `aarch64-unknown-linux-gnu` and its published `.sha256` from the Rust project, check the
   hash, then run it (**check before use**: take the URLs from rustup.rs):
   ```bash
   sha256sum -c rustup-init.sha256    # or compare by hand; it must say OK
   chmod +x rustup-init && ./rustup-init -y --profile minimal
   ```
   **Check:** `"$HOME/.cargo/bin/cargo" --version` prints a version.
3. **Reinstall CAO at the same commit, with cargo on the PATH.** As the box user:
   ```bash
   PATH="$HOME/.cargo/bin:$PATH" uv tool install --force \
     "git+https://github.com/awslabs/cli-agent-orchestrator.git@<CAO_REF>"
   ```
   The build compiles the TUI and can take several minutes.
   **Check:** the binary is there:
   ```bash
   find "$(uv tool dir)/cli-agent-orchestrator" -type f -name 'cao-tui*' -print
   ```
4. **Restart cao-server (admin).** Restarting cao-server doesn't end the panes (README, FAQ):
   ```bash
   sudo systemctl restart cao-server
   for i in $(seq 1 30); do curl -fsS http://127.0.0.1:9889/health && break; sleep 1; done
   ```
   **Check:** `/health` answers.
5. **Run it.** As the box user: `cao tui`.
   **Check:** the TUI opens and lists the session from step 8.

A later `bootstrap.sh` run with a new `CAO_REF` reinstalls CAO without cargo, so repeat item 3
after each upgrade.

## 10. The reboot drill

This proves the box comes back by itself (README, "The phases", phases 0 and 1). A reboot ends
every pane on the box, so get the user's OK first.

1. From the admin shell: `sudo systemctl reboot`.
2. Wait for SSM to show `Online` again (step 3e), then `ssh cao-box true`.
3. As the box user: `/usr/local/lib/cao-box/box-resume.sh`.

**Check:**

- every line is `ok`, and it ends with `All up.`;
- the laptop's herdr lists `cao-<instance-id>` again, with **no** old panes, and the old layout
  is in `~/.config/herdr/sessions/cao-<instance-id>/restore-archive/`. If old panes come back,
  stop and report it: the sample assumes herdr starts empty (README, phase 0);
- CAO may keep terminal records from before the reboot, with status `unknown`. Remove each with
  `cao shutdown --session <name>`, then relaunch with step 8e. A fresh launch works.

## 11. Optional: backups to S3

Only if the user said yes in step 1, and after their OK for the cost (README, "Backups and
restore"; [`iam/README.md`](iam/README.md)).

1. **A KMS key** for the backups (**check before use**; the README assumes the key exists):
   ```bash
   aws kms create-key --region <region> --description "CAO box backups" --query KeyMetadata.Arn --output text
   ```
   **Check:** it prints a key ARN; record it. The key policy must also allow the instance role
   ([`iam/README.md`](iam/README.md), `BackupKey`).
2. **The bucket**, versioned, private and encrypted, then the bucket policy, filled in from
   [`iam/backup-bucket-policy.json.example`](iam/backup-bucket-policy.json.example) (README
   quick start, step 4):
   ```bash
   aws s3api create-bucket --bucket <backup-bucket> --region <region> \
     --create-bucket-configuration LocationConstraint=<region>
   aws s3api put-public-access-block --bucket <backup-bucket> \
     --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
   aws s3api put-bucket-versioning --bucket <backup-bucket> --versioning-configuration Status=Enabled
   aws s3api put-bucket-encryption --bucket <backup-bucket> --server-side-encryption-configuration \
     '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms","KMSMasterKeyID":"<backup-key-arn>"}}]}'
   aws s3api put-bucket-policy --bucket <backup-bucket> --policy file://<filled-bucket-policy.json>
   ```
   **Check:** `aws s3api get-bucket-versioning --bucket <backup-bucket>` shows `Enabled`.
3. **The role.** Put back the `WriteAndReadBackupsOnly` and `BackupKey` statements, filled in,
   and apply the policy again with `aws iam put-role-policy` (step 3a). Show it to the user first.
   **Check:** `aws iam get-role-policy ... --query 'PolicyDocument.Statement[].Sid'` lists both.
4. **The settings.** Add two lines to `/opt/cao-box-src/box-settings.env` (admin):
   `BACKUP_S3_URI=s3://<backup-bucket>/cao-box/` and `BACKUP_KMS_KEY_ARN=<backup-key-arn>`.
   Then re-run `bootstrap.sh` **without** `--format-new-volume`: first `--dry-run`, then for
   real (step 5d).
   **Check:** `cat /etc/cao-box/backup.env` shows the two lines (they're not secrets).
5. **One backup now, then a restore drill.** The timer runs nightly at 03:30 (box time,
   [`systemd/cao-backup.timer`](systemd/cao-backup.timer)). Run one now from the admin shell:
   ```bash
   sudo systemctl start cao-backup.service
   journalctl -u cao-backup.service -n 30 --no-pager
   ```
   Then, as the box user, restore that copy into a temp folder; this touches nothing live
   (README, "Backups and restore"):
   ```bash
   /usr/local/lib/cao-box/restore.sh --drill s3://<backup-bucket>/cao-box/cao-<stamp>.db.gz
   ```
   The role can't list the bucket, so take `<stamp>` from the newest file in `~/backups/cao/`.
   **Check:** the drill says `integrity_check ok`.

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `BOX_UID must be a number from 1000 to 59999` | A macOS uid (for example 501) or another one below 1000. | Pick a uid from 1000 to 59999 and keep it for every rebuild (`bootstrap.sh` step 0). |
| A dry run stops with `... is blank. To format it as ext4, re-run with --format-new-volume` | The data volume is new and blank, and the flag is missing. | Add `--format-new-volume` to the first dry run and the first real run only (step 5d). |
| `curl /health` fails right after `systemctl start` | cao-server takes a few seconds to listen after systemd says it started. | Use the retry loop from step 6a. If it never answers: `journalctl -u cao-server -b`. |
| `box-resume.sh` says `FAIL cao-backup.timer is active` | `bootstrap.sh` only enables the timer; it starts at the next boot. | `sudo systemctl start cao-backup.timer` (step 6a). |
| `cao: command not found` or `uv: command not found` | `~/.local/bin` isn't on the login shell's `PATH`. | Step 8a. |
| An agent's launch times out, and its pane shows "do you trust this folder?" | Claude Code's trust prompt, on a folder not yet trusted on this box. | Pre-trust the folder (step 8c), then remove the session and relaunch. |
| A launch fails on a box with no Codex | The profile is pinned to a Codex model, and `--provider` doesn't override that. | Pick a Claude Code profile, or adapt one (step 8d). |
| Bedrock says a model id isn't valid | `us.` ids aren't offered in `ap-southeast-2` (README, "Bedrock in Sydney"). | Use the region's ids from step 2e in both settings files (step 7b), and in the role policy. |
| `cao tui` says the `cao-tui` binary isn't present | CAO was installed without cargo on the `PATH`. | Step 9; repeat its item 3 after each CAO upgrade. |
| `refusing ... it is inside the box user's home` | The checkout or the settings file is in `BOX_HOME`. | Move both to `/opt/cao-box-src`, root-owned (step 5a). |
| `refusing to run as root from ...` | Someone other than root owns or can write the checkout. | Fix the owner and modes, then step 5a's check must print nothing. |
| `cao` calls fail, or `herdr status` looks wrong after an upgrade | The herdr version differs between laptop and box. A mismatch has broken every CAO call before (README, "herdr"). | Pin the same `HERDR_VERSION` (and its sha256) on both, re-run `bootstrap.sh`, check `herdr status`. |
| `set these pins first ...` | A tool isn't on the box and its version or sha256 isn't set. | Add the named settings (step 5b). |

## 13. Teardown and cost

Most of the bill is the instance; then the EBS volumes, snapshots, S3, KMS and Bedrock use
(README, "Teardown and cost"). **Stopping** the box keeps everything and stops the instance
charge; the volumes are still charged (README, FAQ). Get the user's OK before any of these.

**Stop (keeps everything):**

```bash
aws ec2 stop-instances --region <region> --instance-ids <instance-id>
```

**Remove everything.** This deletes data: the user downloads what they want to keep first. The
data volume survives termination, so delete it on purpose:

```bash
aws ec2 terminate-instances --region <region> --instance-ids <instance-id>
aws ec2 wait instance-terminated --region <region> --instance-ids <instance-id>
aws ec2 delete-volume --region <region> --volume-id <data-volume-id>
aws ec2 delete-snapshot --region <region> --snapshot-id <snapshot-id>      # each one, if any
aws ec2 delete-security-group --region <region> --group-id <sg-id>
aws ec2 delete-key-pair --region <region> --key-name <cao-box-key>
aws iam remove-role-from-instance-profile --instance-profile-name <cao-box-profile> --role-name <cao-box-role>
aws iam delete-instance-profile --instance-profile-name <cao-box-profile>
aws iam delete-role-policy --role-name <cao-box-role> --policy-name cao-box
aws iam delete-role --role-name <cao-box-role>
```

With backups: delete every object version, then `aws s3api delete-bucket --bucket <backup-bucket>`,
then schedule the KMS key's deletion (`aws kms schedule-key-deletion`, **check before use**).

On the laptop: remove the `cao-box`, `cao-box-dash` and `cao-box-admin` entries from
`~/.ssh/config`, and run `herdr machine remove cao-box`.

**Check:** `aws ec2 describe-volumes --region <region> --volume-ids <data-volume-id>` says the
volume doesn't exist, and `aws ec2 describe-instances` shows the instance as `terminated`.

## 14. The final report

Give the user this, filled in. Ids only, no secrets or values from env.

```text
CAO box: set up

Created (region <region>):
  instance        <instance-id>  (<instance-type>)
  data volume     <vol-id>       (200 GiB gp3, encrypted, survives termination)
  security group  <sg-id>        (no inbound rules)
  key pair        <cao-box-key>
  role / profile  <cao-box-role> / <cao-box-profile>
  backups         <backup-bucket>, key <backup-key-arn>   | none
Cost: about <price> per hour for the instance (source: <pricing page or API>), plus EBS
  storage (charged even while stopped), S3/KMS if any, and Bedrock use.

Checks passed: /health, box-resume.sh all ok, herdr machine cao-box, first agent idle,
  reboot drill <done | not done>, restore drill <done | not done>,
  end-to-end check (step 16) <all ticked | which items failed>.

To attach: run `herdr` (the box is cao-box in the sidebar), or
  herdr --remote cao-box --session cao-<instance-id>   (AGENT-SETUP.md step 6e)

Left for you:
  - <logins not done yet: gh, Codex, Kiro>
  - <folders still to trust>
  - a budget alarm and a status-check alarm (README, phase 3)
  - <anything a check flagged>

To stop it: aws ec2 stop-instances --region <region> --instance-ids <instance-id>
To remove it: AGENT-SETUP.md step 13, which deletes the data volume on purpose.
```

## 15. Daily use: stop, start, re-attach

**Stop the box to save money.** Stopping ends every pane and agent on the box (like a reboot),
so get the user's OK and let running agents finish first:

```bash
aws ec2 stop-instances --region <region> --instance-ids <instance-id>
aws ec2 wait instance-stopped --region <region> --instance-ids <instance-id>
```

**What a stopped box costs:** no instance charge, but the EBS volumes (the 50 GiB root and the
200 GiB data volume) and any snapshots are still charged (README, FAQ, "Can I stop the box at
night"). Quote no price; take it from the EBS pricing page (rule 1).

**Start it again:**

```bash
aws ec2 start-instances --region <region> --instance-ids <instance-id>
aws ec2 wait instance-running --region <region> --instance-ids <instance-id>
```

Then wait for SSM to show `Online` (step 3e's command), and `ssh cao-box true`. The instance id,
the session name `cao-<instance-id>` and the data volume all stay the same; SSH over SSM doesn't
care that the public IP changed. The units are enabled, so they come back on boot by themselves
(README, "Reboot recovery"). Check them, as the box user:

```bash
ssh cao-box 'bash -lc /usr/local/lib/cao-box/box-resume.sh'   # a login shell, so herdr is on the PATH (step 8a)
```

**Check:** every line is `ok`, and it ends with `All up.` Then, as after a reboot (step 10):
herdr is empty, so remove CAO's stale records (`cao shutdown --session <name>`) and relaunch the
agents you need (step 8e). A backup missed while the box was stopped runs at boot
(`Persistent=true`).

**Re-attach** with any of the three ways in step 6e; plain `herdr` usually reconnects by
itself. If it says the box isn't reachable, step 6f.

**While the laptop sleeps** (or restarts, or loses its network):

| Keeps running, on the box | Stops, on the laptop |
|---|---|
| herdr's server and every pane, so the agents keep working | the laptop's herdr window and its SSH connection (it reconnects on wake) |
| cao-server, the backup timer, and the optional Telegram relay | an `ssh -N cao-box-dash` tunnel to the dashboard: run it again |
| | anything started in a laptop-local herdr session, not on `cao-box` |

The user's AWS sign-in may also expire while the laptop sleeps. Then SSH, and so herdr, can't
reach the box until they run `aws sso login` again (step 0h). The box itself never needs that
sign-in: it uses its instance role.

## 16. The end-to-end check on a new machine

Run this last, on the new laptop, and tick each line. Stop at the first one that fails, and fix
it at the step named.

- [ ] Every tool from step 0 prints its version, and `herdr --version` matches the box's pin
      (steps 0, 2c).
- [ ] The preflight passes: the CLI, the plugin, the sign-in, the box type, the model ids and the
      image (step 2).
- [ ] The box is created, with its ids in the answers file (step 3).
- [ ] SSM says `Online` (step 3e).
- [ ] `ssh cao-box-admin` and `ssh cao-box true` both work (steps 4, 6c).
- [ ] `systemctl is-active herdr-cao cao-server cao-backup.timer` prints `active` three times
      (step 6a).
- [ ] `/health` answers on `127.0.0.1:9889`, and only there (step 6a).
- [ ] `box-resume.sh` ends with `All up.` (step 6b).
- [ ] herdr is attached: `herdr machine status cao-box` says reachable, and a pane on the box
      prints `aarch64` (steps 6d, 6e).
- [ ] The first agent reached idle and answered a short prompt (step 8).
- [ ] The reboot drill passed (step 10).

Then give the user the final report (step 14).
