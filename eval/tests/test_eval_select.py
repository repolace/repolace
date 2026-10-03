"""`harness.select_instances`: every filter on its own, then the whole run.

Three layers, none of which touches the network or a real repository: the cheap
pure filters on hand-built rows; the git filters against a fixture "upstream"
built with real `git`; and `run_select` end to end through `httpx.MockTransport`
with that upstream standing in for GitHub.
"""

import json
from pathlib import Path

import httpx
import pytest

from eval_support import (
    CRLF_TEST,
    MOD,
    TEST_MOD,
    Upstream,
    build_upstream,
    dataset_row,
    dataset_transport,
    git_text,
    make_repo,
    patch_from,
    swebench_table,
    write_specs_file,
)
from harness import select_instances as sel
from harness.select_instances import (
    Candidate,
    CloneCache,
    Options,
    RawRow,
    Rejected,
    SelectError,
    SelectionResult,
    Selected,
    analyse_in_git,
    build_instance,
    choose,
    fetch_dataset_rows,
    order_key,
    parse_candidate,
    patch_rejection,
    run_select,
)
from harness.specgen import SpecgenError
from repolace_shared.instances import InstanceError, load_instance, load_instances

TABLE = swebench_table()

GOOD_TEST_PATCH = (
    "diff --git a/tests/test_mod.py b/tests/test_mod.py\n--- a/tests/test_mod.py\n+++ b/tests/test_mod.py\n"
    "@@ -1,5 +1,8 @@\n from pkg.mod import f1\n \n \n def test_it():\n     assert f1() == 1\n"
    "+\n+\n+def test_more():\n+    assert f1() == 100\n"
)
GOOD_GOLD = (
    "diff --git a/pkg/mod.py b/pkg/mod.py\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1,3 +1,3 @@\n"
    " def f1():\n-    return 1\n+    return 100\n def f2():\n"
)


def row_for(**overrides) -> RawRow:
    values = dataset_row(patch=GOOD_GOLD, test_patch=GOOD_TEST_PATCH)
    values.update(overrides)
    return RawRow(0, values, ())


def rejection_of(**overrides) -> Rejected:
    with pytest.raises(Rejected) as caught:
        parse_candidate(row_for(**overrides), TABLE)
    return caught.value


def candidate_for(**overrides) -> Candidate:
    return parse_candidate(row_for(**overrides), TABLE)


def one_file_patch(path: str, *, status: str = "modified") -> str:
    header = f"diff --git a/{path} b/{path}\n"
    if status == "added":
        return header + f"new file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+x\n"
    if status == "deleted":
        return header + f"deleted file mode 100644\n--- a/{path}\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    return header + f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-x\n+y\n"


# --- the dataset -------------------------------------------------------------


def client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport)


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def sample_rows() -> list[dict]:
    return [dataset_row(instance_id=f"psf__requests-{i}", patch=GOOD_GOLD, test_patch=GOOD_TEST_PATCH)
            for i in range(1, 231)]


@pytest.mark.anyio
class TestPaging:
    async def test_every_row_is_fetched_in_pages_of_a_hundred(self, sample_rows):
        requests: list[httpx.Request] = []
        async with client(dataset_transport(sample_rows, requests=requests)) as http:
            rows = await fetch_dataset_rows(http)

        assert [r.row["instance_id"] for r in rows] == [r["instance_id"] for r in sample_rows]
        assert [(int(r.url.params["offset"]), int(r.url.params["length"])) for r in requests] == [
            (0, 100), (100, 100), (200, 100),
        ]
        first = requests[0].url.params
        assert (first["dataset"], first["config"], first["split"]) == ("princeton-nlp/SWE-bench_Verified", "default", "test")

    async def test_a_server_error_is_retried_with_backoff(self, sample_rows):
        sleeps = Sleeps()
        async with client(dataset_transport(sample_rows[:3], script=[503, 502])) as http:
            rows = await fetch_dataset_rows(http, sleep=sleeps)
        assert len(rows) == 3
        assert sleeps.delays == [1.0, 2.0]

    async def test_a_transport_error_is_retried(self, sample_rows):
        sleeps = Sleeps()
        async with client(dataset_transport(sample_rows[:2], script=[httpx.ConnectError("refused")])) as http:
            assert len(await fetch_dataset_rows(http, sleep=sleeps)) == 2
        assert sleeps.delays == [1.0]

    async def test_retry_after_is_honoured(self, sample_rows):
        sleeps = Sleeps()

        def handler(request: httpx.Request) -> httpx.Response:
            if not sleeps.delays:
                return httpx.Response(429, headers={"retry-after": "7"}, text="slow down")
            return dataset_transport(sample_rows[:1]).handler(request)

        async def sleeper(delay: float) -> None:
            sleeps.delays.append(delay)

        async with client(httpx.MockTransport(handler)) as http:
            await fetch_dataset_rows(http, sleep=sleeper)
        assert sleeps.delays == [7.0]

    async def test_a_page_that_keeps_failing_is_an_error_not_a_short_dataset(self, sample_rows):
        sleeps = Sleeps()
        async with client(dataset_transport(sample_rows, script=[500, 500, 500, 500])) as http:
            with pytest.raises(SelectError, match="failed after 4 attempts: HTTP 500"):
                await fetch_dataset_rows(http, sleep=sleeps)

    async def test_a_client_error_is_final_and_not_retried(self, sample_rows):
        sleeps = Sleeps()
        async with client(dataset_transport(sample_rows, script=[404])) as http:
            with pytest.raises(SelectError, match="HTTP 404"):
                await fetch_dataset_rows(http, sleep=sleeps)
        assert sleeps.delays == []

    async def test_fewer_rows_than_the_server_reports_is_an_error(self, sample_rows):
        async with client(dataset_transport(sample_rows[:3], claimed_total=5)) as http:
            with pytest.raises(SelectError, match="empty page at offset 3 of 5"):
                await fetch_dataset_rows(http)

    async def test_a_response_without_a_total_is_an_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"rows": []})

        async with client(httpx.MockTransport(handler)) as http:
            with pytest.raises(SelectError, match="num_rows_total"):
                await fetch_dataset_rows(http)

    async def test_a_cell_the_server_truncated_is_re_requested_alone(self, sample_rows):
        requests: list[httpx.Request] = []
        async with client(dataset_transport(sample_rows[:5], truncate_paged=[2], requests=requests)) as http:
            rows = await fetch_dataset_rows(http)
        assert rows[2].truncated == () and rows[2].row["patch"] == GOOD_GOLD
        assert [int(r.url.params["length"]) for r in requests] == [100, 1]
        assert int(requests[1].url.params["offset"]) == 2

    async def test_a_cell_that_stays_truncated_is_rejected_downstream_never_used(self, sample_rows):
        async with client(dataset_transport(sample_rows[:5], truncate_always=[1])) as http:
            rows = await fetch_dataset_rows(http)
        assert rows[1].truncated == ("patch",)
        with pytest.raises(Rejected, match="truncated-cell: the dataset server truncated patch"):
            parse_candidate(rows[1], TABLE)


