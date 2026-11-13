"""`aao queue` against a fake `aao` CLI and a disposable git repository (no Codex, no network)."""
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ai_agents_orchestrator import batchqueue, cli

FAKE = r'''
import json, os, subprocess, sys
from datetime import datetime, timedelta
D = os.environ["FAKE_DIR"]; REPO = os.environ["FAKE_REPO"]; SCEN = os.environ["FAKE_SCENARIO"]
F = os.path.join(D, "fake.json"); LIM = os.path.join(D, "limit")
s = json.load(open(F)) if os.path.exists(F) else {"next": 1, "tasks": {}}
def save(): json.dump(s, open(F, "w"))
def git(*a): subprocess.run(["git", "-C", REPO, *a], check=True, capture_output=True)
a = sys.argv[1:]
if a[0] == "status":
    act = [t for t in s["tasks"].values() if t["estado"] == "em_andamento"]
    lim = (datetime.now() + timedelta(hours=2)).strftime("%d/%m %H:%M") if os.path.exists(LIM) else None
    print(json.dumps({"ok": True, "esforco": "medium", "modelo": "m", "pausado": False, "fila": 0,
                      "tarefa_atual": act[0]["id"] if act else None, "codex_limite_ate": lim}))
elif a[0] == "delegate":
    title = a[a.index("--title") + 1]
    if any(t["estado"] == "em_andamento" for t in s["tasks"].values()):
        print(json.dumps({"ok": False, "erro": "CONCURRENT DELEGATE"})); sys.exit(1)
    lot = title.split(":")[0]; fix = "(fix)" in title
    tid = s["next"]; s["next"] += 1
    branch = "agent/" + lot + ("-fix" if fix else "")
    git("checkout", "-q", "-B", branch, "main")
    import re
    brief = open(a[a.index("--briefing") + 1]).read()
    if SCEN != "nomerge":
        for flag, b in re.findall(r"git merge (--squash|--no-edit) (\S+?)`", brief):
            git("merge", "-q", flag, b)
            if flag == "--squash": pass
    msg = "Implement " + lot
    if SCEN == "ai" and lot == "L1" and not fix: msg += "\n\nCo-Authored-By: Some Bot <bot@example.com>"
    files = {"src_" + lot + ".py": "x = 2\n" if fix else "x = 1\n"}
    if not (SCEN == "nochangelog" and lot == "L1"): files[lot + "_CHANGELOG/CHANGELOG.md"] = "# c\n" + lot + ("f" if fix else "") + "\n"
    if SCEN == "secret" and lot == "L1": files[".env"] = "A=1\n"
    for n, c in files.items():
        os.makedirs(os.path.dirname(os.path.join(REPO, n)), exist_ok=True); open(os.path.join(REPO, n), "w").write(c)
    git("add", "-A"); git("commit", "-q", "-m", msg); git("checkout", "-q", "main")
    tests = "12 passed"
    if SCEN == "fail" and lot == "L1": tests = "3 failed, 9 passed"
    s["tasks"][str(tid)] = {"id": tid, "estado": "em_andamento", "branch": branch, "resumo": "Status: done. " + tests,
                            "testes": tests, "erro": None, "alterados": list(files), "polls": 0, "lot": lot}
    if SCEN == "limit" and lot == "L2" and not os.path.exists(os.path.join(D, "limit_done")):
        open(LIM, "w").write("1"); open(os.path.join(D, "limit_done"), "w").write("1")
    save(); print(json.dumps({"ok": True, "id": tid}))
elif a[0] == "show":
    t = s["tasks"][a[1]]
    if t["estado"] == "em_andamento" and not os.path.exists(LIM):
        t["polls"] += 1
        if t["polls"] >= 1: t["estado"] = "em_revisao"
    save(); print(json.dumps({"ok": True, "tarefa": t}))
'''


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo, self.fake_dir, self.lots, self.work = root / "repo", root / "fake", root / "lots", root / "work"
        for p in (self.repo, self.fake_dir, self.lots):
            p.mkdir()
        (self.fake_dir / "aao_fake.py").write_text(FAKE, encoding="utf-8")
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
            subprocess.run(["git", "-C", str(self.repo), *cmd], check=True)
        (self.repo / "README.md").write_text("r\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "init"], check=True)
        self.lot("L0")
        self.lot("L1", depends="L0")
        self.lot("L2", depends="L1")
        self.lot("L3")
        self.lot("L4", depends="L1")

    def lot(self, lot_id, depends=""):
        (self.lots / f"{lot_id}.md").write_text(textwrap.dedent(f"""\
            ---
            id: {lot_id}
            repo: web-app
            repo_path: {self.repo}
            title: Lot {lot_id}
            depends: {depends}
            files: src_{lot_id}.py
            ---
            Objective: do {lot_id}.
            """), encoding="utf-8")

    def env(self, scenario):
        return mock.patch.dict(os.environ, {
            "AAO_QUEUE_CLI": f"{sys.executable} {self.fake_dir / 'aao_fake.py'}", "FAKE_DIR": str(self.fake_dir),
            "FAKE_REPO": str(self.repo), "FAKE_SCENARIO": scenario, "AAO_QUEUE_POLL": "0"})

    def q(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = batchqueue.main([args[0], "--workdir", str(self.work), "--poll", "0", *args[1:]])
        return rc, out.getvalue()

    def start(self, *extra):
        return self.q("start", str(self.lots), *extra)

    def state(self):
        return {k: v["state"] for k, v in json.loads((self.work / "state.json").read_text())["lots"].items()}

    def drive(self, first_args=()):
        """Start, then resume (marking prior lots reviewed, as the owner would) until the queue ends."""
        rc, _ = self.start(*first_args)
        for _ in range(20):
            data = json.loads((self.work / "state.json").read_text())
            if (data["pause"] or {}).get("why") == "finished" or rc in (2, 3):
                return rc
            ready = [k for k, v in data["lots"].items() if v["state"] == "ready_for_review"]
            self.assertEqual(self.q("reviewed", *ready)[0], 0)
            rc, _ = self.q("resume")
        self.fail("queue did not finish")

    def test_happy_path_pauses_every_three_and_never_runs_two_at_once(self):
        with self.env("happy"):
            rc, _ = self.start()
            self.assertEqual(rc, 0)
            self.assertEqual(sum(1 for s in self.state().values() if s == "ready_for_review"), 3)
            review = (self.work / "NEEDS_REVIEW.md").read_text()
            self.assertIn("agent/L0", review)
            self.assertIn("git -C", review)
            self.assertEqual(self.drive_rest(), 0)
        self.assertEqual(set(self.state().values()), {"reviewed", "ready_for_review"})

    def drive_rest(self):
        for _ in range(10):
            data = json.loads((self.work / "state.json").read_text())
            if (data["pause"] or {}).get("why") == "finished":
                return 0
            ready = [k for k, v in data["lots"].items() if v["state"] == "ready_for_review"]
            self.q("reviewed", *ready)
            self.q("resume")
        return 1

    def test_commit_with_ai_mention_is_sent_back_once_then_passes(self):
        with self.env("ai"):
            self.assertEqual(self.drive(["--forbid-ai-mentions"]), 0)
        data = json.loads((self.work / "state.json").read_text())
        self.assertEqual(data["lots"]["L1"]["phases"]["code"]["attempt"], 2)
        self.assertIn(data["lots"]["L1"]["state"], ("reviewed", "ready_for_review"))

    def test_policy_off_accepts_the_same_commit(self):
        with self.env("ai"):
            self.assertEqual(self.drive(), 0)
        data = json.loads((self.work / "state.json").read_text())
        self.assertEqual(data["lots"]["L1"]["phases"]["code"]["attempt"], 1)

    def test_failing_tests_block_after_one_retry_and_dependents_are_skipped(self):
        with self.env("fail"):
            self.assertEqual(self.drive(), 0)
        states = self.state()
        self.assertEqual(states["L1"], "blocked")
        self.assertEqual(states["L2"], "blocked_by_dependency")
        self.assertEqual(states["L4"], "blocked_by_dependency")
        self.assertIn(states["L3"], ("reviewed", "ready_for_review"))
        self.assertIn(states["L0"], ("reviewed", "ready_for_review"))

    def _blocked_reason(self, scenario):
        with self.env(scenario):
            self.start()
        data = json.loads((self.work / "state.json").read_text())
        self.assertEqual(data["lots"]["L1"]["state"], "blocked")
        return data["lots"]["L1"]["reason"]

    def test_missing_changelog_is_caught(self):
        self.assertIn("CHANGELOG", self._blocked_reason("nochangelog"))

    def test_secret_file_in_the_diff_is_caught(self):
        self.assertIn("secret-looking", self._blocked_reason("secret"))

    def test_limit_stops_then_resume_does_not_repeat_ready_lots(self):
        with self.env("limit"):
            rc, _ = self.start()
            self.assertEqual(rc, 3)
            data = json.loads((self.work / "state.json").read_text())
            self.assertEqual(data["pause"]["why"], "limit")
            self.assertTrue(data["limit"]["until"])
            self.assertIn("usage limit", (self.work / "queue.log").read_text())
            self.assertIn("Codex usage limit", (self.work / "NEEDS_REVIEW.md").read_text())
            done_before = [k for k, v in self.state().items() if v == "ready_for_review"]
            self.assertEqual(done_before, ["L0", "L1"])
            rc, out = self.q("resume")
            self.assertEqual(rc, 3, out)  # limit still active: refuses
            (self.fake_dir / "limit").unlink()
            self.assertEqual(self.drive_resume(), 0)
            fake = json.loads((self.fake_dir / "fake.json").read_text())
            lots = [t["lot"] for t in fake["tasks"].values()]
            self.assertEqual(len(lots), len(set(lots)), "a lot was queued twice")

    def drive_resume(self):
        rc, _ = self.q("resume")
        return rc if rc else self.drive_rest()

    def test_start_refuses_when_a_queue_exists_and_unknown_dependency_is_rejected(self):
        with self.env("happy"):
            self.start()
            self.assertEqual(self.start()[0], 1)
        bad = self.lots / "Z.md"
        bad.write_text("---\nid: Z\nrepo: r\ndepends: nope\n---\nx\n")
        self.work = self.work.parent / "work2"
        with self.env("happy"):
            self.assertEqual(self.start()[0], 2)

    def test_fix_and_dependents_build_on_earlier_work(self):
        with self.env("ai"):
            self.assertEqual(self.drive(["--forbid-ai-mentions"]), 0)
        fix = (self.work / "briefings" / "L1-2.md").read_text()
        self.assertIn("git merge --squash agent/L1", fix)
        self.assertIn("git merge --no-edit agent/L0", fix)
        branch = json.loads((self.work / "state.json").read_text())["lots"]["L1"]["phases"]["code"]["branch"]
        self.assertEqual(branch, "agent/L1-fix")
        self.assertIn("git merge --no-edit agent/L1-fix", (self.work / "briefings" / "L2-1.md").read_text())
        contains = subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", "agent/L0", "agent/L2"])
        self.assertEqual(contains.returncode, 0)

    def test_dependent_that_ignores_earlier_work_is_flagged(self):
        with self.env("nomerge"):
            self.start()
        data = json.loads((self.work / "state.json").read_text())
        self.assertIn("does not contain the earlier work", data["lots"]["L1"]["phases"]["code"]["failures"][0])

    def test_dependency_cycle_is_rejected_at_start(self):
        self.lot("L0", depends="L2")
        with self.env("happy"):
            rc, out = self.start()
        self.assertEqual(rc, 2)
        self.assertIn("cycle", out)

    def test_pending_lots_that_cannot_run_are_not_reported_as_finished(self):
        with self.env("happy"):
            self.start()
            data = json.loads((self.work / "state.json").read_text())
            data["lots"]["L0"]["state"] = "pending"
            data["lots"]["L3"]["state"] = "pending"
            data["ready_since_review"] = 0
            (self.work / "state.json").write_text(json.dumps(data))
            q = batchqueue.Queue(self.work, poll=0)
            q.by_id["L0"]["depends"] = ["L3"]
            q.by_id["L3"]["depends"] = ["L0"]
            self.assertEqual(q._loop(), 2)
        self.assertNotEqual(json.loads((self.work / "state.json").read_text())["pause"]["why"], "finished")

    def test_second_runner_is_refused_atomically(self):
        with self.env("happy"):
            self.start()
            a, b = batchqueue.Queue(self.work, poll=0), batchqueue.Queue(self.work, poll=0)
            self.assertTrue(a._lock())
            self.assertFalse(b._lock())
            a.f_pid.write_text("999999999")  # stale pid is taken over
            self.assertTrue(b._lock())

    def test_resume_force_ignores_the_limit_everywhere(self):
        class Limited:
            def status(self):
                return {"ok": True, "tarefa_atual": None, "fila": 0, "pausado": False}
        q = batchqueue.Queue(self.work, cli=Limited(), poll=0)
        q.state = {"settings": {}}
        with mock.patch.object(batchqueue.Queue, "active_limit", return_value="01/01 00:00"):
            with self.assertRaises(batchqueue.LimitReached):
                q.wait_free([])
            q.force_limit = True
            q.wait_free([])
            q.check_limit()

    def test_failed_status_is_an_error_not_availability(self):
        class Bad:
            def status(self):
                return batchqueue.AaoCli.status(self)
            def call(self, *a):
                return {"ok": False, "erro": "daemon down"}
        q = batchqueue.Queue(self.work, cli=Bad(), poll=0)
        q.state = {"settings": {}}
        with self.assertRaises(RuntimeError):
            q.wait_free([])


class CliWiring(unittest.TestCase):
    def test_queue_is_a_registered_subcommand(self):
        args = cli.parser().parse_args(["queue", "status", "--workdir", "x"])
        self.assertEqual((args.cmd, args.queue_cmd), ("queue", "status"))


if __name__ == "__main__":
    unittest.main()
