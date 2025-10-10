#!/bin/bash

# =============================================================================
# CLI Agent Orchestrator - tmux Installation Script
# =============================================================================
# 
# This script:
# 1. Checks for tmux and verifies version (requires 3.3+)
# 2. Attempts to install/upgrade tmux via package manager (brew/apt/yum)
# 3. Falls back to source installation if package manager version is too old
# 4. Optionally installs enhanced tmux configuration
# 
# Usage:
#   bash <(curl -s https://raw.githubusercontent.com/awslabs/cli-agent-orchestrator/main/tmux-install.sh)
#   or
#   bash tmux-install.sh
# =============================================================================

set -e  # Exit immediately if any command fails

# Color codes for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly BLUE='\033[0;34m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m'

# Configuration
readonly TMUX_CONF_URL="https://raw.githubusercontent.com/awslabs/cli-agent-orchestrator/main/.tmux.conf"

# Logging functions
log_info() { echo -e "${BLUE}[INFO]${NC} $1" >&2; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1" >&2; }
log_error() { echo -e "${RED}[ERROR]${NC} $1" >&2; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1" >&2; }

# Check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Detect package manager
detect_package_manager() {
    if command_exists brew; then
        echo "brew"
    elif command_exists apt-get; then
        echo "apt"
    elif command_exists yum; then
        echo "yum"
    elif command_exists dnf; then
        echo "dnf"
    else
        echo "none"
    fi
}

# Install tmux via package manager
install_tmux_via_package_manager() {
    local pkg_mgr
    pkg_mgr=$(detect_package_manager)
    
    case "$pkg_mgr" in
        brew)
            log_info "Installing tmux via Homebrew..."
            if brew install tmux; then
                log_success "tmux installed via Homebrew"
                return 0
            else
                log_error "Failed to install tmux via Homebrew"
                return 1
            fi
            ;;
        apt)
            log_info "Installing tmux via apt..."
            if sudo apt-get update && sudo apt-get install -y tmux; then
                log_success "tmux installed via apt"
                return 0
            else
                log_error "Failed to install tmux via apt"
                return 1
            fi
            ;;
        yum)
            log_info "Installing tmux via yum..."
            if sudo yum install -y tmux; then
                log_success "tmux installed via yum"
                return 0
            else
                log_error "Failed to install tmux via yum"
                return 1
            fi
            ;;
        dnf)
            log_info "Installing tmux via dnf..."
            if sudo dnf install -y tmux; then
                log_success "tmux installed via dnf"
                return 0
            else
                log_error "Failed to install tmux via dnf"
                return 1
            fi
            ;;
        none)
            log_warning "No supported package manager found"
            return 1
            ;;
    esac
}

# Upgrade tmux via package manager
upgrade_tmux_via_package_manager() {
    local pkg_mgr
    pkg_mgr=$(detect_package_manager)
    
    case "$pkg_mgr" in
        brew)
            log_info "Upgrading tmux via Homebrew..."
            if brew upgrade tmux; then
                log_success "tmux upgraded via Homebrew"
                return 0
            else
                log_warning "Failed to upgrade tmux via Homebrew (may already be latest)"
                return 1
            fi
            ;;
        apt)
            log_info "Upgrading tmux via apt..."
            if sudo apt-get update && sudo apt-get install --only-upgrade -y tmux; then
                log_success "tmux upgraded via apt"
                return 0
            else
                log_warning "Failed to upgrade tmux via apt"
                return 1
            fi
            ;;
        yum)
            log_info "Upgrading tmux via yum..."
            if sudo yum update -y tmux; then
                log_success "tmux upgraded via yum"
                return 0
            else
                log_warning "Failed to upgrade tmux via yum"
                return 1
            fi
            ;;
        dnf)
            log_info "Upgrading tmux via dnf..."
            if sudo dnf upgrade -y tmux; then
                log_success "tmux upgraded via dnf"
                return 0
            else
                log_warning "Failed to upgrade tmux via dnf"
                return 1
            fi
            ;;
        none)
            log_warning "No supported package manager found"
            return 1
            ;;
    esac
}

