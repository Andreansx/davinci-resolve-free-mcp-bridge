#!/usr/bin/env python3
"""
bridge_client.py  --  EXTERNAL side of the DaVinci Resolve free-version bridge.

Runs inside the davinci-resolve-mcp process (Claude Code). It connects to the
in-Resolve RPC server (resolve_bridge_server.py) over localhost TCP and returns
a *transparent proxy* for the Resolve object graph. Any method call on the proxy
is marshaled, sent to Resolve, executed against the live scripting API, and the
result is sent back -- objects come back as new proxies, primitives by value.

Wire format: 4-byte big-endian length prefix + a JSON payload.
Marshaling tags: {"k":"v"} value | {"k":"list"} | {"k":"dict"} | {"k":"obj","id"}

Pure stdlib. Nothing here talks to fusionscript.so -- that's the whole point:
it works on the FREE version, which blocks the .so's external scriptapp().
"""

import os
import json
import socket
import struct
import threading

DEFAULT_HOST = os.environ.get("RESOLVE_BRIDGE_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("RESOLVE_BRIDGE_PORT", "21474"))
DEFAULT_TIMEOUT = float(os.environ.get("RESOLVE_BRIDGE_TIMEOUT", "120"))


class BridgeError(Exception):
    """Raised when the in-Resolve server reports a failure for a call."""


# --- framing -----------------------------------------------------------------

def _send(sock, obj):
    data = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recvn(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("bridge closed the connection")
        buf += chunk
    return buf


def _recv(sock):
    (ln,) = struct.unpack(">I", _recvn(sock, 4))
    return json.loads(_recvn(sock, ln).decode("utf-8"))


# --- connection (auto-reconnecting, thread-safe) -----------------------------

class Connection:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._lock = threading.RLock()

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        self._sock = s

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def request(self, req):
        with self._lock:
            last = None
            for attempt in (0, 1):  # one transparent reconnect
                try:
                    if self._sock is None:
                        self._connect()
                    _send(self._sock, req)
                    return _recv(self._sock)
                except (OSError, ConnectionError) as e:
                    last = e
                    self._close()
            raise ConnectionError("bridge unreachable at %s:%d (%s)"
                                  % (self.host, self.port, last))

    def ping(self):
        try:
            return bool(self.request({"op": "ping"}).get("ok"))
        except Exception:
            return False


# one shared connection per (host, port) so repeated scriptapp() calls reuse it
_CONN_CACHE = {}
_CONN_LOCK = threading.Lock()


def _get_conn(host, port):
    with _CONN_LOCK:
        key = (host, port)
        conn = _CONN_CACHE.get(key)
        if conn is None:
            conn = Connection(host, port)
            _CONN_CACHE[key] = conn
        return conn


# --- marshaling --------------------------------------------------------------

def _marshal_arg(x):
    """Client -> server. Proxies become handles; containers recurse."""
    if isinstance(x, Proxy):
        return {"k": "obj", "id": x._bridge_id}
    if x is None or isinstance(x, (bool, int, float, str)):
        return {"k": "v", "v": x}
    if isinstance(x, (list, tuple)):
        return {"k": "list", "v": [_marshal_arg(i) for i in x]}
    if isinstance(x, dict):
        return {"k": "dict", "v": dict((str(k), _marshal_arg(v)) for k, v in x.items())}
    # last resort: hand it over as a raw value and let JSON/Resolve cope
    return {"k": "v", "v": x}


def _unmarshal(m, conn):
    """Server -> client. Object handles become live proxies."""
    if not isinstance(m, dict):
        return m
    k = m.get("k")
    if k == "v":
        return m.get("v")
    if k == "list":
        return [_unmarshal(i, conn) for i in m.get("v", [])]
    if k == "dict":
        return dict((key, _unmarshal(v, conn)) for key, v in m.get("v", {}).items())
    if k == "obj":
        return Proxy(conn, m["id"], m.get("repr"))
    return None


def _check(resp):
    if not resp.get("ok"):
        raise BridgeError(resp.get("error", "unknown bridge error"))


# --- the transparent proxy ---------------------------------------------------

class _Method:
    """A bound remote method: calling it performs the RPC."""
    def __init__(self, conn, hid, name):
        self._conn = conn
        self._id = hid
        self._name = name

    def __call__(self, *args, **kwargs):
        resp = self._conn.request({
            "op": "call",
            "handle": self._id,
            "method": self._name,
            "args": [_marshal_arg(a) for a in args],
            "kwargs": dict((k, _marshal_arg(v)) for k, v in kwargs.items()),
        })
        _check(resp)
        return _unmarshal(resp["result"], self._conn)

    def __repr__(self):
        return "<ResolveBridgeMethod %s>" % self._name


class Proxy:
    """
    Stand-in for a live Resolve API object living inside Resolve. Attribute
    access yields a remote-callable method; the DaVinci API is all method calls
    (GetProjectManager(), GetCurrentProject(), ...), so this is transparent.
    """
    def __init__(self, conn, hid, typ=None):
        # set via __dict__ so __getattr__ never intercepts these
        self.__dict__["_bridge_conn"] = conn
        self.__dict__["_bridge_id"] = hid
        self.__dict__["_bridge_type"] = typ

    def __getattr__(self, name):
        if name.startswith("_bridge_") or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return _Method(self.__dict__["_bridge_conn"], self.__dict__["_bridge_id"], name)

    def __getitem__(self, idx):
        resp = self._bridge_conn.request({
            "op": "index", "handle": self._bridge_id, "index": idx})
        _check(resp)
        return _unmarshal(resp["result"], self._bridge_conn)

    def __eq__(self, other):
        return isinstance(other, Proxy) and other._bridge_id == self._bridge_id

    def __hash__(self):
        return hash(("resolve-bridge", self._bridge_id))

    def __bool__(self):
        return True

    def __repr__(self):
        return "<ResolveBridgeProxy %s #%s>" % (self._bridge_type or "?", self._bridge_id)


# --- entry points used by the DaVinciResolveScript shim ----------------------

def is_bridge_up(host=DEFAULT_HOST, port=DEFAULT_PORT):
    try:
        return _get_conn(host, port).ping()
    except Exception:
        return False


def scriptapp(name="Resolve", host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Return a proxy root for `name` (e.g. "Resolve"), or None if bridge down."""
    try:
        conn = _get_conn(host, port)
        resp = conn.request({"op": "root", "name": name})
        _check(resp)
        return _unmarshal(resp["result"], conn)
    except Exception:
        return None
