"""An `InstanceSpec.spec` is something `verify` can actually consume.

`shared` cannot import `verify`, so the instance format stores `RepoSpec` fields
as a raw mapping and promises it is `spec_from_mapping`-compatible. Nothing in
`shared` can hold it to that, so the promise is tested from the side that can.
"""

import pytest

from repolace_shared.instances import InstanceSpec
from verify.errors import SpecError
from verify.spec import spec_from_mapping


def instance(spec: dict) -> InstanceSpec:
    return InstanceSpec(
        instance_id="psf__requests-2317",
        repo="psf/requests",
        base_commit="091991be0da19de9108dbe5e3752917fea3d7fdc",
        version="2.4",
        problem_statement="p",
        issue_number=2317,
        fail_to_pass=("t::a",),
        pass_to_pass=(),
        test_files={},
        gold_files={},
        spec=spec,
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

    built = spec_from_mapping("repolace/bench-psf__requests-2317", instance(raw).spec)

    assert built.key == "repolace/bench-psf__requests-2317"
    assert built.base_image == "python:3.9-slim"
    assert built.install == ("pip install -e .",)
    assert built.timeout_seconds == 900.0
    assert dict(built.extra_env) == {"A": "1"}


def test_the_key_is_not_part_of_the_stored_mapping():
    """`spec_from_mapping` takes the key separately and refuses it inside, so an
    instance that stored one would fail the moment the pipeline used it."""
    with pytest.raises(SpecError, match="key"):
        spec_from_mapping("a/b", instance({"key": "a/b"}).spec)


def test_an_empty_mapping_is_the_default_spec():
    assert spec_from_mapping("a/b", instance({}).spec).base_image == "python:3.12-slim"
