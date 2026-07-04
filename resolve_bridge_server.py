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


def _ping_existing():
    try:
        s = socket.create_connection((HOST, PORT), timeout=1.5)
        payload = json.dumps({"op": "ping"}).encode("utf-8")
        s.sendall(struct.pack(">I", len(payload)) + payload)
        hdr = _recvn(s, 4)
        if not hdr:
            s.close()
            return False
        (ln,) = struct.unpack(">I", hdr)
        body = _recvn(s, ln)
        s.close()
        return bool(json.loads(body.decode("utf-8")).get("ok"))
    except Exception:
        return False


_SERVER = None
_THREAD = None


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
        if _ping_existing():
            _log("bridge already running on %s:%d -- nothing to do." % (HOST, PORT))
            _write_status(True, {"note": "already running", "product": product,
                                 "version": version})
        else:
            _log("cannot bind %s:%d (%s) and it is not our bridge." % (HOST, PORT, e))
            _write_status(False, {"error": "bind failed: %s" % e})
        return False

    _log("LISTENING on %s:%d  (source=%s). Leave Resolve open." % (HOST, PORT, RESOLVE_SRC))
    _write_status(True, {"product": product, "version": version, "source": RESOLVE_SRC,
                         "listening": True})

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
