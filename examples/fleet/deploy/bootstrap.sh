#!/usr/bin/env bash
# bootstrap.sh — set up one CAO node so a coordinator can reach it over your
# private network. Run ONCE on each fleet node (Linux or macOS). Idempotent.
#
#   bash bootstrap.sh
#
# Transport-agnostic: it does NOT require any specific VPN/mesh. It picks a bind
# address in this order and binds cao-server to it:
#   1. $CAO_BIND_HOST                 (set this to force a specific address)
#   2. the Tailscale IPv4, if tailscale is installed and up
#   3. the primary outbound interface IP (default route)
#   4. the first `hostname -I` address
# Use whichever private network you like (Tailscale, WireGuard, VPN, SSH tunnel,
# or a trusted LAN) — just make sure the coordinator can reach $BIND_HOST:9889.
set -euo pipefail

log()  { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }

CAO_REPO="${CAO_REPO:-git+https://github.com/awslabs/cli-agent-orchestrator.git@main}"
CAO_PORT="${CAO_PORT:-9889}"

# 1. bind address
BIND_HOST="${CAO_BIND_HOST:-}"
if [ -z "$BIND_HOST" ] && command -v tailscale >/dev/null 2>&1; then
  BIND_HOST="$(tailscale ip -4 2>/dev/null | head -1 || true)"
fi
if [ -z "$BIND_HOST" ]; then
  # primary outbound interface IP (works on Linux; harmless elsewhere)
  BIND_HOST="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)"
fi
if [ -z "$BIND_HOST" ]; then
  BIND_HOST="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
fi
[ -n "$BIND_HOST" ] || { echo "could not determine a bind address — set CAO_BIND_HOST=<ip-or-hostname>"; exit 1; }
HOSTSHORT="$(hostname -s 2>/dev/null || hostname)"
log "bind address: $BIND_HOST  host: $HOSTSHORT  port: $CAO_PORT"

# 2. uv
if ! command -v uv >/dev/null 2>&1; then
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

# 3. tmux (required by CAO)
command -v tmux >/dev/null 2>&1 || warn "tmux missing — install tmux >= 3.3 before launching agents"

# 4. CAO
log "installing/upgrading CAO from $CAO_REPO"
uv tool install "$CAO_REPO" --upgrade

# 5. agent CLI (example uses Claude Code; any supported provider works)
command -v claude >/dev/null 2>&1 || warn "claude CLI missing — install (npm i -g @anthropic-ai/claude-code) and authenticate (claude setup-token)"

# 6. db + profiles (idempotent)
cao init || true
for p in code_supervisor developer reviewer; do
  cao install "$p" --provider claude_code || warn "profile $p install skipped"
done

# 7. persistent service bound to the private-network address
ALLOWED="${BIND_HOST},${HOSTSHORT},localhost,127.0.0.1"
CAO_BIN="$HOME/.local/bin/cao-server"
OS="$(uname -s)"

write_systemd_unit() { # $1 = destination path, adds User/Group lines if $2 = "system"
  local dest="$1" scope="${2:-user}" extra=""
  if [ "$scope" = "system" ]; then
    extra="User=$(id -un)
Group=$(id -gn)
Environment=HOME=${HOME}"
  fi
  cat > "$dest" <<UNIT
[Unit]
Description=CAO server (private-network bound)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
${extra}
Environment=PATH=${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=CAO_ALLOWED_HOSTS=${ALLOWED}
Environment=CAO_MCP_APPS_ENABLED=true
ExecStart=${CAO_BIN} --host ${BIND_HOST} --port ${CAO_PORT}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=$([ "$scope" = "system" ] && echo multi-user.target || echo default.target)
UNIT
}

if [ "$OS" = "Linux" ] && [ "$(id -u)" = "0" ]; then
  log "installing systemd system service (root)"
  write_systemd_unit /etc/systemd/system/cao-server.service system
  systemctl daemon-reload
  systemctl enable --now cao-server.service
elif [ "$OS" = "Linux" ]; then
  log "installing systemd user service"
  mkdir -p "$HOME/.config/systemd/user"
  write_systemd_unit "$HOME/.config/systemd/user/cao-server.service" user
  loginctl enable-linger "$(id -un)" 2>/dev/null || warn "enable-linger failed (run: sudo loginctl enable-linger $(id -un))"
  systemctl --user daemon-reload
  systemctl --user enable --now cao-server.service
elif [ "$OS" = "Darwin" ]; then
  log "installing launchd agent"
  PLIST="$HOME/Library/LaunchAgents/dev.cao.server.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.cao.server</string>
  <key>ProgramArguments</key>
    <array><string>${CAO_BIN}</string><string>--host</string><string>${BIND_HOST}</string><string>--port</string><string>${CAO_PORT}</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>CAO_ALLOWED_HOSTS</key><string>${ALLOWED}</string>
    <key>CAO_MCP_APPS_ENABLED</key><string>true</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
PL
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
else
  echo "unsupported OS: $OS"; exit 1
fi

sleep 3
log "verifying local health"
curl -s "http://${BIND_HOST}:${CAO_PORT}/health" || { echo "server not answering on ${BIND_HOST}:${CAO_PORT}"; exit 1; }
echo
log "DONE. This node is reachable at http://${BIND_HOST}:${CAO_PORT} over your private network."
log "Add it to the coordinator's fleet.json:  { \"name\": \"...\", \"host\": \"${BIND_HOST}\" }"
