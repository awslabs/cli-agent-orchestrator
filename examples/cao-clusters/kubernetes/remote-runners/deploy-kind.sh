#!/usr/bin/env bash
# Build the CAO image from this checkout, load it into a kind cluster, and apply
# the remote-runner topology: one cao-server, one supervisor runner, a pool of
# worker runners.
#
# Usage: examples/cao-clusters/kubernetes/remote-runners/deploy-kind.sh [kind-cluster]
#
# Requires: docker, kind, kubectl, openssl, and AWS credentials for Bedrock in
# the environment (`aws configure export-credentials` must work). The cluster
# must bind-mount a shared host directory at /cao-workspace on every node (see
# kind-config.yaml in this directory).
set -euo pipefail

CLUSTER="${1:-cao-k8s-cluster}"
CTX="kind-${CLUSTER}"
NS=cao-remote
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../../../.." && pwd)"
TAG="dev-$(git -C "${REPO}" rev-parse --short HEAD)$(git -C "${REPO}" diff --quiet HEAD -- || echo "-dirty")-$(date +%s)"
k() { kubectl --context "${CTX}" "$@"; }

echo "building cao-server:${TAG}"
docker build -q -f "${REPO}/examples/cao-clusters/kubernetes/eks/Dockerfile" -t "cao-server:${TAG}" "${REPO}"
kind load docker-image --name "${CLUSTER}" "cao-server:${TAG}"

k apply -f "${HERE}/namespace.yaml"

# Minted once and kept: a new token would lock out runners already connected.
if ! k -n "${NS}" get secret cao-runner-token >/dev/null 2>&1; then
  k -n "${NS}" create secret generic cao-runner-token --from-literal="token=$(openssl rand -hex 24)"
fi

# Refreshed on every run: these are usually short-lived session credentials.
tmp="$(mktemp)"; trap 'rm -f "${tmp}" "${tmp}.all"' EXIT
( umask 077; aws configure export-credentials --format env-no-export >"${tmp}.all" )
grep -E '^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)=' "${tmp}.all" >"${tmp}"
k -n "${NS}" create secret generic cao-provider-credentials --from-env-file="${tmp}" \
  --dry-run=client -o yaml | k apply -f - >/dev/null
echo "AWS credentials expire: $(grep -E '^AWS_CREDENTIAL_EXPIRATION=' "${tmp}.all" | cut -d= -f2- || echo n/a)"

render="$(mktemp -d)"; trap 'rm -rf "${render}" "${tmp}" "${tmp}.all"' EXIT
cp "${HERE}"/*.yaml "${render}/"
( cd "${render}" && kubectl kustomize . | sed "s|image: cao-server:dev|image: cao-server:${TAG}|" ) | k apply -f -

k -n "${NS}" rollout status statefulset/cao-server --timeout=300s
k -n "${NS}" rollout status deployment/cao-supervisor-runner --timeout=300s
k -n "${NS}" rollout status deployment/cao-pool-runner --timeout=300s
k -n "${NS}" exec cao-server-0 -- curl -s localhost:9889/runners
echo
