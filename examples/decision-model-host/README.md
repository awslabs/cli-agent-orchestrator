# Self-hosted decision model: JevK5-4B, local or on AWS

This sample runs an open-weight decision model behind a `/v1/systemone` typed-decision API. You can run
it on your own machine or in your own AWS account, so routing questions never leave infrastructure you
control. It's a candidate backend for an external decider on CAO's decision platform (#810); the CAO
side of that is tracked in #952.

- **The model** is [JevK5-4B v0.3](https://huggingface.co/alibiserikbay/JevK5), as the Q8_0 GGUF, under
  Apache-2.0.
  - It's Qwen3.5-4B plus a LoRA. One forward pass returns a probability for every option of a yes/no,
    choice or score question, with no generated tokens.
- **The runtime** has three parts:
  - [llama.cpp](https://github.com/ggml-org/llama.cpp) (MIT);
  - the [jevk5](https://github.com/allebee/jevk5) GGUF client, at a pinned tag. Its GGUF path uses only
    the standard library;
  - `serve_gguf.py`, a loopback-only shim exposing `POST /v1/systemone`, `GET /v1/models` and
    `GET /health`. Its request and response shape matches the jevk5 runtime's own `jevk5-serve`.

```
caller ──► 127.0.0.1:8090  serve_gguf.py ──► 127.0.0.1:8080  llama-server (GGUF)
            local, or the AWS host through an SSH tunnel on 127.0.0.1:18090
```

## Why JevK5-4B

We compared three open models on 64 synthetic routing cases, the decision platform's model-size and
reasoning-effort questions. Each was asked 3 times (as written, repeated, and with the options
reversed), using the same rules a hosted decider has to pass:
- no two-step downsizing;
- at least 80% acceptable among applied answers;
- a prompt-injection case never routed to `small`.

| Model (Q8_0) | Rules | Model size at confidence ≥ 0.70: applied / acceptable | Apple M1 Pro, p50 |
|---|---|---|---|
| **JevK5-4B v0.3** | **pass** | 54% / 97%, or 80% / 91% at ≥ 0.60 | ~0.7 s |
| plumb-4b v5 (a JevK5 fine-tune) | pass | 59% / 91% | ~0.7 s |
| JevK5-2B v0.2 | **fail**: the injection case went to `small` | — | ~0.3 s |

- **Why JevK5-4B:** it's the most accurate at a given coverage, and it falls back on the injection case
  instead of answering. The other models are listed in `models.json`.
- **Expect more fallbacks than from a hosted decider:** both 4B models are less confident, so a threshold
  around 0.6 suits them better than 0.7.
- **Consistency:** repeated runs give identical answers, but 19–25% of top choices change when the options
  are reversed.

## Run it locally (macOS or Linux)

```sh
brew install llama.cpp                # or build llama.cpp b11669 or newer
./serve.sh setup                      # clone jevk5 at the tag pinned in models.json
./serve.sh fetch jevk5-4b             # ~4.5 GB from Hugging Face, SHA-256 verified
./serve.sh start jevk5-4b             # llama-server on :8080, the shim on :8090, loopback only
python3 check.py                      # three routing questions, with probabilities and latency
./serve.sh stop
```

On an M1 Pro (Metal), a decision takes about 0.6–0.9 s. You don't need a GPU: llama.cpp also runs on a
CPU, more slowly.

## Run it on AWS (CDK)

The stack in `aws/cdk` creates one GPU instance (g4dn.xlarge with a T4), a security group and a key pair.
- **No IAM:** it uses L1 resources, so it creates no role, profile or policy, and the instance has no
  credentials.
- **No assumed roles:** it deploys with your own credentials through `CliCredentialsStackSynthesizer`.

```sh
cd aws
ssh-keygen -t ed25519 -N "" -C decision-host -f keys/decision-host      # keys/ is git-ignored
REGION=ap-southeast-2                                                   # the region nearest you
VPC=$(aws ec2 describe-vpcs --region $REGION --filters Name=isDefault,Values=true \
      --query 'Vpcs[0].VpcId' --output text)
cd cdk && npm ci && npm test
npx cdk deploy -c region=$REGION -c vpcId=$VPC \
  -c operatorCidr="$(curl -s https://checkip.amazonaws.com)/32" --outputs-file ../cdk-outputs.json
```

- **First boot takes about 30–40 minutes.** It builds llama.cpp for CUDA, then downloads and verifies the
  model. Follow it with `cli/ssh.sh tail -f /var/log/decision-host-setup.log`.
- **When it's ready**, open the tunnel and check:

  ```sh
  cli/ssh.sh --tunnel &            # 127.0.0.1:18090 -> the host's 127.0.0.1:8090
  python3 ../check.py http://127.0.0.1:18090
  ```

- **Pick the region nearest you.** The model takes about 0.25–0.35 s per decision on the T4. Every request
  also crosses the tunnel: from a nearby region, a full decision took 0.29–0.50 s, while a region about
  220 ms away made it 0.7–1.1 s, slower than running on a laptop.
- **Same answers:** on the AWS host, 381 of 384 choices matched the local run. The rest were near-ties.

### Security and cost

- **Reachable only by SSH:** llama-server and the shim bind to `127.0.0.1`. The security group allows
  TCP 22 from one `/32` only, and the stack refuses anything wider.
- **Instance hardening:** IMDSv2 is required, and the 100 GB gp3 root volume is encrypted. The model's
  SHA-256 is checked before use, and llama.cpp and jevk5 are pinned to tags.
- **Third-party code:** both projects come from third parties. Review them at those tags before you
  deploy, as for any dependency.
- **Idle stop:** the instance stops itself after 120 idle minutes, meaning no decision request and no
  interactive login. Shutdown means *stop*, so the disk is kept.
  - Start it again with `aws ec2 start-instances`. Its public IP changes, and `cli/ssh.sh` looks up the
    current one.
- **Cost:** on-demand g4dn.xlarge pricing applies while it runs, plus the volume while it's stopped.
  `npx cdk destroy` removes everything.

## Layout

| Path | What |
|---|---|
| `models.json` | Models with SHA-256s and calibration temperatures, the llama.cpp and jevk5 tags, the idle limit |
| `serve_gguf.py` | The `/v1/systemone` shim, used both locally and on AWS |
| `serve.sh`, `check.py` | The local runner and a smoke check |
| `aws/host/user-data.sh.tmpl` | The instance bootstrap: CUDA build, verified download, systemd units, idle stop |
| `aws/cli/render_user_data.py`, `aws/cli/ssh.sh` | Render the bootstrap by hand; SSH or tunnel with the stack's key |
| `aws/cdk/` | The CDK app and its tests (no IAM, SSH from a single `/32`, IMDSv2, encryption, user-data parity) |
