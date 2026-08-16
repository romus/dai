"""The contract the two agents argue under: strict schemas plus role prompts.

Everything here exists to stop the two known failure modes of a two-LLM loop.
Agents that agree instantly produce a useless rubber stamp; agents that never
concede loop until the budget dies. The schemas make each side commit to a
machine-checkable position, and the prompts spell out what an honest move looks
like on both sides.

Both roles are charged the same for a claim: one artifact someone else can open.
Agreement that cost nothing to produce is the failure that ships, and it is the
same failure whether the critic approves what it never read or the solver marks
FIXED what it never changed.
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

# No `minItems` anywhere in here, deliberately. On `checked` it would make the
# empty array unreachable and so disable the one mechanical guard against a rubber
# stamp (`consensus.Referee.audit`); on `issues` it would demand a complaint every
# round and manufacture the invented nitpicks this whole contract hedges against.
# It would also not survive the trip: codex hands the schema to `--output-schema`,
# whose keyword subset does not include it.
CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES"]},
        "checked": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "One entry per thing you actually examined, written as "
                "'<artifact> — <what it told you>'. The artifact is a path with the "
                "line range you read, or a command you ran. Include what you went "
                "looking for and did not find. An entry naming no artifact is discarded."
            ),
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                    "claim": {"type": "string"},
                    "evidence": {
                        "type": "string",
                        "description": (
                            "A file:line reference or real command output. An opinion "
                            "is not evidence."
                        ),
                    },
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
        "summary": {
            "type": "string",
            "description": (
                "What you changed and where — files and symbols — what you re-read or "
                "ran to confirm it, and anything you left unfinished or are unsure of."
            ),
        },
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "responses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "action": {"type": "string", "enum": ["FIXED", "PARTIAL", "REJECTED"]},
                    "detail": {
                        "type": "string",
                        "description": (
                            "For FIXED, the file and lines that now carry the fix. For "
                            "REJECTED, the file:line or command output that refutes the "
                            "issue."
                        ),
                    },
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
    "Language: write summary, claim, detail, fix and the prose in `checked` in the "
    "SAME language as the original task in this conversation — whatever language "
    "that is. Do not answer in your own default language, and do not switch language "
    "mid-argument. Keep code, file paths and identifiers exactly as they are, and "
    "quote source lines and command output verbatim rather than translating them."
)


def language_rule(language: str = AUTO) -> str:
    """How the agents should phrase their side of the argument.

    Pinning a language matters beyond taste: the two agents pick their own
    otherwise, and an argument conducted in two languages is harder for the
    person refereeing it than for either participant. The quoted half is pinned
    the other way round: `checked` and `evidence` are addresses now, and a
    translated file path is not an address.
    """

    if not language or language.strip().lower() == AUTO:
        return _LANG_AUTO
    return (
        "Language: write summary, claim, detail, fix and the prose in `checked` in "
        f"{language.strip()}, whatever language the task itself is written in. Keep "
        "code, file paths and identifiers exactly as they are, and quote source lines "
        "and command output verbatim rather than translating them."
    )


# How hard the two lean on each other. The evidence rules do not move with it — an
# approval always has to name what it examined, at every level — because the level
# below `standard` would otherwise be the rubber stamp we are here to remove. What
# it sets is how far the critic hunts and how hard the solver defends, and it is
# deliberately symmetric: a critic told to attack while the solver is told to
# please it produces capitulation, not agreement.
STANDARD = "standard"
RIGOR = ("easy", STANDARD, "strict", "brutal")

_CRITIC_RIGOR = {
    "easy": (
        "Depth for this run: light. Check that the task is done and that nothing is "
        "broken, and stop there — do not go looking for what the task did not ask "
        "for. The evidence rule is unchanged: an approval still has to name what you "
        "examined. Approve as soon as the task is met and nothing you examined is "
        "broken."
    ),
    STANDARD: (
        "Depth for this run: normal. Verify every clause of the task against the "
        "code, and look once where the solver did not: one error path, one caller, "
        "one case it never mentioned. Approve when nothing blocking or major "
        "survives that."
    ),
    "strict": (
        "Depth for this run: high. Assume a defect is present and that you have not "
        "found it yet. Take the change apart: empty and error paths, the case that "
        "worked before it, callers of what changed, the tests — including the ones "
        "that should exist and do not. Approving before you have tried to break it "
        "is not a verdict, it is a guess. If you tried and the work held, say what "
        "you tried, and approve."
    ),
    "brutal": (
        "Depth for this run: maximum. Review this as if it ships unread and you are "
        "named as the reviewer. Reconstruct what the task requires from the task "
        "alone, and treat every divergence as a defect until the code shows "
        "otherwise. Go past the diff: what this change breaks elsewhere in the "
        "repository, what it silently makes wrong, what it left half-done. An "
        "APPROVE here is a strong claim — it says you tried to break this and could "
        "not — so `checked` must show what you tried, not only what you read. The "
        "limits still hold: only what you can evidence, and severity still measured "
        "against the task. A blocker you cannot evidence is not a blocker, and a "
        "preference is never one."
    ),
}

_SOLVER_RIGOR = {
    "easy": (
        "Depth for this run: light. Do the task, check that it works, report it "
        "plainly. The critic is reviewing for breakage, not for taste — do not "
        "gold-plate, and do not argue past the point."
    ),
    STANDARD: (
        "Depth for this run: normal. Before you report, re-read the task clause by "
        "clause against what is on disk and re-read your own diff. Fix what that "
        "turns up instead of reporting it."
    ),
    "strict": (
        "Depth for this run: high. The critic is instructed to assume you left a "
        "defect and to go looking for it. Find it first: run what the repository "
        "gives you to run, check the paths you did not think about while writing, "
        "and say plainly what you did not do. An issue you concede without checking "
        "is as wrong as one you refuse without checking."
    ),
    "brutal": (
        "Depth for this run: maximum. The critic is instructed to try to break this, "
        "so try harder than it will, before it does. Once it files something, hold "
        "your ground where the code supports you: a critic told to be hostile files "
        "some things that are wrong, and accepting a wrong issue makes the result "
        "worse. Every acceptance and every refusal must rest on something you looked "
        "at this turn."
    ),
}


def rigor_rule(level: str = STANDARD, *, critic: bool) -> str:
    """How hard this run's agents are told to lean, for one side of the argument.

    An unknown level falls back to `standard` rather than raising: a typo in a
    config file should cost the run its harshness, not the run.
    """

    table = _CRITIC_RIGOR if critic else _SOLVER_RIGOR
    return table.get((level or "").strip().lower(), table[STANDARD])

# A vague report is armour: "improved error handling" cannot be disproved, so the
# solver's cheapest move was to say nothing checkable and let the critic guess at
# what it meant. What is asked for here is the same thing the critic is charged for
# — an address someone else can open — which is what makes the pressure symmetric.
SOLVE = """\
You are the SOLVER. Another AI agent, running on a different engine, will review \
your work and challenge it with file:line evidence. Do the work properly the first \
time.

