"""Accessibility review fixes: live regions, focus, contrast tokens, names, forced colours."""

import re

import pytest
from api_support import FakeGithub, raw_issue, seed_repo, seed_task
from repolace_api.ui import STATIC_DIR
from repolace_shared.db.models import TaskOutcome, TaskStatus
from ui_support import at, seed_stage, ui_client

pytestmark = [pytest.mark.anyio, pytest.mark.db]

CSS = (STATIC_DIR / "app.css").read_text()
UI_JS = (STATIC_DIR / "ui.js").read_text()
THEME_JS = (STATIC_DIR / "theme.js").read_text()


def luminance(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


async def get(db_session_factory, path: str, github=None) -> str:
    async with ui_client(db_session_factory, github) as client:
        return (await client.get(path)).text


class TestLiveRegions:
    async def test_no_polled_region_is_a_live_region_and_one_status_element_sits_outside_them(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=TaskStatus.RUNNING, started_at=at(0))
        listing = await get(db_session_factory, f"/ui/repos/{repo.id}/tasks")
        detail = await get(db_session_factory, f"/ui/tasks/{task.id}")
        fragment = await get(db_session_factory, f"/ui/tasks/{task.id}/live")

        assert "aria-live" not in listing
        assert "aria-live" not in fragment
        assert 'aria-live' not in re.search(r'<div id="live"[^>]*>', detail).group(0)
        for html in (listing, detail):
            assert html.count('role="status"') == 1  # the announcer, in base.html
            assert re.search(r'<div id="announcer" class="sr" role="status"></div>', html)
            assert re.search(r'<p id="htmx-error"[^>]* role="alert" hidden></p>', html)
            # the announcer is outside <main>, so no swap can replace it
            assert html.index('id="announcer"') < html.index('<main id="main"')

    async def test_rows_and_the_live_fragment_carry_the_data_the_announcer_reads(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, issue_number=12, status=TaskStatus.RUNNING, started_at=at(0))
        await seed_stage(db_session, task, "agent", "running", 0, None)
        row = await get(db_session_factory, f"/ui/tasks/{task.id}/row")
        live = await get(db_session_factory, f"/ui/tasks/{task.id}/live")
        for html in (row, live):
            assert 'data-issue="12"' in html and 'data-task-status="running"' in html
            assert 'data-task-stage="Edit"' in html
        assert "Task #" in UI_JS and "is now" in UI_JS

    async def test_the_ticking_elapsed_time_is_hidden_from_assistive_technology(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=TaskStatus.RUNNING, started_at=at(0))
        await seed_stage(db_session, task, "agent", "running", 0, None)
        live = await get(db_session_factory, f"/ui/tasks/{task.id}/live")
        assert re.search(r'<b>running</b> &middot; <span aria-hidden="true">\d+:\d\d</span>', live)
        assert re.search(r'running for <span class="mono" aria-hidden="true">', live)


class TestFocusAndPolling:
    async def test_rows_links_and_filter_controls_have_stable_ids_so_htmx_keeps_focus(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=TaskStatus.RUNNING)
        html = await get(db_session_factory, f"/ui/repos/{repo.id}/tasks")
        assert f'id="task-{task.id}"' in html and f'id="link-{task.id}"' in html
        assert 'id="filter-outcome"' in html and 'id="filter-benchmark"' in html

    def test_polling_pauses_while_hidden_in_static_js_not_through_an_eval_filter(self):
        assert 'document.visibilityState === "hidden"' in UI_JS and "evt.preventDefault()" in UI_JS
        assert "htmx:beforeRequest" in UI_JS

    async def test_no_trigger_uses_a_filter_expression_which_allowEval_false_would_break(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=TaskStatus.RUNNING)
        for path in (f"/ui/repos/{repo.id}/tasks", f"/ui/tasks/{task.id}", f"/ui/tasks/{task.id}/row"):
            html = await get(db_session_factory, path)
            for trigger in re.findall(r'hx-trigger="([^"]*)"', html):
                assert "[" not in trigger, trigger

    def test_request_failures_are_written_to_the_visible_alert(self):
        assert "htmx:responseError" in UI_JS and "htmx:sendError" in UI_JS
        assert "errorBox.hidden = false" in UI_JS

    def test_focus_moves_to_the_first_new_row_after_load_more(self):
        assert "pendingLoadMore" in UI_JS and "link.focus()" in UI_JS

    async def test_a_form_error_is_focused_from_static_js_not_autofocus(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        async with ui_client(db_session_factory, FakeGithub(raw_issue(7))) as client:
            response = await client.post(
                f"/ui/repos/{repo.id}/tasks",
                data={"issue_number": ""},
                headers={"Origin": "http://test"},
            )
        assert "autofocus" not in response.text
        assert 'id="form-error" role="alert" tabindex="-1" data-focus-on-load' in response.text
        assert "data-focus-on-load" in UI_JS


class TestContrast:
    @pytest.mark.parametrize(
        ("block", "surface"),
        [("light", "#ffffff"), ("dark", "#151a21")],
    )
    def test_control_borders_reach_3_to_1_on_the_surface(self, block, surface):
        values = re.findall(r"--border-strong: (#[0-9a-f]{6})", CSS)
        assert len(values) == 3  # light, dark via media query, dark via the manual attribute
        value = values[0] if block == "light" else values[1]
        assert contrast(value, surface) >= 3.0, (block, value)
        assert values[1] == values[2]


class TestNamesAndRoles:
    async def test_the_delivery_group_is_a_list_item_wrapping_a_labelled_list(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo)
        html = await get(db_session_factory, f"/ui/tasks/{task.id}")
        assert '<li class="tailgrp"><ol class="strip" aria-label="Delivery">' in html
        assert 'li class="tailgrp" aria-label' not in html

    async def test_no_pr_is_a_hidden_dash_plus_screen_reader_text_not_an_aria_label_on_a_span(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        await seed_task(db_session, repo)
        html = await get(db_session_factory, f"/ui/repos/{repo.id}/tasks")
        assert '<span class="subtle" aria-hidden="true">&mdash;</span><span class="sr">No PR</span>' in html
        assert 'aria-label="No PR"' not in html

    async def test_times_and_approval_have_text_not_only_title_attributes(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(
            db_session,
            repo,
            status=TaskStatus.COMPLETED,
            outcome=TaskOutcome.PASSED_WITH_TEST_EDIT,
            test_edit_justification="x",
            test_edit_approved_by="maintainer-x",
            test_edit_approved_at=at(3),
            started_at=at(0),
        )
        html = await get(db_session_factory, f"/ui/tasks/{task.id}")
        assert '<span class="sr"> (2026-10-01 12:00:00 UTC)</span>' in html
        assert 'approved<span class="sr"> by maintainer-x</span>' in html

    def test_the_theme_toggle_name_contains_its_visible_text(self):
        assert 'btn.textContent = "Theme: " + current' in THEME_JS
        assert "aria-label" not in THEME_JS

    def test_forced_colours_keep_the_failed_stage_visible(self):
        block = CSS[CSS.index("@media (forced-colors: active)") :]
        assert ".st-failed" in block and "!important" in block
        assert ".st-skipped" in block and ".badge" in block

    async def test_the_disabled_start_button_is_described_and_the_enabled_one_points_at_the_cap(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        github = FakeGithub(raw_issue(7))
        empty = await get(db_session_factory, f"/ui/repos/{repo.id}/tasks/new/confirm", github)
        picked = await get(db_session_factory, f"/ui/repos/{repo.id}/tasks/new/confirm?issue_number=7", github)
        assert 'aria-describedby="pick-hint" disabled' in empty and 'id="pick-hint"' in empty
        assert 'aria-describedby="cap-hint"' in picked and 'id="cap-hint"' in picked

    async def test_the_branch_select_is_always_described_and_marked_invalid_on_error(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        page = await get(db_session_factory, f"/ui/repos/{repo.id}/tasks/new", FakeGithub(raw_issue(7)))
        assert 'aria-describedby="branch-hint"' in page and "aria-invalid" not in page

    async def test_issue_search_results_are_announced_through_a_stable_status_element(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        github = FakeGithub(raw_issue(7), raw_issue(8, title="Another bug"))
        page = await get(db_session_factory, f"/ui/repos/{repo.id}/tasks/new", github)
        fragment = await get(db_session_factory, f"/ui/repos/{repo.id}/tasks/new/issues?q=another", github)
        assert '<p class="sr" id="issue-count" role="status"></p>' in page
        assert 'hx-swap-oob="innerHTML:#issue-count"' not in page
        assert 'hx-swap-oob="innerHTML:#issue-count">1 open issue match &ldquo;another&rdquo;' in fragment
