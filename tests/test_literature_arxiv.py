"""Tests for arXiv literature search module."""

import json

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from meteora.core.debug_log import configure_debug_logging
from meteora.data.literature_arxiv import (
    _parse_arxiv_atom,
    _extract_arxiv_entry,
    search_arxiv,
    get_arxiv_detail,
)

SAMPLE_ATOM_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <link href="http://export.arxiv.org/api/query?search_query=all%3Atropical+cyclone&amp;start=0&amp;max_results=2" rel="self" type="application/atom+xml"/>
  <title type="html">ArXiv Query: search_query=all:tropical cyclone&amp;start=0&amp;max_results=2</title>
  <id>http://export.arxiv.org/api/Abc123</id>
  <updated>2025-06-01T00:00:00Z</updated>
  <opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">42</opensearch:totalResults>
  <opensearch:startIndex xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">0</opensearch:startIndex>
  <opensearch:itemsPerPage xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">2</opensearch:itemsPerPage>
  <entry>
    <id>http://arxiv.org/abs/2301.12345v1</id>
    <updated>2025-01-15T10:00:00Z</updated>
    <published>2025-01-15T10:00:00Z</published>
    <title>Tropical Cyclone Intensity Prediction Using Deep Learning</title>
    <summary>This paper proposes a novel deep learning approach for predicting tropical cyclone intensity using satellite imagery and reanalysis data. Experiments on the Atlantic basin show 95% accuracy.</summary>
    <author>
      <name>John Smith</name>
    </author>
    <author>
      <name>Jane Doe</name>
    </author>
    <arxiv:primary_category term="cs.AI"/>
    <category term="physics.ao-ph"/>
    <link title="pdf" href="http://arxiv.org/pdf/2301.12345v1" rel="related" type="application/pdf"/>
    <link title="doi" href="http://dx.doi.org/10.1234/test.001" rel="related"/>
    <arxiv:comment>Accepted at Nature Climate Change, 15 pages, 4 figures</arxiv:comment>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2302.67890v2</id>
    <updated>2025-02-20T15:00:00Z</updated>
    <published>2025-02-20T15:00:00Z</published>
    <title>ENSO Forecasting with Graph Neural Networks</title>
    <summary>A graph neural network framework for ENSO forecasting achieving 18-month lead times.</summary>
    <author>
      <name>Alice Wang</name>
    </author>
    <arxiv:primary_category term="physics.ao-ph"/>
    <link title="pdf" href="http://arxiv.org/pdf/2302.67890v2" rel="related" type="application/pdf"/>
  </entry>
</feed>"""

SINGLE_ENTRY_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2301.12345v1</id>
    <updated>2025-01-15T10:00:00Z</updated>
    <published>2025-01-15T10:00:00Z</published>
    <title>Test Paper Title</title>
    <summary>Test abstract.</summary>
    <author><name>Test Author</name></author>
    <arxiv:primary_category term="cs.AI"/>
    <link title="pdf" href="http://arxiv.org/pdf/2301.12345v1" rel="related" type="application/pdf"/>
  </entry>
</feed>"""


