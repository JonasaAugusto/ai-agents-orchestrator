"""Authenticated loopback transport for lite; Unix peer credentials remain in full mode."""
import json
import re
import socket


def endpoint(path):
    d = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(d, dict) or d.get("host") != "127.0.0.1"
            or type(d.get("port")) is not int or not 1 <= d["port"] <= 65535
            or not isinstance(d.get("token"), str) or not re.fullmatch(r"[0-9a-f]{64}", d["token"])):
        raise ValueError("invalid local endpoint")
    return d


def connect(cfg, timeout):
    if cfg.use_tcp:
        d = endpoint(cfg.socket_path)
        return socket.create_connection((d["host"], d["port"]), timeout=timeout), d["token"]
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.settimeout(timeout)
        c.connect(str(cfg.socket_path))
        return c, None
    except BaseException:
        c.close()
        raise
