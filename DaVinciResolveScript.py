#!/usr/bin/env python3
"""
DaVinciResolveScript.py  --  BRIDGE SHIM (installed over Blackmagic's original).

The original is a 40-line loader that dlopen()s fusionscript.so and exposes
scriptapp(). On the FREE version, scriptapp("Resolve") returns None from an
external process, so davinci-resolve-mcp / Claude Code cannot connect.

This shim keeps the same public surface but makes scriptapp() try, in order:
  1) the in-Resolve bridge server (resolve_bridge_server.py) -- works on FREE;
  2) the real fusionscript.so -- so Studio users and every other tool are
     unaffected when the bridge isn't running.

The original loader is preserved verbatim as `_load_real()` below, and a copy of
Blackmagic's file is kept next to this one as `DaVinciResolveScript_original.py`.
To uninstall: restore that original over this file.
"""

import os
import sys

_BRIDGE_DIR = os.path.expanduser("~/.resolve-free-bridge")
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

# The external bridge client (pure stdlib; no fusionscript dependency).
try:
    import bridge_client as _bridge
except Exception:
    _bridge = None


# --- original Blackmagic loader (verbatim behavior), used as fallback --------

def _load_dynamic(module_name, file_path):
    if sys.version_info[0] >= 3 and sys.version_info[1] >= 5:
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.ExtensionFileLoader(module_name, file_path)
        spec = importlib.util.spec_from_loader(module_name, loader)
        module = importlib.util.module_from_spec(spec) if spec else None
        if module:
            loader.exec_module(module)
        return module
    else:
        import imp
        return imp.load_dynamic(module_name, file_path)


_real = None
_real_tried = False


def _load_real():
    global _real, _real_tried
    if _real_tried:
        return _real
    _real_tried = True
    module = None
    try:
        import fusionscript as module
    except ImportError:
        lib_path = os.getenv("RESOLVE_SCRIPT_LIB")
        if lib_path:
            try:
                module = _load_dynamic("fusionscript", lib_path)
            except ImportError:
                module = None
        if not module:
            ext = ".so"
            path = ""
            if sys.platform.startswith("darwin"):
                path = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/"
            elif sys.platform.startswith("win") or sys.platform.startswith("cygwin"):
                ext = ".dll"
                path = "C:\\Program Files\\Blackmagic Design\\DaVinci Resolve\\"
            elif sys.platform.startswith("linux"):
                path = "/opt/resolve/libs/Fusion/"
            try:
                module = _load_dynamic("fusionscript", path + "fusionscript" + ext)
            except Exception:
                module = None
    _real = module
    return _real


# --- public surface ----------------------------------------------------------

def scriptapp(name="Resolve", *args, **kwargs):
    # 1) the in-Resolve bridge -- the only path that works on the FREE version.
    if _bridge is not None:
        try:
            proxy = _bridge.scriptapp(name)
            if proxy is not None:
                return proxy
        except Exception:
            pass
    # 2) fall back to the genuine fusionscript.so (Studio / future).
    real = _load_real()
    if real is not None:
        try:
            return real.scriptapp(name, *args, **kwargs)
        except Exception:
            return None
    return None


def bridge_is_up():
    return bool(_bridge is not None and _bridge.is_bridge_up())


def __getattr__(name):
    # Delegate anything else (rarely used) to the real module.
    real = _load_real()
    if real is not None and hasattr(real, name):
        return getattr(real, name)
    raise AttributeError("module 'DaVinciResolveScript' has no attribute %r" % name)
