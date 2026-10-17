"""The words the UI renders are the words in the enums, and the stylesheet knows every one of them."""

import re
from pathlib import Path

import pytest
from repolace_api.ui import STATIC_DIR
from repolace_api.ui.format import STOP_REASON_GLOSS
from repolace_api.ui.pipeline import NODE_LABELS, NODE_STAGES, STATES
from repolace_api.ui.router import env
from repolace_api.ui.views import OUTCOME_FILTERS, UNSCORED
from repolace_shared.db.models import AGENT_STOP_REASONS, TASK_STAGE_STATUSES, TaskOutcome, TaskStatus

CSS = (STATIC_DIR / "app.css").read_text()
TEMPLATES = Path(__file__).resolve().parents[1] / "repolace_api" / "ui" / "templates"


def macro(call: str, **ctx) -> str:
    source = '{% from "_macros.html" import badge, chip, strip_node %}' + "{{ " + call + " }}"
    return env.from_string(source).render(**ctx)


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<svg.*?</svg>", "", html, flags=re.DOTALL)).strip()


class TestBadge:
    @pytest.mark.parametrize("status", list(TaskStatus), ids=lambda s: s.value)
    def test_each_status_renders_its_exact_value_with_an_icon(self, status):
        html = macro("badge(s)", s=status.value)
        assert text_of(re.sub(r"<[^>]+>", "", html)) == status.value
        assert f'class="badge b-{status.value}"' in html
        assert "<svg" in html and 'aria-hidden="true"' in html  # never colour-only

    def test_there_is_a_style_and_an_icon_for_every_status(self):
        for status in TaskStatus:
            assert f".b-{status.value} " in CSS, status
            html = macro("badge(s)", s=status.value)
            assert re.search(r"<svg[^>]*>\s*<[a-z]", html), status  # a non-empty icon

    def test_the_status_filter_vocabulary_is_the_enum(self):
        assert env.globals["statuses"] == [s.value for s in TaskStatus]
        assert len(TaskStatus) == 6


class TestChip:
    @pytest.mark.parametrize("outcome", list(TaskOutcome), ids=lambda o: o.value)
    def test_each_outcome_renders_its_exact_value(self, outcome):
        html = macro("chip(o)", o=outcome.value, unscored=UNSCORED)
        assert outcome.value in text_of(re.sub(r"<[^>]+>", "", html))
        assert "<svg" in html

    def test_null_is_labelled_unscored_and_unscored_is_not_an_enum_value(self):
        html = macro("chip(o)", o=None, unscored=UNSCORED)
        assert 'class="chip c-unscored"' in html and "unscored" in html
        assert UNSCORED not in {o.value for o in TaskOutcome}
        assert ("all", *(o.value for o in TaskOutcome), UNSCORED) == OUTCOME_FILTERS

    def test_badge_and_chip_are_different_shapes_in_the_stylesheet(self):
        badge_rule = re.search(r"\.badge \{[^}]*\}", CSS).group(0)
        chip_rule = re.search(r"\.chip \{[^}]*\}", CSS).group(0)
        assert "border-radius: var(--radius-pill)" in badge_rule
        assert "border-radius: var(--radius-chip)" in chip_rule

    def test_every_chip_class_is_styled(self):
        for cls in ("c-passed", "c-failed", "c-edit", "c-unscored"):
            assert f".{cls}" in CSS

    def test_the_test_edit_chip_shows_the_approval_state_and_has_no_pass_colour(self):
        pending = macro("chip(o)", o="passed_with_test_edit", unscored=UNSCORED)
        assert "awaiting sign-off" in pending and "c-passed" not in pending
        approved = macro("chip(o, 'someone', when)", o="passed_with_test_edit", unscored=UNSCORED, when="2026-01-01")
        assert "approved" in approved and 'title="Approved by someone"' in approved


class TestStrip:
    def test_stage_states_are_the_stored_three_plus_two_derived_and_never_passed(self):
        assert set(STATES) == {*TASK_STAGE_STATUSES, "pending", "skipped"}
        assert "passed" not in STATES

    @pytest.mark.parametrize("state", STATES)
    def test_every_state_has_a_style(self, state):
        assert f".st-{state}" in CSS

    def test_every_stored_stage_name_maps_to_a_node(self):
        from repolace_api.ui.pipeline import STAGE_TO_NODE

        stored = {
            "instance", "preflight", "embedder", "clone", "index", "retrieve", "verify_baseline",
            "record_baseline", "baseline_files", "branch", "agent", "review", "squash", "push", "pr", "finalize",
        }  # fmt: skip
        assert set(STAGE_TO_NODE) == stored
        assert set(NODE_STAGES) | {"plan"} == set(NODE_LABELS)


class TestStopReasons:
    def test_every_stop_reason_has_a_gloss(self):
        assert set(STOP_REASON_GLOSS) == set(AGENT_STOP_REASONS)
        assert all(STOP_REASON_GLOSS.values())


class TestTokens:
    def test_the_dark_theme_defines_exactly_the_tokens_the_light_theme_does(self):
        def names(block: str) -> set[str]:
            return set(re.findall(r"(--[a-z0-9-]+):", block))

        root = re.search(r"^:root \{(.*?)\n\}", CSS, flags=re.DOTALL | re.MULTILINE).group(1)
        manual = re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', CSS, flags=re.DOTALL).group(1)
        auto = re.search(r"prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme=\"light\"\]\) \{(.*?)\n  \}", CSS, flags=re.DOTALL)
        colour = {n for n in names(root) if not n.startswith(("--sans", "--mono", "--space", "--radius"))} - {"color-scheme", "--btn-fg"}
        assert names(manual) == colour
        assert names(auto.group(1)) == colour

    def test_no_template_marks_stored_text_safe_and_none_carries_an_inline_handler_or_style(self):
        for path in TEMPLATES.glob("*.html"):
            text = path.read_text()
            assert "|safe" not in text and "| safe" not in text and "autoescape false" not in text, path.name
            assert not re.search(r'\sstyle="', text), path.name
            assert not re.search(r"\son[a-z]+=", text), path.name
            assert "<script>" not in text, path.name
            assert not re.search(r"https?://(?!www\.w3\.org)", re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)), path.name
