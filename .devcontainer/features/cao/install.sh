#!/usr/bin/env bash
# Devcontainer feature install script for CLI Agent Orchestrator (CAO)
# https://github.com/awslabs/cli-agent-orchestrator
set -euo pipefail

VERSION="${VERSION:-latest}"
WEBUI="${WEBUI:-false}"
PORT="${PORT:-9889}"
AUTOSTART="${AUTOSTART:-false}"

REPO_URL="${REPO_URL:-https://github.com/awslabs/cli-agent-orchestrator.git}"
INSTALL_DIR="/usr/local/share/cao"

echo "Installing CLI Agent Orchestrator (version: ${VERSION})..."

# Install system dependencies
apt-get update -y \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tmux git curl \
    && rm -rf /var/lib/apt/lists/*

TMUX_VERSION="$(tmux -V | awk '{print $2}')"
if ! printf '3.3\n%s\n' "$TMUX_VERSION" | sort -V -C; then
    echo "ERROR: tmux >= 3.3 is required, but found $TMUX_VERSION." >&2
    exit 1
fi

# Clone repository to a fixed location so editable install keeps
# web UI asset paths correct relative to the Python package source.
mkdir -p "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/repo"
if [[ "$VERSION" = "latest" ]]; then
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR/repo"
else
    # Try a filtered clone first to reduce image build cost.
    if ! git clone --filter=blob:none "$REPO_URL" "$INSTALL_DIR/repo"; then
        git clone "$REPO_URL" "$INSTALL_DIR/repo"
    fi
    if ! git -C "$INSTALL_DIR/repo" checkout "$VERSION"; then
        rm -rf "$INSTALL_DIR/repo"
        git clone "$REPO_URL" "$INSTALL_DIR/repo"
        if ! git -C "$INSTALL_DIR/repo" checkout "$VERSION"; then
            echo "ERROR: Version '${VERSION}' not found in repository ${REPO_URL}." >&2
            exit 1
        fi
    fi
fi

# Editable install keeps server static asset resolution aligned with
# the checked out source layout for the selected version.
python3 -m pip install -e "$INSTALL_DIR/repo"

# Build web UI if requested
if [[ "$WEBUI" = "true" ]]; then
    if ! command -v npm &>/dev/null; then
        echo "ERROR: npm is not available. Install the Node.js devcontainer feature before this one, or set webui=false." >&2
        exit 1
    fi
    echo "Building web UI..."
    cd "$INSTALL_DIR/repo/web"
    if [[ -f package-lock.json ]]; then
        npm ci
    else
        npm install
    fi
    npm run build
    echo "Web UI built successfully."
fi

# Create entrypoint script that optionally starts cao-server on container start
AUTOSTART_DEFAULT_LITERAL="$(printf '%q' "$AUTOSTART")"
PORT_DEFAULT_LITERAL="$(printf '%q' "$PORT")"

cat > "$INSTALL_DIR/entrypoint.sh" << EOF
#!/usr/bin/env bash
# CAO devcontainer entrypoint
AUTOSTART_DEFAULT=${AUTOSTART_DEFAULT_LITERAL}
PORT_DEFAULT=${PORT_DEFAULT_LITERAL}

AUTOSTART_VALUE="${AUTOSTART:-$AUTOSTART_DEFAULT}"
PORT_VALUE="${PORT:-$PORT_DEFAULT}"

if [[ "$AUTOSTART_VALUE" = "true" ]]; then
    echo "Starting cao-server on port $PORT_VALUE..."
    exec cao-server --host 0.0.0.0 --port "$PORT_VALUE"
fi

if [[ "$#" -gt 0 ]]; then
    exec "$@"
fi

exec tail -f /dev/null
EOF
chmod +x "$INSTALL_DIR/entrypoint.sh"

echo "CLI Agent Orchestrator installed successfully."
echo "  - Run 'cao --help' to verify the CLI."
echo "  - Run 'cao-server --help' to see server options."
if [[ "$WEBUI" = "true" ]]; then
    echo "  - Web UI will be served at http://localhost:${PORT} when cao-server is running."
fi
