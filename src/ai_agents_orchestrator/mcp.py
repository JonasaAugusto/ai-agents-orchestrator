"""Minimal MCP server (stdio, newline-delimited JSON-RPC 2.0) exposing what the CLI already does safely.

Tools: delegate, status, result, review. There is deliberately NO publish tool: merging and pushing needs the
human's interactive approval in the CLI (`aao publish`). No third-party dependency.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable

from . import __version__

PROTOCOL = "2025-06-18"
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]

TOOLS: list[dict[str, Any]] = [
    {"name": "delegate", "description": "Queue a task for Codex on a registered repository. Returns the task id.",
     "inputSchema": {"type": "object", "required": ["repo", "title"], "additionalProperties": False, "properties": {
         "repo": {"type": "string", "description": "registered repository name"},
         "title": {"type": "string"},
         "briefing": {"type": "string", "description": "markdown briefing for the task"},
         "files": {"type": "array", "items": {"type": "string"}, "description": "paths/globs the task touches"},
         "priority": {"type": "integer"},
         "model": {"type": "string", "description": "Codex model for this task only"},
         "effort": {"type": "string", "enum": EFFORTS, "description": "reasoning effort for this task only"}}}},
    {"name": "status", "description": "Service status: queue, current task and usage windows.",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "result", "description": "Short result of a task: status, branch, diffstat, warnings and report.",
     "inputSchema": {"type": "object", "required": ["id"], "additionalProperties": False, "properties": {
         "id": {"type": "integer"}}}},
    {"name": "review", "description": "Cross-review of a branch by Codex (read-only). Never publishes anything.",
     "inputSchema": {"type": "object", "required": ["repo", "branch"], "additionalProperties": False, "properties": {
         "repo": {"type": "string"}, "branch": {"type": "string"}, "title": {"type": "string"}}}},
]


class ToolError(Exception):
    pass


def _short(t: dict) -> str:
    return "\n".join([f"Status: {t.get('estado')}", f"Branch: {t.get('branch') or '-'}",
                      f"Diffstat: {t.get('diffstat') or '-'}",
                      f"Warnings: {t.get('aviso') or '-'}; error: {t.get('erro') or '-'}",
                      t.get("resumo") or "(no report)"])


def _need(args: dict, key: str, kind: type) -> Any:
    v: Any = args.get(key)
    if not isinstance(v, kind) or isinstance(v, bool) or (isinstance(v, str) and not v.strip()):
        raise ToolError(f"missing or invalid argument: {key}")
    return v


def call_tool(chamar: Callable[[dict], dict], name: str, args: dict) -> str:
    """Run one tool through the same service request the CLI sends; returns text or raises ToolError."""
    if name == "delegate":
        effort = args.get("effort")
        if effort is not None and effort not in EFFORTS:
            raise ToolError("effort must be one of " + ", ".join(EFFORTS))
        files = args.get("files") or []
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise ToolError("files must be a list of strings")
        r = chamar({"cmd": "delegar", "repo": _need(args, "repo", str), "titulo": _need(args, "title", str),
                    "descricao": "", "briefing": args.get("briefing"), "arquivos": files,
                    "prioridade": int(args.get("priority") or 0), "modelo": args.get("model"), "esforco": effort})
        if not r.get("ok"):
            raise ToolError(str(r.get("erro") or r))
        return f"queued task #{r.get('id')}"
    if name == "status":
        r = chamar({"cmd": "status"})
    elif name == "result":
        r = chamar({"cmd": "resultado", "id": _need(args, "id", int), "short": True, "full": False})
        if r.get("ok"):
            return _short(r["tarefa"])
    elif name == "review":
        r = chamar({"cmd": "revisar", "repo": _need(args, "repo", str), "branch": _need(args, "branch", str),
                    "titulo": args.get("titulo") or args.get("title")})
    else:
        raise ToolError(f"unknown tool: {name}")
    if not r.get("ok", True):
        raise ToolError(str(r.get("erro") or r))
    return json.dumps(r, ensure_ascii=False, indent=1, default=str)


def handle(chamar: Callable[[dict], dict], msg: Any) -> dict | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    if not isinstance(msg, dict) or "method" not in msg:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    mid, method = msg.get("id"), msg["method"]
    params: dict = msg["params"] if isinstance(msg.get("params"), dict) else {}
    if "id" not in msg:
        return None  # notifications (initialized, cancelled...) need no answer

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code: int, text: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in SUPPORTED else PROTOCOL,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "aao", "version": __version__},
                   "instructions": "Delegates work to Codex through ai-agents-orchestrator. Publishing is not exposed: "
                                   "the human approves it in the CLI."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return err(-32602, "invalid params")
        if name not in {t["name"] for t in TOOLS}:
            return err(-32602, f"unknown tool: {name}")
        try:
            return ok({"content": [{"type": "text", "text": call_tool(chamar, name, args)}], "isError": False})
        except ToolError as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:  # service down, bad config...: report to the model, keep serving
            return ok({"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True})
    return err(-32601, f"method not found: {method}")


def serve(chamar: Callable[[dict], dict], stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp: dict | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle(chamar, msg)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0