# --- the cheap filters -------------------------------------------------------


class TestRowFilters:
    def test_a_good_row_becomes_a_candidate(self):
        candidate = candidate_for()
        assert candidate.instance_id == "psf__requests-1001"
        assert candidate.issue_number == 1001
        assert candidate.fail_to_pass == ("tests/test_mod.py::test_it",)
        assert candidate.spec["base_image"] == "python:3.9-slim"

    def test_id_lists_may_arrive_as_json_strings_or_lists(self):
        assert candidate_for(FAIL_TO_PASS=["a::b"]).fail_to_pass == ("a::b",)
        assert candidate_for(FAIL_TO_PASS='["a::b"]').fail_to_pass == ("a::b",)

    def test_a_missing_column(self):
        row = row_for()
        del row.row["patch"]
        with pytest.raises(Rejected, match="malformed-row: missing column"):
            parse_candidate(row, TABLE)

    def test_a_column_of_the_wrong_type(self):
        assert rejection_of(version=2.0).code == "malformed-row"

    @pytest.mark.parametrize("value", ["not json", '{"a": 1}', "[1, 2]"])
    def test_malformed_id_lists(self, value):
        assert rejection_of(FAIL_TO_PASS=value).code == "malformed-row"

    def test_an_empty_fail_to_pass(self):
        assert rejection_of(FAIL_TO_PASS="[]").code == "empty-fail-to-pass"

    @pytest.mark.parametrize("instance_id", ["../x-1", "a b-1", "psf__requests", "psf__requests-0", "x..y-1"])
    def test_an_instance_id_that_cannot_be_a_file_a_container_and_a_branch(self, instance_id):
        assert rejection_of(instance_id=instance_id).code == "bad-instance-id"

    @pytest.mark.parametrize("sha", ["main", "A" * 40, "a" * 39, "--upload-pack=x" + "a" * 25])
    def test_a_base_commit_that_is_not_a_full_lowercase_sha(self, sha):
        assert rejection_of(base_commit=sha).code == "bad-base-commit"

    def test_an_unknown_environment(self):
        rejection = rejection_of(version="9.9")
        assert rejection.code == "environment" and "version is not in the snapshot" in rejection.detail

    def test_an_environment_that_needs_a_conda_environment(self):
        assert "conda" in rejection_of(repo="pydata/xarray", version="2022.03").detail

    def test_old_python_with_system_packages(self):
        assert "archived" in rejection_of(version="0.1").detail

    def test_old_python_without_system_packages_is_fine(self):
        assert candidate_for(version="0.2").spec["base_image"] == "python:3.6-slim"

    def test_the_first_failing_filter_is_the_recorded_reason(self):
        # Both the F2P list and the version are bad; the F2P filter comes first.
        assert rejection_of(FAIL_TO_PASS="[]", version="9.9").code == "empty-fail-to-pass"