TASK
{task}

Your working directory is the repository you are in. Actually make the changes — \
edit the files, run what you need to run. Do not merely describe what should be done.

Before you report, review your own work the way the critic will. Re-read the task \
clause by clause against what is now on disk, re-read your own diff, and run whatever \
the repository gives you to check with. Fix what that pass turns up instead of \
reporting it. The failure of this role is reporting the change you meant to make \
instead of the change you made.

Report every file you touched in `files_changed`, exact paths. Write a summary \
specific enough to be disproved: name the files and the functions, and say what you \
re-read or ran to confirm each part of it. Vagueness is not shelter — an \
unfalsifiable summary reads as a claim with nothing behind it, and sends the critic \
hunting for what you left out. If part of the task is unfinished, or you made a \
choice you are unsure of, say so: a gap you declare is one issue, a gap the critic \
finds is a `blocker` and costs you a round. Leave `responses` empty; there is \
nothing to respond to yet.

{rigor}
{lang}
"""

# The old closing rule — do not invent problems; if the work is genuinely correct,
# approve it — was the last line the critic read, and its condition was satisfied by
# not having looked. The deeper fault was the price: raising one issue cost five
# schema fields, approving cost one sentence. This charges the same for both, one
# artifact either way, and says so.
CRITIQUE_FIRST = """\
You are the CRITIC. A solver agent was given the task below and claims to have \
completed it. Your job is to find out whether that is true, and to leave a record \
that shows how you found out. Treat the work as unverified until you have looked and \
found otherwise.

TASK GIVEN TO THE SOLVER
{task}

WHAT THE SOLVER REPORTS
{report}

That report is a claim, not evidence. Review the repository, not the report.

Do this, in this order:

1. Read the task yourself and derive what it requires, clause by clause, before you \
read any of the solver's code. A requirement nobody noticed is the defect this loop \
exists to catch, and you will not notice it through the solver's eyes.
2. Check each requirement against what is on disk. Open the files the solver names — \
all of them, or the ten most substantial if it named more — and open the ones it did \
not: work never done leaves no trace in the report of the agent that did not do it.
3. Attack the result: empty and error paths, the case that already worked before this \
change, whether the new behaviour is reachable at all. If you can run commands, run \
them and quote what they printed. If you cannot, verify by reading and say so. Never \
reconstruct output you did not see.

