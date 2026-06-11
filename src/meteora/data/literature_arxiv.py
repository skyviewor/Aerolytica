"""arXiv literature search — queries arXiv public API, parses Atom XML."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from meteora.core.debug_log import debug_exception

logger = structlog.get_logger()

ARXIV_API_BASE = "https://export.arxiv.org/api/query"
USER_AGENT = "meteora-literature/0.1 (mailto:dev@meteora.example)"

ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def _clean_tag(tag: str) -> str:
    for prefix, uri in ARXIV_NS.items():
        tag = tag.replace(f"{{{uri}}}", f"{prefix}:")
    return tag


def _parse_arxiv_atom(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    entries = []
    for entry_elem in root.findall("atom:entry", ARXIV_NS):
        entry = {}
        for child in entry_elem:
            tag = _clean_tag(child.tag)
            if tag == "atom:id":
                entry["id"] = (child.text or "").strip()
            elif tag == "atom:title":
                entry["title"] = (child.text or "").strip().replace("\n", " ").replace("  ", " ")
            elif tag == "atom:summary":
                entry["summary"] = (child.text or "").strip().replace("\n", " ").replace("  ", " ")
            elif tag == "atom:published":
                entry["published"] = (child.text or "").strip()
            elif tag == "atom:updated":
                entry["updated"] = (child.text or "").strip()
            elif tag == "atom:author":
                name_elem = child.find("atom:name", ARXIV_NS)
                if name_elem is not None and name_elem.text:
                    entry.setdefault("authors", []).append(name_elem.text.strip())
            elif tag == "atom:link":
                title = child.get("title", "")
                href = child.get("href", "")
                rel = child.get("rel", "")
                if title == "pdf" or rel == "related" and href.endswith(".pdf"):
                    entry["pdf_url"] = href
                elif title == "doi":
                    entry["doi"] = href.replace("http://dx.doi.org/", "").replace("https://doi.org/", "")
                elif rel == "alternate" and title:
                    entry["landing_page_url"] = href
            elif tag == "arxiv:primary_category":
                entry["primary_category"] = child.get("term", "")
            elif tag == "atom:category":
                entry.setdefault("categories", []).append(child.get("term", ""))
            elif tag == "arxiv:comment":
                entry["comment"] = (child.text or "").strip()
        if entry.get("id"):
            entries.append(entry)
    return entries


def _extract_arxiv_entry(entry: dict) -> dict:
    arxiv_id = entry.get("id", "")
    arxiv_id = arxiv_id.split("/")[-1]
    arxiv_id = re.sub(r"v\d+$", "", arxiv_id)

    doi = entry.get("doi", "")
    if not doi:
        doi = ""

    return {
        "work_id": f"arxiv:{arxiv_id}",
        "doi": doi,
        "title": entry.get("title", ""),
        "authors": entry.get("authors", []),
        "source": "arXiv",
        "issn": "",
        "publication_date": entry.get("published", ""),
        "publication_year": int(entry["published"][:4]) if entry.get("published") and len(entry.get("published", "")) >= 4 else None,
        "cited_by_count": 0,
        "abstract": entry.get("summary", ""),
        "is_oa": True,
        "oa_status": "gold",
        "oa_url": "",
        "pdf_url": entry.get("pdf_url", f"https://arxiv.org/pdf/{arxiv_id}.pdf"),
        "landing_page_url": entry.get("landing_page_url", f"https://arxiv.org/abs/{arxiv_id}"),
        "type": "article",
        "concepts": [],
        "keywords": [],
        "arxiv_id": arxiv_id,
        "primary_category": entry.get("primary_category", ""),
        "categories": entry.get("categories", []),
        "comment": entry.get("comment", ""),
    }


async def search_arxiv(
    keyword: str | None = None,
    author: str | None = None,
    title: str | None = None,
    limit: int = 10,
    page: int = 0,
) -> dict[str, Any]:
    """Search arXiv API and return structured results."""
    search_parts = []
    if keyword:
        search_parts.append(f"all:{quote(keyword)}")
    if author:
        search_parts.append(f"au:{quote(author)}")
    if title:
        search_parts.append(f"ti:{quote(title)}")

    if not search_parts:
        return {"found": False, "error": "至少需要 keyword、author 或 title", "results": []}

    search_query = "+AND+".join(search_parts)
    start = page * limit
    url = (
        f"{ARXIV_API_BASE}?"
        f"search_query={search_query}"
        f"&start={start}"
        f"&max_results={limit}"
        f"&sortBy=relevance"
    )

    headers = {"User-Agent": USER_AGENT}

    try:
        async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as e:
        logger.warning("arxiv search failed", error=str(e))
        debug_exception("arxiv.search_failed", e, url=url, keyword=keyword, author=author, title=title)
        return {"found": False, "error": str(e), "results": []}

    entries = _parse_arxiv_atom(resp.text)
    results = [_extract_arxiv_entry(entry) for entry in entries]

    total_info = ""
    for line in resp.text.splitlines()[:20]:
        if "totalResults" in line:
            total_info = line
            break

    total_available = len(results)
    match = re.search(r"totalResults[^0-9]*(\d+)", total_info)
    if match:
        total_available = int(match.group(1))

    return {
        "found": len(results) > 0,
        "count": len(results),
        "total_available": total_available,
        "results": results,
        "references": ["https://arxiv.org"],
    }


async def get_arxiv_detail(arxiv_id: str) -> dict[str, Any] | None:
    arxiv_id = arxiv_id.replace("arxiv:", "")
    search_query = f"id:{arxiv_id}"
    url = f"{ARXIV_API_BASE}?id_list={quote(arxiv_id)}&max_results=1"
    headers = {"User-Agent": USER_AGENT}

    try:
        async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as e:
        logger.warning("arxiv get detail failed", arxiv_id=arxiv_id, error=str(e))
        debug_exception("arxiv.get_detail_failed", e, arxiv_id=arxiv_id, url=url)
        return None

    entries = _parse_arxiv_atom(resp.text)
    if not entries:
        return None
    return _extract_arxiv_entry(entries[0])
