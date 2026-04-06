#!/usr/bin/env bash
# =============================================================================
# gh-tf-action — Entrypoint
# Installs the requested Terraform version then delegates to Python.
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

log()     { echo -e "${CYAN}[tf-action]${RESET} $*"; }
success() { echo -e "${GREEN}[tf-action] ✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}[tf-action] ⚠${RESET} $*"; }
error()   { echo -e "${RED}[tf-action] ✗${RESET} $*"; }

# ── Install Terraform ─────────────────────────────────────────────────────────
TF_VERSION="${INPUT_TERRAFORM_VERSION:-}"

install_terraform() {
    local version="$1"
    # Validate format: x.y.z (strip leading v)
    version="${version#v}"
    if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        error "Invalid terraform version: '$version'. Expected x.y.z"
        exit 1
    fi

    local arch
    case "$(uname -m)" in
        x86_64)  arch="amd64" ;;
        aarch64) arch="arm64" ;;
        arm64)   arch="arm64" ;;
        *)       arch="amd64" ;;
    esac

    local os_name="linux"
    local filename="terraform_${version}_${os_name}_${arch}.zip"
    local url="https://releases.hashicorp.com/terraform/${version}/${filename}"

    log "Installing Terraform ${version} (${os_name}/${arch})…"
    curl -fsSL --retry 3 -o /tmp/terraform.zip "$url"
    unzip -q -o /tmp/terraform.zip -d /usr/local/bin terraform
    rm /tmp/terraform.zip
    chmod +x /usr/local/bin/terraform
    success "Terraform $(terraform version -json | python3 -c 'import sys,json; print(json.load(sys.stdin)["terraform_version"])') installed"
}

# Install if a version is requested and terraform isn't already that version
if [[ -n "$TF_VERSION" ]]; then
    if ! command -v terraform &>/dev/null; then
        install_terraform "$TF_VERSION"
    else
        CURRENT=$(terraform version -json 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)["terraform_version"])' 2>/dev/null || echo "")
        WANT="${TF_VERSION#v}"
        if [[ "$CURRENT" != "$WANT" ]]; then
            install_terraform "$TF_VERSION"
        else
            log "Terraform ${CURRENT} already installed."
        fi
    fi
else
    if command -v terraform &>/dev/null; then
        log "Using terraform from PATH: $(terraform version -json | python3 -c 'import sys,json; print(json.load(sys.stdin)["terraform_version"])' 2>/dev/null || terraform version | head -1)"
    else
        error "No terraform-version specified and terraform not found in PATH."
        exit 1
    fi
fi

# ── Run main Python script ────────────────────────────────────────────────────
exec python3 /action/src/main.py
