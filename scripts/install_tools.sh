#!/usr/bin/env bash
# Install the Alpaca toolchain the hackathon expects: CLI, MCP server, skills.
set -euo pipefail

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }

bold "1/4  uv / uvx (runs the Alpaca MCP server)"
if command -v uvx >/dev/null 2>&1; then
  ok "uvx already installed: $(command -v uvx)"
else
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ok "installed uv - restart your shell if uvx is not on PATH"
fi

bold "2/4  Alpaca CLI"
if command -v alpaca >/dev/null 2>&1; then
  ok "alpaca already installed: $(command -v alpaca)"
elif command -v brew >/dev/null 2>&1; then
  brew install alpacahq/tap/cli && ok "installed via Homebrew"
elif command -v go >/dev/null 2>&1; then
  go install github.com/alpacahq/cli/cmd/alpaca@latest && ok "installed via go install"
else
  warn "install Homebrew or Go, then: brew install alpacahq/tap/cli"
fi

bold "3/4  Alpaca MCP server (warm the uvx cache)"
if command -v uvx >/dev/null 2>&1; then
  uvx alpaca-mcp-server --version >/dev/null 2>&1 && ok "alpaca-mcp-server reachable" \
    || warn "could not run alpaca-mcp-server - check network/uv install"
else
  warn "uvx not on PATH yet - rerun this script after restarting your shell"
fi

bold "4/4  Alpaca agent skills (optional, includes the backtest skill)"
if command -v npx >/dev/null 2>&1; then
  npx --yes skills add alpacahq/alpaca-skills --list 2>/dev/null \
    && ok "skills available - run without --list to install" \
    || warn "npx skills unavailable; clone github.com/alpacahq/alpaca-skills manually"
else
  warn "npx not found - skip, or clone github.com/alpacahq/alpaca-skills"
fi

echo
bold "Next"
cat <<'TXT'
  alpaca profile login --api-key      # authenticate the CLI (paper)
  make doctor                         # verify everything end to end
TXT