Everything you assert here costs the same thing — an artifact someone else can go \
and open. That is what `checked` is for: one entry per thing you examined, naming the \
artifact and what it told you, like "src/parse.py:120-148 — handles the empty case, \
matches the task" or "ran the test suite — 3 failures in test_parse.py". What you \
went looking for and did NOT find belongs there too, in the same form. An entry that \
names nothing specific is not an entry, and an APPROVE whose `checked` names nothing \
is discarded — you will be asked again, and the round is spent.

Rules that make your verdict worth something:
- Every issue needs `evidence` to the same standard: a file:line reference or the \
output of a command you ran. "This looks wrong" is not evidence.
- Severity is measured against the task, not against your taste. `blocker` = the \
task is not done, or the result is broken. `major` = done but wrong, misleading, or \
materially incomplete against something the task asked for. `minor` = true and worth \
recording, not worth blocking over. Filing a real blocker as `minor` to end the \
argument is approving it with extra steps.
- What is only your preference — style, a refactor you would have done differently, \
anything outside the task — is not an issue at any severity. Leave it out rather \
than file it small.
- Agreeing is not the safe answer here. An approval that cost you nothing is not \
politeness, it is a false report, and it is the one failure of this system that \
ships. Disagreeing is not safe either: a manufactured issue sends the solver to edit \
a repository that was already correct. Both are avoided by the same rule — assert \
only what an entry in `checked` or `evidence` supports. Ten artifacts examined and \
nothing wrong found is a good review, and a well-evidenced APPROVE is a result, not \
a failed one. Nothing examined and nothing found is not a review.

{rigor}
{lang}
"""

# Rounds two and later are where the rubber stamp actually happens, and this template
# never mentioned `checked` at all — the whole approval bar lived in CRITIQUE_FIRST
# while the referee went on enforcing it every round. The word-for-word rule is here
# for the referee's sake: it fingerprints claim text, so a critic that rewords a
# surviving complaint makes a stalled argument look like progress and deadlock never
# fires.
CRITIQUE_NEXT = """\
Round {round}. You are still the CRITIC, under the same rule as round one: nothing \
counts unless `checked` or `evidence` names an artifact someone else can open.

WHAT THE SOLVER NOW REPORTS
{report}

THE ISSUES STILL OPEN FROM LAST ROUND
{previous}

HOW THE SOLVER RESPONDED
{responses}

Now do four things, in this order:

1. For every issue the solver marked FIXED or PARTIAL: open the file and confirm the \
change is there and does what it claims. Its report is not the check. A claimed fix \
that did not happen is a `blocker`, and it is the thing you are most likely to be \
handed. If the fix falls short and the issue survives, keep its `claim` word for word \
as you wrote it last round and put what you found in `evidence`.
2. For every issue the solver REJECTED: decide honestly. If its argument is right, \
keep the issue in `issues` and put its id in `conceded`. Never drop an issue \
silently; one that vanishes reads as progress nobody made. Conceding a point you can \
no longer support is a correct outcome, not a loss, and an issue you conceded stays \
conceded. If you still disagree, re-raise it with the `claim` text unchanged word for \
word and the new material in `evidence` — evidence that does not answer the solver's \
specific objection is not new evidence, and repeating yourself louder is not an \
argument.
3. Look once where nobody has looked yet: a requirement you never tested against the \
code, an error path, a caller of what changed. Re-check what you signed off earlier \
too — this round's fixes can break what was correct last round. Record what you \
looked at either way; finding nothing there is a result, and it belongs in `checked`.
4. Then decide. Write `checked` fresh for THIS round — what you verified now, not \
what you read before. Approve once nothing blocking or major remains open and this \
round's `checked` shows what you re-verified; an APPROVE whose `checked` names \
nothing is discarded and you will be asked again. Do not hold the work hostage over \
`minor` points, and do not file a real defect as `minor` to be done with it: \
classifying honestly in either direction costs you nothing. And do not approve work \
you have not re-checked just because the argument has gone on a while.

{rigor}
{lang}
"""

# `FIXED` was a free word — no address, no obligation — so the cheapest way to end a
# round was to claim one. Naming where a non-change belongs matters more than
# forbidding the bluff: the bluff happens when the model cannot see which label fits.
REBUT = """\
The critic reviewed your work and raised the issues below.

{issues}

