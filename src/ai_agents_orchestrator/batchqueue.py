"""`aao queue`: runs a folder of Markdown "lots" through Codex, one at a time, without an agent waiting.

It only talks to the `aao` CLI (`status`, `delegate`, `show`) and to `git` in the repository paths the lots name.
It never merges, publishes, pushes or deploys: it stops at ``ready_for_review`` and leaves the review to a human
or a reviewing agent.

Lot file (``lots/10-signup.md``)::

    ---
    id: L1
    repo: web-app                 # repository name known to aao
    repo_path: /home/me/web-app   # optional clone, enables branch/commit/diff checks
    title: Validate signup e-mails
    depends: L0                   # optional, comma separated lot ids
    files: app/signup.py, tests/test_signup.py
    effort: medium                # optional, this lot only
    model: gpt-5                  # optional, this lot only
    ---
    <the briefing: objective, files, tests, done criteria>

States of a lot: pending, running, ready_for_review, reviewed, blocked, blocked_by_dependency.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

FINAL = ("em_revisao", "concluida", "bloqueada", "cancelada")
OK_TASK = ("em_revisao", "concluida")
DONE = ("ready_for_review", "reviewed")
DEAD = ("blocked", "blocked_by_dependency")

RE_LIMIT = re.compile(r"usage limit|rate.?limit|limit reached|quota|limite (semanal|de uso)", re.I)
RE_PARTIAL = re.compile(r"(status|estado):\s*(partial|parcial|blocked|bloquead)", re.I)
RE_AI = re.compile(r"co-authored-by|\bclaude\b|\bcodex\b|openai|chatgpt|anthropic|copilot|\bgpt-\d|"
                   r"generated (by|with)|assisted by|\U0001F916", re.I)
RE_TESTS_OK = re.compile(r"\d+\s+(tests?\s+)?(passed|passing|ok)\b|\bgreen\b|\ball tests pass|\bOK\b|"
                         r"\d+\s+testes?\s+(passando|passaram)|\bverdes?\b", re.I)
RE_TESTS_BAD = re.compile(r"\b[1-9]\d*\s+(failed|failures?|errors?|falhas?|erros?)\b|not run|did not run|nao rodou", re.I)
SECRET_FILE = re.compile(
    r"(^|/)\.env(\.(?!example$|sample$|template$)[^/]*)?$|\.(pem|key|p12|pfx)$|(^|/)id_(rsa|ed25519|ecdsa)|"
    r"(^|/)(secrets?|credentials?)(\.|/|$)|(^|/)tokens?\.(json|txt|env)$", re.I)
SECRET_LINE = [re.compile(p) for p in (
    r"sk-[A-Za-z0-9]{20,}", r"AKIA[0-9A-Z]{16}", r"ghp_[A-Za-z0-9]{30,}", r"github_pat_[A-Za-z0-9_]{30,}",
    r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")]
RE_DSN = re.compile(r"(?:postgres(?:ql)?|mysql|mongodb)(?:\+\w+)?://([^\s:@/]+):([^\s@/]+)@([^\s/:]+)", re.I)
DSN_HOST_OK = re.compile(r"localhost|127\.|::1|example|invalid|\.test$|fake|dummy", re.I)
DSN_PASS_OK = re.compile(r"^(password|pass|test|fake|dummy|changeme|x+|\*+|<.*>|\$\{?\w+\}?|%s|\{.*\})$", re.I)


def split_command(text: str) -> list[str]:
    """shlex.split that keeps Windows backslashes (posix mode would eat them)."""
    if sys.platform != "win32":
        return shlex.split(text)
    return [part[1:-1] if len(part) > 1 and part[0] == part[-1] and part[0] in "\"'" else part for part in shlex.split(text, posix=False)]


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":  # os.kill(pid, 0) would terminate the process on Windows
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return True
        return bool(re.search(rf"\b{pid}\b", out))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class LimitReached(Exception):
    def __init__(self, until: str | None = None, reason: str = ""):
        super().__init__(reason)
        self.until, self.reason = until, reason


class StopRequested(Exception):
    pass


def now() -> datetime:
    return datetime.now().astimezone()


def stamp() -> str:
    return now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ lots
def parse_lot(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        head, sep, rest = text[4:].partition("\n---")
        if sep:
            body = rest.lstrip("\n")
            for line in head.splitlines():
                if ":" in line and not line.lstrip().startswith("#"):
                    key, _, value = line.partition(":")
                    meta[key.strip().lower()] = value.split(" #")[0].strip()
    lot_id = meta.get("id") or path.stem
    return {
        "id": lot_id, "repo": meta.get("repo", ""), "repo_path": meta.get("repo_path", ""),
        "title": meta.get("title", lot_id), "depends": [d.strip() for d in meta.get("depends", "").split(",") if d.strip()],
        "files": [f.strip() for f in meta.get("files", "").split(",") if f.strip()],
        "effort": meta.get("effort", ""), "model": meta.get("model", ""), "body": body, "file": path.name,
    }


def load_lots(folder: Path) -> list[dict[str, Any]]:
    lots = [parse_lot(p) for p in sorted(folder.glob("*.md")) if not p.name.startswith("_")]
    ids = [lot["id"] for lot in lots]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate lot ids in " + str(folder))
    if not lots:
        raise ValueError(f"no .md lots in {folder}")
    for lot in lots:
        if not lot["repo"]:
            raise ValueError(f"lot {lot['id']} has no `repo:`")
        for dep in lot["depends"]:
            if dep not in ids:
                raise ValueError(f"lot {lot['id']} depends on unknown lot {dep}")
    graph = {lot["id"]: lot["depends"] for lot in lots}
    done: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in trail:
            raise ValueError("dependency cycle: " + " -> ".join([*trail[trail.index(node):], node]))
        if node in done:
            return
        for dep in graph[node]:
            visit(dep, [*trail, node])
        done.add(node)

    for lot_id in graph:
        visit(lot_id, [])
    return lots


# ------------------------------------------------------------------ aao and git
class AaoCli:
    attempts = 3  # a restart of `aao serve` or a slow answer must not end a queue that polls for hours
    retry_delay = 5.0

    def __init__(self) -> None:
        self.cmd = split_command(os.environ.get("AAO_QUEUE_CLI", "aao"))

    def retrying(self, what: str, fn: Any) -> Any:
        """Run `fn`, retrying transient failures with a growing pause; the clear error comes only after the last try."""
        for attempt in range(1, self.attempts + 1):
            try:
                return fn()
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                if attempt == self.attempts:
                    raise RuntimeError(f"{what} failed after {attempt} attempts: {error}") from error
                time.sleep(self.retry_delay * attempt)

    def call(self, *args: str) -> dict[str, Any]:
        proc = subprocess.run([*self.cmd, *args], capture_output=True, text=True, timeout=120)
        out = proc.stdout.strip()
        start = out.find("{")
        try:
            data = json.loads(out[start:]) if start >= 0 else {}
        except ValueError:
            raise RuntimeError(f"unexpected output from `aao {' '.join(args)}` (rc={proc.returncode}): {out[:200]}") from None
        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected output from `aao {args[0]}`")
        if proc.returncode != 0 and not data:
            raise RuntimeError(f"`aao {' '.join(args)}` failed (rc={proc.returncode}) with no answer")
        return data

    def status(self) -> dict[str, Any]:
        return self.retrying("`aao status`", self._status)

    def _status(self) -> dict[str, Any]:
        data = self.call("status")
        if not data or data.get("ok") is False:
            raise RuntimeError(f"aao status failed: {data.get('erro') or data.get('error') or 'empty answer'}")
        return data

    def show(self, task_id: int) -> dict[str, Any]:
        return self.retrying(f"`aao show {task_id}`", lambda: self._show(task_id))

    def _show(self, task_id: int) -> dict[str, Any]:
        data = self.call("show", str(task_id))
        if not data.get("ok", True):
            raise RuntimeError(f"aao show {task_id}: {data.get('erro') or data.get('error')}")
        task = data.get("tarefa")
        if not isinstance(task, dict):
            raise RuntimeError(f"aao show {task_id}: no task in the answer")
        return task

    def delegate(self, lot: dict[str, Any], title: str, briefing: Path) -> int:
        args = ["delegate", "--repo", lot["repo"], "--title", title, "--description", f"Queue lot {lot['id']}",
                "--briefing", str(briefing)]
        if lot["files"]:
            args += ["--files", ",".join(lot["files"])]
        if lot["effort"]:
            args += ["--effort", lot["effort"]]
        if lot["model"]:
            args += ["--model", lot["model"]]
        data = self.call(*args)
        if not data.get("ok", True) or "id" not in data:
            raise RuntimeError(f"aao delegate failed: {data}")
        return int(data["id"])


def git_info(repo_path: str, branch: str, base: str, exclude: list[str] | None = None) -> dict[str, Any] | None:
    """None when the lot has no usable clone; otherwise what the mechanical checks need."""
    if not repo_path or not Path(repo_path).is_dir():
        return None

    def git(*args: str) -> tuple[int, str]:
        proc = subprocess.run(["git", "-C", repo_path, *args], capture_output=True, text=True, timeout=120)
        return proc.returncode, proc.stdout

    rc, _ = git("rev-parse", "--verify", f"refs/heads/{branch}")
    if rc != 0:
        return {"exists": False, "messages": [], "files": [], "added": []}
    if git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")[0] != 0:
        raise RuntimeError(f"base {base!r} does not exist in {repo_path}; create it or pass --base with an existing ref")

    def must(*args: str) -> str:
        rc, out = git(*args)
        if rc != 0:  # an empty answer from a failed git call would read as "no commits" and fail the lot unfairly
            raise RuntimeError(f"`git {' '.join(args[:2])}` failed in {repo_path} (rc={rc})")
        return out

    known = [f"^{b}" for b in exclude or [] if git("rev-parse", "--verify", f"refs/heads/{b}")[0] == 0]
    log = must("log", f"{base}..{branch}", *known, "--format=%B%x1e")
    if known:  # work merged in from earlier lots was already checked: look only at this task's own commits
        names = must("log", f"{base}..{branch}", *known, "--name-only", "--format=")
        diff = must("log", f"{base}..{branch}", *known, "-p", "-U0", "--format=")
        names = "\n".join(dict.fromkeys(n for n in names.splitlines() if n))
    else:
        names = must("diff", "--name-only", f"{base}...{branch}")
        diff = must("diff", f"{base}...{branch}", "-U0")
    return {"exists": True, "messages": [m.strip() for m in log.split("\x1e") if m.strip()],
            "files": [n for n in names.splitlines() if n],
            "added": [x[1:] for x in diff.splitlines() if x.startswith("+") and not x.startswith("+++")]}


def git_contains(repo_path: str, ancestor: str, branch: str) -> bool | None:
    """True when `branch` contains every commit of `ancestor`; None when it cannot be checked."""
    if not repo_path or not Path(repo_path).is_dir():
        return None
    base = ["git", "-C", repo_path]
    for ref in (ancestor, branch):
        if subprocess.run([*base, "rev-parse", "--verify", f"refs/heads/{ref}"], capture_output=True, timeout=120).returncode != 0:
            return None
    return subprocess.run([*base, "merge-base", "--is-ancestor", ancestor, branch], capture_output=True, timeout=120).returncode == 0


# ------------------------------------------------------------------ the queue
class Queue:
    def __init__(self, workdir: Path, cli: Any = None, poll: float | None = None) -> None:
        self.dir = workdir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.f_state, self.f_log = self.dir / "state.json", self.dir / "queue.log"
        self.f_stop, self.f_review, self.f_pid = self.dir / "STOP", self.dir / "NEEDS_REVIEW.md", self.dir / "queue.pid"
        self.cli = cli or AaoCli()
        self.state: dict[str, Any] = json.loads(self.f_state.read_text(encoding="utf-8")) if self.f_state.exists() else {}
        self.force_limit = False
        self.lots: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self.poll = float(os.environ.get("AAO_QUEUE_POLL", "60")) if poll is None else poll
        if self.state:
            self._load_lots()

    # --- persistence and log
    def _load_lots(self) -> None:
        self.lots = load_lots(Path(self.state["lots_dir"]))
        self.by_id = {lot["id"]: lot for lot in self.lots}
        for lot in self.lots:
            self.state["lots"].setdefault(lot["id"], {"state": "pending", "phases": {}, "reason": None})

    def save(self) -> None:
        self.state["updated"] = stamp()
        tmp = self.f_state.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.f_state)

    def log(self, msg: str) -> None:
        line = f"{stamp()} {msg}"
        print(line, flush=True)
        with open(self.f_log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def notify(self, msg: str) -> None:
        """Log plus an optional hook: AAO_QUEUE_NOTIFY="cmd args" is run with the text appended as the last argument."""
        self.log(f"NOTICE: {msg}")
        cmd = os.environ.get("AAO_QUEUE_NOTIFY")
        if cmd:
            try:
                subprocess.run([*split_command(cmd), msg], capture_output=True, timeout=20)
            except (OSError, subprocess.SubprocessError) as error:
                self.log(f"notify hook failed: {error}")

    def init(self, lots_dir: Path, settings: dict[str, Any]) -> None:
        self.state = {"created": stamp(), "updated": stamp(), "lots_dir": str(lots_dir.resolve()), "settings": settings,
                      "pause": None, "limit": None, "ready_since_review": 0, "lots": {}}
        self._load_lots()
        for repo_path in dict.fromkeys(lot["repo_path"] for lot in self.lots if lot["repo_path"]):
            if Path(repo_path).is_dir():
                rc = subprocess.run(["git", "-C", repo_path, "rev-parse", "--verify", "--quiet", f"{settings.get('base', 'main')}^{{commit}}"],
                                    capture_output=True, timeout=120).returncode
                if rc != 0:
                    self.state = {}
                    raise ValueError(f"base {settings.get('base', 'main')!r} does not exist in {repo_path}; create it or pass --base")
        self.save()

    def lot_state(self, lot_id: str) -> dict[str, Any]:
        return self.state["lots"][lot_id]

    def mark(self, lot_id: str, new_state: str, reason: str | None = None) -> None:
        entry = self.lot_state(lot_id)
        entry["state"], entry["reason"], entry["at"] = new_state, reason, stamp()
        self.log(f"lot {lot_id} -> {new_state}" + (f" ({reason})" if reason else ""))
        self.save()

    def sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.check_stop()
            time.sleep(min(5.0, max(0.0, end - time.time())))
        self.check_stop()

    def check_stop(self) -> None:
        if self.f_stop.exists():
            raise StopRequested()

    # --- usage limit
    @staticmethod
    def parse_when(text: str) -> datetime | None:
        iso = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", text or "")  # what `aao status` prints
        if iso:
            try:
                year, month, day, hour, minute = (int(x) for x in iso.groups())
                return datetime(year, month, day, hour, minute, tzinfo=now().tzinfo)
            except ValueError:
                return None
        m = re.search(r"(\d{2})/(\d{2})\s+(\d{2}):(\d{2})", text or "")
        if not m:
            return None
        day, month, hour, minute = (int(x) for x in m.groups())
        current = now()
        try:
            target = current.replace(month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if target < current - timedelta(days=30):
            target = target.replace(year=target.year + 1)
        return target

    def active_limit(self, status: dict[str, Any]) -> str | None:
        until = status.get("codex_limite_ate")
        if until:
            when = self.parse_when(str(until))
            if when is None or when > now():
                return str(until)
        sched = (status.get("retomada_agendada") or {}).get("codex")
        if isinstance(sched, (int, float)) and sched > time.time():
            return datetime.fromtimestamp(sched).astimezone().strftime("%d/%m %H:%M")
        return None

    def check_limit(self, status: dict[str, Any] | None = None) -> None:
        if self.force_limit:
            return
        status = status or self.cli.status()
        until = self.active_limit(status)
        if until:
            raise LimitReached(until, "Codex usage limit reported by `aao status`")

    # --- enqueue
    def wait_free(self, own: list[int]) -> None:
        cfg = self.state["settings"]
        while True:
            status = self.cli.status()
            self.check_limit(status)
            effort, model = cfg.get("effort"), cfg.get("model")
            if effort and status.get("esforco") not in (effort, None):
                raise RuntimeError(f"service effort is {status.get('esforco')!r}, expected {effort!r}; fix it and resume")
            if model and status.get("modelo") not in (model, None):
                raise RuntimeError(f"service model is {status.get('modelo')!r}, expected {model!r}; fix it and resume")
            current = status.get("tarefa_atual")
            if (current is None or current in own) and not status.get("fila") and not status.get("pausado"):
                return
            self.log(f"queue busy (current={current}, pending={status.get('fila')}, paused={status.get('pausado')}); waiting")
            self.sleep(self.poll * 5)

    def predecessors(self, lot: dict[str, Any], phase: dict[str, Any]) -> list[str]:
        """Branches whose work this task must build on: the lots it depends on and, on a fix, the failed attempt."""
        branches = self.dependency_branches(lot)
        if phase["attempt"] > 1:
            branches.append(phase.get("branch"))
        return [str(b) for b in dict.fromkeys(branches) if b]

    def dependency_branches(self, lot: dict[str, Any]) -> list[Any]:
        return [self.lot_state(d)["phases"].get("code", {}).get("branch") for d in lot["depends"]]

    @staticmethod
    def message_fix(phase: dict[str, Any]) -> bool:
        """A fix for a commit-message failure cannot keep the old commits: it must recreate them with clean messages."""
        return phase["attempt"] > 1 and any("commit message" in f for f in phase.get("failures") or [])

    def briefing(self, lot: dict[str, Any], phase: dict[str, Any]) -> Path:
        text = lot["body"]
        before = self.predecessors(lot, phase)
        if before:
            text += ("\n\n## BASE (mandatory, first step)\nThe workspace starts from the base branch and does not contain earlier work. "
                     "Before changing anything, bring it in with "
                     + "; ".join(f"`git merge {'--squash' if self.message_fix(phase) and b == phase.get('branch') else '--no-edit'} {b}`"
                                 for b in before)
                     + ". If a merge is impossible, stop and report the work as partial. The result is checked: your branch must contain "
                     + "these branches" + (" (except the failed attempt, which you squash into new commits with clean messages)" if self.message_fix(phase) else "") + ".\n")
        if phase["attempt"] > 1 and phase.get("failures"):
            text += (f"\n\n## FIX (second and last attempt)\nThe previous task on branch `{phase.get('branch')}` failed mechanical checks:\n"
                     + "\n".join(f"- {f}" for f in phase["failures"])
                     + f"\nContinue on branch `{phase.get('branch')}` (do not start another one), fix only this, keep tests green.\n")
        out = self.dir / "briefings"
        out.mkdir(exist_ok=True)
        path = out / f"{lot['id']}-{phase['attempt']}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def enqueue(self, lot: dict[str, Any], phase: dict[str, Any]) -> None:
        own = [t for entry in self.state["lots"].values() for ph in entry["phases"].values() for t in ph.get("tasks", [])]
        self.wait_free(own)
        title = lot["title"] + (" (fix)" if phase["attempt"] > 1 else "")
        task_id = self.cli.delegate(lot, f"{lot['id']}: {title}", self.briefing(lot, phase))
        phase.setdefault("tasks", []).append(task_id)
        phase.update(task_id=task_id, verified=False)
        self.log(f"lot {lot['id']}: queued #{task_id} (attempt {phase['attempt']})")
        self.save()

    def wait_task(self, task_id: int) -> dict[str, Any]:
        while True:
            self.check_stop()
            self.check_limit()
            task = self.cli.show(task_id)
            if task.get("estado") in FINAL:
                return task
            if task.get("erro") and RE_LIMIT.search(str(task["erro"])):
                raise LimitReached(_when(str(task["erro"])), f"limit message on task #{task_id}")
            self.sleep(self.poll)

    # --- mechanical checks
    def verify(self, lot: dict[str, Any], task: dict[str, Any], phase: dict[str, Any] | None = None) -> list[str]:
        cfg = self.state["settings"]
        problems: list[str] = []
        if task.get("estado") not in OK_TASK:
            problems.append(f"task ended as {task.get('estado')!r}")
        if task.get("erro"):
            problems.append(f"task error: {str(task['erro'])[:200]}")
        report = str(task.get("resumo") or "")
        if RE_PARTIAL.search(report) or "PARTIAL" in str(task.get("aviso") or ""):
            problems.append("report declares partial/blocked work")
        branch = task.get("branch")
        if not branch:
            problems.append("task has no branch")
            return problems
        info = git_info(lot["repo_path"], str(branch), cfg.get("base", "main"), [str(b) for b in self.dependency_branches(lot) if b])
        files: list[str] = list(task.get("alterados") or [])
        added: list[str] = []
        if info is None:
            self.log(f"lot {lot['id']}: no usable repo_path; branch/commit/diff checks reduced to the task record")
        else:
            if not info["exists"]:
                problems.append(f"branch {branch!r} not found in {lot['repo_path']}")
                return problems
            if not info["messages"]:
                problems.append("branch has no new commits over the base")
            required = self.dependency_branches(lot) if phase and self.message_fix(phase) else (self.predecessors(lot, phase) if phase else [])
            for earlier in required:
                if not earlier:
                    continue
                if git_contains(lot["repo_path"], earlier, str(branch)) is False:
                    problems.append(f"branch {branch!r} does not contain the earlier work from {earlier!r}")
            if cfg.get("forbid_ai_mentions"):
                extra = [re.compile(p, re.I) for p in cfg.get("extra_forbidden", [])]
                for message in info["messages"]:
                    hit = RE_AI.search(message) or next((m for p in extra if (m := p.search(message))), None)
                    if hit:
                        problems.append(f"commit message with co-author or tool mention: {hit.group(0)!r}")
                        break
            files = info["files"] or files
            added = info["added"]
        if cfg.get("check_changelog", True) and not any(Path(f).name.upper() == "CHANGELOG.MD" for f in files):
            problems.append("CHANGELOG was not touched")
        bad = [f for f in files if SECRET_FILE.search(f)]
        if bad:
            problems.append("secret-looking file in the diff: " + ", ".join(bad[:5]))
        for line in added:
            if any(p.search(line) for p in SECRET_LINE):
                problems.append("token or private key pattern in the diff")
                break
            dsn = RE_DSN.search(line)
            if dsn and not DSN_HOST_OK.search(dsn.group(3)) and not DSN_PASS_OK.match(dsn.group(2)):
                problems.append(f"database URL with password and a non-fictional host in the diff ({dsn.group(3)})")
                break
        evidence = task.get("testes") or report
        if not isinstance(evidence, str):
            evidence = json.dumps(evidence)
        if not RE_TESTS_OK.search(evidence):
            problems.append("no sign of green tests in the report")
        elif RE_TESTS_BAD.search(evidence):
            problems.append("report mentions failing or skipped tests")
        return problems

    def run_lot(self, lot: dict[str, Any]) -> None:
        entry = self.lot_state(lot["id"])
        phase = entry["phases"].setdefault("code", {"attempt": 1, "tasks": [], "task_id": None, "verified": False, "failures": []})
        self.mark(lot["id"], "running")
        while True:
            if phase.get("result") == "ok":
                break
            if not phase.get("task_id") or phase.get("verified"):
                self.enqueue(lot, phase)
            task_id = int(phase["task_id"])
            task = self.wait_task(task_id)
            if task["estado"] == "cancelada":
                self.mark(lot["id"], "blocked", f"task #{task_id} was cancelled outside the queue")
                return
            if task["estado"] == "bloqueada" and RE_LIMIT.search(str(task.get("erro") or "") + str(task.get("resumo") or "")):
                phase["task_id"] = None  # requeue after the limit without spending the attempt
                self.save()
                raise LimitReached(_when(str(task.get("erro") or "")), f"task #{task_id} blocked by the usage limit")
            problems = self.verify(lot, task, phase)
            phase.update(verified=True, branch=task.get("branch"), failures=problems)
            if not problems:
                phase["result"] = "ok"
                self.log(f"lot {lot['id']}: mechanical checks passed (#{task_id}, {task.get('branch')})")
                self.save()
                break
            self.log(f"lot {lot['id']}: FAILED attempt {phase['attempt']}: " + "; ".join(problems))
            if phase["attempt"] >= 2:
                self.mark(lot["id"], "blocked", "; ".join(problems))
                return
            phase["attempt"] += 1
            self.save()
        self.mark(lot["id"], "ready_for_review")
        self.state["ready_since_review"] += 1
        self.save()

    def pick(self) -> str | None:
        for lot in self.lots:
            entry = self.lot_state(lot["id"])
            if entry["state"] in DONE + DEAD:
                continue
            dead = [d for d in lot["depends"] if self.lot_state(d)["state"] in DEAD]
            if dead:
                self.mark(lot["id"], "blocked_by_dependency", "depends on " + ", ".join(dead))
                continue
            if all(self.lot_state(d)["state"] in DONE for d in lot["depends"]):
                return str(lot["id"])
        return None

    # --- review file
    def write_review(self, why: str) -> None:
        lines = [f"# Needs review ({why})", f"Generated {stamp()}. The queue never merges or publishes; a human (or a reviewing agent) does.", "",
                 "After reviewing: `aao queue reviewed ID [ID ...]` then `aao queue resume`.", ""]
        base = self.state["settings"].get("base", "main")
        for lot in self.lots:
            entry = self.lot_state(lot["id"])
            if entry["state"] != "ready_for_review":
                continue
            phase = entry["phases"].get("code", {})
            branch, tid = phase.get("branch"), phase.get("task_id")
            lines += [f"## {lot['id']}: {lot['title']}", f"- task #{tid}, branch `{branch}`, repo `{lot['repo']}`",
                      f"- report: `aao result {tid}`"]
            if lot["repo_path"]:
                lines += [f"- diff: `git -C {lot['repo_path']} diff {base}...{branch} --stat`",
                          f"- commits: `git -C {lot['repo_path']} log {base}..{branch} --oneline`"]
            lines.append("")
        blocked = [(lot["id"], self.lot_state(lot["id"])) for lot in self.lots if self.lot_state(lot["id"])["state"] in DEAD]
        if blocked:
            lines.append("## Blocked")
            lines += [f"- {i}: {e['state']} - {e['reason']}" for i, e in blocked]
        if self.state.get("limit"):
            lines += ["", f"## Codex usage limit\n- stopped at {self.state['limit'].get('detected')}; back at: {self.state['limit'].get('until') or 'unknown'}"]
        self.f_review.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # --- main loop
    def run(self) -> int:
        if not self._lock():
            print("another queue is already running (queue.pid)")
            return 1
        self.f_stop.unlink(missing_ok=True)
        try:
            return self._loop()
        finally:
            self.f_pid.unlink(missing_ok=True)

    def _lock(self) -> bool:
        """Atomic: the pid file is written whole to a private name and hard-linked into place, which fails if it exists."""
        mine = self.dir / f"queue.pid.{os.getpid()}"
        mine.write_text(str(os.getpid()))
        try:
            for _ in range(3):
                try:
                    os.link(mine, self.f_pid)
                    return True
                except FileExistsError:
                    pass
                try:
                    if pid_alive(int(self.f_pid.read_text())):
                        return False
                    self.f_pid.unlink(missing_ok=True)  # stale
                except FileNotFoundError:
                    continue
                except (OSError, ValueError):
                    self.f_pid.unlink(missing_ok=True)  # stale
            return False
        finally:
            mine.unlink(missing_ok=True)

    def _loop(self) -> int:
        every = int(self.state["settings"].get("pause_every", 3))
        try:
            self.state["pause"] = None
            self.save()
            while True:
                self.check_stop()
                if every and self.state["ready_since_review"] >= every:
                    self.state["pause"] = {"why": "review", "at": stamp()}
                    self.save()
                    self.write_review(f"{self.state['ready_since_review']} lots ready")
                    self.notify(f"queue paused: {self.state['ready_since_review']} lots ready; see {self.f_review}")
                    return 0
                lot_id = self.pick()
                if lot_id is None:
                    stuck = [lot["id"] for lot in self.lots if self.lot_state(lot["id"])["state"] not in DONE + DEAD]
                    if stuck:
                        raise RuntimeError("no lot can run but these are still pending: " + ", ".join(stuck))
                    break
                self.run_lot(self.by_id[lot_id])
            self.write_review("queue finished")
            self.state["pause"] = {"why": "finished", "at": stamp()}
            self.save()
            self.notify(f"queue finished; see {self.f_review}")
            return 0
        except LimitReached as stop:
            self.state["limit"] = {"detected": stamp(), "until": stop.until, "reason": stop.reason}
            self.state["pause"] = {"why": "limit", "at": stamp()}
            self.save()
            self.write_review("stopped by the Codex usage limit")
            self.notify(f"Codex usage limit ({stop.reason}); back at {stop.until or 'unknown'}. Resume with `aao queue resume`")
            return 3
        except StopRequested:
            self.log("stop requested; the task running in the service was NOT cancelled")
            self.state["pause"] = {"why": "manual stop", "at": stamp()}
            self.save()
            self.f_stop.unlink(missing_ok=True)
            return 0
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            self.log(f"ERROR: {error}")
            self.state["pause"] = {"why": f"error: {error}", "at": stamp()}
            self.save()
            return 2


def _when(text: str) -> str | None:
    m = re.search(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}|\d{2}/\d{2}\s+\d{2}:\d{2}", text or "")
    return m.group(0) if m else None


def table(queue: Queue) -> str:
    rows = [f"{'lot':8} {'state':24} detail"]
    for lot in queue.lots:
        entry = queue.lot_state(lot["id"])
        tasks = ",".join(f"#{t}" for ph in entry["phases"].values() for t in ph.get("tasks", []))
        rows.append(f"{lot['id']:8} {entry['state']:24} {tasks} {entry.get('reason') or ''}".rstrip())
    rows.append(f"pause: {queue.state.get('pause')}  limit: {queue.state.get('limit')}  ready since last review: {queue.state['ready_since_review']}")
    return "\n".join(rows)


# ------------------------------------------------------------------ CLI
def add_arguments(sp: Any) -> None:
    """Register `aao queue` subcommands on an argparse subparsers object."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workdir", default=".aao-queue", help="state, log, briefings and NEEDS_REVIEW.md live here")
    common.add_argument("--poll", type=float, help="seconds between checks (default 60, env AAO_QUEUE_POLL)")
    st = sp.add_parser("start", parents=[common], help="start a new queue from a folder of lots")
    st.add_argument("lots_dir")
    st.add_argument("--base", default="main", help="base branch for the diff checks (default main)")
    st.add_argument("--effort", help="expected service effort (default medium; the queue refuses to start lots otherwise)", default="medium")
    st.add_argument("--model", help="expected service model (not checked when omitted)")
    st.add_argument("--pause-every", type=int, default=3, help="pause for review after N lots ready (0 = never)")
    st.add_argument("--forbid-ai-mentions", action="store_true", help="fail lots whose commit messages carry a co-author or a tool/AI mention")
    st.add_argument("--forbid-pattern", action="append", default=[], help="extra regex forbidden in commit messages (repeatable)")
    st.add_argument("--no-changelog-check", action="store_true")
    for name, text in (("resume", "continue after a pause or usage limit without repeating lots"), ("status", "table of lots"),
                       ("stop", "ask the running queue to stop (does not cancel the task in the service)")):
        sub = sp.add_parser(name, parents=[common], help=text)
        if name == "resume":
            sub.add_argument("--force", action="store_true", help="resume even if the limit still shows as active")
    lg = sp.add_parser("log", parents=[common], help="tail the queue log")
    lg.add_argument("-n", type=int, default=40)
    rv = sp.add_parser("reviewed", parents=[common], help="mark lots as reviewed and reset the pause counter")
    rv.add_argument("ids", nargs="+")


