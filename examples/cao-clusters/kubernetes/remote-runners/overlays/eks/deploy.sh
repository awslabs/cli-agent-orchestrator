#!/usr/bin/env bash
# Build and push the CAO image, then apply the EKS Auto Mode overlay: one
# cao-server and a pool of runners, wss:// between them, Bedrock through Pod
# Identity. Run after iac/cfn-auto-mode.yaml.
#
# Usage: examples/cao-clusters/kubernetes/remote-runners/overlays/eks/deploy.sh <stack-name>
#
# Requires aws, docker, git, kubectl, openssl, python3 and sha256sum, with
# credentials for the stack's account and region (AWS_REGION) and cluster admin
# on the cluster (the stack grants it to whoever deployed it).
#
# Optional:
#   CAO_BEDROCK_MODEL  model or inference profile for claude_code (default
#                      au.anthropic.claude-sonnet-4-6; pick one your region serves)
#   CAO_IMAGE_TAG      tag to push (default: the checkout's short commit id and
#                      a digest of the files the image is built from)
#   CAO_IMAGE_REBUILD  1 to build and push a new image even if the content has
#                      not changed, so it takes the OS package fixes published
#                      since the last build (the default tag names only this
#                      checkout's files, so a re-run reuses the pushed image)
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <stack-name>" >&2
  exit 2
fi
STACK="$1"
: "${AWS_REGION:?set AWS_REGION to the region of the stack}"
NS=cao-remote
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../../../../../.." && pwd)"
MODEL="${CAO_BEDROCK_MODEL:-au.anthropic.claude-sonnet-4-6}"

output() {
  local value
  value="$(aws cloudformation describe-stacks --stack-name "${STACK}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text)"
  [ -n "${value}" ] && [ "${value}" != "None" ] || { echo "stack ${STACK} has no output $1" >&2; exit 1; }
  printf '%s' "${value}"
}

CLUSTER="$(output ClusterName)"
REPO_URI="$(output ServerRepositoryUri)"
VOLUME_HANDLE="$(output WorkspaceVolumeHandle)"
DNS_IP="$(output ClusterDnsIp)"

case "${CAO_IMAGE_REBUILD:-}" in
  ""|0|1) ;;
  *) echo "CAO_IMAGE_REBUILD must be 1, 0 or unset" >&2; exit 2 ;;
esac
if [ -n "${CAO_IMAGE_TAG:-}" ]; then
  TAG="${CAO_IMAGE_TAG}"
else
  # Immutable tags: the tag names the content the image is built from, so the
  # same files always give the same tag and any change, committed or not,
  # gives a new one. The OS packages the build upgrades are not part of it.
  TAG="$(git -C "${REPO}" rev-parse --short HEAD)-$(python3 "${HERE}/context-digest.py" "${REPO}")"
fi
if [ "${CAO_IMAGE_REBUILD:-}" = 1 ]; then
  TAG="${TAG}-r$(date -u +%Y%m%d%H%M%S)"
fi

# Private scratch space (mode 0700): kubeconfig, the registry login, the token
# and TLS material while they are created. Removed on any exit.
umask 077
# The cluster's API endpoint must not be open to the internet (IAM still guards
# it, but this example never intends that). Checked on the live cluster, since
# the stack's rule is not evaluated for every way a stack can be updated.
OPEN="$(aws eks describe-cluster --name "${CLUSTER}" \
  --query "contains(cluster.resourcesVpcConfig.publicAccessCidrs, '0.0.0.0/0')" --output text)" \
  || { echo "could not read cluster ${CLUSTER}'s API endpoint settings" >&2; exit 1; }
if [ "${OPEN}" != "False" ]; then
  echo "refusing: cluster ${CLUSTER}'s API endpoint allows 0.0.0.0/0." >&2
  echo "Set PublicAccessCidrs on the stack to your own source ranges." >&2
  exit 1
fi

CTX="$(mktemp -d)"
trap 'rm -rf "${CTX}"' EXIT
export KUBECONFIG="${CTX}/kubeconfig"
aws eks update-kubeconfig --name "${CLUSTER}" --kubeconfig "${KUBECONFIG}" >/dev/null
k() { kubectl "$@"; }

# 1. NetworkPolicy enforcement. In Auto Mode it is off until this ConfigMap
#    exists, and NetworkPolicy objects are accepted and ignored meanwhile. It
#    goes first, before any CAO object.
k apply -f - <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: amazon-vpc-cni
  namespace: kube-system
data:
  enable-network-policy-controller: "true"
YAML

# 2. The image. Pushed once per tag (the repository's tags are immutable). The
#    registry login goes to a docker config under CTX, so the user's own
#    config is never written and the token goes with CTX on any exit.
REGISTRY="${REPO_URI%%/*}"
if lookup="$(aws ecr describe-images --repository-name "${REPO_URI#*/}" \
  --image-ids "imageTag=${TAG}" 2>&1 >/dev/null)"; then
  echo "image ${REPO_URI}:${TAG} already pushed"
elif ! grep -q ImageNotFoundException <<<"${lookup}"; then
  echo "could not look up image ${REPO_URI}:${TAG}: ${lookup}" >&2
  exit 1