class TestPatchFilters:
    def test_a_test_patch_must_not_touch_a_non_test_file(self):
        rejection = patch_rejection(one_file_patch("pkg/mod.py"), GOOD_GOLD)
        assert rejection.code == "test-patch" and "non-test file: pkg/mod.py" in rejection.detail

    def test_a_test_patch_must_not_delete(self):
        rejection = patch_rejection(one_file_patch("tests/test_mod.py", status="deleted"), GOOD_GOLD)
        assert "deleted tests/test_mod.py" in rejection.detail

    def test_a_test_patch_must_not_rename(self):
        rename = "diff --git a/tests/a.py b/tests/b.py\nsimilarity index 100%\nrename from tests/a.py\nrename to tests/b.py\n"
        assert "renamed tests/b.py" in patch_rejection(rename, GOOD_GOLD).detail

    def test_a_test_patch_must_not_write_into_dot_git(self):
        rejection = patch_rejection(one_file_patch(".git/hooks/post-commit", status="added"), GOOD_GOLD)
        assert rejection.code == "test-patch" and "non-test file" in rejection.detail

    def test_a_test_patch_must_not_touch_a_binary_file(self):
        binary = "diff --git a/tests/x.png b/tests/x.png\nnew file mode 100644\nBinary files /dev/null and b/tests/x.png differ\n"
        assert "binary file: tests/x.png" in patch_rejection(binary, GOOD_GOLD).detail

    @pytest.mark.parametrize("path", ["tox.ini", "setup.cfg", "pyproject.toml", "requirements.txt", "tests/requirements.txt"])
    def test_a_test_patch_must_not_touch_a_dependency_manifest(self, path):
        assert "dependency manifest" in patch_rejection(one_file_patch(path), GOOD_GOLD).detail

    def test_a_test_patch_may_add_or_modify_test_files_conftest_and_data(self):
        for path in ("tests/test_x.py", "tests/conftest.py", "testing/data/golden.txt", "pkg/test_inline.py", "conftest.py"):
            assert patch_rejection(one_file_patch(path, status="added"), GOOD_GOLD) is None, path

    def test_an_empty_test_patch(self):
        assert patch_rejection("", GOOD_GOLD).detail == "it touches no file"

    @pytest.mark.parametrize("path", ["tests/test_mod.py", "conftest.py", "pytest.ini", "pkg/tests.py", "tests/__snapshots__/x.ambr"])
    def test_a_gold_patch_must_not_touch_a_protected_path(self, path):
        rejection = patch_rejection(GOOD_TEST_PATCH, one_file_patch(path))
        assert rejection.code == "gold-patch" and "protected" in rejection.detail

    @pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".GitHub/workflows/ci.yml"])
    def test_a_gold_patch_must_not_touch_github_workflows(self, path):
        assert patch_rejection(GOOD_TEST_PATCH, one_file_patch(path)).code == "gold-patch"

    @pytest.mark.parametrize(
        "path", [".gitignore", ".gitattributes", "pkg/.gitkeep", ".gitmodules", "pkg/.GITfoo/x.py", ".git/hooks/post-commit"]
    )
    def test_a_gold_patch_must_not_touch_a_dot_git_path_the_edit_tool_refuses(self, path):
        rejection = patch_rejection(GOOD_TEST_PATCH, one_file_patch(path))
        assert rejection.code == "gold-patch" and ".git* path" in rejection.detail

    def test_a_gold_patch_must_not_delete_or_rename(self):
        assert "the agent cannot delete or rename" in patch_rejection(GOOD_TEST_PATCH, one_file_patch("pkg/mod.py", status="deleted")).detail
        rename = "diff --git a/pkg/a.py b/pkg/b.py\nsimilarity index 100%\nrename from pkg/a.py\nrename to pkg/b.py\n"
        assert "renamed pkg/b.py" in patch_rejection(GOOD_TEST_PATCH, rename).detail

    def test_a_gold_patch_may_add_a_source_file(self):
        assert patch_rejection(GOOD_TEST_PATCH, one_file_patch("pkg/new.py", status="added")) is None

    def test_an_unreadable_patch_is_a_rejection_not_a_crash(self):
        assert patch_rejection("garbage", GOOD_GOLD).code == "unreadable-patch"


# --- the git filters ---------------------------------------------------------


@pytest.fixture
def upstream(tmp_path) -> Upstream:
    return build_upstream(tmp_path / "upstream")


@pytest.fixture
def caches(tmp_path, upstream) -> CloneCache:
    return CloneCache(tmp_path / "cache", url_for=lambda repo: str(upstream.path))


def real_candidate(upstream: Upstream, tmp_path: Path, *, test_files: dict, gold_files: dict,
                   base: str | None = None, **overrides) -> Candidate:
    base = base or upstream.base
    return parse_candidate(
        RawRow(0, dataset_row(
            base_commit=base,
            test_patch=patch_from(upstream, base, test_files, tmp_path / "scratch"),
            patch=patch_from(upstream, base, gold_files, tmp_path / "scratch"),
            **overrides,
        ), ()),
        TABLE,
    )


def edited_mod(old: str = "    return 1\n", new: str = "    return 100\n") -> str:
    return MOD.replace(old, new, 1)


