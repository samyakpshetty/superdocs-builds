"""Taking a photograph from an inspector's phone into a report.

Five things happen to every photo, in this order, and the order matters:

1. **Size is checked before the bytes are read.** A cap enforced after loading is not a cap.
2. **The image is decoded to prove it is an image.** The filename extension is a claim made
   by whoever uploaded the file, and this endpoint accepts uploads.
3. **EXIF is stripped.** This is the step that is specific to this domain and easy to miss:
   a phone photograph of a house carries the house's GPS coordinates. An inspection report
   is handed to buyers, agents and lenders, so shipping the client's home address inside
   the image metadata would be a real disclosure, silently.
4. **The content is hashed.** The sha256 of the cleaned bytes *is* the photo's identity, so
   the same photograph uploaded twice — a re-run, a retry, a crash halfway through — costs
   one upload, ever.
5. **A thumbnail is derived** for the interface, so the browser is never handed a 10 MB file
   to draw a 200-pixel card with.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from inspection_report.logging import get_logger

_log = get_logger("inspection_report.photos")

# Below the API's 10 MB decoded limit, and a deliberate choice: a report with forty photos
# at 8 MB each is a document nobody can email. Inspectors shoot at phone resolution.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

# What Pillow reports -> what the API accepts. A format outside this map is refused rather
# than guessed at.
_FORMAT_TO_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}

THUMBNAIL_MAX_EDGE = 480


class PhotoRejected(Exception):
    """The upload was refused. The message names the cause and what to do about it."""


@dataclass(frozen=True)
class CleanedPhoto:
    """A photograph that is safe to store and send."""

    data: bytes
    sha256: str
    content_type: str
    width: int
    height: int
    thumbnail: bytes
    stripped_exif: bool

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def clean(raw: bytes, *, filename: str) -> CleanedPhoto:
    """Validate, strip and fingerprint one uploaded photograph."""
    if not raw:
        raise PhotoRejected(f"{filename} is empty. Re-take or re-select the photograph.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise PhotoRejected(
            f"{filename} is {len(raw) // 1024} KB; the limit is "
            f"{MAX_UPLOAD_BYTES // 1024} KB. Reduce the camera resolution or crop the image."
        )

    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()  # structural check; consumes the file object
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            fmt = (image.format or "").upper()
            content_type = _FORMAT_TO_MIME.get(fmt)
            if content_type is None:
                raise PhotoRejected(
                    f"{filename} decoded as {fmt or 'an unknown format'}. Accepted formats "
                    f"are {', '.join(sorted(_FORMAT_TO_MIME))}."
                )
            had_exif = bool(getattr(image, "_getexif", lambda: None)()) or "exif" in image.info
            cleaned, width, height = _strip_metadata(image, fmt)
            thumb = _thumbnail(image, fmt)
    except PhotoRejected:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        # Deliberately does not echo the exception: a decoder message can carry file
        # content, and the cause the user can act on is simply that it is not an image.
        raise PhotoRejected(
            f"{filename} could not be read as an image. If it came from a scanner or a "
            f"messaging app, export it as PNG or JPEG and try again."
        ) from exc

    digest = hashlib.sha256(cleaned).hexdigest()
    _log.info(
        "photo_cleaned",
        extra={
            "sha256": digest[:12],
            "content_type": content_type,
            "bytes": len(cleaned),
            "stripped_exif": had_exif,
        },
    )
    return CleanedPhoto(
        data=cleaned,
        sha256=digest,
        content_type=content_type,
        width=width,
        height=height,
        thumbnail=thumb,
        stripped_exif=had_exif,
    )


def _strip_metadata(image: Image.Image, fmt: str) -> tuple[bytes, int, int]:
    """Re-encode through a bare canvas so no metadata survives.

    Copying pixel data into a fresh image is the reliable way to drop EXIF, ICC profiles and
    XMP together: Pillow's ``save`` will otherwise carry ``info`` through for several
    formats, and a per-key deletion list goes stale the moment a phone vendor invents a new
    one.
    """
    working = image.convert("RGBA") if fmt in ("PNG", "GIF", "WEBP") else image.convert("RGB")
    bare = Image.new(working.mode, working.size)
    bare.paste(working)  # pixels only; `info` is not carried across a paste

    buf = io.BytesIO()
    if fmt == "JPEG":
        bare.save(buf, format="JPEG", quality=88, optimize=True)
    else:
        bare.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), working.width, working.height


def _thumbnail(image: Image.Image, fmt: str) -> bytes:
    thumb = image.convert("RGB")
    thumb.thumbnail((THUMBNAIL_MAX_EDGE, THUMBNAIL_MAX_EDGE))
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=80, optimize=True)
    return buf.getvalue()


def thumbnail_mime() -> str:
    return "image/jpeg"
