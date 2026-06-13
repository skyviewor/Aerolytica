"""Tests for CAMS ADS variable catalogue handling."""

import pytest


@pytest.mark.asyncio
async def test_cams_variable_cache_fetches_missing_dataset(tmp_path, monkeypatch):
    from aero.data import cams_variables

    cache_file = tmp_path / "cams_variables.json"
    monkeypatch.setattr(cams_variables, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cams_variables, "CACHE_FILE", cache_file)
    cache_file.write_text(
        """
{
  "cached_at": "2999-01-01T00:00:00",
  "count": 1,
  "variables": [
    {
      "dataset_id": "cams-global-reanalysis-eac4",
      "name": "total_column_ozone",
      "label": "Total column ozone",
      "level_type": "single"
    }
  ]
}
""".strip()
    )

    async def fake_fetch(dataset_id):
        assert dataset_id == cams_variables.FORECAST_DATASET_ID
        return [
            {
                "dataset_id": dataset_id,
                "name": "particulate_matter_2.5um",
                "label": "Particulate matter d < 2.5 µm (PM2.5)",
                "level_type": "single",
            }
        ]

    monkeypatch.setattr(cams_variables, "fetch_cams_variables", fake_fetch)

    variables = await cams_variables.get_cams_variables(cams_variables.FORECAST_DATASET_ID)

    assert [item["name"] for item in variables] == ["particulate_matter_2.5um"]


def test_cams_forecast_fallback_contains_pm25_alias_target():
    from aero.data.cams_variables import FALLBACK_VARIABLES, FORECAST_DATASET_ID

    names = {item["name"] for item in FALLBACK_VARIABLES[FORECAST_DATASET_ID]}

    assert "particulate_matter_2.5um" in names


def test_cams_variable_resolves_underscore_pm25_variant():
    from aero.data.cams_variables import resolve_cams_variable_names

    variables = [
        {
            "dataset_id": "cams-global-atmospheric-composition-forecasts",
            "name": "particulate_matter_2.5um",
            "label": "Particulate matter d < 2.5 µm (PM2.5)",
            "level_type": "single",
        }
    ]

    resolved, warnings, records = resolve_cams_variable_names(
        ["particulate_matter_2_5um"],
        variables,
    )

    assert resolved == ["particulate_matter_2.5um"]
    assert records[0]["name"] == "particulate_matter_2.5um"
    assert warnings == [
        "particulate_matter_2_5um 已解析为 ADS 变量名 particulate_matter_2.5um"
    ]
