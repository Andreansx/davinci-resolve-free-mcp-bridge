#!/usr/bin/env bash
#
# install.sh -- set up the DaVinci Resolve free-version MCP bridge (macOS).
#
# What it touches (all reversible with ./uninstall.sh):
#   1. Copies the bridge files into           ~/.resolve-free-bridge/
#   2. Backs up Blackmagic's module           .../Developer/Scripting/Modules/DaVinciResolveScript.py
#      -> DaVinciResolveScript_original.py, and installs the shim over it.
#   3. Deploys resolve_bridge_server.py into   .../Fusion/Scripts/{Utility,Comp,Edit}/
#   4. (--with-mcp) registers the davinci-resolve MCP server at user scope.
#
# Usage:  ./install.sh [--with-mcp] [--port N]
#
set -euo pipefail

PORT="21474"
WITH_MCP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --with-mcp) WITH_MCP=1 ;;
    --port) PORT="$2"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$HOME/.resolve-free-bridge"
MODDIR="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
SCRIPTS="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts"
MCP_DIR="${DAVINCI_MCP_DIR:-$HOME/Library/Application Support/davinci-resolve-mcp}"

if [ ! -d "$MODDIR" ]; then
  echo "error: Resolve scripting Modules dir not found:" >&2
  echo "  $MODDIR" >&2
  echo "Is DaVinci Resolve installed?" >&2
  exit 1
fi

echo "==> 1. copy bridge files -> $BRIDGE_DIR"
mkdir -p "$BRIDGE_DIR"
cp -f "$SRC_DIR/resolve_bridge_server.py" "$BRIDGE_DIR/"
cp -f "$SRC_DIR/bridge_client.py"          "$BRIDGE_DIR/"
cp -f "$SRC_DIR/test_connection.py"        "$BRIDGE_DIR/"
cp -f "$SRC_DIR/DaVinciResolveScript.py"   "$BRIDGE_DIR/DaVinciResolveScript_shim.py"

echo "==> 2. install shim over Blackmagic's module (with one-time backup)"
ORIG="$MODDIR/DaVinciResolveScript.py"
if [ -f "$ORIG" ] && ! grep -q "resolve-free-bridge" "$ORIG"; then
  # current file is the genuine Blackmagic loader -- preserve it once
  if [ ! -f "$MODDIR/DaVinciResolveScript_original.py" ]; then
    cp -p "$ORIG" "$MODDIR/DaVinciResolveScript_original.py"
    echo "    backed up original -> DaVinciResolveScript_original.py"
  fi
  cp -p "$ORIG" "$BRIDGE_DIR/DaVinciResolveScript_original.py" || true
fi
cp -f "$SRC_DIR/DaVinciResolveScript.py" "$ORIG"
rm -f "$MODDIR/__pycache__/DaVinciResolveScript."*.pyc 2>/dev/null || true
echo "    installed shim -> $ORIG"

echo "==> 3. deploy menu script -> Workspace -> Scripts"
for cat in Utility Comp Edit; do
  mkdir -p "$SCRIPTS/$cat"
  cp -f "$SRC_DIR/resolve_bridge_server.py" "$SCRIPTS/$cat/resolve_bridge_server.py"
  echo "    -> $cat/resolve_bridge_server.py"
done

if [ "$WITH_MCP" = "1" ]; then
  echo "==> 4. register davinci-resolve MCP (user scope)"
  if ! command -v claude >/dev/null 2>&1; then
    echo "    claude CLI not found; skipping MCP registration." >&2
  elif [ ! -f "$MCP_DIR/venv/bin/python" ] || [ ! -f "$MCP_DIR/src/server.py" ]; then
    echo "    davinci-resolve-mcp not found at: $MCP_DIR" >&2
    echo "    set DAVINCI_MCP_DIR=... and re-run with --with-mcp to register." >&2
  else
    JSON=$(cat <<JSON
{
  "command": "$MCP_DIR/venv/bin/python",
  "args": ["$MCP_DIR/src/server.py"],
  "env": {
    "RESOLVE_SCRIPT_API": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
    "RESOLVE_SCRIPT_LIB": "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
    "PYTHONPATH": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
    "RESOLVE_BRIDGE_PORT": "$PORT"
  }
}
JSON
)
    claude mcp remove davinci-resolve -s user >/dev/null 2>&1 || true
    claude mcp add-json davinci-resolve "$JSON" -s user
  fi
fi

echo
echo "Done. Next: open DaVinci Resolve, then run 'resolve_bridge_server' from"
echo "Workspace -> Scripts (once per Resolve launch). Verify with:"
echo "  PYTHONPATH=\"$MODDIR\" python3 \"$BRIDGE_DIR/test_connection.py\""