# Install tmux from source
install_tmux_from_source() {
    log_info "Installing tmux from source..."
    
    # Check for required build tools and libraries
    local missing=false
    for tool in git autoconf automake pkg-config gcc make bison; do
        if ! command_exists "$tool"; then
            missing=true
            break
        fi
    done
    
    # Check for required libraries using pkg-config
    if command_exists pkg-config; then
        if ! pkg-config --exists libevent 2>/dev/null || (! pkg-config --exists ncurses 2>/dev/null && ! pkg-config --exists ncursesw 2>/dev/null); then
            missing=true
        fi
    else
        missing=true
    fi
    
    if [ "$missing" = true ]; then
        log_error "Missing required dependencies for building tmux"
        log_error "Please install the required dependencies first:"
        log_error ""
        log_error "On macOS:"
        log_error "  brew install libevent ncurses automake pkg-config"
        log_error ""
        log_error "On Ubuntu/Debian:"
        log_error "  sudo apt-get install libevent-dev libncurses-dev build-essential autoconf automake pkg-config bison"
        log_error ""
        log_error "On RHEL/CentOS/Amazon Linux:"
        log_error "  sudo yum install libevent-devel ncurses-devel gcc make bison pkg-config autoconf automake"
        echo ""
        exit 1
    fi
    
    log_success "All tmux build dependencies are installed"
    
    # Create temporary directory
    local tmp_dir
    tmp_dir=$(mktemp -d)
    
    log_info "Building tmux in temporary directory: $tmp_dir"
    
    cd "$tmp_dir" || exit 1
    
    # Clone tmux repository from public GitHub
    log_info "Cloning tmux from GitHub..."
    if ! git clone https://github.com/tmux/tmux.git; then
        log_error "Failed to clone tmux repository"
        rm -rf "$tmp_dir"
        exit 1
    fi
    
    cd tmux || exit 1
    
    # Generate configure script
    log_info "Running autogen.sh..."
    if ! sh autogen.sh; then
        log_error "autogen.sh failed"
        rm -rf "$tmp_dir"
        exit 1
    fi
    
    # Build tmux
    log_info "Running configure..."
    if ! ./configure; then
        log_error "configure failed"
        log_error "You may be missing dependencies like libevent-dev or ncurses-dev"
        rm -rf "$tmp_dir"
        exit 1
    fi
    
    log_info "Running make..."
    if ! make; then
        log_error "make failed"
        rm -rf "$tmp_dir"
        exit 1
    fi
    
    log_info "Installing tmux (may require sudo)..."
    # Check for sudo access first
    if ! sudo -n true 2>/dev/null; then
        log_warning "Sudo access is required to install tmux to /usr/local"
        echo "You will be prompted for your password..."
    fi
    
    if ! sudo make install; then
        log_error "make install failed"
        rm -rf "$tmp_dir"
        exit 1
    fi
    
    # Clean up
    cd - > /dev/null || exit 1
    rm -rf "$tmp_dir"
    
    # Verify installation
    if command_exists tmux; then
        log_success "tmux installed successfully: $(tmux -V)"
        return 0
    else
        log_error "tmux installation verification failed"
        exit 1
    fi
}

# Check tmux version meets requirements
check_tmux_version() {
    local tmux_version
    tmux_version=$(tmux -V | grep -oE '[0-9]+\.[0-9]+' | head -1)
    
    if [ -z "$tmux_version" ]; then
        return 1
    fi
    
    local major minor
    major=$(echo "$tmux_version" | cut -d. -f1)
    minor=$(echo "$tmux_version" | cut -d. -f2)
    
    # Check if version >= 3.3
    if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 3 ]; }; then
        return 0
    else
        return 1
    fi
}

