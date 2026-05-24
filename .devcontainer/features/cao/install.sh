#!/usr/bin/env bash
# Devcontainer feature install script for CLI Agent Orchestrator (CAO)
# https://github.com/awslabs/cli-agent-orchestrator
set -e

VERSION="${VERSION:-latest}"
WEBUI="${WEBUI:-true}"
PORT="${PORT:-9889}"
AUTOSTART="${AUTOSTART:-false}"

REPO_URL="${REPO_URL:-https://github.com/awslabs/cli-agent-orchestrator.git}"
INSTALL_DIR="/usr/local/share/cao"

echo "Installing CLI Agent Orchestrator (version: ${VERSION})..."

# Install system dependencies
apt-get update -y
apt-get install -y --no-install-recommends tmux git curl

# Clone repository to a fixed location so editable install keeps
# web UI asset paths correct relative to the Python package source.
mkdir -p "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/repo"
if [ "$VERSION" = "latest" ]; then
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR/repo"
else
    git clone "$REPO_URL" "$INSTALL_DIR/repo"
    if ! git -C "$INSTALL_DIR/repo" checkout "$VERSION"; then
        echo "ERROR: Version '${VERSION}' not found in repository ${REPO_URL}." >&2
        exit 1
    fi
fi

# Editable install keeps server static asset resolution aligned with
# the checked out source layout for the selected version.
python3 -m pip install -e "$INSTALL_DIR/repo"

# Build web UI if requested
if [ "$WEBUI" = "true" ]; then
    if ! command -v npm &>/dev/null; then
        echo "ERROR: npm is not available. Install the Node.js devcontainer feature before this one, or set webui=false." >&2
        exit 1
    fi
    echo "Building web UI..."
    cd "$INSTALL_DIR/repo/web"
    npm install
    npm run build
    echo "Web UI built successfully."
fi

# Create entrypoint script that optionally starts cao-server on container start
cat > "$INSTALL_DIR/entrypoint.sh" << EOF
#!/usr/bin/env bash
# CAO devcontainer entrypoint
if [ "\${AUTOSTART:-${AUTOSTART}}" = "true" ]; then
    echo "Starting cao-server on port \${PORT:-${PORT}}..."
    exec cao-server --host 0.0.0.0 --port "\${PORT:-${PORT}}"
fi
EOF
chmod +x "$INSTALL_DIR/entrypoint.sh"

echo "CLI Agent Orchestrator installed successfully."
echo "  - Run 'cao --help' to verify the CLI."
echo "  - Run 'cao-server --help' to see server options."
if [ "$WEBUI" = "true" ]; then
    echo "  - Web UI will be served at http://localhost:${PORT} when cao-server is running."
fi
