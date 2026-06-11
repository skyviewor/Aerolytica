import json
from datetime import datetime

import pytest

from meteora.data import gfs_params

INDEX_HTML = """
<html><body>
<a href="grib2_table4-2-0-0.shtml">Temperature</a>
<a href="grib2_table4-2-0-1.shtml">Moisture</a>
<a href="grib2_table4-2.shtml">Index</a>
</body></html>
"""

PARAM_HTML = """
<html><body>
<h4><span>(Meteorological products, Temperature category)</span></h4>
<table>
<tr><th>Number</th><th>Parameter</th><th>Units</th><th>Abbrev</th></tr>
<tr><td>0</td><td>Temperature</td><td>K</td><td>TMP</td></tr>
<tr><td>6</td><td>Dew Point Temperature</td><td>K</td><td>DPT</td></tr>
</table>
</body></html>
"""


def test_extract_table_links_and_parse_parameter_page():
    links = gfs_params._extract_table_links(INDEX_HTML)
    parsed = gfs_params._parse_parameter_page(
        PARAM_HTML,
        discipline=0,
        category=0,
        source_url="https://example.com/grib2_table4-2-0-0.shtml",
    )

    assert links == ["grib2_table4-2-0-0.shtml", "grib2_table4-2-0-1.shtml"]
    assert parsed[0] == {
        "discipline": 0,
        "category": 0,
        "number": 0,
        "abbrev": "TMP",
        "parameter": "Temperature",
        "units": "K",
        "category_label": "Temperature",
        "source_url": "https://example.com/grib2_table4-2-0-0.shtml",
    }


def test_search_gfs_parameters_matches_abbrev_english_and_chinese():
    parameters = gfs_params._parse_parameter_page(PARAM_HTML, 0, 0, "https://example.com")

    assert gfs_params.search_gfs_parameters(parameters, "TMP")[0]["parameter"] == "Temperature"
    assert gfs_params.search_gfs_parameters(parameters, "temperature")[0]["abbrev"] == "TMP"
    assert gfs_params.search_gfs_parameters(parameters, "温度")[0]["abbrev"] == "TMP"


@pytest.mark.asyncio
async def test_get_gfs_parameters_uses_stale_cache_when_fetch_fails(tmp_path, monkeypatch):
    cache_file = tmp_path / "gfs_parameters.json"
    monkeypatch.setattr(gfs_params, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(gfs_params, "CACHE_FILE", cache_file)
    cache_file.write_text(
        json.dumps(
            {
                "cached_at": datetime(2020, 1, 1).isoformat(),
                "count": 1,
                "parameters": [
                    {
                        "discipline": 0,
                        "category": 0,
                        "number": 0,
                        "abbrev": "TMP",
                        "parameter": "Temperature",
                        "units": "K",
                        "source_url": "https://example.com",
                        "cached_at": datetime(2020, 1, 1).isoformat(),
                    }
                ],
            }
        )
    )

    async def fail_fetch():
        raise RuntimeError("network down")

    monkeypatch.setattr(gfs_params, "fetch_gfs_parameters", fail_fetch)

    parameters = await gfs_params.get_gfs_parameters()

    assert parameters[0]["abbrev"] == "TMP"