# Check and install tmux if needed
check_and_install_tmux() {
    if command_exists tmux; then
        local tmux_version
        tmux_version=$(tmux -V | grep -oE '[0-9]+\.[0-9]+' | head -1)
        
        log_info "tmux already installed: $(tmux -V)"
        
        if check_tmux_version; then
            log_success "tmux version $tmux_version meets requirements (>= 3.3)"
            return 0
        else
            echo ""
            log_warning "Your tmux version ($tmux_version) is older than 3.3"
            log_warning "CLI Agent Orchestrator requires tmux 3.3+ to function properly"
            echo ""
            
            # Check if tmux server is running
            if tmux list-sessions >/dev/null 2>&1; then
                log_error "tmux server is currently running with active sessions"
                log_error "Please kill all tmux sessions before upgrading"
                log_error "Run: pkill -f tmux"
                log_error "Then run this installation script again"
                exit 1
            fi
            
            local max_attempts=3
            local attempt=0
            while [ $attempt -lt $max_attempts ]; do
                read -p "Upgrade to latest tmux? [Y/n]: " choice
                case $choice in
                    [Yy]* | "" ) 
                        # Try package manager first
                        if upgrade_tmux_via_package_manager; then
                            # Check if upgraded version is sufficient
                            if check_tmux_version; then
                                log_success "tmux upgraded successfully via package manager"
                                return 0
                            else
                                log_warning "Package manager version is still too old, installing from source..."
                                install_tmux_from_source
                                return 0
                            fi
                        else
                            log_info "Package manager upgrade failed, installing from source..."
                            install_tmux_from_source
                            return 0
                        fi
                        ;;
                    [Nn]* ) 
                        log_error "tmux 3.3+ is required to continue"
                        exit 1
                        ;;
                    * ) 
                        echo "Please answer yes (y) or no (n)."
                        attempt=$((attempt+1))
                        if [ $attempt -eq $max_attempts ]; then
                            log_error "Maximum attempts reached. Exiting."
                            exit 1
                        fi
                        ;;
                esac
            done
        fi
    else
        log_info "tmux not found. Attempting to install..."
        
        # Try package manager first
        if install_tmux_via_package_manager; then
            # Check if installed version is sufficient
            if check_tmux_version; then
                log_success "tmux installed successfully via package manager"
                return 0
            else
                log_warning "Package manager version is too old, installing from source..."
                install_tmux_from_source
                return 0
            fi
        else
            log_info "Package manager installation failed, installing from source..."
            install_tmux_from_source
            return 0
        fi
    fi
}

# Prompt for tmux enhancement installation
prompt_tmux_enhancement() {
    echo ""
    echo "========================================"
    echo "  Enhanced tmux Configuration"
    echo "========================================"
    echo ""
    echo "Install enhanced tmux configuration with:"
    echo "  • Mouse support (click to switch panes, resize with drag)"
    echo "  • Increased scrollback buffer (50,000 lines)"
    echo "  • Activity monitoring and visual alerts"
    echo "  • Enhanced copy/paste with system clipboard"
    echo "  • Beautiful Dracula theme with battery status"
    echo "  • Quick config reload (prefix + r)"
    echo ""
    
    # Check if user already has .tmux.conf and adjust prompt
    local prompt_text="Install enhanced tmux configuration? [Y/n]: "
    if [[ -f "$HOME/.tmux.conf" ]]; then
        prompt_text="Install enhanced tmux configuration? (your current config will be backed up) [Y/n]: "
    fi
    
    while true; do
        read -p "$prompt_text" choice
        case $choice in
            [Yy]* | "" ) return 0;;
            [Nn]* ) return 1;;
            * ) echo "Please answer yes (y) or no (n).";;
        esac
    done
}