else
  docker build -q --platform linux/amd64 -f "${REPO}/examples/cao-clusters/kubernetes/eks/Dockerfile" \
    -t "${REPO_URI}:${TAG}" "${REPO}"
  mkdir "${CTX}/docker"
  aws ecr get-login-password \
    | DOCKER_CONFIG="${CTX}/docker" docker login --username AWS --password-stdin "${REGISTRY}" >/dev/null
  DOCKER_CONFIG="${CTX}/docker" docker push -q "${REPO_URI}:${TAG}"
fi

k apply -f "${HERE}/../../base/namespace.yaml"

# 3. The runner token, minted once and kept: a new one would lock out runners
#    already connected.
#    Written to a private file, never passed on a command line.
if ! k -n "${NS}" get secret cao-runner-token >/dev/null 2>&1; then
  token="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
  [ "${#token}" -eq 48 ] || { echo "could not generate a runner token" >&2; exit 1; }
  printf '%s' "${token}" > "${CTX}/token"
  unset token
  k -n "${NS}" create secret generic cao-runner-token --from-file=token="${CTX}/token"
  rm -f "${CTX}/token"
fi

# 4. TLS for the hub: a CA made for this purpose signs the hub's certificate.
#    The certificate and key go to the cao-hub-tls Secret (server only), the
#    CA certificate to the cao-hub-ca ConfigMap (runners). The CA key is used
#    once and discarded. What is in the cluster is checked on every run: if
#    either object is missing, the certificate does not verify against the CA,
#    or it expires within 30 days, both are issued anew. Delete cao-hub-tls to
#    rotate. A hash of both goes into the server and runner pod templates
#    (step 6), so new material rolls them, also on a run after one that failed.
live() {  # live <kind> <name> <jsonpath key>: the value, empty if absent
  k -n "${NS}" get "$1" "$2" --ignore-not-found -o "jsonpath={.data.${3//./\\.}}"
}
tls_b64="$(live secret cao-hub-tls tls.crt)"
ca_pem="$(live configmap cao-hub-ca ca.crt)"
printf '%s' "${tls_b64}" | base64 -d > "${CTX}/tls.crt"
printf '%s\n' "${ca_pem}" > "${CTX}/ca.crt"
if [ -z "${tls_b64}" ] || [ -z "${ca_pem}" ] \
  || ! openssl verify -CAfile "${CTX}/ca.crt" "${CTX}/tls.crt" >/dev/null 2>&1 \
  || ! openssl x509 -checkend 2592000 -noout -in "${CTX}/tls.crt" >/dev/null 2>&1; then
  (
    cd "${CTX}"
    openssl req -x509 -new -nodes -newkey rsa:3072 -sha256 -days 365 \
      -subj "/CN=cao-remote hub CA" -keyout ca.key -out ca.crt 2>/dev/null
    openssl req -new -nodes -newkey rsa:3072 -sha256 \
      -subj "/CN=cao-server.${NS}.svc" -keyout tls.key -out tls.csr 2>/dev/null
    printf 'subjectAltName=DNS:cao-server.%s.svc,DNS:cao-server.%s,DNS:cao-server\nextendedKeyUsage=serverAuth\n' \
      "${NS}" "${NS}" > ext.cnf
    openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 365 -sha256 \
      -extfile ext.cnf -out tls.crt 2>/dev/null
    rm -f ca.key ca.srl tls.csr ext.cnf
  )
  k -n "${NS}" create secret tls cao-hub-tls --cert="${CTX}/tls.crt" --key="${CTX}/tls.key" \
    --dry-run=client -o yaml | k apply -f -
  k -n "${NS}" create configmap cao-hub-ca --from-file=ca.crt="${CTX}/ca.crt" \
    --dry-run=client -o yaml | k apply -f -
  rm -f "${CTX}/tls.key"
fi
TLS_HASH="$(cat "${CTX}/tls.crt" "${CTX}/ca.crt" | sha256sum | cut -c1-16)"

# 5. Provider settings for the runners. No credentials: Pod Identity supplies
#    them. Their hash goes into the runner pod template (step 6), so a change
#    rolls the runners; running panes keep the environment they started with.
k -n "${NS}" create configmap cao-provider-env \
  --from-literal=CLAUDE_CODE_USE_BEDROCK=1 \
  --from-literal="AWS_REGION=${AWS_REGION}" \
  --from-literal="ANTHROPIC_MODEL=${MODEL}" \
  --dry-run=client -o yaml > "${CTX}/provider-env.yaml"
k apply -f "${CTX}/provider-env.yaml"
PROVIDER_HASH="$(sha256sum "${CTX}/provider-env.yaml" | cut -c1-16)"

# 6. The overlay, with the stack's values rendered in.
k kustomize "${HERE}" \
  | sed -e "s|<server-repository-uri>:<image-tag>|${REPO_URI}:${TAG}|" \
        -e "s|<workspace-volume-handle>|${VOLUME_HANDLE}|" \
        -e "s|<cluster-dns-ip>|${DNS_IP}|" \
        -e "s|<provider-env-hash>|${PROVIDER_HASH}|" \
        -e "s|<hub-tls-hash>|${TLS_HASH}|" \
  > "${CTX}/rendered.yaml"
if grep -n '<[a-z-]*>' "${CTX}/rendered.yaml"; then
  echo "unrendered placeholder in the overlay" >&2
  exit 1
fi
k apply -f "${CTX}/rendered.yaml"

k -n "${NS}" rollout status statefulset/cao-server --timeout=600s
k -n "${NS}" rollout status deployment/cao-runner --timeout=600s
k -n "${NS}" exec cao-server-0 -- curl -fsS localhost:9889/runners
echo
