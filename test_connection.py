#!/usr/bin/env python3
"""
Quick external check that the bridge is live. Run with the MCP's venv python:

  RESOLVE_SCRIPT_API="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting" \
  PYTHONPATH="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules" \
  "$HOME/Library/Application Support/davinci-resolve-mcp/venv/bin/python" \
  "$HOME/.resolve-free-bridge/test_connection.py"
"""
import DaVinciResolveScript as dvr

r = dvr.scriptapp("Resolve")
if r is None:
    print("NOT CONNECTED -- the in-Resolve bridge server isn't running.")
    print("Open DaVinci Resolve and run resolve_bridge_server (Workspace -> Scripts),")
    print("then try again.")
    raise SystemExit(1)

print("CONNECTED:", r.GetProductName(), r.GetVersionString())
pm = r.GetProjectManager()
proj = pm.GetCurrentProject()
if proj:
    print("Current project :", proj.GetName())
    tl = proj.GetCurrentTimeline()
    print("Current timeline:", tl.GetName() if tl else "(none)")
    print("Timeline count  :", proj.GetTimelineCount())
else:
    print("Connected, but no project is open in Resolve.")