# Install enhanced tmux configuration
install_tmux_config() {
    log_info "Installing enhanced tmux configuration..."
    
    # Fetch .tmux.conf from repository
    log_info "Fetching .tmux.conf from repository..."
    local tmp_tmux_conf
    tmp_tmux_conf=$(mktemp)
    
    if ! curl -fsSL "$TMUX_CONF_URL" -o "$tmp_tmux_conf"; then
        log_error "Failed to fetch .tmux.conf from repository"
        rm -f "$tmp_tmux_conf"
        exit 1
    fi
    
    # Verify the file was downloaded
    if [ ! -s "$tmp_tmux_conf" ]; then
        log_error "Downloaded .tmux.conf is empty"
        rm -f "$tmp_tmux_conf"
        exit 1
    fi
    
    # Backup existing .tmux.conf if it exists
    if [[ -f "$HOME/.tmux.conf" ]]; then
        local backup_file="$HOME/.tmux.conf.backup.$(date +%Y%m%d_%H%M%S)"
        if ! cp "$HOME/.tmux.conf" "$backup_file"; then
            log_error "Failed to backup existing .tmux.conf"
            rm -f "$tmp_tmux_conf"
            exit 1
        fi
        echo ""
        log_success "Your existing .tmux.conf has been backed up to:"
        echo "  $backup_file"
        echo ""
    fi
    
    # Copy the enhanced tmux configuration
    if ! cp "$tmp_tmux_conf" "$HOME/.tmux.conf"; then
        log_error "Failed to copy enhanced tmux configuration"
        rm -f "$tmp_tmux_conf"
        exit 1
    fi
    rm -f "$tmp_tmux_conf"
    log_success "Installed enhanced tmux configuration to: $HOME/.tmux.conf"
    
    # Install TPM (Tmux Plugin Manager) if not already installed
    local tpm_dir="$HOME/.tmux/plugins/tpm"
    if [[ ! -d "$tpm_dir" ]]; then
        log_info "Installing TPM (Tmux Plugin Manager)..."
        
        # Create the plugins directory
        if ! mkdir -p "$HOME/.tmux/plugins"; then
            log_error "Failed to create tmux plugins directory"
            exit 1
        fi
        
        # Clone TPM repository
        if ! command_exists git; then
            log_error "git is required to install TPM"
            exit 1
        fi
        
        if ! git clone https://github.com/tmux-plugins/tpm "$tpm_dir"; then
            log_error "Failed to clone TPM repository"
            exit 1
        fi
        log_success "TPM installed successfully"
    else
        log_info "TPM already installed at: $tpm_dir"
    fi
    
    # Install tmux plugins programmatically
    log_info "Installing tmux plugins..."
    local install_script="$tpm_dir/bin/install_plugins"
    
    if [[ ! -x "$install_script" ]]; then
        log_error "TPM install script not found or not executable at: $install_script"
        exit 1
    fi
    
    if ! "$install_script"; then
        log_error "Failed to install tmux plugins"
        exit 1
    fi
    log_success "Tmux plugins installed successfully"
    
    # Reload tmux configuration if tmux is running
    if command_exists tmux && tmux list-sessions >/dev/null 2>&1; then
        log_info "Reloading tmux configuration..."
        tmux source-file "$HOME/.tmux.conf" 2>/dev/null || true
    fi
    
    log_success "Enhanced tmux configuration installed successfully!"
}

# Main installation flow
main() {
    echo "========================================"
    echo "  CLI Agent Orchestrator"
    echo "  tmux Installation Script"
    echo "========================================"
    echo ""
    
    # Check tmux version and install if needed
    check_and_install_tmux
    
    # Optional: Enhanced tmux configuration
    if prompt_tmux_enhancement; then
        install_tmux_config
    else
        log_info "Skipping enhanced tmux configuration"
    fi
    
    # Installation complete
    echo ""
    echo "========================================"
    log_success "Installation completed!"
    echo "========================================"
    echo ""
}

# Handle command line arguments
case "${1:-}" in
    --help|-h)
        echo "CLI Agent Orchestrator - tmux Installation Script"
        echo ""
        echo "Usage:"
        echo "  bash tmux-install.sh"
        echo ""
        echo "This script installs tmux, preferring package managers when available."
        echo ""
        echo "Requirements:"
        echo "  - macOS or Linux"
        echo "  - Internet connection"
        echo "  - Package manager (brew/apt/yum/dnf) or build tools for source installation"
        echo ""
        echo "The installer automatically handles:"
        echo "  - Installing tmux via package manager (brew/apt/yum/dnf)"
        echo "  - Falling back to source installation if package version is too old"
        echo "  - Optionally installing enhanced tmux configuration"
        echo ""
        exit 0
        ;;
    *)
        main
        ;;
esac
