"""OpenAlex literature search — fetches from OpenAlex REST API, caches locally."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import structlog

logger = structlog.get_logger()

CACHE_DIR = Path.home() / ".cache" / "meteora"
CACHE_FILE = CACHE_DIR / "literature_cache.json"
CACHE_TTL = timedelta(hours=24)

DEFAULT_LIMIT = 10
MAX_PER_PAGE = 25
OPENALEX_BASE = "https://api.openalex.org"
USER_AGENT = "meteora-literature/0.1 (mailto:dev@meteora.example)"


def _load_cache() -> dict[str, Any] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE) as f:
            cached = json.load(f)
        cached_at = datetime.fromisoformat(cached["cached_at"])
        if datetime.now() - cached_at > CACHE_TTL:
            return None
        return cached.get("data", {})
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def _save_cache(data: dict[str, Any]):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(
            {"cached_at": datetime.now().isoformat(), "data": data},
            f, ensure_ascii=False, indent=2,
        )


def _cache_key(**kwargs) -> str:
    items = sorted((k, str(v)) for k, v in kwargs.items() if v is not None)
    return json.dumps(items, ensure_ascii=False)


def _reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str:
    if not inverted_index:
        return ""
    max_pos = 0
    for positions in inverted_index.values():
        if positions:
            max_pos = max(max_pos, max(positions))
    words = [""] * (max_pos + 1)
    for word, positions in inverted_index.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words)


async def _fetch_works(
    search: str | None = None,
    author: str | None = None,
    journal: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    doi: str | None = None,
    work_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    page: int = 1,
) -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}
    per_page = min(limit, MAX_PER_PAGE)

    search_terms = []
    if search:
        search_terms.append(search)
    if author:
        search_terms.append(author)
    if journal:
        search_terms.append(journal)

    filters = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    if doi:
        filters.append(f"doi:{doi}")

    url = f"{OPENALEX_BASE}/works?per_page={per_page}&page={page}"
    if search_terms:
        url += f"&search={quote(' '.join(search_terms))}"
    if work_id:
        fpart = f"id:{work_id}" if not filters else f"id:{work_id},{','.join(filters)}"
        url += f"&filter={quote(fpart, safe=':,')}"
    elif filters:
        url += f"&filter={quote(','.join(filters), safe=':,')}"

    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


def _extract_work(work: dict) -> dict:
    authors = [
        a.get("author", {}).get("display_name", "")
        for a in work.get("authorships", [])
    ]
    primary_loc = work.get("primary_location", {}) or {}
    source = primary_loc.get("source", {}) or {}
    oa = work.get("open_access", {}) or {}
    best_oa = oa.get("best_oa_location", {}) or {}
    pdf_urls = [
        location.get("pdf_url", "")
        for location in (work.get("locations") or [])
        if isinstance(location, dict) and location.get("pdf_url")
    ]
    return {
        "work_id": work.get("id", "").split("/")[-1] if work.get("id") else "",
        "doi": work.get("doi", ""),
        "title": work.get("title", work.get("display_name", "")),
        "authors": authors,
        "source": source.get("display_name", ""),
        "issn": source.get("issn_l", ""),
        "publication_date": work.get("publication_date", ""),
        "publication_year": work.get("publication_year"),
        "cited_by_count": work.get("cited_by_count", 0),
        "abstract": _reconstruct_abstract(work.get("abstract_inverted_index")),
        "is_oa": oa.get("is_oa", False),
        "oa_status": oa.get("oa_status", ""),
        "oa_url": oa.get("oa_url", ""),
        "pdf_url": best_oa.get("pdf_url", ""),
        "pdf_urls": pdf_urls,
        "landing_page_url": best_oa.get("landing_page_url", ""),
        "type": work.get("type", ""),
        "concepts": [
            c.get("display_name", "")
            for c in work.get("concepts", [])
            if c.get("level") == 0
        ],
        "keywords": [
            kw.get("display_name", "")
            for kw in work.get("keywords", [])
        ],
    }


async def search_works(
    keyword: str | None = None,
    author: str | None = None,
    journal: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = DEFAULT_LIMIT,
    source: str = "openalex",
) -> dict[str, Any]:
    if source == "arxiv":
        from meteora.data.literature_arxiv import search_arxiv

        return await search_arxiv(
            keyword=keyword,
            author=author,
            title=journal,
            limit=limit,
        )

    results = []
    page = 1
    remaining = limit

    while remaining > 0:
        per_page = min(remaining, MAX_PER_PAGE)
        try:
            data = await _fetch_works(
                search=keyword,
                author=author,
                journal=journal,
                year_from=year_from,
                year_to=year_to,
                limit=per_page,
                page=page,
            )
        except Exception as e:
            logger.warning("openalex search failed", error=str(e))
            return {"found": False, "error": str(e), "results": results}

        works = data.get("results", [])
        if not works:
            break
        for w in works:
            results.append(_extract_work(w))
        remaining -= len(works)
        page += 1

    total = data.get("meta", {}).get("count", 0) if results else 0
    return {
        "found": len(results) > 0,
        "count": len(results),
        "total_available": total,
        "results": results,
        "references": ["https://api.openalex.org"],
    }


async def get_work_detail(work_id: str) -> dict[str, Any] | None:
    if work_id.startswith("arxiv:"):
        from meteora.data.literature_arxiv import get_arxiv_detail
        return await get_arxiv_detail(work_id)

    headers = {"User-Agent": USER_AGENT}
    url = f"{OPENALEX_BASE}/works/{work_id}"
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            work = resp.json()
    except Exception as e:
        logger.warning("openalex get work failed", work_id=work_id, error=str(e))
        return None
    return _extract_work(work)


async def resolve_doi(doi: str) -> dict[str, Any] | None:
    try:
        data = await _fetch_works(doi=doi, limit=1)
        results = data.get("results", [])
        if results:
            return _extract_work(results[0])
    except Exception as e:
        logger.warning("doi resolve failed", doi=doi, error=str(e))
    return None


def _slugify_title(title: str, work_id: str, max_len: int = 60) -> str:
    if not title:
        return work_id
    slug = re.sub(r'[\\/:*?"<>|]', "", title)
    slug = re.sub(r"[\s_]+", "_", slug)
    slug = slug.strip("_.")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("_")
    return f"{slug}_{work_id}"


def save_metadata(work: dict, literature_dir: Path) -> Path:
    work_id = work["work_id"]
    if not work_id:
        raise ValueError("work_id is required")
    title = work.get("title", "")
    dirname = _slugify_title(title, work_id)
    paper_dir = literature_dir / dirname
    paper_dir.mkdir(parents=True, exist_ok=True)
    meta_path = paper_dir / "metadata.json"
    with open(meta_path, "w") as f:
        json.dump(work, f, ensure_ascii=False, indent=2)
    return paper_dir


def update_index(work: dict, literature_dir: Path):
    work_id = work["work_id"]
    doi = work.get("doi", "")
    title = work.get("title", "")
    dirname = _slugify_title(title, work_id)
    index_path = literature_dir / "_index.json"
    index: dict[str, str] = {}
    if index_path.exists():
        try:
            with open(index_path) as f:
                raw = json.load(f)
                if isinstance(raw, dict):
                    index = {str(k): str(v) for k, v in raw.items()}
        except (json.JSONDecodeError, ValueError):
            pass
    if doi:
        index[doi] = dirname
    index[f"id:{work_id}"] = dirname
    with open(index_path, "w") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def _find_paper_dir(work_id: str, literature_dir: Path) -> Path | None:
    index_path = literature_dir / "_index.json"
    if index_path.exists():
        try:
            with open(index_path) as f:
                index = json.load(f)
            dirname = index.get(f"id:{work_id}")
            if dirname:
                candidate = literature_dir / dirname
                if candidate.is_dir() and (candidate / "metadata.json").exists():
                    return candidate
        except (json.JSONDecodeError, ValueError):
            pass
    for entry in literature_dir.iterdir():
        if not entry.is_dir():
            continue
        meta_path = entry / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            if meta.get("work_id") == work_id:
                return entry
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def load_saved_metadata(work_id: str, literature_dir: Path) -> dict[str, Any] | None:
    paper_dir = _find_paper_dir(work_id, literature_dir)
    if paper_dir is None:
        return None
    meta_path = paper_dir / "metadata.json"
    with open(meta_path) as f:
        work = json.load(f)
    work["_dir"] = str(paper_dir)
    return work


def list_saved(literature_dir: Path) -> list[dict[str, Any]]:
    if not literature_dir.exists():
        return []
    results = []
    for entry in sorted(literature_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not entry.is_dir():
            continue
        meta_path = entry / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (json.JSONDecodeError, ValueError):
            continue
        pdf_path = entry / "paper.pdf"
        results.append({
            "work_id": meta.get("work_id", entry.name),
            "title": meta.get("title", ""),
            "authors": meta.get("authors", []),
            "year": meta.get("publication_year"),
            "source": meta.get("source", ""),
            "doi": meta.get("doi", ""),
            "has_pdf": pdf_path.exists(),
            "pdf_size": pdf_path.stat().st_size if pdf_path.exists() else None,
            "dir": str(entry),
        })
    return results
