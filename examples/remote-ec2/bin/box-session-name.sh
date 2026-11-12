#!/usr/bin/env bash
# box-session-name.sh: print the herdr session name for this box,
# cao-<instance-id>, read from the EC2 instance metadata service (IMDSv2).
#
# bootstrap.sh calls this ONCE and stores the answer in /etc/cao-box/box.env;
# nothing recomputes it at boot, so the name never drifts. It uses IMDSv2 only
# (a session token first), so it keeps working with "IMDSv2 required".
#
#   IMDS_ENDPOINT   default http://169.254.169.254 (tests point it elsewhere)
set -euo pipefail

IMDS="${IMDS_ENDPOINT:-http://169.254.169.254}"

token="$(curl -fsS --max-time 3 -X PUT "${IMDS}/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")" || token=""
if [ -z "$token" ]; then
  echo "box-session-name: no IMDSv2 token (is this an EC2 instance with IMDS enabled?)" >&2
  exit 1
fi

instance_id="$(curl -fsS --max-time 3 -H "X-aws-ec2-metadata-token: ${token}" \
  "${IMDS}/latest/meta-data/instance-id")" || instance_id=""
if ! [[ "$instance_id" =~ ^i-[0-9a-f]{8,17}$ ]]; then
  echo "box-session-name: unexpected instance id from IMDS" >&2
  exit 1
fi

printf 'cao-%s\n' "$instance_id"