@pytest.mark.anyio
class TestGitFilters:
    async def test_the_post_apply_files_are_the_bytes_the_commit_would_hold(self, upstream, caches, tmp_path):
        candidate = real_candidate(
            upstream, tmp_path,
            test_files={"tests/test_mod.py": "from pkg.mod import f1\n\n\ndef test_it():\n    assert f1() == 100\n",
                        "tests/test_new.py": "def test_new():\n    pass\n"},
            gold_files={"pkg/mod.py": edited_mod()},
        )

        test_files, gold_files = await analyse_in_git(candidate, caches)

        assert test_files == {
            "tests/test_mod.py": "from pkg.mod import f1\n\n\ndef test_it():\n    assert f1() == 100\n",
            "tests/test_new.py": "def test_new():\n    pass\n",
        }
        assert gold_files == {"pkg/mod.py": edited_mod()}

    async def test_crlf_line_endings_survive(self, upstream, caches, tmp_path):
        new_crlf = CRLF_TEST + "\r\ndef test_c():\r\n    assert True\r\n"
        candidate = real_candidate(
            upstream, tmp_path, test_files={"tests/test_crlf.py": new_crlf}, gold_files={"pkg/mod.py": edited_mod()},
        )

        test_files, _ = await analyse_in_git(candidate, caches)

        assert test_files["tests/test_crlf.py"] == new_crlf
        assert "\r\n" in test_files["tests/test_crlf.py"]

    async def test_an_eol_attribute_in_the_repository_cannot_change_the_bytes(self, tmp_path):
        # A tracked `.gitattributes` asking for CRLF conversion is exactly what a
        # checkout-based read would have honoured; reading blobs consults none of it.
        plain = tmp_path / "plain"
        make_repo(plain, {"pkg/mod.py": MOD, "tests/test_mod.py": TEST_MOD})
        base = commit_attributes(plain, "* text eol=crlf\n")
        fixture = Upstream(plain, base, base)
        caches = CloneCache(tmp_path / "cache", url_for=lambda repo: str(plain))
        candidate = real_candidate(
            fixture, tmp_path, base=base,
            test_files={"tests/test_mod.py": TEST_MOD.replace("== 1", "== 7")},
            gold_files={"pkg/mod.py": edited_mod()},
        )

        test_files, gold_files = await analyse_in_git(candidate, caches)

        assert test_files["tests/test_mod.py"] == TEST_MOD.replace("== 1", "== 7")
        assert gold_files["pkg/mod.py"] == edited_mod()

    async def test_a_symlink_at_the_base_commit_is_rejected_and_named(self, upstream, caches, tmp_path):
        candidate = real_candidate(
            upstream, tmp_path, base=upstream.base_symlink,
            test_files={"tests/test_mod.py": "x = 1\n"}, gold_files={"pkg/mod.py": edited_mod()},
        )
        with pytest.raises(Rejected) as caught:
            await analyse_in_git(candidate, caches)
        assert caught.value.code == "symlink-or-submodule"
        assert "1 symlink/gitlink entry at the base commit: LICENSE" in caught.value.detail

    async def test_a_gold_patch_that_does_not_apply_is_rejected(self, upstream, caches, tmp_path):
        candidate = real_candidate(
            upstream, tmp_path, test_files={"tests/test_mod.py": "x = 1\n"}, gold_files={"pkg/mod.py": edited_mod()},
        )
        broken = Candidate(**{**candidate.__dict__, "patch": candidate.patch.replace(" def f1():", " def zzz():")})
        with pytest.raises(Rejected) as caught:
            await analyse_in_git(broken, caches)
        assert caught.value.code == "apply-failed" and "patch does not apply" in caught.value.detail

    async def test_a_test_patch_that_does_not_apply_is_rejected_by_name(self, upstream, caches, tmp_path):
        candidate = real_candidate(
            upstream, tmp_path, test_files={"tests/test_mod.py": "x = 1\n"}, gold_files={"pkg/mod.py": edited_mod()},
        )
        broken = Candidate(**{**candidate.__dict__, "test_patch": candidate.test_patch.replace("from pkg", "import pkg")})
        with pytest.raises(Rejected, match="apply-failed: test_patch does not apply"):
            await analyse_in_git(broken, caches)

    async def test_a_patch_path_that_climbs_out_of_the_tree_is_rejected(self, upstream, caches, tmp_path):
        candidate = real_candidate(
            upstream, tmp_path, test_files={"tests/test_mod.py": "x = 1\n"}, gold_files={"pkg/mod.py": edited_mod()},
        )
        climbing = one_file_patch("tests/../../escape.py", status="added")
        hostile = Candidate(**{**candidate.__dict__, "test_patch": climbing})
        with pytest.raises(Rejected) as caught:
            await analyse_in_git(hostile, caches)
        assert caught.value.code in ("apply-failed", "invalid-instance")

    async def test_a_post_apply_file_that_is_not_utf8_is_rejected(self, upstream, caches, tmp_path):
        legacy = upstream_legacy_with_line_changed(upstream)
        candidate = real_candidate(
            upstream, tmp_path, test_files={"tests/test_mod.py": "x = 1\n"}, gold_files={"legacy.py": legacy},
        )
        with pytest.raises(Rejected, match="not-utf8: patch: legacy.py is not valid UTF-8"):
            await analyse_in_git(candidate, caches)

    async def test_a_base_commit_the_clone_does_not_have_is_rejected(self, upstream, caches, tmp_path):
        candidate = real_candidate(
            upstream, tmp_path, test_files={"tests/test_mod.py": "x = 1\n"}, gold_files={"pkg/mod.py": edited_mod()},
        )
        missing = Candidate(**{**candidate.__dict__, "base_commit": "b" * 40})
        with pytest.raises(Rejected, match="base-commit-missing"):
            await analyse_in_git(missing, caches)

    async def test_a_commit_made_after_the_clone_is_found_by_fetching_once(self, tmp_path):
        plain = tmp_path / "plain"
        first_base = make_repo(plain, {"pkg/mod.py": MOD, "tests/test_mod.py": TEST_MOD})
        caches = CloneCache(tmp_path / "cache", url_for=lambda repo: str(plain))
        first = real_candidate(
            Upstream(plain, first_base, first_base), tmp_path,
            test_files={"tests/test_mod.py": "x = 1\n"}, gold_files={"pkg/mod.py": edited_mod()},
        )
        await analyse_in_git(first, caches)  # populates the cache
        newer = commit_attributes(plain, "# nothing\n", name="LATER.txt")
        later = Candidate(**{**first.__dict__, "base_commit": newer})

        await analyse_in_git(later, caches)  # not in the cache until it fetches

    async def test_the_cache_clone_is_full_and_is_never_written_to(self, upstream, caches, tmp_path):
        candidate = real_candidate(
            upstream, tmp_path, test_files={"tests/test_mod.py": "x = 1\n"}, gold_files={"pkg/mod.py": edited_mod()},
        )
        cache = await caches.get("psf/requests")
        before = (git_text(cache, "rev-parse", "HEAD"), git_text(cache, "count-objects", "-v"))

        await analyse_in_git(candidate, caches)

        after = (git_text(cache, "rev-parse", "HEAD"), git_text(cache, "count-objects", "-v"))
        assert before == after
        assert git_text(cache, "rev-parse", "--is-shallow-repository") == "false"
        assert git_text(cache, "status", "--porcelain") == ""
        assert not list((tmp_path / "cache").glob("*.partial"))

    async def test_a_cache_path_is_only_ever_for_an_allowlisted_repository(self, tmp_path):
        with pytest.raises(SelectError, match="not an allowlisted owner/name"):
            sel.cache_path(tmp_path, "evil/../../etc")
        with pytest.raises(SelectError, match="not an allowlisted"):
            sel.cache_path(tmp_path, "django/django")
        assert sel.cache_path(tmp_path, "psf/requests") == tmp_path / "psf__requests"

    async def test_an_interrupted_clone_is_not_trusted_next_time(self, upstream, tmp_path):
        cache_dir = tmp_path / "cache"
        (cache_dir / "psf__requests.partial").mkdir(parents=True)
        (cache_dir / "psf__requests.partial" / "junk").write_text("half a clone")
        caches = CloneCache(cache_dir, url_for=lambda repo: str(upstream.path))

        path = await caches.get("psf/requests")

        assert (path / ".git").is_dir() and not (cache_dir / "psf__requests.partial").exists()


