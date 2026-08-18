"""Parsing the two sides' structured moves.

Models drift from any schema now and then. What matters is that drift never
silently changes the *meaning* of a move — an unparseable refusal must not read
as agreement, and a missing verdict must not be guessed at.
"""

from __future__ import annotations

from dai.models import Action, Severity, SolverTurn, Verdict
from dai.protocol import (
    RIGOR,
    STANDARD,
    critique_first_prompt,
    critique_next_prompt,
    parse_critic,
    parse_solver,
    prove_prompt,
    rebut_prompt,
    render_responses,
    rigor_rule,
    solve_prompt,
)


def critique(**overrides):
    payload = {
        "verdict": "REQUEST_CHANGES",
        "checked": ["read docs/matrix.md"],
        "issues": [
            {
                "id": "i1",
                "severity": "major",
                "claim": "Status column is empty",
                "evidence": "docs/matrix.md:7",
                "fix": "fill it from the README",
            }
        ],
        "conceded": [],
        "summary": "Incomplete.",
    }
    payload.update(overrides)
    return payload


def test_parses_a_well_formed_critique():
    turn = parse_critic(critique())

    assert turn.verdict is Verdict.REQUEST_CHANGES
    assert turn.checked == ["read docs/matrix.md"]
    assert len(turn.issues) == 1
    assert turn.issues[0].severity is Severity.MAJOR
    assert turn.worst is Severity.MAJOR


def test_missing_or_bogus_verdict_is_never_guessed():
    assert parse_critic({"issues": []}) is None
    assert parse_critic(critique(verdict="probably fine")) is None
    assert parse_critic(None) is None
    assert parse_critic("APPROVE") is None


def test_verdict_is_case_insensitive():
    assert parse_critic(critique(verdict="approve")).verdict is Verdict.APPROVE


def test_unknown_severity_falls_back_to_major():
    turn = parse_critic(critique(issues=[{"id": "i1", "claim": "x", "severity": "catastrophic"}]))

    assert turn.issues[0].severity is Severity.MAJOR


def test_issues_without_a_claim_are_dropped():
    turn = parse_critic(critique(issues=[{"id": "i1", "claim": "  "}, {"id": "i2", "claim": "real"}]))

    assert [i.claim for i in turn.issues] == ["real"]


def test_duplicate_ids_are_disambiguated():
    """Models reuse ids across rounds; collapsing them would lose an issue."""

    turn = parse_critic(
        critique(issues=[{"id": "i1", "claim": "first"}, {"id": "i1", "claim": "second"}])
    )

    assert len({i.id for i in turn.issues}) == 2
    assert [i.claim for i in turn.issues] == ["first", "second"]


def test_conceded_issues_drop_out_of_the_open_set():
    turn = parse_critic(
        critique(
            issues=[{"id": "i1", "claim": "a"}, {"id": "i2", "claim": "b"}],
            conceded=["i1"],
        )
    )

    assert [i.id for i in turn.open_issues] == ["i2"]


def test_fingerprint_ignores_wording_noise():
    a = parse_critic(critique(issues=[{"id": "i1", "claim": "Status  column IS empty"}]))
    b = parse_critic(critique(issues=[{"id": "z9", "claim": "status column is empty"}]))

    assert a.issues[0].fingerprint == b.issues[0].fingerprint


# --- solver ---------------------------------------------------------------


def test_parses_solver_replies():
    turn = parse_solver(
        {
            "summary": "done",
            "files_changed": ["a.md"],
            "responses": [
                {"id": "i1", "action": "FIXED", "detail": "filled it"},
                {"id": "i2", "action": "REJECTED", "detail": "critic misread the header"},
            ],
        }
    )

    assert turn.files_changed == ["a.md"]
    assert turn.reply_to("i1").action is Action.FIXED
    assert turn.rejected_ids == {"i2"}


def test_unparseable_action_is_treated_as_refusal_not_agreement():
    """A garbled answer must never be read as the solver conceding."""

    turn = parse_solver({"responses": [{"id": "i1", "action": "¯\\_(ツ)_/¯"}]})

    assert turn.reply_to("i1").action is Action.REJECTED


def test_replies_without_an_id_are_dropped():
    turn = parse_solver({"responses": [{"action": "FIXED"}, {"id": "i2", "action": "FIXED"}]})

    assert [r.id for r in turn.replies] == ["i2"]


def test_parse_solver_tolerates_missing_fields():
    turn = parse_solver({})

    assert turn.summary == ""
    assert turn.replies == []


# --- prompts --------------------------------------------------------------


def test_solve_prompt_carries_the_task_verbatim():
    prompt = solve_prompt("rellena la tabla de docs/matrix.md")

    assert "rellena la tabla de docs/matrix.md" in prompt


def test_every_prompt_pins_the_reply_language():
    """A live run answered an English task in Spanish; the rule must be explicit."""

    for prompt in (
        solve_prompt("t"),
        rebut_prompt([]),
        rebut_prompt([], final=True),
        prove_prompt("it named nothing"),
    ):
        assert "SAME language" in prompt
        assert "do not switch language mid-argument" in prompt


def test_rebut_prompt_grants_the_right_to_refuse():
    issues = parse_critic(critique()).issues
    prompt = rebut_prompt(issues)

    assert "REJECTED" in prompt
    assert "not required to obey" in prompt
    assert "i1" in prompt


def test_final_round_prompt_removes_the_right_to_refuse():
    issues = parse_critic(critique()).issues
    prompt = rebut_prompt(issues, final=True)

    assert "FINAL round" in prompt
    assert "without further rebuttal" in prompt


