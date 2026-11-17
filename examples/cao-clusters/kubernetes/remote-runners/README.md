# One cao-server, execution-only runners on Kubernetes (#745, experimental)

One central `cao-server` serves every agent, and runs none. Agents run in a pool
of identical runner pods, each running `cao-runner`: tmux plus the provider CLI,
and **no `cao-server`**. A runner dials out to the server, runs the server's
terminal operations against its own tmux, and streams the pane output back.
Any agent can delegate work to another; the new agent lands on another runner.

| Component | Kind | Runs | Storage |
|---|---|---|---|
| `cao-server` | StatefulSet, 1 replica, ClusterIP Service | API, database, orchestration, status detection | RWO claim (SQLite, locks, FIFOs) |
| `cao-runner` | Deployment, N replicas, one per node | `cao-runner` + tmux + one agent | shared RWX workspace |

With 1 or 10 runners there is still exactly one `cao-server` process.
[Remote runners](../../../../docs/remote-runners.md) is the reference for
configuration, failure outcomes, restart behaviour, security and known limits.

## Layout

```
base/              cluster-neutral: any conformant cluster
  server.yaml        cao-server StatefulSet and its ClusterIP Service
  runners.yaml       the runner pool (required anti-affinity, one terminal each)
  networkpolicy.yaml default-deny, then only the paths below
  workspace.yaml     the RWX workspace claim (no storage class)
overlays/kind/     a local test cluster: hostPath workspace, cleartext ws://
overlays/eks/      EKS Auto Mode: EFS workspace, gp3 state, wss://, Pod Identity
  iac/               CloudFormation for the AWS side (VPC, cluster, EFS, IAM, ECR)
```

