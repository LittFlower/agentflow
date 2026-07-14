import pytest

from agentflow.defaults import (
    bundled_template_names,
    bundled_template_path,
    bundled_template_support_files,
    bundled_templates,
    default_smoke_pipeline_path,
    load_bundled_template,
    render_bundled_template,
)
from agentflow.loader import load_pipeline_from_path


def test_bundled_templates_expose_current_descriptions_and_example_files():
    templates = bundled_templates()

    assert tuple(template.name for template in templates) == bundled_template_names()
    assert bundled_template_names() == (
        "pipeline",
        "codex-repo-sweep-batched",
    )

    by_name = {template.name: template for template in templates}
    assert by_name["pipeline"].example_name == "airflow_like.py"
    assert by_name["pipeline"].parameters == ()
    assert by_name["codex-repo-sweep-batched"].example_name == "airflow_like_fuzz_batched.py"
    assert "fanout" in by_name["codex-repo-sweep-batched"].description


def test_pipeline_template_matches_default_example_file_and_rejects_settings():
    expected = bundled_template_path("pipeline").read_text(encoding="utf-8")

    assert load_bundled_template("pipeline") == expected

    with pytest.raises(ValueError, match=r"template `pipeline` does not accept `--set` values"):
        render_bundled_template("pipeline", values={"name": "custom"})


def test_bundled_template_helpers_reject_unknown_template_names():
    with pytest.raises(ValueError, match=r"unknown bundled template `missing-template`"):
        bundled_template_path("missing-template")

    with pytest.raises(ValueError, match=r"unknown bundled template `missing-template`"):
        bundled_template_support_files("missing-template")

    with pytest.raises(ValueError, match=r"unknown bundled template `missing-template`"):
        render_bundled_template("missing-template")


def test_default_smoke_pipeline_path_points_to_airflow_like():
    assert default_smoke_pipeline_path() == str(bundled_template_path("pipeline"))


def test_bundled_codex_repo_sweep_batched_template_supports_overrides(tmp_path):
    rendered_default = load_bundled_template("codex-repo-sweep-batched")

    assert rendered_default.startswith("# Configurable large-scale Codex repository sweep\n")
    assert "codex-repo-sweep-batched-128" in rendered_default
    assert "codex_repo_sweep_batched_128" in rendered_default
    assert "fanout_count(\n            128," in rendered_default or "fanout_count(128," in rendered_default or "128," in rendered_default
    assert bundled_template_support_files("codex-repo-sweep-batched") == ()

    rendered = load_bundled_template(
        "codex-repo-sweep-batched",
        values={
            "shards": "64",
            "batch_size": "8",
            "concurrency": "20",
            "focus": "security bugs, privilege boundaries, and missing coverage",
            "name": "custom-repo-sweep-64",
            "working_dir": "./custom_repo_sweep",
        },
    )

    assert '"custom-repo-sweep-64"' in rendered
    assert '"./custom_repo_sweep"' in rendered
    assert "concurrency=20" in rendered
    assert "Focus on security bugs, privilege boundaries, and missing coverage." in rendered
    assert "node_defaults" in rendered
    assert "agent_defaults" in rendered
    assert "item.scope.ids" in rendered
    assert "fanouts.batch_merge.with_output.nodes" in rendered

    pipeline_path = tmp_path / "custom-repo-sweep.py"
    pipeline_path.write_text(rendered, encoding="utf-8")
    pipeline = load_pipeline_from_path(str(pipeline_path))

    assert pipeline.concurrency == 20
    assert pipeline.fanouts["sweep"][:3] == ["sweep_00", "sweep_01", "sweep_02"]
    assert pipeline.fanouts["sweep"][-1] == "sweep_63"
    assert len(pipeline.fanouts["sweep"]) == 64
    assert pipeline.node_map["prepare"].agent == "codex"
    assert pipeline.node_map["prepare"].model == "gpt-5.5"
    assert pipeline.node_map["prepare"].tools == "read_only"
    assert pipeline.node_map["sweep_00"].fanout_member["label"] == "slice 1/64"
    assert pipeline.node_map["sweep_00"].extra_args == ["--search"]
    assert pipeline.fanouts["batch_merge"] == [
        "batch_merge_0",
        "batch_merge_1",
        "batch_merge_2",
        "batch_merge_3",
        "batch_merge_4",
        "batch_merge_5",
        "batch_merge_6",
        "batch_merge_7",
    ]
    assert pipeline.node_map["batch_merge_0"].fanout_member["member_ids"] == [
        "sweep_00",
        "sweep_01",
        "sweep_02",
        "sweep_03",
        "sweep_04",
        "sweep_05",
        "sweep_06",
        "sweep_07",
    ]
    assert pipeline.node_map["batch_merge_7"].fanout_member["member_ids"] == [
        "sweep_56",
        "sweep_57",
        "sweep_58",
        "sweep_59",
        "sweep_60",
        "sweep_61",
        "sweep_62",
        "sweep_63",
    ]
    assert pipeline.node_map["merge"].depends_on == pipeline.fanouts["batch_merge"]
