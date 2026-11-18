"""Bounded chat images. SVG originals are displayed only in image context."""

import base64
import binascii
import hashlib
import os
import stat
from pathlib import Path
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


MAX_SHARED = 10 * 1024 * 1024


def workspace_file(root, name, protected):
    path = Path(name)
    if path.is_absolute():
        path = path.relative_to(root)
    if not path.parts or any(part == ".." or part.startswith(".") for part in path.parts):
        raise ValueError("Share a non-hidden file inside the workspace")
    candidate = root / path
    if any(candidate == blocked or candidate.is_relative_to(blocked) for blocked in protected):
        raise ValueError("This path cannot be shared")
    # Walk from an open root; never follow symlinks or block on special files.
    flags = os.O_RDONLY | os.O_NOFOLLOW
    fd = os.open(root, flags | os.O_DIRECTORY)
    try:
        for i, part in enumerate(path.parts):
            mode = os.O_DIRECTORY if i < len(path.parts) - 1 else os.O_NONBLOCK
            child = os.open(part, flags | mode, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_SHARED:
            raise ValueError("Share a regular file of at most 10 MiB (no hard links)")
        with os.fdopen(os.dup(fd), "rb") as file:
            content = file.read(MAX_SHARED + 1)
        if len(content) > MAX_SHARED:
            raise ValueError("Shared files must be at most 10 MiB")
    finally:
        os.close(fd)
    mime = next(
        (mime for mime, matches in RASTER.items() if matches(content)), "application/octet-stream"
    )
    return path, mime, hashlib.sha256(content).hexdigest(), content


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
