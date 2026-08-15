"""The photo pipeline.

The EXIF test is the one that matters most here. A phone photograph of a house carries the
house's coordinates, and an inspection report goes to buyers, agents and lenders. So the
fixture is a real JPEG carrying real GPS tags, and the assertion is that they are gone.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from inspection_report.photos.pipeline import (
    MAX_UPLOAD_BYTES,
    PhotoRejected,
    clean,
)


def _jpeg_with_gps() -> bytes:
    """A genuine JPEG carrying EXIF: camera make, software, and GPS coordinates."""
    exif = Image.Exif()
    exif[0x010F] = "ACME Phone"  # Make
    exif[0x0131] = "InspectorCam 3.1"  # Software
    exif[0x8825] = {  # GPSInfo IFD — 51°30'30"N, 0°7'0"W
        1: "N",
        2: (IFDRational(51), IFDRational(30), IFDRational(30)),
        3: "W",
        4: (IFDRational(0), IFDRational(7), IFDRational(0)),
    }
    image = Image.new("RGB", (64, 48), (200, 90, 60))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def _png(size: tuple[int, int] = (40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (30, 120, 180)).save(buf, format="PNG")
    return buf.getvalue()


class TestExifIsStripped:
    def test_the_fixture_really_carries_gps(self) -> None:
        """Guard the guard: a test that strips nothing would pass silently."""
        with Image.open(io.BytesIO(_jpeg_with_gps())) as img:
            exif = img.getexif()
            assert exif, "fixture has no EXIF at all — the stripping test would prove nothing"
            assert exif.get_ifd(0x8825), "fixture carries no GPS block"

    def test_gps_and_camera_metadata_do_not_survive(self) -> None:
        cleaned = clean(_jpeg_with_gps(), filename="roof.jpg")
        with Image.open(io.BytesIO(cleaned.data)) as img:
            exif = img.getexif()
            assert not exif.get_ifd(0x8825), "the property's GPS coordinates survived"
            assert 0x010F not in exif
            assert 0x0131 not in exif

    def test_it_reports_that_it_stripped_something(self) -> None:
        assert clean(_jpeg_with_gps(), filename="roof.jpg").stripped_exif is True

    def test_a_photo_without_exif_is_not_flagged(self) -> None:
        assert clean(_png(), filename="clean.png").stripped_exif is False

    def test_the_picture_itself_survives(self) -> None:
        """Stripping metadata must not damage the evidence."""
        cleaned = clean(_jpeg_with_gps(), filename="roof.jpg")
        assert (cleaned.width, cleaned.height) == (64, 48)
        with Image.open(io.BytesIO(cleaned.data)) as img:
            assert img.size == (64, 48)


class TestIdentity:
    def test_the_same_bytes_give_the_same_fingerprint(self) -> None:
        """Identity is content, so a re-run never pays to upload a photo twice."""
        a = clean(_png(), filename="a.png")
        b = clean(_png(), filename="b-different-name.png")
        assert a.sha256 == b.sha256

    def test_different_pictures_differ(self) -> None:
        assert clean(_png((40, 30)), filename="a.png").sha256 != (
            clean(_png((41, 30)), filename="b.png").sha256
        )

    def test_two_identical_photos_stripped_separately_still_match(self) -> None:
        """Stripping must be deterministic, or dedupe silently stops working."""
        raw = _jpeg_with_gps()
        assert clean(raw, filename="x.jpg").sha256 == clean(raw, filename="y.jpg").sha256


class TestRejection:
    def test_an_empty_file_is_refused(self) -> None:
        with pytest.raises(PhotoRejected) as exc:
            clean(b"", filename="nothing.png")
        assert "empty" in str(exc.value)

    def test_an_oversized_file_is_refused_before_decoding(self) -> None:
        with pytest.raises(PhotoRejected) as exc:
            clean(b"\x00" * (MAX_UPLOAD_BYTES + 1), filename="huge.png")
        assert "limit" in str(exc.value)

    def test_a_file_that_is_not_an_image_is_refused_whatever_it_is_called(self) -> None:
        """The extension is a claim by whoever uploaded the file."""
        with pytest.raises(PhotoRejected) as exc:
            clean(b"%PDF-1.7\nnot an image at all", filename="roof.png")
        assert "could not be read as an image" in str(exc.value)

    def test_the_rejection_does_not_echo_file_content(self) -> None:
        """A decoder message can carry bytes from the file; the user needs the cause."""
        secret = b"%PDF-1.7 CONFIDENTIAL-CLIENT-REFERENCE-99311"
        with pytest.raises(PhotoRejected) as exc:
            clean(secret, filename="scan.png")
        assert "CONFIDENTIAL" not in str(exc.value)

    def test_an_html_payload_is_not_accepted_as_an_image(self) -> None:
        with pytest.raises(PhotoRejected):
            clean(b"<svg onload=alert(1)><script>x</script></svg>", filename="x.png")


class TestThumbnails:
    def test_a_thumbnail_is_produced_and_is_smaller(self) -> None:
        big = io.BytesIO()
        Image.new("RGB", (2400, 1800), (10, 10, 10)).save(big, format="JPEG", quality=95)
        cleaned = clean(big.getvalue(), filename="wide.jpg")
        assert len(cleaned.thumbnail) < cleaned.size_bytes
        with Image.open(io.BytesIO(cleaned.thumbnail)) as thumb:
            assert max(thumb.size) <= 480

    def test_the_thumbnail_carries_no_metadata_either(self) -> None:
        cleaned = clean(_jpeg_with_gps(), filename="roof.jpg")
        with Image.open(io.BytesIO(cleaned.thumbnail)) as thumb:
            assert not thumb.getexif().get_ifd(0x8825)
