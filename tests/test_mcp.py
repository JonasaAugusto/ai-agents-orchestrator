"""MCP stdio server: protocol handshake, tool list and calls against a fake service (no publish tool)."""
import io
import json
import unittest
from unittest import mock

from ai_agents_orchestrator import cli, mcp


class FakeClient:
    """Plays the MCP client: writes JSON lines to the server's stdin and reads its stdout."""

    def __init__(self, chamar):
        self.chamar = chamar
        self.n = 0

    def run(self, *messages):
        stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
        stdout = io.StringIO()
        self.assertion = mcp.serve(self.chamar, stdin, stdout)
        return [json.loads(x) for x in stdout.getvalue().splitlines()]

    def req(self, method, params=None):
        self.n += 1
        return {"jsonrpc": "2.0", "id": self.n, "method": method, **({"params": params} if params is not None else {})}


class Protocol(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def chamar(req):
            self.calls.append(req)
            cmd = req["cmd"]
            if cmd == "delegar":
                return {"ok": True, "id": 7}
            if cmd == "resultado":
                return {"ok": True, "tarefa": {"estado": "em_revisao", "branch": "codex/x", "diffstat": "1 file",
                                               "aviso": "", "erro": "", "resumo": "done"}}
            if cmd == "status":
                return {"ok": True, "fila": 2}
            if cmd == "revisar":
                return {"ok": True, "id": 8}
            return {"ok": False, "erro": "nope"}
        self.c = FakeClient(chamar)

    def test_handshake_and_list(self):
        out = self.c.run(self.c.req("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                    "clientInfo": {"name": "t", "version": "0"}}),
                         {"jsonrpc": "2.0", "method": "notifications/initialized"},
                         self.c.req("tools/list"), self.c.req("ping"))
        self.assertEqual(len(out), 3)  # the notification gets no reply
        self.assertEqual(out[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", out[0]["result"]["capabilities"])
        names = [t["name"] for t in out[1]["result"]["tools"]]
        self.assertEqual(names, ["delegate", "status", "result", "review"])
        self.assertNotIn("publish", " ".join(names))
        self.assertEqual(out[2]["result"], {})

    def test_tools_call(self):
        out = self.c.run(
            self.c.req("tools/call", {"name": "delegate", "arguments": {
                "repo": "web-app", "title": "T", "briefing": "do it", "model": "m1", "effort": "high"}}),
            self.c.req("tools/call", {"name": "result", "arguments": {"id": 7}}),
            self.c.req("tools/call", {"name": "status", "arguments": {}}),
            self.c.req("tools/call", {"name": "review", "arguments": {"repo": "web-app", "branch": "b"}}))
        text = [r["result"]["content"][0]["text"] for r in out]
        self.assertTrue(all(r["result"]["isError"] is False for r in out))
        self.assertEqual(text[0], "queued task #7")
        self.assertIn("Branch: codex/x", text[1])
        self.assertEqual(self.calls[0]["modelo"], "m1")
        self.assertEqual(self.calls[0]["esforco"], "high")
        self.assertEqual(self.calls[0]["briefing"], "do it")
        self.assertEqual([c["cmd"] for c in self.calls], ["delegar", "resultado", "status", "revisar"])

    def test_errors_are_reported_not_fatal(self):
        out = self.c.run(
            self.c.req("tools/call", {"name": "publish", "arguments": {"id": 1}}),
            self.c.req("tools/call", {"name": "delegate", "arguments": {"repo": "r", "title": "t", "effort": "x"}}),
            self.c.req("tools/call", {"name": "result", "arguments": {}}),
            self.c.req("nope"),
            self.c.req("ping"))
        self.assertEqual(out[0]["error"]["code"], -32602)
        self.assertTrue(out[1]["result"]["isError"])
        self.assertTrue(out[2]["result"]["isError"])
        self.assertEqual(out[3]["error"]["code"], -32601)
        self.assertEqual(out[4]["result"], {})
        self.assertEqual(self.calls, [])

    def test_bad_json_and_service_down(self):
        def down(req):
            raise ConnectionRefusedError("service not running")
        stdout = io.StringIO()
        mcp.serve(down, io.StringIO("not json\n" + json.dumps(self.c.req("tools/call", {"name": "status"})) + "\n"), stdout)
        a, b = [json.loads(x) for x in stdout.getvalue().splitlines()]
        self.assertEqual(a["error"]["code"], -32700)
        self.assertTrue(b["result"]["isError"])

    def test_cli_command_is_wired(self):
        with mock.patch.object(cli, "_cfg", return_value=object()), \
                mock.patch.object(mcp, "serve", return_value=0) as serve:
            self.assertEqual(cli.main(["mcp"]), 0)
        serve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
