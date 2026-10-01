"""An `InstanceSpec.spec` is something `verify` can actually consume.

`shared` cannot import `verify`, so the instance format stores `RepoSpec` fields
as a raw mapping and promises it is `spec_from_mapping`-compatible. Nothing in
`shared` can hold it to that, so the promise is tested from the side that can.
"""

import pytest

from repolace_shared.instances import InstanceSpec
from verify.errors import SpecError
from verify.protocol import RepoSpec
from verify.spec import spec_from_mapping


INSTANCE_ID = "psf__requests-2317"


def instance(spec: dict) -> InstanceSpec:
    return InstanceSpec(
        instance_id=INSTANCE_ID,
        repo="psf/requests",
        base_commit="091991be0da19de9108dbe5e3752917fea3d7fdc",
        version="2.4",
        problem_statement="p",
        issue_number=2317,
        fail_to_pass=("t::a",),
        pass_to_pass=(),
        test_files={"tests/test_a.py": "def test_a():\n    pass\n"},
        gold_files={},
        spec=spec,
        # Must agree with `spec.test_targets`, which is what makes it so.
        targeted_p2p=bool(spec.get("test_targets")),
    )


def test_the_raw_spec_builds_a_repo_spec():
    raw = {
        "base_image": "python:3.9-slim",
        "install": ["pip install -e ."],
        "system_packages": ["gcc"],
        "test_targets": ["tests"],
        "timeout_seconds": 900,
        "extra_env": {"A": "1"},
    }

    # The key is the instance id: it feeds the image tag, so one instance is one
    # environment. `plain_spec()` because `.spec` is frozen all the way down and
    # `spec_from_mapping` accepts only list and dict.
    inst = instance(raw)
    built = spec_from_mapping(inst.instance_id, inst.plain_spec())

    assert built.key == INSTANCE_ID
    assert built.base_image == "python:3.9-slim"
    assert built.install == ("pip install -e .",)
    assert built.test_targets == ("tests",)
    assert built.timeout_seconds == 900.0
    assert dict(built.extra_env) == {"A": "1"}


def test_the_frozen_spec_is_not_what_spec_from_mapping_takes():
    """The reason `plain_spec()` exists, pinned from the side that can see both:
    the frozen spec holds tuples and read-only mappings, and `_coerce` takes lists
    and dicts. If `verify` ever learns to accept the frozen shape this fails, and
    the module docstring's advice can be relaxed."""
    inst = instance({"install": ["pip install -e ."]})

    with pytest.raises(SpecError, match="list of strings"):
        spec_from_mapping(INSTANCE_ID, inst.spec)


def test_the_plain_spec_is_fresh_so_building_cannot_edit_the_instance():
    inst = instance({"install": ["a"], "extra_env": {"A": "1"}})

    inst.plain_spec()["install"].append("smuggled")

    assert spec_from_mapping(INSTANCE_ID, inst.plain_spec()).install == ("a",)


def test_the_key_is_not_part_of_the_stored_mapping():
    """`spec_from_mapping` takes the key separately and refuses it inside, so an
    instance that stored one would fail the moment the pipeline used it."""
    with pytest.raises(SpecError, match="key"):
        inst = instance({"key": "a/b"})
        spec_from_mapping("a/b", inst.plain_spec())


def test_an_empty_mapping_is_the_default_spec():
    inst = instance({})

    assert spec_from_mapping(inst.instance_id, inst.plain_spec()).base_image == RepoSpec(key="x").base_image
