"""PDF text extraction — powered by pdfplumber, designed for academic papers."""

import asyncio
from pathlib import Path
from typing import Any

import pdfplumber
import structlog

logger = structlog.get_logger()


def extract_text(pdf_path: Path) -> dict[str, Any]:
    with pdfplumber.open(str(pdf_path)) as doc:
        pages_data = []
        all_texts: list[str] = []
        all_tables: list[dict] = []
        empty_pages = 0

        for i, page in enumerate(doc.pages, start=1):
            text = page.extract_text() or ""
            tables = page.extract_tables()
            all_texts.append(text)

            page_info: dict = {"page": i, "chars": len(text)}
            if not text.strip():
                page_info["empty"] = True
                empty_pages += 1

            for t in tables:
                caption = _guess_table_caption(page, t)
                header = []
                rows = []
                if t:
                    header = [str(c) if c is not None else "" for c in t[0]]
                    for row in t[1:]:
                        rows.append([str(c) if c is not None else "" for c in row])
                table_entry = {
                    "page": i,
                    "caption": caption,
                    "header": header,
                    "rows": rows,
                }
                all_tables.append(table_entry)
                page_info.setdefault("tables", 0)
                page_info["tables"] += 1

            pages_data.append(page_info)

        full_text = "\n\n".join(all_texts).strip()
        metadata = _extract_metadata(doc.metadata)

        result: dict[str, Any] = {
            "total_pages": len(doc.pages),
            "total_chars": len(full_text),
            "empty_pages": empty_pages,
            "has_text": len(full_text) > 100,
            "text": full_text,
            "tables": all_tables,
            "table_count": len(all_tables),
            "metadata": metadata,
            "pages": pages_data,
        }

        if empty_pages == len(doc.pages):
            result["warning"] = (
                "所有页面均未提取到文本，这可能是扫描版 PDF（需要 OCR）或图片型 PDF。"
            )

        return result


async def extract_text_async(pdf_path: Path) -> dict[str, Any]:
    return await asyncio.to_thread(extract_text, pdf_path)


def _extract_metadata(pdf_meta: dict | None) -> dict[str, str]:
    if not pdf_meta:
        return {}
    result = {}
    for key in ("Title", "Author", "Subject", "Creator", "Producer", "CreationDate", "ModDate"):
        val = pdf_meta.get(key)
        if val is not None:
            result[key.lower()] = str(val)
    return result


def _guess_table_caption(page, table: list[list]) -> str:
    if not table or not table[0]:
        return ""
    try:
        found_tables = page.find_tables()
        if not found_tables:
            return ""
        cells = found_tables[0].cells
        if not cells:
            return ""
        top_y = min(c[1] for row in cells for c in row if hasattr(c, "__getitem__"))
    except (IndexError, AttributeError, ValueError, TypeError):
        return ""

    text_above = page.within_bbox((0, 0, page.width, top_y)).extract_text() or ""
    lines = text_above.strip().split("\n")
    for line in reversed(lines):
        line = line.strip()
        if line.lower().startswith(("table", "fig", "表", "图")):
            return line
        if 5 < len(line) < 300:
            return line[:200]
    return ""


def extract_tables(pdf_path: Path) -> list[dict]:
    with pdfplumber.open(str(pdf_path)) as doc:
        all_tables: list[dict] = []
        for i, page in enumerate(doc.pages, start=1):
            tables = page.extract_tables()
            for t in tables:
                if not t:
                    continue
                header = [str(c) if c is not None else "" for c in t[0]]
                rows = [[str(c) if c is not None else "" for c in row] for row in t[1:]]
                all_tables.append({
                    "page": i,
                    "header": header,
                    "rows": rows,
                })
    return all_tables