def commit_attributes(repo: Path, content: str, *, name: str = ".gitattributes") -> str:
    from eval_support import commit_files

    return commit_files(repo, {name: content}, f"add {name}")


def upstream_legacy_with_line_changed(upstream: Upstream) -> bytes:
    # Line 20 changes; line 1 (not UTF-8) is nowhere near the hunk's context.
    from eval_support import LEGACY

    return LEGACY.replace(b"x20 = 20\n", b"x20 = 2000\n")


# --- building the instance ---------------------------------------------------


class TestBuildInstance:
    def test_the_instance_is_validated_by_the_loader_before_it_counts(self):
        candidate = candidate_for()
        instance = build_instance(candidate, {"tests/test_mod.py": "x = 1\n"}, {"pkg/mod.py": "y = 1\n"})
        assert instance.targeted_p2p is False and "test_targets" not in instance.spec
        assert instance.issue_title == "Fix the thing"

    def test_an_invalid_instance_is_a_rejection(self):
        with pytest.raises(Rejected, match="invalid-instance"):
            build_instance(candidate_for(), {"../escape.py": "x"}, {"pkg/mod.py": "y"})

    def test_an_instance_with_no_test_files_is_a_rejection(self):
        with pytest.raises(Rejected, match="invalid-instance.*test_files is empty"):
            build_instance(candidate_for(), {}, {"pkg/mod.py": "y"})


# --- choosing ----------------------------------------------------------------


def stub_candidates(per_repo: dict[str, int]) -> list[Candidate]:
    base = candidate_for()
    out = []
    for repo, n in per_repo.items():
        for i in range(n):
            out.append(Candidate(**{**base.__dict__, "repo": repo, "instance_id": f"{repo.split('/')[1]}-{i + 1}"}))
    return out


async def accept_all(candidate: Candidate) -> Selected:
    return Selected(candidate, build_instance(candidate, {"tests/test_mod.py": "x = 1\n"}, {"pkg/mod.py": "y = 1\n"}))


