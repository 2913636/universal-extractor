"""Closed-loop integration tests that do not depend on public network uptime."""

from UniversalExtractor.pipeline import (
    ExtractionStage,
    Pipeline,
    PipelineConfig,
    PipelineStageResult,
    StageContext,
)


class _BestEffortStage(ExtractionStage):
    stage_name = "best_effort"
    stage_index = 0

    def extract(self, url: str, context: StageContext) -> PipelineStageResult:
        text = "useful local content " * 20
        return PipelineStageResult(
            stage_name=self.stage_name,
            stage_index=self.stage_index,
            text=text,
            completeness=0.1,
        )


def test_direct_url_bypasses_quick_scan(monkeypatch) -> None:
    pipeline = Pipeline(PipelineConfig(enabled_stages=["best_effort"]))
    pipeline.registry.register(_BestEffortStage())
    monkeypatch.setattr(
        pipeline,
        "_phase_verify",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not scan")),
    )

    result = pipeline.run(url="https://example.com/page")

    assert result.url == "https://example.com/page"
    assert result.text
    assert result.validation_details["result"] == "best_effort"


def test_validation_reports_content_consistency() -> None:
    pipeline = Pipeline(PipelineConfig(require_keyword=False, min_completeness=0.0))
    baseline = "alpha beta gamma " * 100
    passes, details = pipeline._validate(
        baseline + "tail",
        baseline_text=baseline,
    )

    assert passes
    assert details["content_consistency"] > 0.9
