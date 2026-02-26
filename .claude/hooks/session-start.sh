#!/bin/bash
set -euo pipefail

# Only run in remote (Claude Code on the web) environments
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# ── Install Python dependencies ──
pip install -q -r requirements.txt 2>/dev/null || pip install -q requests websocket-client python-dotenv numpy 2>/dev/null || true

# ── Install pytest if missing ──
pip install -q pytest 2>/dev/null || true