@pytest.mark.anyio
class TestChoose:
    POOL = {"psf/requests": 6, "pallets/flask": 6, "mwaskom/seaborn": 6}

    async def test_it_spreads_across_repositories_before_repeating_one(self):
        result = await choose(stub_candidates(self.POOL), accept_all, count=4, max_per_repo=6, seed=0)
        by_repo = [s.candidate.repo for s in result.selected]
        assert sorted(set(by_repo)) == ["mwaskom/seaborn", "pallets/flask", "psf/requests"]
        assert max(by_repo.count(r) for r in set(by_repo)) == 2

    async def test_the_cap_per_repository_holds_even_when_the_target_is_larger(self):
        result = await choose(stub_candidates(self.POOL), accept_all, count=10, max_per_repo=2, seed=0)
        assert len(result.selected) == 6
        assert all([s.candidate.repo for s in result.selected].count(r) == 2 for r in self.POOL)

    async def test_the_target_stops_the_evaluation_and_the_rest_are_listed(self):
        evaluated: list[str] = []

        async def counting(candidate: Candidate) -> Selected:
            evaluated.append(candidate.instance_id)
            return await accept_all(candidate)

        result = await choose(stub_candidates(self.POOL), counting, count=3, max_per_repo=6, seed=0)
        assert len(result.selected) == 3 and len(evaluated) == 3
        assert len(result.not_evaluated) == 15
        assert set(result.not_evaluated).isdisjoint(evaluated)
        assert result.not_evaluated == sorted(result.not_evaluated)

    async def test_a_rejection_is_recorded_with_its_reason_and_does_not_use_the_cap(self):
        pool = stub_candidates({"psf/requests": 4})

        async def picky(candidate: Candidate) -> Selected:
            if candidate.instance_id in ("requests-1", "requests-2"):
                raise Rejected("apply-failed", "does not apply")
            return await accept_all(candidate)

        result = await choose(pool, picky, count=5, max_per_repo=2, seed=0)
        # Which two the seed puts first is not the point; the cap counts accepts only.
        assert len(result.selected) + len(result.rejections) == 4 - len(result.not_evaluated)
        assert all(reason == "apply-failed: does not apply" for reason in result.rejections.values())
        assert len(result.selected) <= 2

    async def test_a_candidate_skipped_because_its_repository_hit_the_cap_is_still_listed(self):
        pool = stub_candidates({"psf/requests": 4})
        result = await choose(pool, accept_all, count=5, max_per_repo=2, seed=0)
        assert len(result.selected) == 2 and len(result.not_evaluated) == 2
        everyone = {c.instance_id for c in pool}
        accounted = {s.candidate.instance_id for s in result.selected} | set(result.rejections) | set(result.not_evaluated)
        assert accounted == everyone

    async def test_the_same_seed_gives_the_same_selection_whatever_the_input_order(self):
        pool = stub_candidates(self.POOL)
        first = await choose(pool, accept_all, count=7, max_per_repo=6, seed=3)
        second = await choose(list(reversed(pool)), accept_all, count=7, max_per_repo=6, seed=3)
        assert [s.candidate.instance_id for s in first.selected] == [s.candidate.instance_id for s in second.selected]

    async def test_a_different_seed_changes_the_order(self):
        pool = stub_candidates({"psf/requests": 20})
        orders = {
            tuple(s.candidate.instance_id for s in (await choose(pool, accept_all, count=20, max_per_repo=20, seed=seed)).selected)
            for seed in (0, 1, 2)
        }
        assert len(orders) > 1

    def test_the_order_key_is_a_stable_hash_not_a_shuffle(self):
        assert order_key(0, "a-1") == order_key(0, "a-1") != order_key(1, "a-1")
        assert len(order_key(0, "a-1")) == 64

    async def test_nothing_to_choose_from(self):
        result = await choose([], accept_all, count=5, max_per_repo=2, seed=0)
        assert result.selected == [] and result.rejections == {} and result.not_evaluated == []


# --- end to end --------------------------------------------------------------


GOOD = "psf__requests-1001"
CRLF = "psf__requests-1002"
SYMLINK = "psf__requests-1003"
NO_APPLY = "psf__requests-1004"
NON_UTF8 = "psf__requests-1005"
NO_COMMIT = "psf__requests-1006"
TEST_TOUCHES_SOURCE = "psf__requests-1007"
NO_F2P = "psf__requests-1008"
OLD_PYTHON = "psf__requests-1009"
OFF_ALLOWLIST = "django__django-1"


