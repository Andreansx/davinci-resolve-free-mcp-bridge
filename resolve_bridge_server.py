#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
resolve_bridge_server.py  --  RUN THIS INSIDE DAVINCI RESOLVE (free or Studio).

Two ways to run it:

  A) Menu:    Workspace  ->  Scripts  ->  Utility  ->  resolve_bridge_server
              Resolve runs this in its own short-lived child process, so the
              script BLOCKS here to keep that process (and its live Resolve
              handle) alive. That is correct -- it does NOT freeze Resolve's UI.
  B) Console: Fusion page -> Console -> switch to Py3 -> paste (wrap in a thread
              so the console tab stays usable):
                 import os, threading
                 threading.Thread(target=lambda: exec(open(os.path.expanduser(
                   "~/.resolve-free-bridge/resolve_bridge_server.py")).read()),
                   daemon=True).start()

What it does
------------
The FREE version of Resolve blocks *external* scripting (fusionscript's
scriptapp("Resolve") returns None from an outside process). But a script running
*inside* Resolve can grab the live app via the undocumented in-app handle
`app.GetResolve()`. This script does exactly that, then exposes the whole
scripting API over a tiny localhost-only RPC server so an external process
(davinci-resolve-mcp / Claude Code) can drive Resolve through it.

It binds 127.0.0.1 only -- nothing leaves the machine. Leave Resolve open; the
server dies with Resolve. Re-run once per Resolve launch (a single menu click).

Self-healing: a watchdog shuts the server down if its Resolve handle dies, so a
crashed/relaunched Resolve does not leave a zombie bridge squatting the port.
And a fresh launch that finds a stale bridge already on the port reclaims it
(asks it to quit, or evicts a legacy orphan) instead of giving up.

