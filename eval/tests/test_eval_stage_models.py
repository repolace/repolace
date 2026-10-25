"""`--stage-model`: parsed strictly, recorded in the manifest, checked by the runner, sent to the child.

A sweep with the editor on one model and the planner on another is a different configuration from
a single-model sweep, so the manifest must carry the whole map and the runner must refuse to run a
manifest under a map it was not given.
"""

import json

import pytest
from eval_stage_models_support import EDITOR, RUN, STRONG, build
from harness import enqueue, runner
from harness.enqueue import ensure_manifest
from harness.run_manifest import ManifestError, dump_manifest, load_manifest, parse_stage_models
from harness.runner import RunnerConfig, check_manifest, child_env


class TestParsing:
    def test_flags_become_a_map_sorted_by_stage(self):
        assert list(parse_stage_models([f"review={STRONG}", f"debug={STRONG}", "plan=x"])) == ["debug", "plan", "review"]

    def test_no_flags_is_an_empty_map(self):
        assert parse_stage_models([]) == {} and parse_stage_models(None) == {}

    @pytest.mark.parametrize("value", ["plan", "plan=", "=model", "", " = "])
    def test_a_malformed_value_is_refused(self, value):
        with pytest.raises(ManifestError, match="is not STAGE=MODEL_KEY"):
            parse_stage_models([value])

    def test_the_agent_stage_belongs_to_model(self):
        with pytest.raises(ManifestError, match="use --model"):
            parse_stage_models(["agent=x"])

    def test_an_unknown_stage_is_refused(self):
        with pytest.raises(ManifestError, match="'edit' is not one of plan, review, debug, resolve"):
            parse_stage_models(["edit=x"])

    def test_a_stage_named_twice_is_refused_even_with_the_same_model(self):
        with pytest.raises(ManifestError, match="'plan' twice"):
            parse_stage_models(["plan=x", "plan=x"])

    def test_both_parsers_take_the_flag_repeatedly(self):
        for module in (enqueue, runner):
            args = module.build_parser().parse_args(["--eval-run-id", RUN, "--stage-model", "plan=a", "--stage-model", "debug=b"])
            assert args.stage_model == ["plan=a", "debug=b"]

    def test_enqueue_exits_2_on_a_bad_flag_before_reading_any_file(self, tmp_path, capsys):
        code = enqueue.main(
            ["--eval-run-id", RUN, "--model", EDITOR, "--stage-model", "agent=x", "--instances-dir", str(tmp_path / "none")]
        )

        assert code == 2 and "use --model" in capsys.readouterr().err

    def test_enqueue_refuses_the_flag_for_gold(self, tmp_path, capsys):
        code = enqueue.main(
            ["--eval-run-id", RUN, "--agent", "gold", "--stage-model", "plan=x", "--instances-dir", str(tmp_path / "none")]
        )

        assert code == 2 and "--stage-model needs --agent llm" in capsys.readouterr().err

    def test_run_exits_2_on_a_bad_flag(self, capsys):
        assert runner.main(["--eval-run-id", RUN, "--model", EDITOR, "--stage-model", "bogus=x"]) == 2
        assert "'bogus' is not one of" in capsys.readouterr().err

    def test_run_refuses_extras_without_a_model(self, capsys):
        assert runner.main(["--eval-run-id", RUN, "--stage-model", "plan=x"]) == 2
        assert "--stage-model needs --agent llm and --model" in capsys.readouterr().err


class TestTheManifest:
    def test_it_records_the_whole_map_sorted(self, tmp_path):
        manifest = build(tmp_path, stage_models={"review": STRONG, "plan": STRONG})

        assert manifest.stage_models == {"agent": EDITOR, "plan": STRONG, "review": STRONG}
        assert list(manifest.stage_models) == ["agent", "plan", "review"]

    def test_it_round_trips_through_the_file(self, tmp_path):
        manifest = build(tmp_path, stage_models={"debug": STRONG})
        path = dump_manifest(manifest, tmp_path)

        assert json.loads(path.read_text())["stage_models"] == {"agent": EDITOR, "debug": STRONG}
        assert load_manifest(path) == manifest

    def test_without_the_flag_the_map_is_just_the_agent(self, tmp_path):
        assert build(tmp_path).stage_models == {"agent": EDITOR}

    def test_a_mixed_sweep_under_a_single_model_run_id_is_a_different_sweep(self, tmp_path):
        ensure_manifest(tmp_path, build(tmp_path))

        with pytest.raises(ManifestError, match="differs in: stage_models"):
            ensure_manifest(tmp_path, build(tmp_path, stage_models={"plan": STRONG}))

    def test_a_map_for_the_gold_agent_is_refused(self, tmp_path):
        with pytest.raises(ManifestError, match="--stage-model needs --agent llm"):
            build(tmp_path, agent="gold", stage_models={"plan": STRONG})

    def test_the_inputs_are_validated_like_the_flags(self, tmp_path):
        with pytest.raises(ManifestError, match="not one of"):
            build(tmp_path, stage_models={"nope": STRONG})


class TestTheRunner:
    def config(self, **overrides) -> RunnerConfig:
        return RunnerConfig(eval_run_id=RUN, model=EDITOR, timeout_seconds=60.0, **overrides)

    def enqueue_manifest(self, tmp_path, **inputs):
        ensure_manifest(tmp_path, build(tmp_path, timeout_seconds=60.0, **inputs))

    def test_the_child_gets_the_whole_map(self):
        env = child_env({"A": "1"}, EDITOR, {"plan": STRONG, "review": STRONG})

        assert json.loads(env["GATEWAY_STAGE_MODELS"]) == {"agent": EDITOR, "plan": STRONG, "review": STRONG}

    def test_without_extras_the_child_gets_only_the_agent(self):
        assert json.loads(child_env({}, EDITOR)["GATEWAY_STAGE_MODELS"]) == {"agent": EDITOR}

    def test_extras_without_a_model_send_nothing(self):
        assert "GATEWAY_STAGE_MODELS" not in child_env({}, None, {"plan": STRONG})

    def test_an_agreeing_mixed_run_passes(self, tmp_path):
        self.enqueue_manifest(tmp_path, stage_models={"plan": STRONG})

        assert check_manifest(tmp_path, self.config(stage_models={"plan": STRONG})) is True

    def test_a_runner_given_no_map_for_a_mixed_manifest_is_refused(self, tmp_path):
        self.enqueue_manifest(tmp_path, stage_models={"plan": STRONG})

        with pytest.raises(ManifestError, match="stage models: the manifest says .*plan.*this run was given \\{\\}"):
            check_manifest(tmp_path, self.config())

    def test_a_runner_given_a_map_for_a_single_model_manifest_is_refused(self, tmp_path):
        self.enqueue_manifest(tmp_path)

        with pytest.raises(ManifestError, match="stage models: the manifest says \\{\\}"):
            check_manifest(tmp_path, self.config(stage_models={"plan": STRONG}))

    def test_a_different_model_for_the_same_stage_is_refused(self, tmp_path):
        self.enqueue_manifest(tmp_path, stage_models={"plan": STRONG})

        with pytest.raises(ManifestError, match="stage models"):
            check_manifest(tmp_path, self.config(stage_models={"plan": EDITOR}))
