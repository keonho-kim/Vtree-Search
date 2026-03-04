"""
목적:
- SourceParser에서 공통으로 쓰는 파싱 유틸을 제공한다.

설명:
- 표 HTML 직렬화, 청킹, DOCX 제목 레벨 추정, PDF 이미지 bbox 변환을 담당한다.

디자인 패턴:
- 함수형 유틸 모듈(Function Utility Module).

참조:
- src_py/vtree_search/ingestion/source_parser.py
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from statistics import median
from typing import Any, Sequence, cast

from PIL import Image

from vtree_search.ingestion.source_types import ExtractedBlock

_MIN_IMAGE_WIDTH_PX = 120
_MIN_IMAGE_HEIGHT_PX = 120
_MIN_IMAGE_AREA_PX = 30_000
_CROP_PADDING_PX = 4


@dataclass(frozen=True, slots=True)
class PdfImageBox:
    """PDF 페이지 내 이미지 bbox 모델."""

    x0: float
    top: float
    x1: float
    bottom: float


def pick_one_file_per_extension(paths: list[Path]) -> list[Path]:
    """확장자별 첫 파일 하나만 선택한다."""
    selected: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        suffix = path.suffix.lower()
        if suffix in seen:
            continue
        selected.append(path)
        seen.add(suffix)
    return selected


def serialize_docx_table(table: object) -> str:
    """DOCX Table 객체를 HTML 문자열로 변환한다."""
    rows_html: list[str] = []
    for row in getattr(table, "rows", []):
        cells_html: list[str] = []
        has_text = False
        for cell in getattr(row, "cells", []):
            value = str(getattr(cell, "text", "") or "")
            normalized = value.strip()
            if normalized:
                has_text = True
            escaped = "<br/>".join(escape(line.strip()) for line in value.splitlines() if line.strip())
            cells_html.append(f"<td>{escaped}</td>")

        if cells_html and has_text:
            rows_html.append(f"<tr>{''.join(cells_html)}</tr>")

    if not rows_html:
        return ""
    return f"<table><tbody>{''.join(rows_html)}</tbody></table>"


def table_matrix_to_html(raw_table: Sequence[Sequence[object | None]]) -> str:
    """pdfplumber 표 행렬을 HTML 문자열로 변환한다."""
    row_html_list: list[str] = []
    for raw_row in raw_table:
        cell_html_list: list[str] = []
        has_non_empty = False
        for raw_cell in raw_row:
            value = "" if raw_cell is None else str(raw_cell)
            normalized = value.strip()
            if normalized:
                has_non_empty = True
            escaped = "<br/>".join(escape(line.strip()) for line in value.splitlines() if line.strip())
            cell_html_list.append(f"<td>{escaped}</td>")

        if cell_html_list and has_non_empty:
            row_html_list.append(f"<tr>{''.join(cell_html_list)}</tr>")

    if not row_html_list:
        return ""
    return f"<table><tbody>{''.join(row_html_list)}</tbody></table>"


def max_docx_font_size(paragraph: object) -> float | None:
    """문단 내 run 중 최대 폰트 크기를 반환한다."""
    sizes: list[float] = []
    for run in getattr(paragraph, "runs", []):
        font = getattr(run, "font", None)
        size = getattr(font, "size", None)
        point = getattr(size, "pt", None)
        if point is None:
            continue
        sizes.append(float(point))
    if not sizes:
        return None
    return max(sizes)


def estimate_docx_body_font_size(paragraphs: list[object]) -> float:
    """문서 본문 폰트 크기를 추정한다."""
    sizes: list[float] = []
    for paragraph in paragraphs:
        size = max_docx_font_size(paragraph)
        if size is not None and size > 0:
            sizes.append(float(size))
    return _estimate_body_font_size_from_samples(sizes)


def resolve_docx_heading_level(
    *,
    style_name: str,
    font_size: float | None,
    body_font_size: float,
    text: str,
) -> int | None:
    """스타일명/폰트크기를 기준으로 제목 레벨을 추정한다."""
    style_level = _parse_heading_level(style_name)
    if style_level is not None:
        return style_level
    return _infer_heading_level(font_size=font_size, body_font_size=body_font_size, text=text)


def extract_pdf_image_boxes(page: object) -> list[PdfImageBox]:
    """pdfplumber 페이지에서 이미지 bbox 후보 목록을 추출한다."""
    raw_images = list(getattr(page, "images", []) or [])
    page_height = _to_float(getattr(page, "height", None))
    if page_height is None or page_height <= 0:
        return []

    boxes: list[PdfImageBox] = []
    seen: set[tuple[float, float, float, float]] = set()
    for raw in raw_images:
        x0 = _to_float(raw.get("x0"))
        x1 = _to_float(raw.get("x1"))
        top = _to_float(raw.get("top"))
        bottom = _to_float(raw.get("bottom"))

        if top is None or bottom is None:
            y0 = _to_float(raw.get("y0"))
            y1 = _to_float(raw.get("y1"))
            if y0 is not None and y1 is not None:
                top = page_height - max(y0, y1)
                bottom = page_height - min(y0, y1)

        if x0 is None or x1 is None or top is None or bottom is None:
            continue

        left = min(x0, x1)
        right = max(x0, x1)
        top_value = min(top, bottom)
        bottom_value = max(top, bottom)
        if (right - left) <= 0 or (bottom_value - top_value) <= 0:
            continue

        key = (
            round(left, 1),
            round(top_value, 1),
            round(right, 1),
            round(bottom_value, 1),
        )
        if key in seen:
            continue
        seen.add(key)

        boxes.append(
            PdfImageBox(
                x0=left,
                top=top_value,
                x1=right,
                bottom=bottom_value,
            )
        )
    return boxes


def to_pixel_box(
    *,
    box: PdfImageBox,
    page_width: float,
    page_height: float,
    image_width: int,
    image_height: int,
) -> tuple[int, int, int, int] | None:
    """PDF 포인트 좌표 bbox를 렌더링 이미지 픽셀 좌표로 변환한다."""
    if page_width <= 0 or page_height <= 0:
        return None
    if image_width <= 0 or image_height <= 0:
        return None

    left = int(math.floor((box.x0 / page_width) * image_width)) - _CROP_PADDING_PX
    right = int(math.ceil((box.x1 / page_width) * image_width)) + _CROP_PADDING_PX
    top = int(math.floor((box.top / page_height) * image_height)) - _CROP_PADDING_PX
    bottom = int(math.ceil((box.bottom / page_height) * image_height)) + _CROP_PADDING_PX

    left = max(0, left)
    top = max(0, top)
    right = min(image_width, right)
    bottom = min(image_height, bottom)

    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None
    if not is_usable_image_size(width=width, height=height):
        return None
    return left, top, right, bottom


def is_usable_image(image: Image.Image) -> bool:
    """주석 대상으로 사용할 수 있는 이미지 크기인지 확인한다."""
    width, height = image.size
    return is_usable_image_size(width=width, height=height)


def is_usable_image_size(*, width: int, height: int) -> bool:
    """이미지 크기 기반 최소 품질 기준을 확인한다."""
    if width < _MIN_IMAGE_WIDTH_PX or height < _MIN_IMAGE_HEIGHT_PX:
        return False
    if width * height < _MIN_IMAGE_AREA_PX:
        return False
    return True


def chunk_blocks(blocks: list[ExtractedBlock], max_chars: int) -> list[ExtractedBlock]:
    """추출 블록을 max_chars 기준으로 결합한다."""
    if not blocks:
        return []

    safe_max_chars = max(256, int(max_chars))
    chunked: list[ExtractedBlock] = []
    buffer: list[ExtractedBlock] = []

    for block in blocks:
        if block.block_type in {"table", "image", "heading"}:
            if buffer:
                chunked.append(flush_buffer(buffer))
                buffer.clear()
            chunked.append(block)
            continue

        if not buffer:
            buffer.append(block)
            continue

        current_len = sum(len(item.text) for item in buffer)
        projected = current_len + len(block.text) + 2
        if projected <= safe_max_chars:
            buffer.append(block)
            continue

        chunked.append(flush_buffer(buffer))
        buffer = [block]

    if buffer:
        chunked.append(flush_buffer(buffer))

    return chunked


def flush_buffer(buffer: list[ExtractedBlock]) -> ExtractedBlock:
    """문단 버퍼를 하나의 블록으로 결합한다."""
    first = buffer[0]
    joined = "\n\n".join(item.text for item in buffer if item.text.strip())
    metadata = dict(first.metadata)
    metadata["merged_block_count"] = len(buffer)

    return ExtractedBlock(
        source_file=first.source_file,
        page_num=first.page_num,
        block_type="paragraph",
        text=joined,
        metadata=metadata,
    )


def to_ltree_label(value: str) -> str:
    """임의 문자열을 ltree 라벨 규칙으로 정규화한다."""
    sanitized = "".join(char if char.isalnum() else "_" for char in value.strip())
    sanitized = sanitized.strip("_")
    if not sanitized:
        return "node"
    if sanitized[0].isdigit():
        sanitized = f"n_{sanitized}"
    return sanitized.lower()


def _parse_heading_level(style_name: str) -> int | None:
    if not style_name:
        return None
    style_lower = style_name.lower()
    matched = re.search(r"(heading|제목)\s*(\d+)", style_lower)
    if not matched:
        return None
    level = int(matched.group(2))
    return max(1, min(level, 6))


def _infer_heading_level(
    *,
    font_size: float | None,
    body_font_size: float,
    text: str,
) -> int | None:
    if font_size is None or body_font_size <= 0:
        return None
    normalized_text = " ".join(text.split())
    if not normalized_text:
        return None
    if len(normalized_text) > 120:
        return None

    ratio = float(font_size) / float(body_font_size)
    if ratio >= 1.65:
        return 1
    if ratio >= 1.45:
        return 2
    if ratio >= 1.30:
        return 3
    return None


def _estimate_body_font_size_from_samples(sizes: list[float]) -> float:
    if not sizes:
        return 11.0
    ordered = sorted(sizes)
    cutoff = max(1, int(len(ordered) * 0.6))
    return float(median(ordered[:cutoff]))


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    return float(cast(Any, value))


__all__ = [
    "PdfImageBox",
    "chunk_blocks",
    "estimate_docx_body_font_size",
    "extract_pdf_image_boxes",
    "is_usable_image",
    "max_docx_font_size",
    "pick_one_file_per_extension",
    "resolve_docx_heading_level",
    "serialize_docx_table",
    "table_matrix_to_html",
    "to_ltree_label",
    "to_pixel_box",
]
