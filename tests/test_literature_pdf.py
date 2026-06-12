"""Tests for legal multi-source literature PDF acquisition."""

import pytest

from aero.data import literature_pdf


@pytest.mark.asyncio
async def test_resolve_pdf_urls_aggregates_and_deduplicates_sources(monkeypatch):
    async def openalex(work):
        return {
            "source": "openalex",
            "candidates": [{"source": "openalex", "url": "https://oa.test/paper.pdf"}],
        }

    async def europe_pmc(doi):
        return {
            "source": "europe_pmc",
            "candidates": [{"source": "europe_pmc", "url": "https://pmc.test/paper.pdf"}],
        }

    async def unpaywall(doi):
        return {
            "source": "unpaywall",
            "candidates": [{"source": "unpaywall", "url": "https://oa.test/paper.pdf"}],
        }

    async def empty(doi):
        return {"source": "empty", "candidates": []}

    monkeypatch.setattr(literature_pdf, "_try_openalex_pdf", openalex)
    monkeypatch.setattr(literature_pdf, "_try_europe_pmc_pdf", europe_pmc)
    monkeypatch.setattr(literature_pdf, "_try_unpaywall_pdf", unpaywall)
    monkeypatch.setattr(literature_pdf, "_try_semantic_scholar_pdf", empty)
    monkeypatch.setattr(literature_pdf, "_try_crossref_pdf", empty)

    result = await literature_pdf.resolve_pdf_urls({"doi": "10.1234/example"})

    assert [candidate["url"] for candidate in result["candidates"]] == [
        "https://oa.test/paper.pdf",
        "https://pmc.test/paper.pdf",
    ]
    assert result["pdf_url"] == "https://oa.test/paper.pdf"
    assert result["available"] is True


@pytest.mark.asyncio
async def test_download_pdf_candidates_falls_back_after_403(monkeypatch, tmp_path):
    calls = []

    async def fake_download(url, dest_path, on_progress=None):
        calls.append(url)
        if "publisher" in url:
            return {"success": False, "error": "403 Forbidden", "status_code": 403}
        dest_path.write_bytes(b"%PDF-success")
        return {"success": True, "file_path": str(dest_path), "file_size": dest_path.stat().st_size}

    monkeypatch.setattr(literature_pdf, "download_pdf", fake_download)
    destination = tmp_path / "paper.pdf"
    result = await literature_pdf.download_pdf_candidates(
        [
            {
                "source": "crossref",
                "url": "https://publisher.test/paper.pdf",
                "kind": "publisher",
            },
            {
                "source": "europe_pmc",
                "url": "https://repository.test/paper.pdf",
                "kind": "open_access",
            },
        ],
        destination,
    )

    assert result["success"] is True
    assert result["source"] == "europe_pmc"
    assert calls == [
        "https://publisher.test/paper.pdf",
        "https://repository.test/paper.pdf",
    ]
    assert result["attempts"][0]["status_code"] == 403
    assert destination.read_bytes().startswith(b"%PDF-")


@pytest.mark.asyncio
async def test_download_pdf_candidates_reports_all_failures(monkeypatch, tmp_path):
    async def fake_download(url, dest_path, on_progress=None):
        return {"success": False, "error": f"blocked: {url}", "status_code": 403}

    monkeypatch.setattr(literature_pdf, "download_pdf", fake_download)
    result = await literature_pdf.download_pdf_candidates(
        [
            {"source": "openalex", "url": "https://one.test/paper.pdf"},
            {"source": "unpaywall", "url": "https://two.test/paper.pdf"},
        ],
        tmp_path / "paper.pdf",
    )

    assert result["success"] is False
    assert len(result["attempts"]) == 2
    assert all(attempt["status_code"] == 403 for attempt in result["attempts"])


@pytest.mark.asyncio
async def test_download_pdf_reuses_existing_valid_pdf(tmp_path):
    destination = tmp_path / "paper.pdf"
    destination.write_bytes(b"%PDF-existing")

    result = await literature_pdf.download_pdf("https://unused.test/paper.pdf", destination)

    assert result["success"] is True
    assert result["reused"] is True
    assert destination.read_bytes() == b"%PDF-existing"
