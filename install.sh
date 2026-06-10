#!/usr/bin/env bash
# Cagentic one-click installer
# Supports: macOS (Apple Silicon + Intel), Linux (x86_64, arm64)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/cagentic/main/install.sh | bash
#   — or —
#   bash install.sh [--model <name>] [--no-ollama] [--cloud-only]

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
CYN='\033[0;36m'; BLD='\033[1m'; NC='\033[0m'

info()  { echo -e "${CYN}${BLD}[cagentic]${NC} $*"; }
ok()    { echo -e "${GRN}${BLD}  ✓${NC} $*"; }
warn()  { echo -e "${YLW}${BLD}  !${NC} $*"; }
die()   { echo -e "${RED}${BLD}  ✗${NC} $*"; exit 1; }
hr()    { echo -e "${CYN}──────────────────────────────────────────────${NC}"; }

# ── Banner ────────────────────────────────────────────────────────────────────
hr
echo -e "${BLD}  Cagentic — your local personal AI assistant${NC}"
hr

# ── Argument parsing ──────────────────────────────────────────────────────────
DEFAULT_MODEL="llama3.2"
INSTALL_OLLAMA=true
MODEL="$DEFAULT_MODEL"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)       MODEL="$2"; shift 2;;
    --no-ollama)   INSTALL_OLLAMA=false; shift;;
    --cloud-only)  INSTALL_OLLAMA=false; MODEL=""; shift;;
    --help|-h)
      echo "Usage: $0 [--model <name>] [--no-ollama] [--cloud-only]"
      echo "  --model <name>   Ollama model to pull after install (default: llama3.2)"
      echo "  --no-ollama      Skip Ollama installation"
      echo "  --cloud-only     Skip Ollama entirely (use OpenAI/Anthropic instead)"
      exit 0;;
    *) warn "Unknown option: $1"; shift;;
  esac
done

# ── Step 1: Check Python 3.9+ ─────────────────────────────────────────────────
info "Checking Python..."

PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" &>/dev/null; then
    ver=$("$cmd" -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>/dev/null || echo "0.0")
    major=${ver%%.*}; minor=${ver##*.}
    if [[ $major -ge 3 && $minor -ge 9 ]]; then
      PYTHON="$cmd"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  die "Python 3.9+ is required but not found.\n  Install it from https://python.org or via your package manager:\n    macOS:   brew install python@3.12\n    Ubuntu:  sudo apt install python3.12\n    Fedora:  sudo dnf install python3.12"
fi

ok "Python $("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"

# ── Step 2: Check pip ─────────────────────────────────────────────────────────
info "Checking pip..."

if ! "$PYTHON" -m pip --version &>/dev/null; then
  warn "pip not found, attempting to install via ensurepip..."
  "$PYTHON" -m ensurepip --upgrade 2>/dev/null || die "pip install failed. Install it manually: https://pip.pypa.io"
fi
ok "pip available"

# ── Step 3: Install / check Ollama ───────────────────────────────────────────
if [[ "$INSTALL_OLLAMA" == "true" ]]; then
  info "Checking Ollama..."
  if command -v ollama &>/dev/null; then
    ok "Ollama $(ollama --version 2>/dev/null | head -1 || echo 'installed')"
  else
    warn "Ollama not found. Installing..."
    OS="$(uname -s)"
    ARCH="$(uname -m)"
    if [[ "$OS" == "Darwin" ]]; then
      if command -v brew &>/dev/null; then
        brew install ollama || die "Homebrew install of Ollama failed."
      else
        warn "Homebrew not found. Downloading Ollama..."
        curl -fsSL https://ollama.ai/install.sh | sh || die "Ollama install failed."
      fi
    elif [[ "$OS" == "Linux" ]]; then
      curl -fsSL https://ollama.ai/install.sh | sh || die "Ollama install failed."
    else
      warn "Unsupported OS '$OS'. Install Ollama manually: https://ollama.com"
      INSTALL_OLLAMA=false
    fi
    if command -v ollama &>/dev/null; then
      ok "Ollama installed successfully"
    fi
  fi
fi

# ── Step 4: Install Cagentic ──────────────────────────────────────────────────
info "Installing Cagentic..."

# If we're running from inside the repo, do an editable install.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/pyproject.toml" ]]; then
  info "Installing from local source (editable)..."
  "$PYTHON" -m pip install -e "$SCRIPT_DIR" --quiet || die "pip install -e failed."
  ok "Cagentic installed from $SCRIPT_DIR"
else
  # Install from PyPI (once published)
  "$PYTHON" -m pip install cagentic --upgrade --quiet 2>/dev/null || {
    warn "PyPI install failed (package may not be published yet)."
    warn "Clone the repo and run: pip install -e ."
    die "Could not install Cagentic."
  }
  ok "Cagentic installed from PyPI"
fi

# Verify the command is available
if ! command -v cagentic &>/dev/null; then
  # May need to add pip's script dir to PATH
  PIP_BIN=$("$PYTHON" -m pip show cagentic 2>/dev/null | grep -i 'location' | awk '{print $2}' || echo "")
  warn "cagentic not in PATH. You may need to add pip's bin dir to your shell profile."
  warn "Try:  export PATH=\"\$PATH:\$HOME/.local/bin\""
else
  ok "cagentic command available"
fi

# ── Step 5: Pull default Ollama model ────────────────────────────────────────
if [[ "$INSTALL_OLLAMA" == "true" && -n "$MODEL" ]]; then
  info "Pulling model '$MODEL' (this may take a few minutes)..."
  if command -v ollama &>/dev/null; then
    # Start ollama serve in background if not running
    if ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
      info "Starting Ollama server..."
      ollama serve &>/dev/null &
      OLLAMA_PID=$!
      sleep 3
    fi
    ollama pull "$MODEL" && ok "Model '$MODEL' ready" || warn "Could not pull '$MODEL'. Run: ollama pull $MODEL"
  else
    warn "ollama not found — skipping model pull. Run: ollama pull $MODEL"
  fi
fi

# ── Done ─────────────────────────────────────────────────────────────────────
hr
echo -e "${GRN}${BLD}  Cagentic is ready!${NC}"
hr
echo ""
if [[ "$INSTALL_OLLAMA" == "true" && -n "$MODEL" ]]; then
  echo -e "  Start chatting:   ${BLD}cagentic${NC}"
  echo -e "  Web interface:    ${BLD}cagentic${NC}  then type  ${BLD}/gateway${NC}"
else
  echo -e "  Start with OpenAI:     ${BLD}cagentic -m openai:gpt-4o${NC}"
  echo -e "    (first run: set key) ${BLD}/login openai sk-...${NC}"
  echo -e "  Start with Anthropic:  ${BLD}cagentic -m anthropic:claude-opus-4-8${NC}"
  echo -e "    (first run: set key) ${BLD}/login anthropic sk-ant-...${NC}"
  echo -e "  Web interface:         ${BLD}cagentic${NC}  then type  ${BLD}/gateway${NC}"
fi
echo ""
echo -e "  Switch to a cloud model anytime:"
echo -e "    /login openai sk-...                  (save OpenAI key)"
echo -e "    /model openai:gpt-4o                  (switch model)"
echo -e "    /login anthropic sk-ant-...            (save Anthropic key)"
echo -e "    /model anthropic:claude-opus-4-8       (switch model)"
echo ""
hr
