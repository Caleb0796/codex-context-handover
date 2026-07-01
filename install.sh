#!/usr/bin/env bash
# Install the Codex context-handover hook into ~/.codex/hooks/ and print the config to add.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
HOOK_DIR="$CODEX_HOME/hooks"
HOOK_PATH="$HOOK_DIR/context-handover.py"

mkdir -p "$HOOK_DIR"
cp "$SRC_DIR/context-handover.py" "$HOOK_PATH"
chmod +x "$HOOK_PATH"
echo "Installed: $HOOK_PATH"

CONFIG="$CODEX_HOME/config.toml"
echo
if grep -q "context-handover.py" "$CONFIG" 2>/dev/null; then
  echo "config.toml already references context-handover.py — nothing to add."
else
  echo "Add this to $CONFIG (Codex hook commands are NOT shell-expanded, so the absolute path is required):"
  cat <<EOF

[hooks]
PreCompact = [{ hooks = [{ type = "command", command = "$HOOK_PATH", async = false, statusMessage = "Writing context handover" }] }]
PostCompact = [{ hooks = [{ type = "command", command = "$HOOK_PATH", async = false, statusMessage = "Refreshing context handover" }] }]
SessionStart = [{ hooks = [{ type = "command", command = "$HOOK_PATH", async = false, statusMessage = "Checking for context handover" }] }]
PreToolUse = [{ hooks = [{ type = "command", command = "$HOOK_PATH", async = false, timeout = 30 }] }]
UserPromptSubmit = [{ hooks = [{ type = "command", command = "$HOOK_PATH", async = false, statusMessage = "Injecting latest context handover" }] }]
EOF
  echo
  echo "(If you already have a [hooks] table, merge these five keys into it.)"
fi
echo
echo "Codex will prompt you to trust the hooks the first time they fire (Settings -> Hooks)."
echo "Optional: run ./set-auto-compact-limits.py to make Codex auto-compact at ~75% (see README)."
