"""The contract the two agents argue under: strict schemas plus role prompts.

Everything here exists to stop the two known failure modes of a two-LLM loop.
Agents that agree instantly produce a useless rubber stamp; agents that never
concede loop until the budget dies. The schemas make each side commit to a
machine-checkable position, and the prompts spell out what an honest move looks
like on both sides.
"""

from __future__ import annotations

from dai.models import (
    Action,
    CriticTurn,
    Issue,
    Reply,
    Severity,
    SolverTurn,
    Verdict,
)

# --- schemas --------------------------------------------------------------

CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES"]},
        "checked": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What you actually inspected or ran to reach this verdict.",
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["id", "severity", "claim", "evidence", "fix"],
                "additionalProperties": False,
            },
        },
        "conceded": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ids of your earlier issues where the solver convinced you.",
        },
        "summary": {"type": "string"},
    },
    "required": ["verdict", "checked", "issues", "conceded", "summary"],
    "additionalProperties": False,
}

SOLVER_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "responses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "action": {"type": "string", "enum": ["FIXED", "PARTIAL", "REJECTED"]},
                    "detail": {"type": "string"},
                },
                "required": ["id", "action", "detail"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "files_changed", "responses"],
    "additionalProperties": False,
}

# --- prompts --------------------------------------------------------------

# Left vague ("same language as the task"), models drift: one live run answered
# an English task in Spanish, and in another the two agents argued in different
# languages. Naming the rule explicitly is what stops it.
AUTO = "auto"

_LANG_AUTO = (
    "Language: write summary, claim, detail and fix in the SAME language as the "
    "original task in this conversation — whatever language that is. Do not answer "
    "in your own default language, and do not switch language mid-argument. Keep "
    "code, file paths and identifiers exactly as they are."
)


def language_rule(language: str = AUTO) -> str:
    """How the agents should phrase their side of the argument.

    Pinning a language matters beyond taste: the two agents pick their own
    otherwise, and an argument conducted in two languages is harder for the
    person refereeing it than for either participant.
    """

    if not language or language.strip().lower() == AUTO:
        return _LANG_AUTO
    return (
        f"Language: write summary, claim, detail and fix in {language.strip()}, "
        "whatever language the task itself is written in. Keep code, file paths "
        "and identifiers exactly as they are."
    )

SOLVE = """\
You are the SOLVER. Another AI agent, running on a different engine, will review \
your work and challenge it. Do the work properly the first time.

TASK
{task}

Your working directory is the repository you are in. Actually make the changes — \
edit the files, run what you need to run. Do not merely describe what should be done.

When you are finished, report what you did: a short summary and the list of files \
you changed. Leave `responses` empty; there is nothing to respond to yet.
{lang}
"""

CRITIQUE_FIRST = """\
You are the CRITIC. A solver agent was given the task below and claims to have \
completed it. Your job is to find out whether that is true.

TASK GIVEN TO THE SOLVER
{task}

WHAT THE SOLVER REPORTS
{report}

Verify it yourself against the actual repository. The solver's report is a claim, \
not evidence — read the files it touched and check the work against the task.

Rules that make your verdict worth something:
- You may only APPROVE if you actually verified the work. List what you inspected \
or ran in `checked`. An APPROVE with an empty `checked` will be rejected outright.
- Every issue needs concrete `evidence`: a file:line reference or the output of a \
command you ran. "This looks wrong" is not evidence.
- Severity: `blocker` = the task is not done or the result is broken; `major` = \
wrong, misleading, or materially incomplete; `minor` = polish.
- Do not invent problems to look useful. If the work is genuinely correct, say so \
and approve it.
{lang}
"""

CRITIQUE_NEXT = """\
Round {round}. You are still the CRITIC.

THE ISSUES YOU RAISED LAST ROUND
{previous}

HOW THE SOLVER RESPONDED
{responses}

Now do three things, in this order:

1. For every issue the solver marked FIXED or PARTIAL: check the repository and \
confirm it really was fixed. A claimed fix that did not happen is a `blocker`.
2. For every issue the solver REJECTED: decide honestly. If its argument is right, \
put that issue id in `conceded` and drop it. If you still disagree, you may re-raise \
it — but only with NEW evidence that answers the solver's specific objection. \
Repeating yourself louder is not an argument.
3. Raise genuinely new issues only if you find them.

Set `verdict` to APPROVE once nothing blocking or major remains open. Do not hold \
the work hostage over `minor` points, and do not approve work you know is broken \
just because the argument has gone on a while.
{lang}
"""

REBUT = """\
The critic reviewed your work and raised the issues below.

{issues}

Answer every issue by id:
- `FIXED` — you agreed, and you have now actually made the change.
- `PARTIAL` — you addressed part of it; say precisely what remains and why.
- `REJECTED` — you believe the critic is wrong. Say why, with evidence.

You are not required to obey the critic. It reviewed your work without having done \
it, and it can be mistaken about the code, the task, or both. Accepting an issue you \
believe is wrong makes the result worse, not more agreeable — so push back when you \
have grounds. Equally, do not defend a real mistake out of stubbornness: if the \
critic is right, fix it and say so.

Make the fixes in the repository now, then report.
{lang}
"""

