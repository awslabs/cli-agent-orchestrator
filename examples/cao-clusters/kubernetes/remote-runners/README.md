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
```

The base names no storage class, registry or model provider, and does not
choose a transport: its runners point at the hub with a cleartext `ws://` URL,
which they refuse until an overlay either opts into cleartext explicitly or
serves `wss://`. An overlay supplies all of these. `overlays/kind` is for local testing only; an EKS overlay
(EFS workspace, images from ECR, `wss://`, provider credentials through EKS Pod
Identity) is the next phase of #745.

What an overlay must decide:

| Concern | Base | `overlays/kind` | Production cluster |
|---|---|---|---|
| Workspace (RWX) | claim, no class | hostPath PV on a shared host dir | an RWX class (EFS, NFS, ...) |
| Runner to hub transport | `ws://`, refused off loopback | `CAO_RUNNER_ALLOW_INSECURE=1` | `wss://` (see [Transport](#transport)) |
| Image | `cao-server:dev` | built and `kind load`ed by `deploy.sh` | your registry |
| Provider settings and credentials | optional `cao-provider-env` ConfigMap, `cao-provider-credentials` Secret | none (bring your own) | workload identity where available |

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
enforces them, and on EKS the VPC CNI's network policy agent must be enabled.
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