The base names no storage class, registry or model provider, and does not
choose a transport: its runners point at the hub with a cleartext `ws://` URL,
which they refuse until an overlay either opts into cleartext explicitly or
serves `wss://`. An overlay supplies all of these. `overlays/kind` is for local testing
only; `overlays/eks` runs it on EKS Auto Mode (see [Run it on EKS Auto Mode](#run-it-on-eks-auto-mode)).

What an overlay must decide:

| Concern | Base | `overlays/kind` | `overlays/eks` |
|---|---|---|---|
| Workspace (RWX) | claim, no class | hostPath PV on a shared host dir | EFS access point (static PV) |
| Server state (RWO) | claim, no class | the cluster default | gp3 (`cao-gp3`, Auto Mode's EBS driver) |
| Runner to hub transport | `ws://`, refused off loopback | `CAO_RUNNER_ALLOW_INSECURE=1` | `wss://`, the hub serves TLS (see [Transport](#transport)) |
| Image | `cao-server:dev` | built and `kind load`ed by `deploy.sh` | built and pushed to the stack's ECR repository |
| Provider settings and credentials | optional `cao-provider-env` ConfigMap, `cao-provider-credentials` Secret | none (bring your own) | Bedrock through EKS Pod Identity, runners only |

## Network paths

`networkpolicy.yaml` denies everything, then allows:

| From | To | Why |
|---|---|---|
| runner | `cao-server:9891` | the execution channel; only runner pods may reach it |
| runner | `cao-server:9889` | the agents' in-session `cao-mcp-server` calling the API |
| runner | public `:443` (not private, carrier-grade NAT `100.64.0.0/10` or link-local addresses) | the model provider |
| every pod | cluster DNS | name resolution |

Every container runs with `allowPrivilegeEscalation: false` and all Linux
capabilities dropped; tmux and the provider CLIs need none.

Nothing reaches a runner: it has no inbound interface and no Service. Neither
the API port (9889) nor the hub port (9891) is exposed outside the cluster: no
Ingress, LoadBalancer or NodePort. Reach the API with `kubectl port-forward`.

Two cluster requirements. The network plugin must enforce NetworkPolicy, or
these objects are accepted and silently ignored: kind's default CNI (kindnet)
enforces them, and on EKS the VPC CNI's network policy agent must be enabled
(in Auto Mode, a ConfigMap that `overlays/eks/deploy.sh` applies first).
And the DNS rule selects pods labelled `k8s-app: kube-dns` in `kube-system`
(CoreDNS's usual label); patch `allow-dns` in your overlay if your cluster's DNS
differs, since under default-deny no pod could resolve `cao-server` otherwise.
The manifests name services without the cluster's DNS domain
(`cao-server.cao-remote.svc`), so they work whatever that domain is.

## Transport

`ws://` is cleartext: the runner token and every frame (agent input, pane
output) cross the network unencrypted. A runner refuses a `ws://` URL to
anything but loopback unless `CAO_RUNNER_ALLOW_INSECURE=1` is set; only the kind
overlay sets it. The hub logs a warning when it serves cleartext on an address
other than loopback.

Elsewhere, serve `wss://`: mount a certificate and key into `cao-server` and
set `CAO_RUNNER_TLS_CERT` / `CAO_RUNNER_TLS_KEY`, point the runners at
`wss://cao-server.cao-remote.svc:9891`, and give them the CA with
`CAO_RUNNER_CA_FILE`. The hub terminates TLS itself; a service mesh is
optional.

## Credentials

- **Runner token.** One shared bearer token in the `cao-runner-token` Secret,
  mounted as a file (`CAO_RUNNER_TOKEN_FILE`) into the server and every runner.
  It authenticates runners to the hub, nothing else: it is not an API
  credential, and neither is accepted in place of the other.
- **No API authentication in this phase.** With `terminal.backend=remote`,
  `cao-server` refuses to start when API authentication is configured
  (`CAO_AUTH_LOCAL_TOKEN` or an identity provider), because agents and runners
  have no credential to send yet. With auth off, any pod that can reach
  `cao-server:9889` has full control of every terminal. The API must stay
  inside the cluster, and the NetworkPolicy above is then its only protection:
  it protects the API only when the cluster's CNI enforces NetworkPolicy (see
  the cluster requirements above).
- **Provider credentials** go only to runner pods (the server runs no agent),
  through the optional `cao-provider-env` ConfigMap and
  `cao-provider-credentials` Secret, or a workload identity.

Runners and every agent on them are one trust domain: agents run as the
runner's user and can read the mounted runner token. Do not mix tenants on one
deployment.

## Run it locally on kind

```bash
mkdir -p ~/cao-kind/workspace
sed "s|<host-workspace-dir>|$HOME/cao-kind/workspace|" \
  examples/cao-clusters/kubernetes/remote-runners/overlays/kind/kind-config.yaml > /tmp/kind.yaml
kind create cluster --config /tmp/kind.yaml
examples/cao-clusters/kubernetes/remote-runners/overlays/kind/deploy.sh cao-remote
```

`deploy.sh` builds the image from this checkout, loads it into kind, mints the
runner token once, and applies the overlay. Check that the two runners are on
different nodes and connected:

```bash
kubectl --context kind-cao-remote -n cao-remote get pods -o wide
kubectl --context kind-cao-remote -n cao-remote exec cao-server-0 -- curl -s localhost:9889/runners
```

Without a model provider, `mock_cli` exercises the whole path (placement, input,
output, status, inbox):

```bash
kubectl --context kind-cao-remote -n cao-remote exec cao-server-0 -- \
  curl -s -X POST 'localhost:9889/sessions?agent_profile=developer&provider=mock_cli&working_directory=/home/cao/workspace'
```

To run `claude_code` on Amazon Bedrock, give the runners its settings and
credentials. The values below are only a sample:

```bash
kubectl --context kind-cao-remote -n cao-remote create configmap cao-provider-env \
  --from-literal=CLAUDE_CODE_USE_BEDROCK=1 --from-literal=AWS_REGION=us-east-1
kubectl --context kind-cao-remote -n cao-remote create secret generic cao-provider-credentials \
  --from-env-file=<file with AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN>
kubectl --context kind-cao-remote -n cao-remote rollout restart deployment/cao-runner
```

## Run it on EKS Auto Mode

`overlays/eks/iac/cfn-auto-mode.yaml` creates the AWS side in one stack: a
two-AZ VPC with one NAT gateway, an EKS Auto Mode cluster with Secrets
encrypted by KMS, the EFS workspace (NFS only from the cluster's nodes), the
Bedrock role the runners assume through EKS Pod Identity, and a private ECR
repository. `overlays/eks/deploy.sh` then builds and pushes the image and
applies the overlay.

```bash
export AWS_REGION=ap-southeast-2
aws cloudformation deploy \
  --template-file examples/cao-clusters/kubernetes/remote-runners/overlays/eks/iac/cfn-auto-mode.yaml \
  --stack-name cao-remote --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ClusterName=cao-remote PublicAccessCidrs=<your-ip>/32
examples/cao-clusters/kubernetes/remote-runners/overlays/eks/deploy.sh cao-remote
```

- **Kubernetes API endpoint:** public, limited to `PublicAccessCidrs` and
  authenticated by IAM; the stack grants cluster admin to whoever deploys it.
  If your outbound address changes between connections (a corporate egress
  pool), list each range it uses. The CAO API and the hub stay ClusterIP:
  reach the API with `kubectl port-forward`.
- **Transport:** `deploy.sh` makes a CA for this purpose, signs the hub's
  certificate with it and discards the CA key. The server gets the certificate
  and key (`cao-hub-tls` Secret), the runners only the CA certificate
  (`cao-hub-ca` ConfigMap). The CA has keyUsage and the certificate names
  `cao-server.cao-remote.svc`, as the strict X.509 checks of Python 3.13 and
  later require. Each run checks what is in the cluster and issues both anew
  if either is missing, the certificate does not pass those checks against the
  CA (material from an earlier `deploy.sh` whose CA had no keyUsage does not),
  or it expires within 30 days. A hash of both is in the server and runner
  pod templates, so new material rolls them, also on the run after a failed
  one. To rotate, delete `cao-hub-tls` and run `deploy.sh` again.
- **Image updates:** the default tag is the commit plus a digest of the files
  the image is built from, and the repository's tags are immutable, so a
  re-run with the same files reuses the pushed image. The OS packages the build
  upgrades are not in that digest: to take package fixes published since the
  last build, run with `CAO_IMAGE_REBUILD=1`, which builds a new image from
  a fresh base pull with no cached layers, pushes it and rolls the pods onto it.
  Later runs without it (a TLS rotation, a model change) keep that rebuilt
  image while the files are the same, instead of going back to the image from
  before the rebuild; a change to the files builds a new image as usual. To
  run a different pushed image, set `CAO_IMAGE_TAG` to its tag.
- **Model:** `claude_code` on Bedrock, model `CAO_BEDROCK_MODEL` (default
  `global.anthropic.claude-sonnet-4-6`, the global cross-Region inference
  profile: the region you deploy in serves it, and Bedrock may process
  requests in any commercial region; for data residency set a geographic
  profile such as `us.`, `eu.` or `au.anthropic.claude-sonnet-4-6`). Of the CAO
  pods, only the runners get AWS credentials, and those allow only invoking the
  models `BedrockModelPattern` names (default Anthropic Claude) and reading the
  model catalogue. Activate model access for the account once, as an
  administrator, before the first run; the runners cannot subscribe to a model.
  The EFS CSI driver has its own roles, for EFS only. Changing the model in
  `deploy.sh` rolls the runners; agents already running keep their model.
- **Workspace:** the EFS access point pins every client to uid/gid 1000 under
  `/cao-workspace`. The volume mounts with the `iam` option, as the EFS CSI
  driver's node role (Pod Identity on `efs-csi-node-sa`), whose policy lets it
  mount and write only through the workspace access point. The file system
  policy allows nothing to clients without IAM, and denies every mount
  without an access point or without TLS, whatever an identity policy
  allows. It also denies root access, which squashes root rather than
  refusing the mount: UID 0 on the client is mapped to a user with limited
  permissions (through the workspace access point, every request runs as
  uid/gid 1000 regardless). It cannot limit mounts to the node role: EFS
  enforces only a few condition keys for NFS clients, and none of them names
  the principal. So any principal of this account whose own IAM policy grants
  EFS client actions (`elasticfilesystem:ClientMount`, `ClientWrite`) can
  still mount through an access point of this file system, including the
  workspace one (there as uid/gid 1000 under `/cao-workspace`; through
  another access point as whatever that access point sets). Whoever can run
  pods as `efs-csi-node-sa` in `kube-system` (a cluster admin) can use the
  node role. TLS is the EFS CSI driver's default, and the file system policy
  refuses a mount without it.

  **Upgrading a stack from before the file system policy:** as soon as the
  stack update applies the policy, mounts made without `iam` (every mount
  until then) are anonymous and lose access, so the workspace is unavailable
  to running agents until the driver's node pods are restarted (Pod Identity
  gives a pod its credentials when it starts) and the runner pods are
  recreated. Update when no agent runs, in this order: the stack update, then
  `kubectl -n kube-system rollout restart daemonset efs-csi-node`, then
  `deploy.sh` right away, which recreates the runner pods.
- **Auto Mode and NetworkPolicy:** cluster DNS runs on each node at the `.10`
  address of the service range, not in CoreDNS pods, and Pod Identity's agent
  answers on `169.254.170.23:80`. The overlay allows exactly those two, on top
  of the base policy. The service range is pinned in the stack so the DNS
  address is known. As everywhere in Kubernetes, NetworkPolicy does not stop a
  pod from reaching its own node, so a runner can reach its node's kubelet
  port; the kubelet requires authentication, and runners mount no service
  account token.
- **Cost:** the control plane, the nodes, the NAT gateway and EFS bill while the
  stack exists. `aws cloudformation delete-stack --stack-name cao-remote`
  removes everything, including the image repository's images and the
  control-plane log group (`/aws/eks/<ClusterName>/cluster`, kept 30 days
  while the stack exists); delete the `cao-remote` namespace first so Auto
  Mode releases its volumes and nodes. The stack declares that log group, so
  creating it fails while a group of that name exists, for example one left
  by an earlier cluster of the same name.
- **Upgrading a stack created before the template declared the log group:**
  its cluster already created the group, so the update cannot create it.
  Import it into the stack first, which keeps its logs:
  1. Save the deployed template
     (`aws cloudformation get-template --stack-name <stack> --template-stage Original`)
     and add only the `ClusterLogGroup` resource, exactly as
     `iac/cfn-auto-mode.yaml` defines it.
  2. Create an import change set and check that it imports only that group:

     ```bash
     aws cloudformation create-change-set --stack-name <stack> \
       --change-set-name import-cluster-log-group --change-set-type IMPORT \
       --resources-to-import '[{"ResourceType":"AWS::Logs::LogGroup","LogicalResourceId":"ClusterLogGroup","ResourceIdentifier":{"LogGroupName":"/aws/eks/<ClusterName>/cluster"}}]' \
       --template-body file://<deployed template with ClusterLogGroup> \
       --parameters ParameterKey=ClusterName,UsePreviousValue=true \
         ParameterKey=KubernetesVersion,UsePreviousValue=true \
         ParameterKey=VpcCidr,UsePreviousValue=true \
         ParameterKey=ServiceIpv4Cidr,UsePreviousValue=true \
         ParameterKey=PublicAccessCidrs,UsePreviousValue=true \
         ParameterKey=BedrockModelPattern,UsePreviousValue=true \
       --capabilities CAPABILITY_NAMED_IAM
     aws cloudformation describe-change-set --stack-name <stack> \
       --change-set-name import-cluster-log-group
     ```

  3. Execute it (`aws cloudformation execute-change-set`) and wait
     (`aws cloudformation wait stack-import-complete`).
  4. The import keeps the group's current retention; set the template's:
     `aws logs put-retention-policy --log-group-name /aws/eks/<ClusterName>/cluster --retention-in-days 30`.
  5. Update the stack with the new template, every parameter with
     `UsePreviousValue`, then follow the workspace upgrade order above.

  Alternatively, delete the group before the update
  (`aws logs delete-log-group --log-group-name /aws/eks/<ClusterName>/cluster`),
  which loses its logs, or delete and recreate the stack.

## Placement

A runner announces how many terminals it hosts (`CAO_RUNNER_MAX_TERMINALS`, one
here), whether it takes unclaimed work, and optionally session names only it may
host (`CAO_RUNNER_CLAIMS`, with `CAO_RUNNER_RESERVED_SESSIONS` on the server so
nobody else takes them). This example sets no claims: every runner is
identical, and a new terminal goes to a runner already hosting its session if
that one has room, otherwise to any runner with room. With one terminal per
runner, an agent's `assign` therefore starts the new agent in another pod. A
create waits up to `CAO_RUNNER_PLACEMENT_TIMEOUT` for room, then fails with
`503`; scale the Deployment for more concurrent agents (at most one runner per
node with the required anti-affinity).

## Supported scope

This is the first remote-execution slice of #745. What is outside it is refused
explicitly rather than misrouted:

- **Providers:** `claude_code` and `mock_cli`. Others read or write the
  server's own filesystem while they launch and are refused under the remote
  backend (see the reference).
- **Refused operations:** interactive attach from the server host (`cao
  launch` attach, the browser terminal), git worktrees, relative working
  directories, and Python workflow scripts and flow pre-scripts, which would
  run on the server's host.
- **One server:** its state lives on one RWO claim. Live agent processes do not
  survive the loss of their runner pod.