Compatible with the Python that Resolve embeds (3.6+). Pure stdlib.
"""

import os
import sys
import json
import time
import socket
import struct
import threading
import traceback

try:
    import socketserver          # py3
except ImportError:              # pragma: no cover
    import SocketServer as socketserver  # py2 (not expected)

HOST = os.environ.get("RESOLVE_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("RESOLVE_BRIDGE_PORT", "21474"))
BRIDGE_DIR = os.path.expanduser("~/.resolve-free-bridge")
LOG_PATH = os.path.join(BRIDGE_DIR, "bridge.log")
STATUS_PATH = os.path.join(BRIDGE_DIR, "bridge_status.json")

_PRIMS = (bool, int, float, str)


# --- logging / status --------------------------------------------------------

def _log(msg):
    try:
        os.makedirs(BRIDGE_DIR, exist_ok=True)
    except Exception:
        pass
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] resolve-bridge: %s" % (stamp, msg)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


def _write_status(ok, extra=None):
    st = {
        "ok": ok,
        "host": HOST,
        "port": PORT,
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if extra:
        st.update(extra)
    try:
        os.makedirs(BRIDGE_DIR, exist_ok=True)
        with open(STATUS_PATH, "w") as f:
            json.dump(st, f, indent=2)
    except Exception as e:
        _log("could not write status file: %s" % e)


# --- obtain the live Resolve object (the free-version trick) -----------------

def _verify(cand):
    if cand is None:
        return None
    try:
        if cand.GetProductName():
            return cand
    except Exception:
        return None
    return None


def _get_resolve():
    """Try every in-app route to a working Resolve handle. Returns (obj, how)."""
    g = globals()

    # 1) Resolve injects a working 'resolve' into menu/console scripts.
    r = _verify(g.get("resolve"))
    if r is not None:
        return r, "injected global 'resolve'"

    # 2) app.GetResolve() -- the documented-nowhere trick that works on FREE.
    for nm in ("app", "fusion", "fu", "bmd"):
        obj = g.get(nm)
        if obj is None:
            continue
        getres = getattr(obj, "GetResolve", None)
        if callable(getres):
            try:
                r = _verify(getres())
                if r is not None:
                    return r, "%s.GetResolve()" % nm
            except Exception as e:
                _log("%s.GetResolve() failed: %s" % (nm, e))

    # 3) bmd.scriptapp('Resolve') from inside the app.
    bmd = g.get("bmd")
    if bmd is not None:
        sa = getattr(bmd, "scriptapp", None)
        if callable(sa):
            try:
                r = _verify(sa("Resolve"))
                if r is not None:
                    return r, "bmd.scriptapp('Resolve')"
            except Exception as e:
                _log("bmd.scriptapp failed: %s" % e)

    # 4) last resort: fusionscript C module directly.
    try:
        import fusionscript as _fs
        r = _verify(_fs.scriptapp("Resolve"))
        if r is not None:
            return r, "fusionscript.scriptapp('Resolve')"
    except Exception as e:
        _log("fusionscript route failed: %s" % e)

    return None, None


# --- object registry + marshaling --------------------------------------------

class _Registry:
    def __init__(self):
        self._objs = {}
        self._next = 1
        self._lock = threading.Lock()

    def put(self, obj):
        with self._lock:
            hid = self._next
            self._next += 1
            self._objs[hid] = obj
            return hid

    def get(self, hid):
        return self._objs[hid]

    def release(self, hid):
        with self._lock:
            self._objs.pop(hid, None)

    def clear(self):
        with self._lock:
            self._objs.clear()


REG = _Registry()
API_LOCK = threading.RLock()   # serialize ALL access to the Resolve API

RESOLVE = None
RESOLVE_SRC = None


def _marshal(x):
    if x is None or isinstance(x, _PRIMS):
        return {"k": "v", "v": x}
    if isinstance(x, (list, tuple)):
        return {"k": "list", "v": [_marshal(i) for i in x]}
    if isinstance(x, dict):
        return {"k": "dict", "v": dict((str(k), _marshal(v)) for k, v in x.items())}
    # opaque Resolve API object -> stored, referenced by handle
    hid = REG.put(x)
    try:
        typ = type(x).__name__
    except Exception:
        typ = "object"
    return {"k": "obj", "id": hid, "repr": typ}


def _unmarshal_arg(m):
    if not isinstance(m, dict):
        return m
    k = m.get("k")
    if k == "v":
        return m.get("v")
    if k == "obj":
        return REG.get(m["id"])
    if k == "list":
        return [_unmarshal_arg(i) for i in m.get("v", [])]
    if k == "dict":
        return dict((key, _unmarshal_arg(v)) for key, v in m.get("v", {}).items())
    return None


# --- request dispatch --------------------------------------------------------

def _handle(req):
    global RESOLVE, RESOLVE_SRC
    op = req.get("op")

    if op == "ping":
        return {"ok": True, "result": {"k": "v", "v": "pong"}}

    if op == "root":
        name = req.get("name", "Resolve")
        with API_LOCK:
            if name in (None, "Resolve"):
                if _verify(RESOLVE) is None:          # refresh a stale handle
                    RESOLVE, RESOLVE_SRC = _get_resolve()
                obj = RESOLVE
            elif name in ("Fusion", "fusion"):
                obj = globals().get("fusion") or globals().get("app")
            else:
                bmd = globals().get("bmd")
                obj = bmd.scriptapp(name) if bmd is not None else None
            if obj is None:
                return {"ok": False, "error": "root '%s' unavailable" % name}
            return {"ok": True, "result": _marshal(obj)}

    if op == "call":
        args = [_unmarshal_arg(a) for a in req.get("args", [])]
        kwargs = dict((k, _unmarshal_arg(v)) for k, v in req.get("kwargs", {}).items())
        with API_LOCK:
            target = REG.get(req["handle"])
            fn = getattr(target, req["method"])
            return {"ok": True, "result": _marshal(fn(*args, **kwargs))}

    if op == "getattr":
        with API_LOCK:
            target = REG.get(req["handle"])
            return {"ok": True, "result": _marshal(getattr(target, req["name"]))}

    if op == "index":
        with API_LOCK:
            target = REG.get(req["handle"])
            return {"ok": True, "result": _marshal(target[req["index"]])}

    if op == "release":
        REG.release(req["handle"])
        return {"ok": True, "result": {"k": "v", "v": None}}

    if op == "reset":
        REG.clear()
        return {"ok": True, "result": {"k": "v", "v": None}}

    if op == "health":
        # Cheap liveness of the Resolve handle -- lets a client (or a re-launching
        # instance) tell a working bridge apart from a stale one whose Resolve died.
        with API_LOCK:
            alive = _verify(RESOLVE) is not None
        return {"ok": True, "result": {"k": "v", "v": alive}}

    if op == "shutdown":
        # Let a client (typically a fresh instance reclaiming the port) ask this
        # server to stop. Scheduled off-thread so this response still flushes.
        _log("shutdown requested by client -- stopping bridge.")
        _schedule_shutdown()
        return {"ok": True, "result": {"k": "v", "v": "stopping"}}

    return {"ok": False, "error": "unknown op %r" % op}


# --- TCP server --------------------------------------------------------------

def _recvn(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        while True:
            hdr = _recvn(sock, 4)
            if hdr is None:
                break
            (ln,) = struct.unpack(">I", hdr)
            body = _recvn(sock, ln)
            if body is None:
                break
            try:
                resp = _handle(json.loads(body.decode("utf-8")))
            except Exception as e:
                resp = {"ok": False,
                        "error": "%s: %s" % (type(e).__name__, e),
                        "traceback": traceback.format_exc()}
            data = json.dumps(resp).encode("utf-8")
            try:
                sock.sendall(struct.pack(">I", len(data)) + data)
            except Exception:
                break


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _rpc_once(req, timeout=2.0):
    """Open a throwaway connection to whatever holds our port, send one request,
    and return the decoded response dict (or None on any failure)."""
    try:
        s = socket.create_connection((HOST, PORT), timeout=timeout)
        s.settimeout(timeout)
        try:
            payload = json.dumps(req).encode("utf-8")
            s.sendall(struct.pack(">I", len(payload)) + payload)
            hdr = _recvn(s, 4)
            if not hdr:
                return None
            (ln,) = struct.unpack(">I", hdr)
            body = _recvn(s, ln)
            if body is None:
                return None
            return json.loads(body.decode("utf-8"))
        finally:
            s.close()
    except Exception:
        return None


def _probe_existing():
    """Classify whatever already holds our port:
        "down"    -- nothing usable answers
        "healthy" -- one of our bridges, with a LIVE Resolve handle
        "stale"   -- one of our bridges, but its Resolve handle is dead
        "foreign" -- answers, but does not speak our protocol
    """
    pong = _rpc_once({"op": "ping"})
    if pong is None:
        return "down"
    if not (isinstance(pong, dict) and pong.get("ok")):
        return "foreign"
    # It is our bridge. Ask whether its Resolve handle is still alive.
    h = _rpc_once({"op": "health"})
    if isinstance(h, dict) and h.get("ok") and isinstance(h.get("result"), dict):
        return "healthy" if h["result"].get("v") else "stale"
    # Older bridge with no 'health' op: fall back to a 'root' probe. Server-side
    # that already tries to refresh a stale handle and fails when Resolve is gone.
    r = _rpc_once({"op": "root", "name": "Resolve"})
    if isinstance(r, dict) and r.get("ok"):
        return "healthy"
    if isinstance(r, dict):
        return "stale"
    return "foreign"


def _shutdown_existing():
    """Ask an existing bridge to stop. True if it accepted the request."""
    r = _rpc_once({"op": "shutdown"})
    return bool(isinstance(r, dict) and r.get("ok"))


def _squatting_pids():
    """PIDs LISTENing on our port that are clearly one of OUR bridge processes
    (their command line runs resolve_bridge_server) -- never the Resolve app
    itself. POSIX best-effort; returns [] when it cannot tell."""
    pids = []
    try:
        import subprocess
        out = subprocess.check_output(
            ["lsof", "-nP", "-iTCP:%d" % PORT, "-sTCP:LISTEN", "-t"],
            stderr=subprocess.DEVNULL).decode("utf-8", "replace")
    except Exception:
        return pids
    for tok in out.split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            import subprocess
            cmd = subprocess.check_output(
                ["ps", "-o", "command=", "-p", str(pid)],
                stderr=subprocess.DEVNULL).decode("utf-8", "replace")
        except Exception:
            cmd = ""
        if "resolve_bridge_server" in cmd:
            pids.append(pid)
    return pids


def _evict_squatter():
    """Last resort for a legacy orphan that predates the 'shutdown' op: SIGTERM
    any of OUR bridge processes still holding the port. Guarded to never touch
    the Resolve app process. True if it signalled at least one."""
    import signal
    killed = False
    for pid in _squatting_pids():
        try:
            os.kill(pid, signal.SIGTERM)
            _log("evicted stale bridge process pid %d (held port %d)." % (pid, PORT))
            killed = True
        except Exception as e:
            _log("could not signal stale bridge pid %d: %s" % (pid, e))
    return killed


def _try_bind(retries=20, delay=0.25):
    """Bind the server, retrying while a just-evicted predecessor releases the
    port. Returns the server, or None if the port never frees."""
    for _ in range(retries):
        try:
            return _Server((HOST, PORT), _Handler)
        except OSError:
            time.sleep(delay)
    return None


_SERVER = None
_THREAD = None
_WATCHDOG = None


def _schedule_shutdown(delay=0.2):
    """Stop the server from OUTSIDE its serving loop, after the current response
    has a moment to flush. Safe to call from a request-handler thread."""
    def _stop():
        time.sleep(delay)
        srv = _SERVER
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
    threading.Thread(target=_stop, name="resolve-bridge-stop", daemon=True).start()


def _watchdog(interval=15.0, max_failures=3):
    """Self-terminate if Resolve goes away, so a dead bridge never squats the
    port after Resolve quits or restarts (the bug that used to force a manual
    kill). Needs several consecutive failures so a momentarily-busy Resolve does
    not trip it."""
    global RESOLVE, RESOLVE_SRC
    fails = 0
    while True:
        time.sleep(interval)
        srv = _SERVER
        if srv is None:
            return
        with API_LOCK:
            alive = _verify(RESOLVE) is not None
            if not alive:                      # one refresh attempt before counting
                RESOLVE, RESOLVE_SRC = _get_resolve()
                alive = RESOLVE is not None
        if alive:
            fails = 0
            continue
        fails += 1
        _log("watchdog: Resolve handle unavailable (%d/%d)." % (fails, max_failures))
        if fails >= max_failures:
            _log("watchdog: Resolve is gone -- shutting down bridge to free port %d." % PORT)
            try:
                srv.shutdown()
            except Exception:
                pass
            return


def start(block=True):
    global RESOLVE, RESOLVE_SRC, _SERVER, _THREAD

    _log("starting (python %s, pid %d)" % (sys.version.split()[0], os.getpid()))

    RESOLVE, RESOLVE_SRC = _get_resolve()
    if RESOLVE is None:
        _log("ERROR: could not obtain a Resolve object. Run this INSIDE DaVinci "
             "Resolve (Workspace->Scripts, or the Fusion-page Console in Py3 mode).")
        _write_status(False, {"error": "no Resolve object -- run inside Resolve"})
        return False

    try:
        product = RESOLVE.GetProductName()
        version = RESOLVE.GetVersionString()
    except Exception as e:
        product, version = "?", "?"
        _log("warning: version query failed: %s" % e)
    _log("got Resolve: %s %s  (via %s)" % (product, version, RESOLVE_SRC))

    try:
        _SERVER = _Server((HOST, PORT), _Handler)
    except OSError as e:
        status = _probe_existing()
        if status == "healthy":
            _log("bridge already running (healthy) on %s:%d -- nothing to do." % (HOST, PORT))
            _write_status(True, {"note": "already running", "product": product,
                                 "version": version})
            return False
        if status == "foreign":
            _log("cannot bind %s:%d (%s): a non-bridge process holds the port." % (HOST, PORT, e))
            _write_status(False, {"error": "port held by foreign process: %s" % e})
            return False
        # "stale" (dead Resolve handle) or "down" (half-open socket): the old bug
        # left a zombie bridge here and gave up. Now we reclaim the port instead.
        _log("existing bridge on %s:%d is %s -- reclaiming the port." % (HOST, PORT, status))
        if not _shutdown_existing():     # newer bridges stop themselves on request
            _evict_squatter()            # legacy orphan with no 'shutdown' op
        _SERVER = _try_bind()
        if _SERVER is None:
            _log("could not reclaim port %d from the stale bridge -- kill it by hand "
                 "(lsof -nP -iTCP:%d -sTCP:LISTEN) and re-run." % (PORT, PORT))
            _write_status(False, {"error": "reclaim failed on port %d" % PORT})
            return False
        _log("reclaimed port %d from the stale bridge." % PORT)

    _log("LISTENING on %s:%d  (source=%s). Leave Resolve open." % (HOST, PORT, RESOLVE_SRC))
    _write_status(True, {"product": product, "version": version, "source": RESOLVE_SRC,
                         "listening": True})

    global _WATCHDOG
    _WATCHDOG = threading.Thread(target=_watchdog, name="resolve-bridge-watchdog",
                                 daemon=True)
    _WATCHDOG.start()

    if not block:
        # Background mode -- for tests, or a persistent interpreter (Fusion Console)
        # where the thread genuinely survives after the call returns.
        _THREAD = threading.Thread(target=_SERVER.serve_forever, name="resolve-bridge",
                                   daemon=True)
        _THREAD.start()
        return True

    # Foreground/blocking mode -- the important one. Resolve runs a Workspace->Scripts
    # item in a SHORT-LIVED SUBPROCESS: if we returned here, that process would exit
    # and take the listening socket with it. So we block and keep serving. This runs
    # in its own process, so it does NOT freeze Resolve's UI.
    _log("serving (blocking) -- keep Resolve open. Re-run this script after each launch.")
    try:
        _SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            _SERVER.shutdown()
        except Exception:
            pass
        _log("server stopped.")
    return True


# Runs on import/exec/menu-invocation. Blocks (see above) so the Resolve-spawned
# process stays alive and keeps the bridge port open.
start()