@pytest.fixture
def dataset(upstream, tmp_path) -> list[dict]:
    scratch = tmp_path / "scratch"
    mod_gold = patch_from(upstream, upstream.base, {"pkg/mod.py": edited_mod()}, scratch)
    plain_test = patch_from(upstream, upstream.base, {"tests/test_mod.py": "x = 1\n", "tests/test_new.py": "y = 1\n"}, scratch)
    crlf_test = patch_from(upstream, upstream.base, {"tests/test_crlf.py": CRLF_TEST + "\r\ndef test_c():\r\n    pass\r\n"}, scratch)
    legacy_gold = patch_from(upstream, upstream.base, {"legacy.py": upstream_legacy_with_line_changed(upstream)}, scratch)
    sym_test = patch_from(upstream, upstream.base_symlink, {"tests/test_mod.py": "x = 2\n"}, scratch)
    sym_gold = patch_from(upstream, upstream.base_symlink, {"pkg/mod.py": edited_mod()}, scratch)
    source_test = patch_from(upstream, upstream.base, {"pkg/mod.py": edited_mod("return 2", "return 22")}, scratch)

    def row(instance_id, **kw):
        values = dict(instance_id=instance_id, base_commit=upstream.base, patch=mod_gold, test_patch=plain_test)
        values.update(kw)
        return dataset_row(**values)

    return [
        row(GOOD),
        row(CRLF, test_patch=crlf_test),
        row(SYMLINK, base_commit=upstream.base_symlink, patch=sym_gold, test_patch=sym_test),
        row(NO_APPLY, patch=mod_gold.replace(" def f1():", " def zzz():")),
        row(NON_UTF8, patch=legacy_gold),
        row(NO_COMMIT, base_commit="b" * 40),
        row(TEST_TOUCHES_SOURCE, test_patch=source_test),
        row(NO_F2P, fail_to_pass=[]),
        row(OLD_PYTHON, version="0.1"),
        row(OFF_ALLOWLIST, repo="django/django"),
    ]


def options(tmp_path: Path, **overrides) -> Options:
    tmp_path.mkdir(parents=True, exist_ok=True)
    specs = write_specs_file(tmp_path / "specs.json", TABLE)
    values = dict(instances_dir=tmp_path / "instances", cache_dir=tmp_path / "cache", specs_path=specs)
    values.update(overrides)
    return Options(**values)