def run(args: argparse.Namespace, cli: Any = None) -> int:
    workdir = Path(args.workdir)
    q = Queue(workdir, cli=cli, poll=args.poll)
    action = args.queue_cmd
    if action == "start":
        if q.state:
            print("a queue already exists in this workdir; use `aao queue resume`")
            return 1
        try:
            q.init(Path(args.lots_dir), {"base": args.base, "effort": args.effort, "model": args.model, "pause_every": args.pause_every,
                                         "forbid_ai_mentions": args.forbid_ai_mentions, "extra_forbidden": args.forbid_pattern,
                                         "check_changelog": not args.no_changelog_check})
        except ValueError as error:
            print(f"error: {error}")
            return 2
        return q.run()
    if not q.state:
        print("no queue in this workdir; run `aao queue start LOTS_DIR` first")
        return 1
    if action == "status":
        print(table(q))
        return 0
    if action == "log":
        if q.f_log.exists():
            print("\n".join(q.f_log.read_text(encoding="utf-8").splitlines()[-args.n:]))
        return 0
    if action == "stop":
        q.f_stop.write_text(stamp())
        print("stop requested; the queue stops at its next check")
        return 0
    if action == "reviewed":
        for lot_id in args.ids:
            if lot_id not in q.state["lots"] or q.lot_state(lot_id)["state"] != "ready_for_review":
                print(f"{lot_id}: not ready_for_review")
                return 1
        for lot_id in args.ids:
            q.lot_state(lot_id)["state"] = "reviewed"
        q.state["ready_since_review"] = 0
        q.save()
        print("marked as reviewed")
        return 0
    # resume
    try:
        q._load_lots()
    except ValueError as error:
        print(f"error: {error}")
        return 2
    if q.state.get("limit") and not args.force:
        try:
            q.check_limit()
        except LimitReached as stop:
            print(f"usage limit still active until {stop.until}; use --force to try anyway")
            return 3
    pause = q.state.get("pause") or {}
    q.force_limit = bool(args.force)
    q.state["limit"] = None
    if pause.get("why") == "review":
        q.state["ready_since_review"] = 0
    q.save()
    return q.run()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aao queue", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="queue_cmd", required=True)
    add_arguments(sp)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