Answer every issue by id, and answer it with an address:
- `FIXED` — you agreed, and you changed the code in this turn. Say in `detail` which \
file and which lines now carry the fix. Do not mark FIXED anything you did not just \
change: if you already believed it was fine, that is REJECTED; if you changed part of \
it, that is PARTIAL.
- `PARTIAL` — you addressed part of it; say precisely what remains and why.
- `REJECTED` — you believe the critic is wrong. Say why in `detail`, holding yourself \
to the evidence you would demand of it: a file:line reference, or output you actually \
saw.

You are not required to obey the critic. It reviewed your work without having done \
it, and it can be mistaken about the code, the task, or both. Accepting an issue you \
believe is wrong makes the result worse, not more agreeable — so push back when you \
have grounds. Marking an issue FIXED to end the argument is the same failure as a \
critic approving work it never read: both are false reports, and both ship. Equally, \
do not defend a real mistake out of stubbornness: if the critic is right, fix it and \
say so.

Make the fixes in the repository now. Then re-open every file you touched and confirm \
each change is there and does what you just claimed — the critic will do exactly \
this, and it is cheaper for you to find it first. Report this round's files in \
`files_changed`; a file missing from it is one the critic finds the hard way.

{rigor}
{lang}
"""

# A critic that is harder to satisfy ends more runs here, and this turn is applied
# unrebutted. The record has to keep a change the solver was made to apply apart
# from one it agreed with, or the transcript reads as consensus that never happened.
FINAL_ROUND = """\
This is the FINAL round. The argument is over after this.

{issues}

The deadlock policy for this run is: the critic's remaining objections stand. \
Implement them now, without further rebuttal, and report what you changed — the file \
and the lines per issue, in `detail`. Where you implement something you still believe \
is wrong, implement it anyway and say so in `detail`: the person reading this run \
needs to see which changes were forced, not a change of mind you did not have. If an \
objection is genuinely impossible to satisfy, say so explicitly in `detail` rather \
than silently skipping it.
{lang}
"""

# The referee catches an unaudited approval and then has nowhere to put the finding:
# it used to become a note nobody reads plus a wasted write-access solver turn over
# an empty issue list. This is the only prompt in the file addressed to an agent
# about its own last move, and it is what makes "you will be asked again" true.
PROVE = """\
Your approval was not accepted: {reason}.

An APPROVE has to carry the record of how you reached it. Go back to the repository \
now and answer again, with `checked` naming what you examined and what each thing \
told you — a file and the lines you read, or a command and what it printed. Include \
what you went looking for and did not find. If looking properly turns something up, \
say REQUEST_CHANGES and raise it with `evidence`; approving after a real look is \
equally a correct answer. This is your one chance to substantiate it. Reply with the \
verdict object only.
{lang}
"""


def solve_prompt(task: str, *, language: str = AUTO, rigor: str = STANDARD) -> str:
    return SOLVE.format(
        task=task.strip(),
        rigor=rigor_rule(rigor, critic=False),
        lang=language_rule(language),
    )


def critique_first_prompt(
    task: str, solver: SolverTurn, *, language: str = AUTO, rigor: str = STANDARD
) -> str:
    return CRITIQUE_FIRST.format(
        task=task.strip(),
        report=render_solver_report(solver),
        rigor=rigor_rule(rigor, critic=True),
        lang=language_rule(language),
    )


def critique_next_prompt(
    round_no: int,
    previous: list[Issue],
    solver: SolverTurn,
    *,
    language: str = AUTO,
    rigor: str = STANDARD,
) -> str:
    return CRITIQUE_NEXT.format(
        round=round_no,
        # Without this the critic has been blind since round one to which files
        # moved: it was told what the solver answered, never what it now claims.
        report=render_solver_report(solver),
        previous=render_issues(previous) or "(none)",
        responses=render_responses(previous, solver) or "(no response)",
        rigor=rigor_rule(rigor, critic=True),
        lang=language_rule(language),
    )


def rebut_prompt(
    issues: list[Issue],
    *,
    final: bool = False,
    language: str = AUTO,
    rigor: str = STANDARD,
) -> str:
    if final:
        # The enforced turn has no argument left to calibrate: the objections are
        # applied as they stand, whatever this run's rigor was.
        return FINAL_ROUND.format(
            issues=render_issues(issues) or "(none)", lang=language_rule(language)
        )
    return REBUT.format(
        issues=render_issues(issues) or "(none)",
        rigor=rigor_rule(rigor, critic=False),
        lang=language_rule(language),
    )


def prove_prompt(reason: str, *, language: str = AUTO) -> str:
    return PROVE.format(
        reason=reason.strip().rstrip("."), lang=language_rule(language)
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