@pytest.mark.anyio
class TestRunSelect:
    async def run(self, tmp_path, upstream, dataset, **overrides) -> SelectionResult:
        async with client(dataset_transport(dataset)) as http:
            return await run_select(
                options(tmp_path, **overrides), http, url_for=lambda repo: str(upstream.path),
            )

    async def test_the_good_instances_are_selected_and_each_failure_has_its_own_reason(self, tmp_path, upstream, dataset):
        result = await self.run(tmp_path, upstream, dataset)

        assert sorted(s.candidate.instance_id for s in result.selected) == [GOOD, CRLF]
        assert result.rejections[SYMLINK].startswith("symlink-or-submodule:")
        assert result.rejections[NO_APPLY].startswith("apply-failed: patch does not apply")
        assert result.rejections[NON_UTF8].startswith("not-utf8:")
        assert result.rejections[NO_COMMIT].startswith("base-commit-missing:")
        assert result.not_evaluated == []
        # Each reason is one line, or it would break the manifest's table.
        assert all("\n" not in reason for reason in result.rejections.values())

    async def test_the_written_instances_load_and_carry_the_overlay_bytes(self, tmp_path, upstream, dataset):
        await self.run(tmp_path, upstream, dataset)
        instances = load_instances(tmp_path / "instances")

        assert sorted(instances) == [GOOD, CRLF]
        good = instances[GOOD]
        assert good.repo == "psf/requests" and good.version == "2.0" and good.issue_number == 1001
        assert dict(good.test_files) == {"tests/test_mod.py": "x = 1\n", "tests/test_new.py": "y = 1\n"}
        assert dict(good.gold_files) == {"pkg/mod.py": edited_mod()}
        assert good.targeted_p2p is False and "test_targets" not in good.spec
        assert good.spec["base_image"] == "python:3.9-slim"
        assert good.fail_to_pass == ("tests/test_mod.py::test_it",)
        assert "\r\n" in instances[CRLF].test_files["tests/test_crlf.py"]

    async def test_the_gold_patch_is_kept_beside_the_instance_verbatim(self, tmp_path, upstream, dataset):
        await self.run(tmp_path, upstream, dataset)
        sidecar = tmp_path / "instances" / f"{GOOD}.gold.patch"
        assert sidecar.read_text() == next(r for r in dataset if r["instance_id"] == GOOD)["patch"]
        # `load_instances` must keep ignoring it.
        assert GOOD in load_instances(tmp_path / "instances")

    async def test_the_manifest_lists_every_candidate_and_every_rejection_with_its_reason(self, tmp_path, upstream, dataset):
        await self.run(tmp_path, upstream, dataset)
        manifest = (tmp_path / "instances" / "MANIFEST.md").read_text()

        for instance_id in (GOOD, CRLF):
            assert f"| {instance_id} | psf/requests | 2.0 | python:3.9-slim |" in manifest
        for instance_id, code in [
            (SYMLINK, "symlink-or-submodule"), (NO_APPLY, "apply-failed"), (NON_UTF8, "not-utf8"),
            (NO_COMMIT, "base-commit-missing"), (TEST_TOUCHES_SOURCE, "test-patch"),
            (NO_F2P, "empty-fail-to-pass"), (OLD_PYTHON, "environment"),
        ]:
            assert f"| {instance_id} | {code}:" in manifest, instance_id
        assert "touches a non-test file: pkg/mod.py" in manifest
        assert "django/django (1)" in manifest
        table_rows = [line for line in manifest.split("## Every rejection")[1].split("## Not evaluated")[0].splitlines() if line.strip()]
        assert len(table_rows) == 2 + 7 and all(line.startswith("|") for line in table_rows)
        assert "The seed is a degree of freedom in the result: fix and record it before the first agent run." in manifest
        assert "- selected: 2; rejected: 7; not evaluated (target or per-repo cap reached): 0" in manifest
        assert OFF_ALLOWLIST not in manifest

    async def test_the_selection_is_reproducible_byte_for_byte(self, tmp_path, upstream, dataset):
        await self.run(tmp_path / "one", upstream, dataset)
        await self.run(tmp_path / "two", upstream, dataset)
        for name in ("MANIFEST.md", f"{GOOD}.json", f"{GOOD}.gold.patch", f"{CRLF}.json"):
            assert (tmp_path / "one" / "instances" / name).read_bytes() == (tmp_path / "two" / "instances" / name).read_bytes()

    async def test_the_target_count_caps_the_selection_and_the_rest_are_not_evaluated(self, tmp_path, upstream, dataset):
        result = await self.run(tmp_path, upstream, dataset, count=1)
        assert len(result.selected) == 1
        manifest = (tmp_path / "instances" / "MANIFEST.md").read_text()
        assert "not evaluated (target or per-repo cap reached):" in manifest and result.not_evaluated
        assert all(i in manifest for i in result.not_evaluated)

    async def test_an_existing_file_is_never_overwritten_without_the_flag(self, tmp_path, upstream, dataset):
        await self.run(tmp_path, upstream, dataset)
        instance = tmp_path / "instances" / f"{GOOD}.json"
        edited = json.loads(instance.read_text())
        edited["targeted_p2p"] = True
        edited["spec"]["test_targets"] = ["tests"]
        instance.write_text(json.dumps(edited))
        manifest_before = (tmp_path / "instances" / "MANIFEST.md").read_bytes()

        with pytest.raises(SelectError, match="refusing to overwrite .*hand edits"):
            await self.run(tmp_path, upstream, dataset)

        assert json.loads(instance.read_text())["targeted_p2p"] is True
        assert (tmp_path / "instances" / "MANIFEST.md").read_bytes() == manifest_before

        await self.run(tmp_path, upstream, dataset, overwrite=True)
        assert json.loads(instance.read_text())["targeted_p2p"] is False

    async def test_nothing_is_written_when_selection_fails_before_it_finishes(self, tmp_path, upstream, dataset):
        with pytest.raises(SelectError):
            async with client(dataset_transport(dataset, script=[404])) as http:
                await run_select(options(tmp_path), http, url_for=lambda repo: str(upstream.path))
        assert not (tmp_path / "instances").exists()

    async def test_a_missing_snapshot_fails_before_any_request_is_made(self, tmp_path, upstream, dataset):
        requests: list[httpx.Request] = []
        with pytest.raises(SpecgenError, match="does not exist"):
            async with client(dataset_transport(dataset, requests=requests)) as http:
                await run_select(options(tmp_path, specs_path=tmp_path / "absent.json"), http)
        assert requests == []

    async def test_a_repeated_instance_id_in_the_dataset_is_an_error(self, tmp_path, upstream, dataset):
        with pytest.raises(SelectError, match="repeats instance id"):
            await self.run(tmp_path, upstream, [*dataset, dataset[0]])

    async def test_the_written_files_pass_the_loader_that_the_pipeline_uses(self, tmp_path, upstream, dataset):
        await self.run(tmp_path, upstream, dataset)
        for instance_id in (GOOD, CRLF):
            load_instance(tmp_path / "instances" / f"{instance_id}.json")
        with pytest.raises(InstanceError):
            load_instance(tmp_path / "instances" / "MANIFEST.md")


class TestCommandLine:
    def test_help_exits_zero(self, capsys):
        assert sel.main(["--help"]) == 0
        assert "--max-per-repo" in capsys.readouterr().out

    @pytest.mark.parametrize("flag", ["--count", "--max-per-repo"])
    def test_a_target_below_one_is_a_usage_error(self, flag, capsys):
        assert sel.main([flag, "0"]) == 2
        assert "at least 1" in capsys.readouterr().err

    def test_an_unknown_flag_is_a_usage_error(self):
        assert sel.main(["--nope"]) == 2

    def test_a_missing_snapshot_is_a_clean_failure_with_the_instruction(self, tmp_path, capsys):
        code = sel.main(["--specs", str(tmp_path / "absent.json"), "--instances-dir", str(tmp_path / "i"),
                         "--cache-dir", str(tmp_path / "c")])
        assert code == 1
        assert "snippet in the docstring of harness.specgen" in capsys.readouterr().err

    def test_the_allowlist_is_the_pure_python_pytest_set(self):
        assert set(sel.ALLOWED_REPOS) == {
            "pytest-dev/pytest", "pylint-dev/pylint", "psf/requests", "pydata/xarray",
            "sphinx-doc/sphinx", "mwaskom/seaborn", "pallets/flask",
        }

