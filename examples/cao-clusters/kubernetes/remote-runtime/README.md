# One cao-server, one execution runtime (EKS)

Runs the first slice of
[#745](https://github.com/awslabs/cli-agent-orchestrator/issues/745) on Amazon
EKS: a central `cao-server` that runs no agents, and an execution runtime
(`cao-bridge`) that runs them. The runtime dials the server; everything is
driven through the server's HTTP API. See
[Execution runtimes](../../../../docs/execution-runtimes.md) for the design.

| Object | Role |
|---|---|
| `StatefulSet/cao-server`, `Service/cao-server` | the API and central state, on a gp3 volume; `CAO_LOCAL_EXECUTION=0`, so it refuses to start agents itself |
| `StatefulSet/cao-runtime` | `cao-bridge` beside tmux and the provider CLIs; runtime id is the pod name, `cao-runtime-0` |
| `Secret/cao-runtime-token` | the shared channel token, mounted as a file into both pods (you create it) |
| `Secret/cao-api-token` | the API bearer token, given to the server only (you create it) |
| `NetworkPolicy/cao-server-ingress`, `NetworkPolicy/cao-runtime-ingress` | only the runtime may reach the server's API; the runtime accepts no ingress |

Every API call needs the bearer token from `cao-api-token`
(`CAO_AUTH_LOCAL_TOKEN` on the server); `/health` and the runtime channel do
not. The runtime pod has no API token, so agents there cannot drive the API
even though they can reach it. The NetworkPolicies are a second fence, and take
effect only on a cluster whose CNI enforces NetworkPolicy (on EKS, enable
network policy in the VPC CNI).

## Build

Both pods use the image from [`../eks/Dockerfile`](../eks/Dockerfile), which
contains `cao-server`, `cao-bridge`, tmux, Claude Code and `mock_cli`. Build and
push it as described in the [EKS guide](../eks/README.md#build), then point the
example at it: in `kustomization.yaml`, set `newName` to your repository and
`newTag` to your tag. With the `kustomize` CLI installed, this does the same:

```bash
cd examples/cao-clusters/kubernetes/remote-runtime
kustomize edit set image cao-node=<registry>/<repository>:<tag>
```

## Deploy

```bash
kubectl apply -f namespace.yaml
kubectl -n cao-remote create secret generic cao-runtime-token \
  --from-literal=token="$(openssl rand -hex 32)"
kubectl -n cao-remote create secret generic cao-api-token \
  --from-literal=token="$(openssl rand -hex 32)"
kubectl apply -k .
kubectl -n cao-remote rollout status statefulset/cao-server
kubectl -n cao-remote rollout status statefulset/cao-runtime
```

The runtime is Ready once its channel to the server is up.

Claude Code on Bedrock needs AWS credentials in the runtime pod. Give the
`cao-runtime` service account an IAM role that allows `bedrock:InvokeModel*`
(see the annotation in `runtime.yaml`, and
[Provider credentials](../eks/README.md#provider-credentials)). `mock_cli`
needs no credentials.

## Drive it

```bash
kubectl -n cao-remote port-forward svc/cao-server 9889:9889 &
B=http://localhost:9889
AUTH="Authorization: Bearer $(kubectl -n cao-remote get secret cao-api-token \
  -o jsonpath='{.data.token}' | base64 -d)"

curl -s -H "$AUTH" $B/runtimes
T=$(curl -s -H "$AUTH" -X POST $B/runtimes/cao-runtime-0/terminals \
  -H 'Content-Type: application/json' \
  -d '{"agent_profile": "developer", "provider": "claude_code"}' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])')

curl -s -H "$AUTH" $B/terminals/$T                          # status: idle
curl -s -H "$AUTH" -X POST "$B/terminals/$T/input?message=Reply%20with%20one%20word%3A%20ok"
curl -s -H "$AUTH" $B/terminals/$T                          # processing, then completed
curl -s -H "$AUTH" "$B/terminals/$T/output?mode=last"
curl -s -H "$AUTH" -X DELETE $B/terminals/$T
```

Use `"provider": "mock_cli"` to try the path without a model. A call without
the token gets `401`.

## Verify

- The agent runs in the runtime pod and never in the server pod:
  `kubectl -n cao-remote exec cao-runtime-0 -- tmux ls` lists its session;
  `kubectl -n cao-remote exec cao-server-0 -- tmux ls` lists none.
- With the runtime stopped (`kubectl -n cao-remote scale statefulset/cao-runtime --replicas=0`),
  operations on its terminals return `503` and their status is `unknown`.

## Cleanup

```bash
kubectl delete -k .
```

This deletes the namespace too, and with it both token Secrets. The server's
state volume uses the `gp3` storage class; if that class retains volumes,
delete the released PersistentVolume as well.