FINAL_ROUND = """\
This is the FINAL round. The argument is over after this.

{issues}

The deadlock policy for this run is: the critic's remaining objections stand. \
Implement them now, without further rebuttal, and report what you changed. If an \
objection is genuinely impossible to satisfy, say so explicitly in `detail` rather \
than silently skipping it.
{lang}
"""


def solve_prompt(task: str, *, language: str = AUTO) -> str:
    return SOLVE.format(task=task.strip(), lang=language_rule(language))


def critique_first_prompt(task: str, solver: SolverTurn, *, language: str = AUTO) -> str:
    return CRITIQUE_FIRST.format(
        task=task.strip(),
        report=render_solver_report(solver),
        lang=language_rule(language),
    )


def critique_next_prompt(
    round_no: int, previous: list[Issue], solver: SolverTurn, *, language: str = AUTO
) -> str:
    return CRITIQUE_NEXT.format(
        round=round_no,
        previous=render_issues(previous) or "(none)",
        responses=render_responses(previous, solver) or "(no response)",
        lang=language_rule(language),
    )


def rebut_prompt(
    issues: list[Issue], *, final: bool = False, language: str = AUTO
) -> str:
    template = FINAL_ROUND if final else REBUT
    return template.format(
        issues=render_issues(issues) or "(none)", lang=language_rule(language)
    )


# --- rendering ------------------------------------------------------------


def render_issues(issues: list[Issue]) -> str:
    lines = []
    for issue in issues:
        lines.append(f"[{issue.id}] ({issue.severity.value}) {issue.claim}")
        if issue.evidence:
            lines.append(f"    evidence: {issue.evidence}")
        if issue.fix:
            lines.append(f"    suggested fix: {issue.fix}")
    return "\n".join(lines)


def render_responses(issues: list[Issue], solver: SolverTurn) -> str:
    lines = []
    for issue in issues:
        reply = solver.reply_to(issue.id)
        if reply is None:
            lines.append(f"[{issue.id}] NO ANSWER — the solver ignored this one.")
            continue
        lines.append(f"[{issue.id}] {reply.action.value}: {reply.detail}")
    return "\n".join(lines)


def render_solver_report(solver: SolverTurn) -> str:
    files = ", ".join(solver.files_changed) if solver.files_changed else "(none reported)"
    return f"{solver.summary}\n\nFiles it says it changed: {files}"


# --- parsing --------------------------------------------------------------
#
# Models drift from any schema occasionally — a missing field, a severity spelled
# differently, an id repeated. Parsing is deliberately forgiving about shape and
# strict about meaning: never invent a verdict, but never crash on a stray field.


def parse_critic(payload: dict | None) -> CriticTurn | None:
    if not isinstance(payload, dict):
        return None

    raw_verdict = str(payload.get("verdict", "")).strip().upper()
    if raw_verdict not in (Verdict.APPROVE.value, Verdict.REQUEST_CHANGES.value):
        return None

    issues, seen = [], set()
    for index, raw in enumerate(payload.get("issues") or [], start=1):
        if not isinstance(raw, dict):
            continue
        claim = str(raw.get("claim", "")).strip()
        if not claim:
            continue
        issue_id = str(raw.get("id") or "").strip() or f"i{index}"
        while issue_id in seen:  # models do reuse ids across rounds
            issue_id += "'"
        seen.add(issue_id)
        issues.append(
            Issue(
                id=issue_id,
                severity=_severity(raw.get("severity")),
                claim=claim,
                evidence=str(raw.get("evidence", "")).strip(),
                fix=str(raw.get("fix", "")).strip(),
            )
        )

    return CriticTurn(
        verdict=Verdict(raw_verdict),
        issues=issues,
        checked=_strings(payload.get("checked")),
        conceded=_strings(payload.get("conceded")),
        summary=str(payload.get("summary", "")).strip(),
    )


def parse_solver(payload: dict | None) -> SolverTurn | None:
    if not isinstance(payload, dict):
        return None

    replies = []
    for raw in payload.get("responses") or []:
        if not isinstance(raw, dict):
            continue
        issue_id = str(raw.get("id") or "").strip()
        if not issue_id:
            continue
        replies.append(
            Reply(
                id=issue_id,
                action=_action(raw.get("action")),
                detail=str(raw.get("detail", "")).strip(),
            )
        )

    return SolverTurn(
        summary=str(payload.get("summary", "")).strip(),
        files_changed=_strings(payload.get("files_changed")),
        replies=replies,
    )


def _severity(value: object) -> Severity:
    try:
        return Severity(str(value).strip().lower())
    except ValueError:
        # An unrecognised severity is treated as major: significant enough to
        # keep arguing about, not so severe that it alone blocks the run.
        return Severity.MAJOR


def _action(value: object) -> Action:
    try:
        return Action(str(value).strip().upper())
    except ValueError:
        # An unparseable action must not read as agreement.
        return Action.REJECTED


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]
