from __future__ import annotations

from pathlib import Path


from bibit_intent import EmbeddedBibitPipeline, PipelineSettings


def test_packaged_pipeline_builds_question_detector_index(tmp_path: Path) -> None:
    pipeline = EmbeddedBibitPipeline.from_package(
        data_root=tmp_path,
        settings=PipelineSettings(local_timeout_seconds=1),
    )

    assert pipeline.question_embedding_cache_path == (
        tmp_path / "question-embedding-index.json"
    )


def test_web_search_visual_report_escalates_to_cloud(tmp_path: Path) -> None:
    pipeline = EmbeddedBibitPipeline.from_package(
        data_root=tmp_path,
        settings=PipelineSettings(local_timeout_seconds=1),
    )

    outcome = pipeline.handle("搜索AI资讯并生成可视化网页报告", "report-session")

    assert not outcome.handled
    assert outcome.reply == ""
    assert outcome.route == "cloud"
    assert outcome.reason == "writing_task"
