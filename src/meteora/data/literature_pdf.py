"""Legal multi-source discovery and fallback downloading for literature PDFs."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import structlog

logger = structlog.get_logger()

CHUNK_SIZE = 1024 * 1024
USER_AGENT = "meteora-literature/0.1 (mailto:dev@meteora.example)"
_PDF_LOOKUP_TIMEOUT = 10


def _normalize_doi(doi: str) -> str:
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/"):
        if doi.startswith(prefix):
            return doi[len(prefix) :]
    return doi


def _candidate(source: str, url: str, *, kind: str = "open_access") -> dict[str, str]:
    return {"source": source, "url": url, "kind": kind}


def _deduplicate_candidates(candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for candidate in candidates:
        url = candidate.get("url", "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(candidate)
    return result


async def _try_openalex_pdf(work: dict) -> dict[str, Any]:
    candidates = []
    for url in work.get("pdf_urls") or []:
        if isinstance(url, str):
            candidates.append(_candidate("openalex", url))
    pdf_url = work.get("pdf_url", "")
    if pdf_url:
        candidates.append(_candidate("openalex", pdf_url))
    return {"source": "openalex", "candidates": _deduplicate_candidates(candidates)}


async def _try_unpaywall_pdf(doi: str) -> dict[str, Any]:
    source = "unpaywall"
    if not doi:
        return {"source": source, "candidates": []}
    clean_doi = _normalize_doi(doi)
    url = f"https://api.unpaywall.org/v2/{clean_doi}?email=dev@meteora.example"
    try:
        async with httpx.AsyncClient(timeout=_PDF_LOOKUP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        locations = list(data.get("oa_locations") or [])
        best = data.get("best_oa_location") or {}
        if best:
            locations.append(best)
        locations.sort(key=lambda item: item.get("host_type") != "repository")
        candidates = [
            _candidate(source, location.get("url_for_pdf", "") or location.get("pdf_url", ""))
            for location in locations
        ]
        return {"source": source, "candidates": _deduplicate_candidates(candidates)}
    except Exception as exc:
        logger.debug("unpaywall lookup failed", doi=doi, error=str(exc))
        return {"source": source, "candidates": [], "error": str(exc)}


async def _try_semantic_scholar_pdf(doi: str) -> dict[str, Any]:
    source = "semantic_scholar"
    if not doi:
        return {"source": source, "candidates": []}
    lookup = f"DOI:{_normalize_doi(doi)}"
    url = f"https://api.semanticscholar.org/graph/v1/paper/{lookup}?fields=openAccessPdf"
    try:
        async with httpx.AsyncClient(timeout=_PDF_LOOKUP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        oa = data.get("openAccessPdf") or {} if isinstance(data, dict) else {}
        pdf_url = oa.get("url") or ""
        candidates = [_candidate(source, pdf_url)] if pdf_url else []
        return {"source": source, "candidates": candidates}
    except Exception as exc:
        logger.debug("semantic scholar lookup failed", lookup=lookup, error=str(exc))
        return {"source": source, "candidates": [], "error": str(exc)}


async def _try_europe_pmc_pdf(doi: str) -> dict[str, Any]:
    source = "europe_pmc"
    if not doi:
        return {"source": source, "candidates": []}
    clean_doi = _normalize_doi(doi)
    url = (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        f"?query=DOI:{quote(clean_doi)}&format=json&pageSize=5"
    )
    try:
        async with httpx.AsyncClient(timeout=_PDF_LOOKUP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        results = data.get("resultList", {}).get("result") or []
        candidates = []
        for result in results:
            pmcid = result.get("pmcid", "")
            if pmcid and result.get("isOpenAccess") in ("Y", True):
                candidates.append(
                    _candidate(source, f"https://europepmc.org/articles/{pmcid}?pdf=render")
                )
        return {"source": source, "candidates": _deduplicate_candidates(candidates)}
    except Exception as exc:
        logger.debug("europe pmc lookup failed", doi=doi, error=str(exc))
        return {"source": source, "candidates": [], "error": str(exc)}


async def _try_crossref_pdf(doi: str) -> dict[str, Any]:
    source = "crossref"
    if not doi:
        return {"source": source, "candidates": []}
    clean_doi = _normalize_doi(doi)
    url = f"https://api.crossref.org/works/{quote(clean_doi, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=_PDF_LOOKUP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        links = data.get("message", {}).get("link") or []
        candidates = [
            _candidate(source, link.get("URL", ""), kind="publisher")
            for link in links
            if "pdf" in str(link.get("content-type", "")).casefold()
        ]
        return {"source": source, "candidates": _deduplicate_candidates(candidates)}
    except Exception as exc:
        logger.debug("crossref lookup failed", doi=doi, error=str(exc))
        return {"source": source, "candidates": [], "error": str(exc)}


async def resolve_pdf_urls(work: dict) -> dict[str, Any]:
    """Discover legal PDF candidates without attempting to bypass access controls."""
    doi = work.get("doi", "")
    lookup_results = await asyncio.gather(
        _try_openalex_pdf(work),
        _try_europe_pmc_pdf(doi),
        _try_unpaywall_pdf(doi),
        _try_semantic_scholar_pdf(doi),
        _try_crossref_pdf(doi),
    )
    candidates = _deduplicate_candidates(
        [
            candidate
            for lookup in lookup_results
            for candidate in lookup.get("candidates", [])
        ]
    )
    tried_sources = [
        {
            "source": lookup["source"],
            "found": len(lookup.get("candidates", [])),
            **({"error": lookup["error"]} if lookup.get("error") else {}),
        }
        for lookup in lookup_results
    ]
    return {
        "pdf_url": candidates[0]["url"] if candidates else "",
        "available": bool(candidates),
        "candidates": candidates,
        "tried_sources": tried_sources,
    }


def _is_valid_pdf(filepath: Path) -> bool:
    if not filepath.exists():
        return False
    try:
        with open(filepath, "rb") as file:
            return file.read(5) == b"%PDF-"
    except OSError:
        return False


async def download_pdf(
    pdf_url: str,
    dest_path: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*;q=0.8"}
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if _is_valid_pdf(dest_path):
        return {
            "success": True,
            "reused": True,
            "file_size": dest_path.stat().st_size,
            "file_path": str(dest_path),
        }
    dest_path.unlink(missing_ok=True)
    try:
        async with httpx.AsyncClient(timeout=120, headers=headers, follow_redirects=True) as client:
            async with client.stream("GET", pdf_url) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "").lower()
                if "text/html" in content_type:
                    return {
                        "success": False,
                        "error": "服务器返回 HTML 页面，可能需要登录、机构权限或人工验证",
                        "content_type": content_type,
                    }
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with open(dest_path, "wb") as file:
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                        file.write(chunk)
                        downloaded += len(chunk)
                        if on_progress is not None and total > 0:
                            on_progress(downloaded, total)
        if not _is_valid_pdf(dest_path):
            dest_path.unlink(missing_ok=True)
            return {"success": False, "error": "下载内容不是有效 PDF"}
        size = dest_path.stat().st_size
        return {
            "success": True,
            "downloaded_bytes": downloaded,
            "total_bytes": total,
            "file_size": size,
            "file_path": str(dest_path),
        }
    except Exception as exc:
        dest_path.unlink(missing_ok=True)
        status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        return {
            "success": False,
            "error": str(exc),
            **({"status_code": status_code} if status_code is not None else {}),
        }


async def download_pdf_candidates(
    candidates: list[dict[str, str]],
    dest_path: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Try candidates in priority order until one yields a valid PDF."""
    attempts: list[dict[str, Any]] = []
    for candidate in _deduplicate_candidates(candidates):
        result = await download_pdf(candidate["url"], dest_path, on_progress=on_progress)
        attempt = {
            "source": candidate["source"],
            "url": candidate["url"],
            "kind": candidate.get("kind", "open_access"),
            "success": result["success"],
        }
        if not result["success"]:
            attempt["error"] = result.get("error", "下载失败")
            if result.get("status_code") is not None:
                attempt["status_code"] = result["status_code"]
        attempts.append(attempt)
        if result["success"]:
            return {
                **result,
                "source": candidate["source"],
                "source_url": candidate["url"],
                "attempts": attempts,
            }
    return {
        "success": False,
        "error": "所有已发现的合法全文来源均下载失败",
        "attempts": attempts,
        "suggestion": "可能需要通过 DOI 页面使用学校或机构的合法访问权限。",
    }
