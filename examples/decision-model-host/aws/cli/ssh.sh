#!/bin/zsh
# SSH to the decision host with the project's own key and known_hosts (never ~/.ssh).
#   cli/ssh.sh [command...]        cli/ssh.sh --tunnel  (127.0.0.1:18090 -> host 127.0.0.1:8090)
H="${0:A:h:h}"
# Host IP: $DECISION_HOST_IP, else the CDK instance's current address (it changes on every stop and
# start, so it's looked up live).
IP="${DECISION_HOST_IP:-}"
if [[ -z "$IP" && -f "$H/cdk-outputs.json" ]]; then
  IID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['CaoDecisionHost']['InstanceId'])" "$H/cdk-outputs.json")
  REGION=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['context']['region'])" "$H/cdk/cdk.json")
  IP=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$IID" \
        --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null)
  if [[ -z "$IP" || "$IP" == "None" ]]; then
    echo "instance $IID has no public IP (stopped?): aws ec2 start-instances --region $REGION --instance-ids $IID" >&2
    exit 1
  fi
fi
if [[ -z "$IP" ]]; then
  echo "no host: deploy the stack with --outputs-file ../cdk-outputs.json, or set DECISION_HOST_IP" >&2
  exit 1
fi
opts=(-i "$H/keys/decision-host" -o "UserKnownHostsFile=$H/keys/known_hosts"
      -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o BatchMode=yes
      -o ServerAliveInterval=30 -o IdentitiesOnly=yes)
if [[ "${1:-}" == "--tunnel" ]]; then
  exec ssh "${opts[@]}" -N -L 127.0.0.1:18090:127.0.0.1:8090 "ubuntu@$IP"
fi
exec ssh "${opts[@]}" "ubuntu@$IP" "$@"
