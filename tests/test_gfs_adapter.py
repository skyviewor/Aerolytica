import pytest

from meteora.adapters.gfs_adapter import (
    GFSAdapter,
    parse_gfs_idx,
    select_gfs_entries,
    summarize_gfs_inventory,
)

IDX_TEXT = """1:0:d=2026060400:PRMSL:mean sea level:anl:
2:998971:d=2026060400:TMP:2 m above ground:anl:
3:1101952:d=2026060400:TMP:500 mb:anl:
4:1313579:d=2026060400:UGRD:500 mb:anl:
5:1513579:d=2026060400:APCP:surface:0-1 hour acc fcst:
"""


def test_parse_gfs_idx_computes_byte_ranges():
    entries = parse_gfs_idx(IDX_TEXT)

    assert len(entries) == 5
    assert entries[0].start_byte == 0
    assert entries[0].end_byte == 998970
    assert entries[1].range_header == "bytes=998971-1101951"
    assert entries[3].start_byte == 1313579
    assert entries[3].end_byte == 1513578
    assert entries[-1].start_byte == 1513579
    assert entries[-1].end_byte is None
    assert entries[-1].range_header == "bytes=1513579-"


def test_select_gfs_entries_matches_variable_and_level():
    entries = parse_gfs_idx(IDX_TEXT)

    selected, missing = select_gfs_entries(entries, ["TMP", "RH"], ["500 mb"])

    assert [(e.variable, e.level) for e in selected] == [("TMP", "500 mb")]
    assert missing == [{"variable": "RH", "level": "500 mb"}]


def test_summarize_gfs_inventory_filters_variable():
    entries = parse_gfs_idx(IDX_TEXT)

    inventory = summarize_gfs_inventory(entries, ["APCP"])

    assert inventory == [
        {
            "variable": "APCP",
            "level": "surface",
            "forecast": "0-1 hour acc fcst",
            "message_count": 1,
            "byte_count": 0,
            "examples": ["5:1513579:d=2026060400:APCP:surface:0-1 hour acc fcst:"],
        }
    ]


def test_download_ranges_uses_http_range_headers(tmp_path, monkeypatch):
    entries = parse_gfs_idx(IDX_TEXT)[:2]
    dest = tmp_path / "subset.grib2"
    seen_ranges = []

    class FakeStream:
        def __init__(self, headers):
            self.headers = headers
            self.status_code = 206

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_bytes(self, chunk_size):
            yield self.headers["Range"].encode()

        def raise_for_status(self):
            raise AssertionError("unexpected status error")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, headers):
            assert method == "GET"
            assert url == "https://example.com/gfs"
            seen_ranges.append(headers["Range"])
            return FakeStream(headers)

    monkeypatch.setattr("meteora.adapters.gfs_adapter.httpx.Client", FakeClient)

    size = GFSAdapter._download_ranges("https://example.com/gfs", entries, dest, 100)

    assert seen_ranges == ["bytes=0-998970", "bytes=998971-1101951"]
    assert size == dest.stat().st_size
    assert dest.read_bytes() == b"bytes=0-998970bytes=998971-1101951"


@pytest.mark.asyncio
async def test_download_one_selects_idx_messages(tmp_path, monkeypatch):
    def fake_fetch_text(url):
        assert url.endswith(".idx")
        return IDX_TEXT

    def fake_download_ranges(url, entries, dest, total_bytes, on_progress=None):
        dest.write_bytes(b"GRIB")
        return 4

    monkeypatch.setattr(GFSAdapter, "_fetch_text", staticmethod(fake_fetch_text))
    monkeypatch.setattr(GFSAdapter, "_download_ranges", staticmethod(fake_download_ranges))

    adapter = GFSAdapter(base_url="https://example.com")
    result = await adapter.download_one(
        date="2026-06-04",
        cycle="00",
        forecast_hour=0,
        variables=["TMP"],
        levels=["500 mb"],
        dest_dir=tmp_path,
    )

    assert result.file_path.read_bytes() == b"GRIB"
    assert result.selected_entries[0].variable == "TMP"
    assert result.selected_entries[0].level == "500 mb"
    assert result.missing == []
