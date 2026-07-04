#!/usr/bin/env bash
#
# uninstall.sh -- undo install.sh (macOS).
#
# Usage:  ./uninstall.sh [--with-mcp] [--purge]
#   --with-mcp  also unregister the davinci-resolve MCP server (user scope)
#   --purge     also delete ~/.resolve-free-bridge
#
set -euo pipefail

WITH_MCP=0
PURGE=0
for a in "$@"; do
  case "$a" in
    --with-mcp) WITH_MCP=1 ;;
    --purge) PURGE=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

BRIDGE_DIR="$HOME/.resolve-free-bridge"
MODDIR="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
SCRIPTS="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts"

echo "==> restore Blackmagic's original module"
if [ -f "$MODDIR/DaVinciResolveScript_original.py" ]; then
  cp -f "$MODDIR/DaVinciResolveScript_original.py" "$MODDIR/DaVinciResolveScript.py"
  rm -f "$MODDIR/DaVinciResolveScript_original.py"
  rm -f "$MODDIR/__pycache__/DaVinciResolveScript."*.pyc 2>/dev/null || true
  echo "    restored from DaVinciResolveScript_original.py"
elif [ -f "$BRIDGE_DIR/DaVinciResolveScript_original.py" ]; then
  cp -f "$BRIDGE_DIR/DaVinciResolveScript_original.py" "$MODDIR/DaVinciResolveScript.py"
  rm -f "$MODDIR/__pycache__/DaVinciResolveScript."*.pyc 2>/dev/null || true
  echo "    restored from bridge-dir backup"
else
  echo "    no backup found; leaving the module in place." >&2
fi

echo "==> remove menu scripts"
for cat in Utility Comp Edit; do
  rm -f "$SCRIPTS/$cat/resolve_bridge_server.py" 2>/dev/null || true
done

if [ "$WITH_MCP" = "1" ]; then
  echo "==> unregister davinci-resolve MCP"
  if command -v claude >/dev/null 2>&1; then
    claude mcp remove davinci-resolve -s user >/dev/null 2>&1 || true
  fi
fi

if [ "$PURGE" = "1" ]; then
  echo "==> delete $BRIDGE_DIR"
  rm -rf "$BRIDGE_DIR"
fi

echo "Done."
