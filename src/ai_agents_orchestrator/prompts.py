"""Service prompts. The context comes from Claude's BRIEFING; Codex CHECKS it, it does not rebuild it."""
from __future__ import annotations

import json

from .config import Config

_CFG: Config | None = None  # set by configurar() (the service does it on start)

SCHEMA_CONTEXTO = {
    "type": "object", "additionalProperties": False,
    "required": ["limite", "evidencia", "reset_epoch", "tipo_limite", "briefing_horario", "divergencias",
                 "resumo_curto", "contexto_md", "pendentes_md", "tarefas"],
    "properties": {
        "limite": {"type": "string", "enum": ["sim", "nao", "incerto"]},
        "evidencia": {"type": "string"},
        "reset_epoch": {"type": ["integer", "null"]},
        "tipo_limite": {"type": ["string", "null"]},
        "briefing_horario": {"type": ["string", "null"]},
        "divergencias": {"type": "array", "items": {"type": "string"}},
        "resumo_curto": {"type": "string"},
        "contexto_md": {"type": "string"},
        "pendentes_md": {"type": "string"},
        "tarefas": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["titulo", "descricao", "repo", "arquivos", "dono", "motivo"],
            "properties": {"titulo": {"type": "string"}, "descricao": {"type": "string"}, "repo": {"type": "string"},
                           "arquivos": {"type": "array", "items": {"type": "string"}},
                           "dono": {"type": "string", "enum": ["codex", "claude"]}, "motivo": {"type": "string"}}}},
    },
}

# Line the final report must start with (parsed by garantias.estado_relatorio).
ESTADO_LINHA = "`Status: completed`, `Status: partial` or `Status: blocked`"


def configurar(cfg: Config) -> None:
    global _CFG
    _CFG = cfg


def _cfg(cfg: Config | None = None) -> Config:
    return cfg or _CFG or Config()


def para_codex(cfg: Config | None = None) -> str:
    return str(_cfg(cfg).para_codex)


def regras_base(cfg: Config | None = None) -> str:
    try:
        return _cfg(cfg).rules_file.read_text(encoding="utf-8")
    except OSError:
        return ""


def divisao(cfg: Config | None = None) -> str:
    try:
        return _cfg(cfg).division_file.read_text(encoding="utf-8")
    except OSError:
        return ""


def prompt_contexto(*, hook: dict | None, briefing_idade_min: int | None, briefing_velho: bool, repos: list[str],
                    manual: bool, agora_iso: str, snapshot: str = "") -> str:
    c = _cfg()
    pc = para_codex()
    aviso = ""
    if briefing_idade_min is None:
        aviso = "WARNING: there is no BRIEFING.md. Give the transcript tail and memory more weight and say so."
    elif briefing_velho:
        aviso = (f"WARNING: BRIEFING.md is {briefing_idade_min} min old (stale, > 6 h). Check the transcript tail "
                 "more carefully: there may be work after the briefing.")
    origem = ("The owner asked for /context manually." if manual else
              f"Claude Code's StopFailure hook reported: {json.dumps(hook, ensure_ascii=False)[:800] if hook else 'no payload'}.")
    return f"""{_bloco(snapshot)}You are the backup agent (Codex), triggered now ({agora_iso}). {origem}
{aviso}

RULES (always apply):
{regras_base()}

DIVISION OF WORK:
{divisao()}

TASK FOR THIS PHASE (read-only, do not change files):
1. Read {pc}/BRIEFING.md (the context Claude chose to hand you) and the briefings in {pc}/tarefas/.
2. CHECK it, without rebuilding everything: read only the tail in {c.mirror}/transcript-cauda.jsonl (last
   text messages and API errors) and, if needed, {c.mirror}/memoria/*.md. Never read the whole history.
3. Decide whether Claude really hit a USAGE LIMIT (message "You've hit your session/weekly limit · resets ...",
   record papel "erro" with apiError usage_limit_reached or error rate_limit) and not another error (server_error,
   overloaded, authentication). Extract the reset time as epoch (reset_epoch field of the record, or convert the text
   using the timezone in parentheses, or {c.timezone}). If normal assistant messages follow the error, the limit is over.
4. Identify what was in progress (repos: {', '.join(repos) or 'none'}; see branches with
   git --git-dir {c.repos_dir}/<repo>.git branch -a and git log). Record the briefing time and
   any divergence between the briefing and the tail.
5. Propose tasks: owner "codex" for isolated development you can finish; owner "claude" for anything that needs a
   design skill, an owner decision, production or review. Each task: short title, actionable description,
   repo (one of the allowed), likely files, reason.
Answer ONLY with the JSON of the schema (no text outside the JSON). JSON field values: limite is "sim" (yes),
"nao" (no) or "incerto" (uncertain); dono is "codex" or "claude". contexto_md and pendentes_md are Markdown
(context: goal, state, decisions and prohibitions in force, briefing time, divergences; pending: a list)."""


