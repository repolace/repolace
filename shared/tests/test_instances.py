"""The instance file format: round trip, and everything it must refuse.

The rejections are the substance. A benchmark instance is trusted operator data,
but its path keys become paths on the host filesystem, and an instance that
loads when it should not -- an empty `fail_to_pass`, a `..` in an overlay key --
is either a pass nobody could defend or an arbitrary file write. Each rule has a
test that fails if the rule is removed.
"""

import json
from pathlib import Path

import pytest

from repolace_shared.instances import (
    SCHEMA_VERSION,
    InstanceError,
    InstanceSpec,
    dump_instance,
    load_instance,
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
        spec = make_instance(targeted_p2p=True)

        dump_instance(spec, file)

        assert load_instance(file) == spec

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

        dump_instance(a, tmp_path / "a.json")
        dump_instance(b, tmp_path / "b.json")

        assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()

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
        dump_instance(make_instance(), tmp_path / "renamed.json")

        with pytest.raises(InstanceError, match="psf__requests-2317.json"):
            load_instances(tmp_path)

    def test_two_files_claiming_one_id_are_rejected(self, tmp_path):
        """No separate duplicate check is needed: the second file's stem cannot
        match, so the stem rule is what refuses it."""
        spec = make_instance()
        dump_instance(spec, tmp_path / "psf__requests-2317.json")
        dump_instance(spec, tmp_path / "psf__requests-2317-copy.json")

        with pytest.raises(InstanceError, match="psf__requests-2317.json"):
            load_instances(tmp_path)

    def test_a_json_file_that_is_not_an_instance_fails_the_load(self, tmp_path):
        """Skipped silently, an instance that failed to parse would vanish from the set."""
        dump_instance(make_instance(), tmp_path / "psf__requests-2317.json")
        (tmp_path / "notes.json").write_text('{"hello": "world"}')

        with pytest.raises(InstanceError, match="notes.json"):
            load_instances(tmp_path)