def test_unanswered_issues_are_shown_as_ignored():
    issues = parse_critic(critique()).issues
    rendered = render_responses(issues, SolverTurn())

    assert "NO ANSWER" in rendered


def test_the_approval_bar_is_restated_every_round():
    """It used to be stated in round one only — and rounds two and later are
    exactly where an agreeable critic waves the work through."""

    for prompt in (
        critique_first_prompt("t", SolverTurn()),
        critique_next_prompt(2, [], SolverTurn()),
    ):
        assert "`checked` names nothing" in prompt


def test_the_critic_is_told_what_an_artifact_looks_like():
    """`checked: ["reviewed the changes"]` satisfied every rule there was."""

    prompt = critique_first_prompt("t", SolverTurn())

    assert "file:line" in prompt
    assert "is not an entry" in prompt


def test_the_critic_is_told_to_keep_its_claims_word_for_word():
    """The referee fingerprints claim text. Reword a surviving complaint and a
    stalled argument reads as progress, so deadlock never fires."""

    prompt = critique_next_prompt(2, [], SolverTurn())

    assert "word for word" in prompt
    assert "`conceded`" in prompt


def test_approving_is_still_a_legitimate_outcome():
    """Trading a rubber stamp for a critic that never approves is not a fix."""

    assert "is a good review" in critique_first_prompt("t", SolverTurn())


def test_the_later_critic_sees_what_the_solver_now_reports():
    """From round two it was told what the solver answered, never what it did."""

    prompt = critique_next_prompt(2, [], SolverTurn(files_changed=["src/parse.py"]))

    assert "src/parse.py" in prompt


def test_the_solver_may_not_claim_a_fix_it_did_not_make():
    prompt = rebut_prompt(parse_critic(critique()).issues)

    assert "Do not mark FIXED anything you did not just change" in prompt


def test_the_final_round_records_forced_changes():
    """A change applied under the deadlock policy is not a change of mind."""

    assert "which changes were forced" in rebut_prompt([], final=True)


def test_the_reask_names_what_was_wrong_with_the_approval():
    prompt = prove_prompt("it named nothing you examined.")

    assert "was not accepted: it named nothing you examined." in prompt
    assert "verdict object only" in prompt


# --- rigor ----------------------------------------------------------------


def test_every_rigor_level_reaches_both_roles():
    """The complaint was that review was toothless — for both sides of it."""

    for level in RIGOR:
        critic_rule = rigor_rule(level, critic=True)
        solver_rule = rigor_rule(level, critic=False)

        assert critic_rule in critique_first_prompt("t", SolverTurn(), rigor=level)
        assert critic_rule in critique_next_prompt(2, [], SolverTurn(), rigor=level)
        assert solver_rule in solve_prompt("t", rigor=level)
        assert solver_rule in rebut_prompt([], rigor=level)


def test_the_two_sides_are_not_handed_the_same_paragraph():
    """A critic told to attack while the solver is told to please it produces
    capitulation, not agreement."""

    assert rigor_rule("brutal", critic=True) != rigor_rule("brutal", critic=False)


def test_an_unknown_rigor_falls_back_to_standard():
    """A typo in a config file should cost the run its harshness, not the run."""

    assert rigor_rule("ferocious", critic=True) == rigor_rule(STANDARD, critic=True)
    assert rigor_rule("", critic=False) == rigor_rule(STANDARD, critic=False)


def test_the_evidence_bar_does_not_move_with_the_rigor():
    """If `easy` could waive it, `easy` would be the rubber stamp we removed."""

    for level in RIGOR:
        assert "`checked` names nothing" in critique_first_prompt(
            "t", SolverTurn(), rigor=level
        )


# --- language -------------------------------------------------------------


def test_auto_language_follows_the_task():
    from dai.protocol import language_rule

    rule = language_rule("auto")

    assert "SAME language" in rule
    assert "do not switch language mid-argument" in rule


def test_a_pinned_language_overrides_the_task_language():
    """Both agents must argue in one language, or refereeing them is harder."""

    from dai.protocol import language_rule

    rule = language_rule("Spanish")

    assert "in Spanish" in rule
    assert "whatever language the task itself is written in" in rule
    assert "Keep code, file paths and identifiers exactly as they are" in rule


def test_pinned_language_reaches_every_role():
    prompts = [
        solve_prompt("t", language="Spanish"),
        rebut_prompt([], language="Spanish"),
        rebut_prompt([], final=True, language="Spanish"),
        critique_first_prompt("t", SolverTurn(), language="Spanish"),
        critique_next_prompt(2, [], SolverTurn(), language="Spanish"),
        prove_prompt("it named nothing", language="Spanish"),
    ]

    assert all("in Spanish" in p for p in prompts)


def test_blank_language_falls_back_to_auto():
    from dai.protocol import language_rule

    assert "SAME language" in language_rule("")
    assert "SAME language" in language_rule("AUTO")


def test_an_image_in_the_task_is_pointed_out_to_both_sides():
    task = "match the header to .dai/runs/run-1/images/img1.png"

    for prompt in (solve_prompt(task), critique_first_prompt(task, SolverTurn())):
        assert "attachment the human added on purpose" in prompt


def test_a_task_with_no_image_reads_exactly_as_it_did():
    from dai.protocol import attachments_rule

    assert attachments_rule("fill in the table in docs/matrix.md") == ""
    assert "attachment" not in solve_prompt("rename config.py to settings.py")
