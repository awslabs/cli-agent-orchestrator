# Remote CAO: run CAO on an always-on EC2 box

CAO on a laptop stops when the laptop sleeps or restarts. Long jobs can't finish overnight, and many agents can fill the disk or the memory.

Remote CAO moves CAO to one always-on Linux box in AWS. The agents run there, under systemd. Your laptop keeps only a window onto them.

This page is the user guide. The reference setup is the sample in [`examples/remote/ec2/`](../examples/remote/ec2/README.md) (PR #1316). The guide links its files for the details.

> **What exists today.** This guide uses **herdr mode**. The sample also has a tmux mode. The commands `cao remote check` and `cao remote connect` do **not** exist yet. See [Coming next](#coming-next).

## Why run CAO on a box

- Your agents keep working while the laptop is asleep, closed or restarting.
- The box has room: 16 vCPU and 64 GiB by default.
- Nothing is open to the internet. There is no inbound port, and cao-server listens on `127.0.0.1` only.
- Your laptop's cloud credentials never go to the box. The box uses its own narrow instance role.

It costs money while it runs, and you set up AWS resources by hand. If you only need CAO now and then, the laptop is simpler.

## How it fits together

```text
laptop                                  EC2 box (Ubuntu 24.04 arm64)
------                                  ----------------------------
herdr window  --ssh inside SSM------->  herdr-cao.service   the agents' panes
browser       --ssh -L 19889:9889---->  cao-server.service  127.0.0.1:9889 only
                                        cao-backup.timer    nightly backup
```

- SSH runs inside an AWS Session Manager (SSM) session. That's why no port is open.
- The agents run as a Linux user with no `sudo`. We suggest the name `cao-herdr`. The sample takes any name (`BOX_USER`), and this guide uses `cao-herdr` in every example.

## Quick start

The short version. Each line is explained in [the steps](#the-steps).

1. On the laptop, install the AWS CLI v2, the `session-manager-plugin` program, and herdr. Log in to AWS.
2. Create the instance role, the security group (no inbound rules) and the instance. Optionally create the backup key and bucket.
3. On the box, as root, run the sample's `bootstrap.sh`. It installs everything and sets up the `cao-herdr` user.
4. Add your public SSH key for `cao-herdr`, and add the SSH block to `~/.ssh/config`.
5. Check the services, then log in to your agent CLIs on the box.
6. On the laptop, run `herdr machine add cao-box --remote-session cao-<instance-id> --label cao-box`, then `herdr`.

Replace every `<...>` placeholder with your own value:

| Placeholder | Meaning |
|---|---|
| `<profile>`, `<region>` | your AWS CLI profile and region |
| `<instance-id>`, `<sg-id>`, `<vpc-id>`, `<subnet-id>`, `<data-volume-id>` | the ids you create or pick |
| `<role>`, `<instance-profile>`, `<sg-name>` | names you choose |
| `<backup-bucket>`, `<backup-key-arn>` | the backup bucket and its KMS key (optional) |
| `<command-id>`, `<ami-id>` | ids that AWS prints |

## The steps

### 1. Prepare the laptop

You need:
- the AWS CLI v2, and the `session-manager-plugin` program;
- `ssh`;
- `jq`;
- herdr. Its version must match the one on the box.
- an AWS login that can create the resources in step 2 and call `ssm:StartSession` on the instance.

```bash
aws --version                      # aws-cli/2.x
session-manager-plugin --version
herdr --version                    # the same version as on the box
aws sso login --profile <profile>
ssh-keygen -t ed25519 -f ~/.ssh/cao-box_ed25519 -C cao-box   # if you have no key; only the .pub half goes to the box
```

Use AWS SSO or another short-lived login. Don't put long-lived access keys on the box. Your laptop's credentials stay on the laptop.

### 2. Create the AWS resources

Nothing in the sample creates AWS resources for you. You run every command. The sample's [README](../examples/remote/ec2/README.md#quick-start) has them all; this is the short form.

**The box type and the image** (Ubuntu 24.04 arm64):

```bash
aws ec2 describe-instance-type-offerings --profile <profile> --region <region> \
  --location-type availability-zone --filters Name=instance-type,Values=m8g.4xlarge,m7g.4xlarge \
  --query 'InstanceTypeOfferings[].[InstanceType,Location]' --output table
aws ssm get-parameter --profile <profile> --region <region> \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
  --query Parameter.Value --output text
```

**The instance role.** Fill in [`iam/instance-role-policy.json.example`](../examples/remote/ec2/iam/instance-role-policy.json.example) first, and read the [IAM notes](../examples/remote/ec2/iam/README.md). The ARN forms there say "check before use".

```bash
aws iam create-role --profile <profile> --role-name <role> \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam put-role-policy --profile <profile> --role-name <role> --policy-name cao-box \
  --policy-document file://instance-role-policy.json
aws iam create-instance-profile --profile <profile> --instance-profile-name <instance-profile>
aws iam add-role-to-instance-profile --profile <profile> --instance-profile-name <instance-profile> --role-name <role>
```

**A security group with no inbound rules, and the instance.** IMDSv2 is required. The data volume is encrypted and survives termination.

```bash
aws ec2 create-security-group --profile <profile> --region <region> --vpc-id <vpc-id> \
  --group-name <sg-name> --description "CAO box: no inbound"
aws ec2 run-instances --profile <profile> --region <region> \
  --image-id <ami-id> --instance-type m8g.4xlarge \
  --subnet-id <subnet-id> --security-group-ids <sg-id> \
  --iam-instance-profile Name=<instance-profile> \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled \
  --block-device-mappings '[
    {"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":50,"VolumeType":"gp3","Encrypted":true}},
    {"DeviceName":"/dev/sdf","Ebs":{"VolumeSize":200,"VolumeType":"gp3","Encrypted":true,"DeleteOnTermination":false}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=cao-box}]'
```

The subnet needs outbound internet (a NAT gateway, or a public IP with no inbound rules), or VPC endpoints for SSM, Bedrock and S3.

Write down the data volume's id. `bootstrap.sh` finds the disk by that id:

```bash
aws ec2 describe-instances --profile <profile> --region <region> --instance-ids <instance-id> \
  --query 'Reservations[].Instances[].BlockDeviceMappings[?DeviceName==`/dev/sdf`].Ebs.VolumeId' --output text
```

**Wait for SSM.** It should print `Online`:

```bash
aws ssm describe-instance-information --profile <profile> --region <region> \
  --filters Key=InstanceIds,Values=<instance-id> \
  --query 'InstanceInformationList[].PingStatus' --output text
```

**Check the security group.** This should print `[[]]`:

```bash
aws ec2 describe-security-groups --profile <profile> --region <region> --group-ids <sg-id> \
  --query 'SecurityGroups[].IpPermissions'
```

#### Optional: the backup key and bucket

Skip this if you don't want S3 backups. The box still keeps 14 local copies.

The backups hold message text, so treat the bucket like the box. Do this in order:

1. **Make a KMS key.** With the default key policy, the role's `BackupKey` statement (see the [IAM notes](../examples/remote/ec2/iam/README.md)) is sufficient. If you write your own key policy, keep the statement that enables IAM policies.

   ```bash
   aws kms create-key --profile <profile> --region <region> --description "CAO box backups" \
     --query KeyMetadata.Arn --output text
   ```

2. **Make the bucket.** It's private, versioned and encrypted with that key. Fill in [`iam/backup-bucket-policy.json.example`](../examples/remote/ec2/iam/backup-bucket-policy.json.example) first. The policy allows TLS only, and only uploads that use this key.

   ```bash
   aws s3api create-bucket --profile <profile> --bucket <backup-bucket> --region <region> \
     --create-bucket-configuration LocationConstraint=<region>
   aws s3api put-public-access-block --profile <profile> --bucket <backup-bucket> \
     --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
   aws s3api put-bucket-versioning --profile <profile> --bucket <backup-bucket> --versioning-configuration Status=Enabled
   aws s3api put-bucket-encryption --profile <profile> --bucket <backup-bucket> --server-side-encryption-configuration \
     '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms","KMSMasterKeyID":"<backup-key-arn>"}}]}'
   aws s3api put-bucket-policy --profile <profile> --bucket <backup-bucket> --policy file://backup-bucket-policy.json
   ```

   In `us-east-1`, omit `--create-bucket-configuration LocationConstraint=<region>`.

3. **Check it.** This should print `Enabled`:

   ```bash
   aws s3api get-bucket-versioning --profile <profile> --bucket <backup-bucket>
   ```

4. **Allow the role.** Keep the role's backup statements (`WriteAndReadBackupsOnly` and `BackupKey`) in the instance-role policy, filled in, and apply it again with `aws iam put-role-policy`.

You can drop `s3:GetObject` from the role. Then an agent can't read old backups out of S3. The cost is that you restore from the laptop: download the backup, copy it to the box, then run `restore.sh`. Keep `kms:Decrypt` either way, because large uploads need it.

### 3. Set up the box

`bootstrap.sh` does the setup, as root, from a checkout outside the box user's home. It installs the tools (every download is checked against a sha256), sets up the `cao-herdr` user and the data volume, and writes the config files and the systemd units. It's safe to run again. It enables and starts the units, unless you add `--no-start`.

**First, find out where "Session Manager shell" lands.** If Session Manager "Run As" is on in your account, `aws ssm start-session` opens the shell as the Run As user, not as `ssm-user`. On a real box that user was the box user, which has no `sudo`. So the "admin shell" wasn't one.

Check it from the laptop:

```bash
aws ssm get-document --profile <profile> --region <region> --name SSM-SessionManagerRunShell \
  --query Content --output text | jq '.inputs.runAsEnabled'
```

- `true`: a Session Manager shell is **not** an admin shell. Do the admin steps with Run Command.
- `false`, or the document doesn't exist: Run As is off. Use `sudo -i` in a Session Manager shell if you like.

**Run Command works either way.** `AWS-RunShellScript` runs as root and needs no shell. You need `ssm:SendCommand` on the instance for it:

```bash
aws ssm send-command --profile <profile> --region <region> --instance-ids <instance-id> \
  --document-name AWS-RunShellScript --comment "remote cao: show the uid" \
  --parameters 'commands=["id -u"]' --query Command.CommandId --output text
aws ssm get-command-invocation --profile <profile> --region <region> \
  --command-id <command-id> --instance-id <instance-id> --query '[Status,StandardOutputContent]'
```

It should print `Success` and `0`.

Everything below runs as root. Use `sudo -i`, or put each line in the `commands` list of Run Command.

```bash
install -d -m 0755 /opt/cao-box-src
git clone https://github.com/awslabs/cli-agent-orchestrator.git /opt/cao-box-src/cli-agent-orchestrator
```

Write the settings file, `/opt/cao-box-src/box-settings.env`, one `KEY=VALUE` per line. The sample README lists every setting and where to find each checksum ([step 6](../examples/remote/ec2/README.md#6-first-login-and-your-ssh-key)). These lines are the ones that matter here:

```text
BOX_USER=cao-herdr
BOX_UID=2001
BOX_HOME=/home/cao-herdr
DATA_MOUNT=/home/cao-herdr
BOX_SSH_PUBLIC_KEY=<the line from your .pub file>
DATA_VOLUME_ID=<data-volume-id>
HERDR_VERSION=<the laptop's herdr version>
HERDR_SHA256=<sha256 of herdr-linux-aarch64>
CAO_REF=<the CAO commit or tag to install>
BACKUP_S3_URI=s3://<backup-bucket>/cao-box/        # optional
BACKUP_KMS_KEY_ARN=<backup-key-arn>                # optional
AWS_CLI_VERSION=<version>
AWS_CLI_SHA256=<sha256>
SAM_VERSION=<version>
SAM_SHA256=<sha256>
YAZI_SHA256=<sha256>
HELIX_SHA256=<sha256>
UV_VERSION=<version>
UV_SHA256=<sha256>
```

- `BOX_UID` is a number from 1000 to 59999, and you never change it. Pick a higher unused uid, such as 2001. Make sure that `getent passwd <uid>` prints nothing.
- `DATA_MOUNT` equal to `BOX_HOME` mounts the data volume as the home itself.
- The file must be root's, not group- or world-writable, and outside the home. `bootstrap.sh` refuses anything else.

Then run it. Do the dry run first. `--format-new-volume` is for the first run only, and it formats only a blank disk that matches `DATA_VOLUME_ID`:

```bash
bash /opt/cao-box-src/cli-agent-orchestrator/examples/remote/ec2/bootstrap.sh \
  --settings /opt/cao-box-src/box-settings.env --dry-run --format-new-volume
bash /opt/cao-box-src/cli-agent-orchestrator/examples/remote/ec2/bootstrap.sh \
  --settings /opt/cao-box-src/box-settings.env --format-new-volume
```

**Your SSH key.** `bootstrap.sh` installs `BOX_SSH_PUBLIC_KEY` if the line is missing. Or add the one `.pub` line by hand as the box user, with the folder at 0700 and the file at 0600:

```bash
runuser -u cao-herdr -- env HOME=/home/cao-herdr sh -c \
  'umask 077; mkdir -p ~/.ssh; printf "%s\n" "<the line from your .pub file>" >> ~/.ssh/authorized_keys'
```

Never copy a private key to the box.

**Check:** `systemctl is-enabled herdr-cao.service cao-server.service cao-backup.timer` prints `enabled` three times.

### 4. Connect: SSH inside SSM

Put this above any `Host *` block in `~/.ssh/config` on the laptop. It's [`ssh/config.snippet`](../examples/remote/ec2/ssh/config.snippet) with your profile, region and key filled in:

```text
Host cao-box
  HostName <instance-id>
  User cao-herdr
  IdentityFile ~/.ssh/cao-box_ed25519
  ProxyCommand sh -c "aws ssm start-session --profile <profile> --region <region> --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p'"
  ForwardAgent no
  ServerAliveInterval 30
  ServerAliveCountMax 4
```

Add the dashboard entry too:

```text
Host cao-box-dash
  HostName <instance-id>
  User cao-herdr
  IdentityFile ~/.ssh/cao-box_ed25519
  ProxyCommand sh -c "aws ssm start-session --profile <profile> --region <region> --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p'"
  ForwardAgent no
  LocalForward 19889 127.0.0.1:9889
  ExitOnForwardFailure yes
```

**Check:** `ssh cao-box true` exits 0, and `ssh -G cao-box | grep -i '^forwardagent'` prints `forwardagent no`.

The first time, ssh asks you to accept the host key. To check it instead of trusting it blindly, read the box's key through Run Command (`cat /etc/ssh/ssh_host_ed25519_key.pub`) and compare.

### 5. Check the services and log in

`bootstrap.sh` already starts the services. As root (Run Command, or `sudo -i`), check them:

```bash
systemctl is-active herdr-cao.service cao-server.service cao-backup.timer
```

**Check:** on the laptop, wait for the server to listen:

```bash
ssh cao-box 'for i in $(seq 1 30); do curl -fsS http://127.0.0.1:9889/health && break; sleep 1; done'
```

Then log in to the agent CLIs **on the box**, as `cao-herdr`. Don't copy login files from your laptop:
- Claude Code on Bedrock uses the instance role. See [`config/claude-settings.box.json.example`](../examples/remote/ec2/config/claude-settings.box.json.example).
- Codex: `codex login --device-auth`.
- GitHub: `gh auth login`, with no extra scopes.
- kiro-cli on linux-arm64 isn't confirmed.

Claude Code asks once per folder whether to trust it, and a CAO launch waits on that question. Open `claude` once in each folder you'll use. Never trust the whole home.

The sample is set up for Sydney: the model ids start with `au.` in its role and settings. In another region, change them. See [Bedrock in Sydney](../examples/remote/ec2/README.md#bedrock-in-sydney).

### 6. Attach from the laptop

Save the box once:

```bash
herdr machine add cao-box --remote-session cao-<instance-id> --label cao-box
```

If it offers to stop the remote server, answer **No**. Then plain `herdr` shows the box in its sidebar and reconnects by itself.

You can also go straight to the box's session:

```bash
herdr --remote cao-box --session cao-<instance-id>
herdr machine status cao-box       # check without attaching
```

The session name is in `/etc/cao-box/box.env` on the box.

**The dashboard.** Open a tunnel, then browse to `http://127.0.0.1:19889`:

```bash
ssh -N cao-box-dash
```

Port 19889 doesn't clash with a CAO that runs on your laptop on 9889.

Now start an agent on the box with `cao launch`, from an SSH shell or a herdr pane. The box has its own CAO home. Don't point the laptop's CAO at the box's files.

## Day 2

**After a reboot, or a stop and start.** The units come back by themselves, and the session name stays the same. Panes and agents don't come back, so relaunch them.
- Remove old session records first: `cao shutdown --session <name>`.
- To check the box: `ssh cao-box 'bash -lc /usr/local/lib/cao-box/box-resume.sh'`. It prints `All up.`

**Upgrade CAO.** As root, run `bootstrap.sh` again with a new `CAO_REF`, then `systemctl restart cao-server`.

**Upgrade herdr.** The laptop and the box must run the **same** version. A mismatch breaks CAO calls with `protocol_mismatch`. Never run `herdr update` on one side only. Stop the agents, upgrade both sides, restart `herdr-cao`, then relaunch. Check with `herdr status`.

**Backups.** `cao-backup.timer` runs a SQLite backup every night, checks the copy, and uploads it if you set up the bucket.
- Try a restore once a month. It touches nothing live: `/usr/local/lib/cao-box/restore.sh --drill <backup>`.
- Add EBS snapshots of the data volume for whole-box restores.
- With the home as the data volume, `box.env` lives only in `/etc/cao-box/`. Copy it by hand before a whole-box restore.
- More in the sample's [backups and restore](../examples/remote/ec2/README.md#backups-and-restore).

**Cost.** Most of the bill is the instance. Stop it when it's idle. A stopped box still pays for its volumes and snapshots, so set a budget alarm first.

```bash
aws ec2 stop-instances --profile <profile> --region <region> --instance-ids <instance-id>
aws ec2 start-instances --profile <profile> --region <region> --instance-ids <instance-id>
```

**Take it down.** Get an OK before you terminate the instance, and a separate OK before you delete any data (the data volume, the snapshots, the bucket, the KMS key). The data volume survives termination on purpose. The sample's [teardown section](../examples/remote/ec2/README.md#teardown-and-cost) has the commands. Then remove the `cao-box` and `cao-box-dash` entries from `~/.ssh/config`, run `ssh-keygen -R <instance-id>`, and run `herdr machine remove cao-box`.

## Security in short

- **No inbound port.** SSH runs inside SSM. Session Manager doesn't record what you type inside an SSH session, so it isn't an audit trail.
- **Laptop credentials stay on the laptop.** The SSH block has `ForwardAgent no` (check it with `ssh -G`) and adds no `SendEnv`.
- **Run Command is root.** Anyone with `ssm:SendCommand` on the instance can run anything on it as root. Give it to your admin profile only, scoped to this instance.
- **The instance role is the limit on what an agent can do in your account.** Every agent on the box can use it. The sample's role allows the SSM agent's actions, Bedrock on named profiles, and one backup prefix with one KMS key. It allows nothing for IAM, EC2, Secrets Manager or Parameter Store.
- **A login stored on the box can be read by any agent.** That includes `gh auth login` and `codex login`. Claude Code with Bedrock through the role stores no secret.
- **cao-server is on loopback only.** You reach it through `ssh -L`.
- **The box user has no `sudo` and isn't in the `docker` group.** Either one would make every agent root.
- **Root never trusts the box user's home.** `bootstrap.sh` refuses a checkout or settings file in the home, and refuses symlinks there.

The sample's [design notes](../examples/remote/ec2/README.md#design-notes) have the full list.

## FAQ

**Why not just keep the laptop awake?**
Restarts still happen: updates, crashes, a flat battery. With the box, a laptop restart is a non-event, because herdr reconnects by itself.

**Does my laptop have to stay on?**
No. Only the herdr window and any dashboard tunnel drop while it sleeps. The agents keep running.

**Does restarting cao-server kill my agents?**
No. The panes belong to the `herdr-cao` unit. Restarting herdr does end the panes.

**Can I use the tmux backend on the box?**
Yes. Set `ATTACH_MODE=tmux` before the first run, and use `User cao-tmux` in the SSH entries. See the sample's tmux mode. This guide uses herdr mode.

**Can I keep my laptop's paths, like `/Users/<you>`?**
Yes. If your kits, prompts or memory hold absolute laptop paths, set `BOX_HOME=/Users/<you>` and `DATA_MOUNT=/Users/<you>`. The user can still be `cao-herdr`.

**Why `cao-herdr`?**
It's our suggestion: a name that says what the user is for, so the commands in this guide work on any box. The sample takes any name in `BOX_USER`.

**Which regions and CPUs work?**
The sample is set up for Sydney and arm64 (Graviton), with Ubuntu 24.04. `bootstrap.sh` refuses to run on any other OS. In another region, change the model ids in the role, the Claude Code settings and CAO's `model_tiers`.

**Is it safe to let agents run there?**
There's no open port, and the role is narrow. But an agent can read anything the box user owns, and it can use the instance role. Keep both small.

**A `cao` command I ran by hand started its own herdr. Is that a problem?**
It can be. If herdr is down, `cao` starts one of its own, without the clean start-up. Check `systemctl is-active herdr-cao` before you run `cao` after a problem. Set `"herdr_auto_start": false` in the `terminal` object of the box user's `~/.aws/cli-agent-orchestrator/settings.json`. This needs a `CAO_REF` that includes #1411.

**herdr says the box isn't reachable.**
Run `ssh cao-box true`. The box may be stopped, your AWS login may have expired (`aws sso login --profile <profile>`), or the `session-manager-plugin` program may be old.

**My Session Manager shell has no `sudo`.**
"Run As" is probably on. Use Run Command for admin steps (step 3).

## Coming next

These are **proposals** in issue [#1385](https://github.com/awslabs/cli-agent-orchestrator/issues/1385). They don't exist yet, and this guide doesn't depend on them:

- `cao remote check`: a read-only check of the laptop's prerequisites and the box.
- `cao remote connect`: the AWS login, the SSH key, the pinned host key and the SSH config, done for you with your OK.
- An optional CloudFormation template for step 2.

Say what you'd like in #1385.

## Related

- [`examples/remote/ec2/`](../examples/remote/ec2/README.md): the sample this guide builds on. [`AGENT-SETUP.md`](../examples/remote/ec2/AGENT-SETUP.md) is a version written for an agent to follow; it's listed with the other [setup guides](setup/README.md).
- [herdr in CAO](herdr.md) and [tmux](tmux.md).
- #1316: the sample's PR. #1310: its issue. #1238: remote steering on Kubernetes.
