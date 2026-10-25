"""Shared builders for the `--stage-model` tests (flags, manifest, runner, report)."""

from datetime import UTC, datetime
from pathlib import Path

from harness.enqueue import ManifestInputs, build_manifest

RUN = "mixed-run"
CREATED = datetime(2026, 10, 3, tzinfo=UTC)
EDITOR = "vertex-gemini-3-8-flash"
STRONG = "vertex-gemini-3-1-pro-preview"


def build(tmp_path: Path, **inputs):
    return build_manifest(
        eval_run_id=RUN, git_sha="a" * 40, instance_ids=["psf__requests-1001"], runs=1,
        inputs=ManifestInputs(runs_dir=tmp_path, model=EDITOR, **inputs), created_at=CREATED,
    )