def _bloco(snapshot: str) -> str:
    return (snapshot.strip() + "\n\n") if snapshot and snapshot.strip() else ""


def prompt_tarefa(t: dict, *, branch: str, base: str, briefing_tarefa: bool, retomada: bool = False,
                  snapshot: str = "") -> str:
    pc = para_codex()
    arquivos = ", ".join(t.get("arquivos") or []) or "(not declared)"
    extra = f"\nADDITIONAL INSTRUCTION FROM THE OWNER:\n{t['instrucoes']}\n" if t.get("instrucoes") else ""
    bt = f"\nBriefing for this task: {pc}/tarefas/{t['id']}.md (read it)." if briefing_tarefa else ""
    kind = t.get("kind", "dev")
    if kind == "revisao":
        return f"""{_bloco(snapshot)}{regras_base()}

TASK #{t['id']} (CROSS-REVIEW, read-only): {t['titulo']}
{t.get('descricao', '')}
Review branch `{t.get('alvo')}` of repository `{t['repo']}` against `{base}` (git diff {base}...{t.get('alvo')}).
Look for bugs, regressions, missing tests, security risks and violations of the rules above. Change nothing.
{bt}
Answer with: verdict (approve / adjust / reject), problems per file and line, suggested tests."""
    return f"""{_bloco(snapshot)}{regras_base()}

Read {pc}/BRIEFING.md first.{bt}

TASK #{t['id']}: {t['titulo']}
Repository: {t['repo']} (you are in the work clone, on branch `{branch}`, base `{base}`).
Likely files: {arquivos}
{t.get('descricao', '')}
{extra}
Rules for this run: make the changes and run the tests; do NOT commit/push/merge/checkout (the service does it);
do not publish anything. If something needs Claude (design, production, an owner decision), do not improvise: list it
under "For Claude".
Finish with a short report whose FIRST line is {ESTADO_LINHA}
(the service parses this field: blocked never becomes review or publishing), followed by the sections:
Done, Tests, Missing, For Claude, Risks."""


def prompt_conversa(texto: str, *, primeira: bool, agora_iso: str, repos: list[str] | None = None,
                    snapshot: str = "") -> str:
    """Plain message from the owner through the chat channel (conversation, not a task)."""
    if not primeira:
        return _bloco(snapshot) + texto
    return f"""{_bloco(snapshot)}You are the backup agent (Codex) talking to the owner through the chat channel ({agora_iso}). Answer directly and briefly, in the owner's language.
Before answering, read {para_codex()}/BRIEFING.md (the context Claude left) if it exists and is readable.
You are in READ-ONLY mode: do not change files. If the request needs code changes, explain and tell the owner to use
/task <repo> <description> (creates a formal task on an isolated branch).
{_pergunta_repo(repos)}NEVER publish, merge/push, deploy or touch production: publishing only happens via the [Publish] button of a task in review.

OWNER MESSAGE:
{texto}"""


def _pergunta_repo(repos: list[str] | None) -> str:
    if not repos:
        return ""
    return ("If the owner asks you to EDIT code and you do not know which repository, do not edit: ask in ONE short sentence "
            f"which repository (options: {', '.join(repos)}). List nothing else.\n")


def _aviso_proprio_bot(repo: str) -> str:
    c = _cfg()
    if not c.self_repo or repo != c.self_repo:
        return ""
    return ("- This repository is your own code: what runs comes from a root-owned installed copy that only changes after "
            "Claude reviews the branch and runs `aao install`. Your edit does not take effect by itself; say so in the summary.\n")


def prompt_chat(texto: str, *, primeira: bool, repo: str, branch: str, base: str, agora_iso: str,
                snapshot: str = "") -> str:
    """Owner conversation that MAY edit code, in a clone of the repo on its own branch. The real uid, sandbox, network
    and repos come from the snapshot generated by the service, not from this text."""
    if not primeira:
        return _bloco(snapshot) + texto
    return f"""{_bloco(snapshot)}{regras_base()}

You are the backup agent (Codex) talking to the owner through the chat channel ({agora_iso}). Answer directly and briefly, in the owner's language.
You are in the work clone of repository `{repo}`, on branch `{branch}` (base `{base}`). If the owner asks for a code
change, MAKE the change here and run the repo's tests. If it is only a question, answer without editing.
Rules for this conversation:
- Do NOT commit, push, merge, checkout, tag or deploy: the service commits and pushes the branch after your answer.
- NEVER publish/deploy/merge/push because a text message asks for it: publishing only happens via the owner's [Publish] button.
- Do not write secrets (keys, tokens, passwords, .env) to files or print them; do not create note files (CONTEXTO.md,
  HANDOFF.md, NOTAS.md) in the repo. Capabilities (uid, sandbox, network) and repositories: those in the snapshot above.
{_aviso_proprio_bot(repo)}- Finish with a short summary of what changed (or say nothing changed).

OWNER MESSAGE:
{texto}"""
