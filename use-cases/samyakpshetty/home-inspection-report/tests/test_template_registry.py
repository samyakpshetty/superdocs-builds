"""Report formats are registered with SuperDocs and the report is built from what it returns.

The point of these tests is to make "we use the templates surface" falsifiable. The strongest
one is the last: delete the format from the account and the round trip stops working. A
surface that is really load-bearing breaks when you take it away.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inspection_report import sample
from inspection_report.render import report as render_report
from inspection_report.superdocs.fake import FakeSuperDocsClient
from inspection_report.templates import registry

TEMPLATE_DIR = Path("templates")


@pytest.fixture
def client() -> FakeSuperDocsClient:
    return FakeSuperDocsClient()


class TestRegistration:
    def test_every_shipped_format_is_registered(self, client: FakeSuperDocsClient) -> None:
        formats = registry.ensure_registered(client, TEMPLATE_DIR)
        assert set(formats) == {p.stem for p in TEMPLATE_DIR.glob("*.html")}
        assert len(client.list_templates()) == len(formats)

    def test_registering_twice_uploads_nothing_new(self, client: FakeSuperDocsClient) -> None:
        """Idempotent: a restart must not fill the account with duplicate formats."""
        registry.ensure_registered(client, TEMPLATE_DIR)
        before = {t.id for t in client.list_templates()}
        registry.ensure_registered(client, TEMPLATE_DIR)
        assert {t.id for t in client.list_templates()} == before

    def test_an_edited_format_registers_as_a_new_version(
        self, client: FakeSuperDocsClient, tmp_path: Path
    ) -> None:
        """A changed format must not redefine the one existing reports were built from."""
        d = tmp_path / "templates"
        d.mkdir()
        target = d / "house.html"
        target.write_text(_minimal_format("v1"))
        first = registry.ensure_registered(client, d)["house"]

        target.write_text(_minimal_format("v2"))
        second = registry.ensure_registered(client, d)["house"]

        assert first.content_sha != second.content_sha
        assert first.name != second.name
        assert len(client.list_templates()) == 2, "the earlier version must still exist"


class TestTheRoundTripIsRefusedForANamedReason:
    """SuperDocs strips HTML comments, and this build's region markers are HTML comments.

    Verified against the live API: four comments sent, none returned, while `class` and
    `{{placeholder}}` both survive. So a template registered today comes back without the
    markers the binding engine needs, and the round trip cannot complete.

    That is recorded here rather than hidden, and the code refuses the returned document
    instead of building a report on a skeleton it cannot group. Making the round trip work
    means moving the region markers onto `class` attributes, which survive — a change to the
    binding engine and the three shipped formats, noted in PROGRESS.
    """

    def test_comments_do_not_survive_a_round_trip(self, client: FakeSuperDocsClient) -> None:
        formats = registry.ensure_registered(client, TEMPLATE_DIR)
        html, from_service = registry.materialise(client, formats["buyer_summary"], session_id="s1")
        assert "<!-- region:system -->" not in html or from_service is False

    def test_the_document_is_refused_rather_than_built_on(
        self, client: FakeSuperDocsClient
    ) -> None:
        """The failure mode that matters: never silently produce an ungrouped report."""
        formats = registry.ensure_registered(client, TEMPLATE_DIR)
        _, from_service = registry.materialise(client, formats["buyer_summary"], session_id="s1")
        assert from_service is False, (
            "the round trip claimed success even though the region markers cannot survive it"
        )

    def test_the_caller_is_told_which_path_produced_the_document(
        self, client: FakeSuperDocsClient
    ) -> None:
        """A build that fell back must say so; that flag is what keeps the README honest."""
        formats = registry.ensure_registered(client, TEMPLATE_DIR)
        html, from_service = registry.materialise(client, formats["buyer_summary"], session_id="s1")
        assert from_service is False
        assert html == formats["buyer_summary"].local_html

    def test_a_report_renders_from_the_returned_skeleton(self, client: FakeSuperDocsClient) -> None:
        formats = registry.ensure_registered(client, TEMPLATE_DIR)
        html, _ = registry.materialise(client, formats["buyer_summary"], session_id="s1")
        report_html = render_report.render(sample.sample_inspection(), html)
        assert "14 Alder Lane" in report_html
        assert "Recommend prompt evaluation" in report_html


class TestDegradation:
    def test_an_unreachable_service_still_produces_a_report(
        self, client: FakeSuperDocsClient
    ) -> None:
        """An inspector who walked a property gets their report out even if SuperDocs is down.

        What must never happen is claiming the round trip occurred when it did not — hence
        the flag, which the caller reports rather than swallowing.
        """
        formats = registry.ensure_registered(client, TEMPLATE_DIR)
        fmt = formats["buyer_summary"]
        client.templates.clear()
        client.template_bytes.clear()

        html, from_service = registry.materialise(client, fmt, session_id="s1")
        assert from_service is False
        assert "<!-- region:system -->" in html, "the local copy still renders a report"

    def test_a_document_that_is_not_the_format_is_refused(
        self, client: FakeSuperDocsClient, tmp_path: Path
    ) -> None:
        """A skeleton without the regions cannot group a report, so it is not accepted."""
        d = tmp_path / "t"
        d.mkdir()
        (d / "broken.html").write_text("<h1>Not a report format at all</h1>")
        fmt = registry.ensure_registered(client, d)["broken"]
        _, from_service = registry.materialise(client, fmt, session_id="s1")
        assert from_service is False, "an unusable skeleton was accepted as the format"


def _minimal_format(marker: str) -> str:
    return (
        f"<h1>{{{{firm_name}}}} {marker}</h1>"
        "<!-- region:legend --><p>{{severity_label}} {{severity_description}}</p>"
        "<!-- /region:legend -->"
        "<!-- region:system --><h2>{{system_name}}</h2><p>{{system_blurb}}</p>"
        "<p>{{system_summary}}</p>"
        "<!-- region:finding --><h3>{{severity_label}} {{finding_title}}</h3>"
        '<p class="finding-note">{{finding_text}}</p><p>{{finding_recommendation}}</p>'
        "<!-- /region:finding -->"
        "<!-- /region:system -->"
        "<p>{{property_address}} {{inspector_name}}{{licence_suffix}} {{inspected_on}} "
        "{{preamble}} {{limits}} {{systems_inspected}}</p>"
    )
