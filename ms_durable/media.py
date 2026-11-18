"""Bounded chat images. SVG originals are displayed only in image context."""

import base64
import binascii
import hashlib
from xml.etree import ElementTree

from pydantic import BaseModel, Field

MAX_IMAGE = 1024 * 1024
MAX_IMAGES = 3
MAX_BODY = 5 * 1024 * 1024
RASTER = {
    "image/png": lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda b: b.startswith(b"\xff\xd8\xff"),
    "image/gif": lambda b: b.startswith((b"GIF87a", b"GIF89a")),
    "image/webp": lambda b: b.startswith(b"RIFF") and b[8:12] == b"WEBP",
}


class ImageInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    mime: str
    data: str = Field(max_length=((MAX_IMAGE + 2) // 3) * 4)

    def decode(self):
        try:
            data = base64.b64decode(self.data, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Invalid image data") from None
        if not data or len(data) > MAX_IMAGE:
            raise ValueError("Each image must be at most 1 MiB")
        if self.mime == "image/svg+xml":
            try:
                svg = data.decode("utf-8-sig")
                if "<!DOCTYPE" in svg or "<!ENTITY" in svg:
                    raise ValueError("SVG document type declarations and entities are not accepted")
                if ElementTree.fromstring(svg).tag != "{http://www.w3.org/2000/svg}svg":
                    raise ValueError("Expected an SVG document")
            except (UnicodeError, ElementTree.ParseError):
                raise ValueError("Invalid UTF-8 SVG") from None
        elif self.mime not in RASTER or not RASTER[self.mime](data):
            raise ValueError("Expected PNG, JPEG, GIF, WebP or SVG image bytes")
        return self.name, self.mime, hashlib.sha256(data).hexdigest(), data