class TestParseArxivAtom:
    def test_parses_multiple_entries(self):
        entries = _parse_arxiv_atom(SAMPLE_ATOM_RESPONSE)
        assert len(entries) == 2
        assert entries[0]["title"].startswith("Tropical Cyclone")
        assert entries[1]["title"].startswith("ENSO Forecasting")

    def test_parses_title_correctly(self):
        entries = _parse_arxiv_atom(SAMPLE_ATOM_RESPONSE)
        assert "Deep Learning" in entries[0]["title"]

    def test_parses_authors(self):
        entries = _parse_arxiv_atom(SAMPLE_ATOM_RESPONSE)
        assert "John Smith" in entries[0]["authors"]
        assert "Jane Doe" in entries[0]["authors"]
        assert len(entries[0]["authors"]) == 2
        assert len(entries[1]["authors"]) == 1

    def test_parses_pdf_url(self):
        entries = _parse_arxiv_atom(SAMPLE_ATOM_RESPONSE)
        assert entries[0]["pdf_url"] == "http://arxiv.org/pdf/2301.12345v1"

    def test_parses_doi(self):
        entries = _parse_arxiv_atom(SAMPLE_ATOM_RESPONSE)
        assert entries[0]["doi"] == "10.1234/test.001"

    def test_parses_primary_category(self):
        entries = _parse_arxiv_atom(SAMPLE_ATOM_RESPONSE)
        assert entries[0]["primary_category"] == "cs.AI"

    def test_parses_comment(self):
        entries = _parse_arxiv_atom(SAMPLE_ATOM_RESPONSE)
        assert "Nature Climate Change" in entries[0]["comment"]

    def test_parses_empty_atom(self):
        entries = _parse_arxiv_atom("<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'></feed>")
        assert len(entries) == 0

    def test_handles_missing_fields(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
          <entry>
            <id>http://arxiv.org/abs/9999.99999v1</id>
            <updated>2025-01-01T00:00:00Z</updated>
          </entry>
        </feed>"""
        entries = _parse_arxiv_atom(xml)
        assert len(entries) == 1
        assert entries[0]["id"] == "http://arxiv.org/abs/9999.99999v1"


class TestExtractArxivEntry:
    def test_extracts_arxiv_work_id(self):
        entry = _parse_arxiv_atom(SINGLE_ENTRY_ATOM)[0]
        work = _extract_arxiv_entry(entry)
        assert work["work_id"] == "arxiv:2301.12345"
        assert work["arxiv_id"] == "2301.12345"

    def test_extracts_basic_fields(self):
        entry = _parse_arxiv_atom(SINGLE_ENTRY_ATOM)[0]
        work = _extract_arxiv_entry(entry)
        assert work["title"] == "Test Paper Title"
        assert work["authors"] == ["Test Author"]
        assert work["source"] == "arXiv"
        assert work["is_oa"] is True
        assert work["oa_status"] == "gold"

    def test_has_pdf_url_with_fallback(self):
        entry = {"id": "http://arxiv.org/abs/9999.99999v1", "title": "Test", "summary": "Test",
                 "authors": [], "published": "2025-01-01T00:00:00Z"}
        work = _extract_arxiv_entry(entry)
        assert work["pdf_url"] == "https://arxiv.org/pdf/9999.99999.pdf"
        assert work["landing_page_url"] == "https://arxiv.org/abs/9999.99999"

    def test_strips_version_suffix(self):
        entry = _parse_arxiv_atom(SINGLE_ENTRY_ATOM)[0]
        work = _extract_arxiv_entry(entry)
        assert work["arxiv_id"] == "2301.12345"
        assert "v1" not in work["arxiv_id"]

    def test_handles_arxiv_id_without_version(self):
        entry = {"id": "http://arxiv.org/abs/9999.99999", "title": "T", "summary": "S",
                 "authors": [], "published": "2025-01-01T00:00:00Z"}
        work = _extract_arxiv_entry(entry)
        assert work["arxiv_id"] == "9999.99999"


class TestSearchArxiv:
    @pytest.mark.asyncio
    async def test_search_returns_empty_without_query(self):
        result = await search_arxiv()
        assert result["found"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_search_calls_api_and_parses_response(self):
        mock_response = MagicMock()
        mock_response.text = SAMPLE_ATOM_RESPONSE
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            result = await search_arxiv(keyword="tropical cyclone", limit=2)

        assert result["found"] is True
        assert result["count"] == 2
        assert "results" in result
        assert len(result["results"]) == 2
        assert result["references"] == ["https://arxiv.org"]

    @pytest.mark.asyncio
    async def test_search_handles_http_error(self):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = Exception("Connection error")
            result = await search_arxiv(keyword="test")

        assert result["found"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_search_logs_caught_error_to_debug_log(self, tmp_path):
        debug_path = tmp_path / "debug.log"
        configure_debug_logging(path=debug_path)

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = Exception("Connection error")
            result = await search_arxiv(keyword="test")

        assert result["found"] is False
        events = [
            json.loads(line)
            for line in debug_path.read_text(encoding="utf-8").splitlines()
        ]
        arxiv_events = [event for event in events if event["event"] == "arxiv.search_failed"]
        assert arxiv_events
        assert arxiv_events[-1]["error"] == "Connection error"
        assert "Exception: Connection error" in arxiv_events[-1]["traceback"]


class TestGetArxivDetail:
    @pytest.mark.asyncio
    async def test_gets_detail_by_arxiv_id(self):
        mock_response = MagicMock()
        mock_response.text = SINGLE_ENTRY_ATOM
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            work = await get_arxiv_detail("2301.12345")

        assert work is not None
        assert work["work_id"] == "arxiv:2301.12345"
        assert work["title"] == "Test Paper Title"

    @pytest.mark.asyncio
    async def test_handles_strip_arxiv_prefix(self):
        mock_response = MagicMock()
        mock_response.text = SINGLE_ENTRY_ATOM
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            work = await get_arxiv_detail("arxiv:2301.12345")

        assert work is not None
        assert work["arxiv_id"] == "2301.12345"

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = Exception("Network error")
            work = await get_arxiv_detail("2301.12345")

        assert work is None
