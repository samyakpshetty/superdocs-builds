"""The whole path: inspection in, verified export out.

This is the card's own bar — "a sample report covering at least four inspection systems, each
with a finding, a severity label, and a photo, and confirm the exported report groups
everything correctly by system" — asserted rather than demonstrated. It runs offline against
the deterministic fake, so anyone can check the claim without a key or a cost.
"""

from __future__ import annotations

import pytest

from inspection_report import sample
from inspection_report.domain import catalogue
from inspection_report.render import pipeline, report
from inspection_report.superdocs.fake import FakeSuperDocsClient
from inspection_report.verify import exports as verify_exports

TEMPLATE = "templates/buyer_summary.html"


@pytest.fixture
def built() -> tuple[pipeline.PipelineResult, object]:
    from pathlib import Path

    inspection = sample.sample_inspection()
    result = pipeline.build(
        inspection,
        Path(TEMPLATE).read_text(),
        sample.sample_photo_data(),
        FakeSuperDocsClient(),
        session_id="test-session",
    )
    return result, inspection


class TestTheCardsBar:
    def test_the_sample_covers_at_least_four_systems_with_finding_severity_and_photo(self) -> None:
        inspection = sample.sample_inspection()
        covered = {f.system_key for f in inspection.findings}
        assert len(covered) >= 4
        for finding in inspection.findings:
            assert finding.severity_key, "every finding carries a severity label"
            assert finding.photos, "every finding carries photo evidence"
            assert finding.observation.strip()

    @pytest.mark.parametrize("fmt", ["pdf", "docx"])
    def test_the_exported_report_groups_everything_by_system(
        self, built: tuple[pipeline.PipelineResult, object], fmt: str
    ) -> None:
        result, inspection = built
        report_card = verify_exports.verify(
            data=result.exports[fmt].content,
            fmt=fmt,
            inspection=inspection,  # type: ignore[arg-type]
        )
        assert report_card.passed, report_card.render()

    @pytest.mark.parametrize("fmt", ["pdf", "docx"])
    def test_every_photograph_is_in_the_exported_file(
        self, built: tuple[pipeline.PipelineResult, object], fmt: str
    ) -> None:
        result, inspection = built
        expected = inspection.photo_count()  # type: ignore[attr-defined]
        data = result.exports[fmt].content
        count = (
            len(verify_exports.images_in_docx(data))
            if fmt == "docx"
            else verify_exports.images_in_pdf(data)
        )
        assert count == expected


class TestTheRailHoldsAllTheWayToTheFile:
    def test_certifying_rewrites_are_refused(
        self, built: tuple[pipeline.PipelineResult, object]
    ) -> None:
        result, _ = built
        assert result.rail_refusals >= 1, "the sample is chosen to provoke refusals"
        assert any(p.approved for p in result.proposals), "well-behaved rewrites still land"

    def test_a_refused_rewrite_leaves_the_inspectors_own_words_in_the_report(
        self, built: tuple[pipeline.PipelineResult, object]
    ) -> None:
        result, inspection = built
        refused = [p for p in result.proposals if p.refused_by_rail]
        assert refused
        text = verify_exports.text_of_docx(result.exports["docx"].content)
        for proposal in refused:
            bad = pipeline._text_of(proposal.diff.new_html)
            assert bad not in text, "a refused rewrite reached the exported document"
        # And the original survives: the GFCI note is the one the fake certifies about.
        gfci = next(f for f in inspection.findings if "GFCI" in f.observation)  # type: ignore[attr-defined]
        assert gfci.observation in text

    def test_no_photo_url_travels_inside_the_document(
        self, built: tuple[pipeline.PipelineResult, object]
    ) -> None:
        result, _ = built
        text = verify_exports.text_of_docx(result.exports["docx"].content)
        assert "superdocs-document-images" not in text


class TestDeterminism:
    def test_rendering_twice_gives_identical_bytes(self) -> None:
        """Structure comes from code, so the same inspection always renders the same."""
        from pathlib import Path

        template = Path(TEMPLATE).read_text()
        a = report.render(sample.sample_inspection(), template)
        b = report.render(sample.sample_inspection(), template)
        assert a == b

    def test_findings_are_ordered_most_urgent_first_within_a_system(self) -> None:
        """The Electrical section holds a safety_concern and a monitor, in that order."""
        from pathlib import Path

        inspection = sample.sample_inspection()
        html = report.render(inspection, Path(TEMPLATE).read_text())
        # Anchor on the heading, not the name: every system is also named in the
        # "what was inspected" sentence near the top of the report.
        start = html.index("<h2>Electrical</h2>")
        end = html.index("<h2>", start + 1)
        section = html[start:end]
        prompt_at = section.find("Recommend prompt evaluation")
        monitor_at = section.find("Monitor:")
        assert prompt_at >= 0, section[:400]
        assert monitor_at >= 0, section[:400]
        assert prompt_at < monitor_at


class TestIdempotentPhotoUploads:
    def test_the_same_photograph_is_never_uploaded_twice(self) -> None:
        """Uploads cost money, so identity is content and a re-run re-spends nothing."""
        inspection = sample.sample_inspection()
        client = FakeSuperDocsClient()
        photo_data = sample.sample_photo_data()

        first_up, first_reused = pipeline.upload_photos(inspection, photo_data, client)
        assert first_up == inspection.photo_count()
        assert first_reused == 0

        # A crash and re-run: the same photographs, already known by content hash.
        known = {p.sha256: p.remote_url for f in inspection.findings for p in f.photos}
        again = sample.sample_inspection()
        second_up, second_reused = pipeline.upload_photos(again, photo_data, client, known=known)
        assert second_up == 0, "a re-run paid to upload photographs again"
        assert second_reused == again.photo_count()


def test_a_system_with_no_findings_says_so_rather_than_going_missing() -> None:
    """Silence about a system reads as "not inspected". It has to say what it found."""
    from pathlib import Path

    inspection = sample.sample_inspection()
    inspection.findings = [f for f in inspection.findings if f.system_key == "roof"]
    html = report.render(inspection, Path(TEMPLATE).read_text())
    for system in catalogue.systems():
        assert system.name in html
    assert report.NOTHING_OBSERVED in html
