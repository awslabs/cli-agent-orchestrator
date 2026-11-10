#!/usr/bin/env bash
# Build the CAO image from this checkout, load it into a kind cluster, and apply
# the kind overlay: one cao-server and a pool of two runners.
#
# Usage: examples/cao-clusters/kubernetes/remote-runners/overlays/kind/deploy.sh [kind-cluster]
#
# Requires docker, kind, kubectl and python3. The cluster must come from
# kind-config.yaml in this directory (shared /cao-workspace on every node).
set -euo pipefail

if [ "$#" -gt 1 ]; then
  echo "usage: $0 [kind-cluster]" >&2
  exit 2
fi
CLUSTER="${1:-cao-remote}"
CTX="kind-${CLUSTER}"
NS=cao-remote
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../../../../../.." && pwd)"
TAG="dev-$(git -C "${REPO}" rev-parse --short HEAD)$(git -C "${REPO}" diff --quiet HEAD -- || echo "-dirty")-$(date +%s)"
k() { kubectl --context "${CTX}" "$@"; }

echo "building cao-server:${TAG}"
docker build -q -f "${REPO}/examples/cao-clusters/kubernetes/eks/Dockerfile" -t "cao-server:${TAG}" "${REPO}"
kind load docker-image --name "${CLUSTER}" "cao-server:${TAG}"

k apply -f "${HERE}/../../base/namespace.yaml"

# Minted once and kept: a new token would lock out runners already connected.
if ! k -n "${NS}" get secret cao-runner-token >/dev/null 2>&1; then
  token="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
  [ "${#token}" -eq 48 ] || { echo "could not generate a runner token" >&2; exit 1; }
  k -n "${NS}" create secret generic cao-runner-token --from-literal="token=${token}"
fi

k kustomize "${HERE}" | sed "s|image: cao-server:dev$|image: cao-server:${TAG}|" | k apply -f -

k -n "${NS}" rollout status statefulset/cao-server --timeout=300s
k -n "${NS}" rollout status deployment/cao-runner --timeout=300s
k -n "${NS}" exec cao-server-0 -- curl -fsS localhost:9889/runners
echo
