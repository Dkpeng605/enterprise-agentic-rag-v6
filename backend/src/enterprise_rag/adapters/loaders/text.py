"""Deterministic DOCX, HTML, plain text, and Markdown loaders."""

import hashlib
import mimetypes
import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from PIL import Image

from enterprise_rag.domain.documents import RootKind
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.loader import BinarySource, IngestionContext, LoadedImage, LoadedRoot
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind

_SUPPORTED: dict[str, frozenset[str]] = {
    ".docx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/zip",
        }
    ),
    ".html": frozenset({"text/html", "application/xhtml+xml"}),
    ".htm": frozenset({"text/html", "application/xhtml+xml"}),
    ".txt": frozenset({"text/plain"}),
    ".md": frozenset({"text/markdown", "text/plain", "text/x-markdown"}),
}
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DRAWING_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PACKAGE_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_MAX_DOCX_ENTRIES = 10_000
_MAX_DOCX_UNCOMPRESSED = 200 * 1024 * 1024


class TextLoaderError(AppError):
    """A stable client-safe text document parsing failure."""


@dataclass(slots=True)
class _DocxSection:
    title: str
    level: int | None
    blocks: list[str] = field(default_factory=list)
    images: list[LoadedImage] = field(default_factory=list)


class _SafeHtmlMarkdownParser(HTMLParser):
    _BLOCKED = frozenset({"script", "style", "nav", "noscript", "template", "iframe", "object"})
    _BLOCKS = frozenset({"p", "div", "section", "article", "header", "footer", "aside"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.blocked_depth = 0
        self.list_stack: list[str] = []
        self.in_pre = False
        self.table_rows: list[list[str]] = []
        self.current_row: list[str] | None = None
        self.current_cell: list[str] | None = None
        self.image_references: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if self.blocked_depth:
            if tag in self._BLOCKED:
                self.blocked_depth += 1
            return
        if tag in self._BLOCKED:
            self.blocked_depth = 1
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        if re.fullmatch(r"h[1-6]", tag):
            self._break()
            self.parts.append("#" * int(tag[1]) + " ")
        elif tag in self._BLOCKS:
            self._break()
        elif tag in {"ul", "ol"}:
            self.list_stack.append(tag)
            self._break()
        elif tag == "li":
            self._break()
            self.parts.append("1. " if self.list_stack and self.list_stack[-1] == "ol" else "- ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "pre":
            self._break()
            self.parts.append("```\n")
            self.in_pre = True
        elif tag == "code" and not self.in_pre:
            self.parts.append("`")
        elif tag == "table":
            self._break()
            self.table_rows = []
        elif tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"}:
            self.current_cell = []
        elif tag == "img":
            source = values.get("src", "").strip()
            alt = values.get("alt", "").strip()
            self.image_references.append({"source": source, "alt": alt})
            label = alt or PurePosixPath(source).name or "image"
            self.parts.append(f"![{label}]({source})" if source else f"[Image: {label}]")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.blocked_depth:
            if tag in self._BLOCKED:
                self.blocked_depth -= 1
            return
        if re.fullmatch(r"h[1-6]", tag) or tag in self._BLOCKS or tag == "li":
            self._break()
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self._break()
        elif tag == "pre":
            self.parts.append("\n```\n")
            self.in_pre = False
        elif tag == "code" and not self.in_pre:
            self.parts.append("`")
        elif tag in {"td", "th"}:
            if self.current_row is not None and self.current_cell is not None:
                self.current_row.append(self._normalize_inline("".join(self.current_cell)))
            self.current_cell = None
        elif tag == "tr":
            if self.current_row is not None and any(self.current_row):
                self.table_rows.append(self.current_row)
            self.current_row = None
        elif tag == "table":
            self._emit_table()

    def handle_data(self, data: str) -> None:
        if self.blocked_depth or not data:
            return
        target = self.current_cell if self.current_cell is not None else self.parts
        if self.in_pre:
            target.append(data)
        else:
            normalized = re.sub(r"\s+", " ", data)
            if normalized.strip():
                if target and not target[-1].endswith((" ", "\n", "[", "`")):
                    target.append(" ")
                target.append(normalized.strip())

    def markdown(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()

    def _break(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n\n"):
            self.parts.append("\n\n" if not self.parts[-1].endswith("\n") else "\n")

    def _emit_table(self) -> None:
        if not self.table_rows:
            return
        width = max(len(row) for row in self.table_rows)
        rows = [row + [""] * (width - len(row)) for row in self.table_rows]
        self.parts.append("| " + " | ".join(rows[0]) + " |\n")
        self.parts.append("| " + " | ".join("---" for _ in range(width)) + " |\n")
        for row in rows[1:]:
            self.parts.append("| " + " | ".join(row) + " |\n")
        self.parts.append("\n")
        self.table_rows = []

    @staticmethod
    def _normalize_inline(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().replace("|", "\\|")


class TextDocumentLoader:
    """One deterministic adapter for structurally related text document formats."""

    def __init__(self) -> None:
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.LOADER,
            name="text_documents",
            version="1",
            capabilities=frozenset({"docx", "html", "txt", "markdown", "tables", "images"}),
            is_remote=False,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.HEALTHY,
        )

    def supports(self, media_type: str, suffix: str) -> bool:
        expected = _SUPPORTED.get(suffix.casefold())
        actual = media_type.casefold().split(";", 1)[0].strip()
        return expected is not None and actual in expected

    async def load(self, source: BinarySource, context: IngestionContext) -> list[LoadedRoot]:
        del context
        if self._closed:
            raise RuntimeError("text document Loader is closed")
        suffix = Path(source.name).suffix.casefold()
        if not self.supports(source.media_type, suffix):
            raise TextLoaderError(
                ErrorCode.DOCUMENT_UNSUPPORTED_TYPE,
                "The source is not a supported text document.",
                {"media_type": source.media_type, "suffix": suffix},
            )
        data = bytearray()
        async for chunk in source.chunks():
            if not isinstance(chunk, bytes):
                raise TypeError("source chunks must be bytes")
            data.extend(chunk)
        try:
            if suffix == ".docx":
                roots = self._load_docx(bytes(data))
            elif suffix in {".html", ".htm"}:
                roots = self._load_html(bytes(data))
            else:
                roots = self._load_utf8(bytes(data), markdown=suffix == ".md")
        except AppError:
            raise
        except (BadZipFile, ElementTree.ParseError, KeyError, ValueError) as error:
            raise TextLoaderError(
                ErrorCode.DOCUMENT_CORRUPT,
                "The text document could not be decoded.",
            ) from error
        if not roots:
            raise TextLoaderError(ErrorCode.DOCUMENT_EMPTY, "The document contains no text.")
        return roots

    async def aclose(self) -> None:
        self._closed = True

    @staticmethod
    def _decode_utf8(data: bytes) -> str:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise TextLoaderError(
                ErrorCode.DOCUMENT_ENCODING_INVALID,
                "The document is not valid UTF-8.",
                {"encoding": "utf-8"},
            ) from error

    @classmethod
    def _load_utf8(cls, data: bytes, *, markdown: bool) -> list[LoadedRoot]:
        text = cls._decode_utf8(data).strip()
        if not text:
            return []
        return [
            LoadedRoot(
                ordinal=0,
                kind=RootKind.TEXT_BLOCK,
                source_locator={"block": 1},
                raw_text=text,
                metadata={"format": "markdown" if markdown else "text"},
            )
        ]

    @classmethod
    def _load_html(cls, data: bytes) -> list[LoadedRoot]:
        parser = _SafeHtmlMarkdownParser()
        parser.feed(cls._decode_utf8(data))
        parser.close()
        text = parser.markdown()
        if not text:
            return []
        external = sum(
            reference["source"].casefold().startswith(("http://", "https://", "//"))
            for reference in parser.image_references
        )
        return [
            LoadedRoot(
                ordinal=0,
                kind=RootKind.SECTION,
                source_locator={"section": "document"},
                raw_text=text,
                metadata={
                    "format": "html",
                    "image_references": parser.image_references,
                    "external_resources_not_fetched": external,
                },
            )
        ]

    @classmethod
    def _load_docx(cls, data: bytes) -> list[LoadedRoot]:
        with ZipFile(BytesIO(data)) as archive:
            cls._validate_docx_archive(archive)
            relationships = cls._docx_relationships(archive)
            document = ElementTree.fromstring(archive.read("word/document.xml"))
            sections = [_DocxSection("Document", None)]
            body = document.find(f"{_WORD_NS}body")
            if body is None:
                return []
            image_ordinal = 0
            for element in body:
                if element.tag == f"{_WORD_NS}p":
                    text = cls._paragraph_text(element)
                    style = element.find(f"{_WORD_NS}pPr/{_WORD_NS}pStyle")
                    style_name = style.get(f"{_WORD_NS}val", "") if style is not None else ""
                    heading = re.fullmatch(r"Heading\s*([1-6])", style_name, re.IGNORECASE)
                    if heading and text.strip():
                        sections.append(_DocxSection(text.strip(), int(heading.group(1))))
                    elif text.strip():
                        sections[-1].blocks.append(text.strip())
                    for relationship_id in cls._paragraph_image_ids(element):
                        target = relationships.get(relationship_id)
                        if target is None:
                            continue
                        archive_path = posixpath.normpath(posixpath.join("word", target))
                        if not archive_path.startswith("word/media/"):
                            continue
                        image_data = archive.read(archive_path)
                        media_type = (
                            mimetypes.guess_type(archive_path)[0]
                            or "application/octet-stream"
                        )
                        width, height = cls._image_size(image_data)
                        image = LoadedImage(
                            page=None,
                            ordinal=image_ordinal,
                            name=PurePosixPath(archive_path).name,
                            media_type=media_type,
                            sha256=hashlib.sha256(image_data).hexdigest(),
                            width=width,
                            height=height,
                            data=image_data,
                        )
                        image_ordinal += 1
                        sections[-1].images.append(image)
                        sections[-1].blocks.append(f"![{image.name}]({image.name})")
                elif element.tag == f"{_WORD_NS}tbl":
                    table = cls._docx_table(element)
                    if table:
                        sections[-1].blocks.append(table)
            roots: list[LoadedRoot] = []
            for section in sections:
                text = "\n\n".join(section.blocks).strip()
                if not text:
                    continue
                roots.append(
                    LoadedRoot(
                        ordinal=len(roots),
                        kind=RootKind.SECTION,
                        source_locator={"section": section.title},
                        raw_text=text,
                        metadata={
                            "format": "docx",
                            "heading": section.title,
                            "level": section.level,
                        },
                        images=tuple(section.images),
                    )
                )
            return roots

    @staticmethod
    def _validate_docx_archive(archive: ZipFile) -> None:
        entries = archive.infolist()
        names = {entry.filename for entry in entries}
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise ValueError("missing DOCX package parts")
        total_size = sum(entry.file_size for entry in entries)
        if len(entries) > _MAX_DOCX_ENTRIES or total_size > _MAX_DOCX_UNCOMPRESSED:
            raise ValueError("DOCX archive exceeds safe expansion limits")
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("unsafe DOCX package path")

    @staticmethod
    def _docx_relationships(archive: ZipFile) -> dict[str, str]:
        path = "word/_rels/document.xml.rels"
        if path not in archive.namelist():
            return {}
        root = ElementTree.fromstring(archive.read(path))
        return {
            element.get("Id", ""): element.get("Target", "")
            for element in root.findall(f"{_PACKAGE_REL_NS}Relationship")
            if element.get("TargetMode") != "External"
        }

    @staticmethod
    def _paragraph_text(element: ElementTree.Element) -> str:
        fragments: list[str] = []
        for node in element.iter():
            if node.tag == f"{_WORD_NS}t" and node.text:
                fragments.append(node.text)
            elif node.tag == f"{_WORD_NS}tab":
                fragments.append("\t")
            elif node.tag in {f"{_WORD_NS}br", f"{_WORD_NS}cr"}:
                fragments.append("\n")
        return "".join(fragments)

    @staticmethod
    def _paragraph_image_ids(element: ElementTree.Element) -> Iterable[str]:
        for blip in element.iter(f"{_DRAWING_NS}blip"):
            relationship_id = blip.get(f"{_REL_NS}embed")
            if relationship_id:
                yield relationship_id

    @classmethod
    def _docx_table(cls, table: ElementTree.Element) -> str:
        rows: list[list[str]] = []
        for row in table.findall(f"{_WORD_NS}tr"):
            cells: list[str] = []
            for cell in row.findall(f"{_WORD_NS}tc"):
                cell_text = "".join(
                    cls._paragraph_text(paragraph)
                    for paragraph in cell.findall(f"{_WORD_NS}p")
                )
                cells.append(re.sub(r"\s+", " ", cell_text).strip().replace("|", "\\|"))
            if any(cells):
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = [
            "| " + " | ".join(rows[0]) + " |",
            "| " + " | ".join("---" for _ in range(width)) + " |",
        ]
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        return "\n".join(lines)

    @staticmethod
    def _image_size(data: bytes) -> tuple[int, int]:
        with Image.open(BytesIO(data)) as image:
            return image.width, image.height
