# DaVinci Resolve (free) MCP bridge

Drive the **free** (non-Studio) version of DaVinci Resolve from
[`davinci-resolve-mcp`](https://github.com/samuelgursky/davinci-resolve-mcp) /
Claude Code / any external Python -- even though the free version blocks external
scripting.

## The problem

Blackmagic disables *external* scripting on the free version. From an outside
process, the scripting entry point returns nothing:

```python
import DaVinciResolveScript as dvr
dvr.scriptapp("Resolve")   # -> None  on the free version
```

So `davinci-resolve-mcp` (and anything else that scripts Resolve from outside)
cannot connect.

## The trick this is built on

The free version still lets a script running **inside** Resolve reach the live
app through an undocumented in-app handle:

```python
resolve = app.GetResolve()   # works INSIDE Resolve, even on the free version
```

(`app` is the running Fusion instance that Resolve injects into its scripting
environment. This works because you reuse the existing in-process connection
instead of creating a new external one.)

## How the bridge works

A tiny script runs **inside** Resolve, grabs `app.GetResolve()`, and exposes the
whole scripting API over a localhost-only TCP socket. An installed shim makes the
*external* `DaVinciResolveScript` module transparently proxy every call through
that socket -- so `davinci-resolve-mcp` works unmodified.

```
Claude Code --stdio--> davinci-resolve-mcp (external python)
                          |  import DaVinciResolveScript -> scriptapp("Resolve")
                          v
                  [shim] DaVinciResolveScript.py   -- returns a transparent proxy
                          |  length-prefixed JSON over TCP 127.0.0.1:21474
                          v
                  resolve_bridge_server.py   -- runs INSIDE Resolve (blocking)
                          |  resolve = app.GetResolve()      <- the free trick
                          v
                  live DaVinci Resolve scripting API
```

Method calls are marshaled to Resolve, executed against the live API, and
returned: primitives come back by value, API objects come back as new proxies,
and proxies passed back as arguments keep their identity. Only `127.0.0.1` is
used -- nothing leaves the machine.

## Files

| File | Role |
|---|---|
| `resolve_bridge_server.py` | Runs **inside** Resolve; serves the API over the socket. |
| `bridge_client.py` | External proxy/client (pure stdlib). |
| `DaVinciResolveScript.py` | Shim installed **over** Blackmagic's module; tries the bridge, falls back to the real `fusionscript` library when the bridge is down (so Studio and other tools are unaffected). |
| `test_connection.py` | Quick "is it live?" check. |
| `install.sh` / `uninstall.sh` | Set up / tear down (macOS). |

## Requirements

- macOS (paths below are macOS; the approach is portable, the installer is not).
- DaVinci Resolve (free is fine), with working **Python 3** scripting inside
  Resolve (the Fusion Console Py3 must work). A python.org 3.x build is the most
  reliable. The internal Python version does **not** need to match the external
  one -- they only talk over a socket.
- `davinci-resolve-mcp` installed, if that is your client.

## Install (macOS)

```bash
./install.sh
```

This copies the files to `~/.resolve-free-bridge/`, backs up and replaces
Blackmagic's `DaVinciResolveScript.py` with the shim, and deploys
`resolve_bridge_server.py` into Resolve's `Workspace -> Scripts` menu. Pass
`--with-mcp` to also register the `davinci-resolve` MCP server at user scope with
`claude mcp` (requires the Claude Code CLI).

See the top of `install.sh` for exactly what it touches. To do it by hand,
follow the same steps -- they are all plain copies plus one backup.

## Use it -- once per Resolve launch

1. Open DaVinci Resolve and a project.
2. Start the bridge **inside Resolve**:
   - **Menu (simplest):** `Workspace -> Scripts -> resolve_bridge_server`.
     The script keeps running (it blocks in its own child process to hold the
     port open). That is intended and does not freeze Resolve.
   - **Console:** Fusion page -> Console -> set to **Py3** -> paste:
     ```python
     import os, threading
     threading.Thread(target=lambda: exec(open(os.path.expanduser(
       "~/.resolve-free-bridge/resolve_bridge_server.py")).read()),
       daemon=True).start()
     ```
3. Use your MCP tools / external scripts normally.

The bridge dies when Resolve closes, so re-run it after each launch. Resolve runs
`Workspace -> Scripts` items in a short-lived child process, which is why the
script must block to stay alive.

## Verify

```bash
PYTHONPATH="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules" \
python3 ~/.resolve-free-bridge/test_connection.py
```

Expect `CONNECTED: DaVinci Resolve <version>` and your current project/timeline.

## Troubleshooting

- **"not connected" / test prints NOT CONNECTED** -- the in-Resolve server is not
  running. Re-run step 2. Check `~/.resolve-free-bridge/bridge.log` and
  `~/.resolve-free-bridge/bridge_status.json`.
- **The menu item errors, or the Py3 console will not start** -- Resolve cannot
  find/use a compatible Python. Install a python.org build it supports, then in
  Resolve pick it under **Fusion -> Fusion Settings -> Script** (or Preferences ->
  System -> General), restart Resolve, and re-run.
- **Port clash** -- set `RESOLVE_BRIDGE_PORT` to the same value in the external
  client's environment and before launching Resolve.

## Uninstall

```bash
./uninstall.sh
```

Restores Blackmagic's original `DaVinciResolveScript.py`, removes the menu
scripts, and (with `--with-mcp`) unregisters the MCP server.

## Security

The server binds `127.0.0.1` only and speaks a minimal JSON protocol. Any local
process can call it while Resolve is open; treat it like any other localhost dev
service. It executes method calls against the Resolve API on request -- do not run
it on a shared/multi-user machine you do not trust.

## Credits

- The `app.GetResolve()` in-app handle is the key that makes free-version
  scripting possible from inside Resolve.
- Built to work with
  [`davinci-resolve-mcp`](https://github.com/samuelgursky/davinci-resolve-mcp).

## License

MIT -- see [LICENSE](LICENSE).
