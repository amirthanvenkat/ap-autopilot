"""Accepted media types and content sniffing.

Spec 01 section 4 lists the accepted types. The declared type is checked
against the leading bytes, because an upload can declare anything and a
Document AI call on a file that is not what it claims is a paid failure.
"""

from __future__ import annotations

from src.common.errors import UnsupportedMediaTypeError

PDF = "application/pdf"
PNG = "image/png"
JPEG = "image/jpeg"
TIFF = "image/tiff"

ACCEPTED_MEDIA_TYPES = frozenset({PDF, PNG, JPEG, TIFF})

_EXTENSIONS = {
    PDF: ".pdf",
    PNG: ".png",
    JPEG: ".jpg",
    TIFF: ".tif",
}

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", PDF),
    (b"\x89PNG\r\n\x1a\n", PNG),
    (b"\xff\xd8\xff", JPEG),
    (b"II*\x00", TIFF),
    (b"MM\x00*", TIFF),
)


def sniff(data: bytes) -> str | None:
    """Identify the content from its leading bytes, or None if unknown."""
    for prefix, media_type in _MAGIC:
        if data.startswith(prefix):
            return media_type
    return None


def normalise_media_type(declared: str | None) -> str | None:
    """Strip parameters and lower case a declared type."""
    if not declared:
        return None
    return declared.split(";", 1)[0].strip().lower()


def extension_for(media_type: str) -> str:
    """File extension for an accepted type."""
    return _EXTENSIONS.get(media_type, ".bin")


def require_accepted(declared: str | None, data: bytes) -> str:
    """Return the media type to record, or raise.

    The sniffed type wins whenever the two disagree, because the bytes are
    the thing Document AI will read. A declared type that cannot be
    confirmed from the content is refused.
    """
    normalised = normalise_media_type(declared)
    sniffed = sniff(data)

    # The bytes decide. The magic list covers every accepted type, so
    # content that cannot be identified is not one of them, whatever the
    # upload claims. Sending an unidentifiable file to Document AI is a
    # failure that costs money.
    if sniffed is not None and sniffed in ACCEPTED_MEDIA_TYPES:
        return sniffed
    raise UnsupportedMediaTypeError(
        "Unsupported media type",
        detail=(
            f"declared={normalised or 'none'} sniffed={sniffed or 'unknown'}; "
            f"accepted: {', '.join(sorted(ACCEPTED_MEDIA_TYPES))}"
        ),
    )


def is_accepted(declared: str | None) -> bool:
    """Cheap check for a declared type, used when the bytes are not to hand."""
    return normalise_media_type(declared) in ACCEPTED_MEDIA_TYPES
