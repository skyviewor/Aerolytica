import pytest


def _fake_grib2_message(discipline: int = 0, category: int = 0, parameter: int = 0) -> bytes:
    section4 = (
        (11).to_bytes(4, "big")
        + bytes([4])
        + (0).to_bytes(2, "big")
        + (0).to_bytes(2, "big")
        + bytes([category, parameter])
    )
    total_length = 16 + len(section4) + 4
    section0 = b"GRIB" + b"\x00\x00" + bytes([discipline, 2]) + total_length.to_bytes(8, "big")
    return section0 + section4 + b"7777"


def test_parse_grib2_messages_reads_product_metadata():
    from meteora.toolbox.tools.grib import _parse_grib2_messages

    data = (
        _fake_grib2_message(category=0, parameter=0)
        + _fake_grib2_message(category=2, parameter=2)
    )
    messages = _parse_grib2_messages(data)

    assert len(messages) == 2
    assert messages[0]["range"] == "bytes=0-30"
    assert messages[0]["discipline"] == 0
    assert messages[0]["category"] == 0
    assert messages[0]["parameter_number"] == 0
    assert messages[0]["product_definition_template"] == 0
    assert messages[0]["ends_with_7777"] is True
    assert messages[1]["offset"] == 31
    assert messages[1]["category"] == 2
    assert messages[1]["parameter_number"] == 2


@pytest.mark.asyncio
async def test_inspect_grib2_returns_structural_metadata(tmp_path, monkeypatch):
    from meteora.toolbox import builtin_tools

    path = tmp_path / "sample.grib2"
    path.write_bytes(_fake_grib2_message(category=0, parameter=0))

    async def fake_lookup():
        return {
            (0, 0, 0): {
                "abbrev": "TMP",
                "name": "Temperature",
                "units": "K",
                "source_url": "https://example.com",
            }
        }

    monkeypatch.setattr("meteora.toolbox.tools.grib._load_gfs_parameter_lookup", fake_lookup)
    monkeypatch.setattr(
        "meteora.toolbox.tools.grib._inspect_grib2_with_cfgrib",
        lambda p: {"available": False, "message": "cfgrib unavailable"},
    )

    result = await builtin_tools.inspect_grib2(str(path))

    assert result["status"] == "ok"
    assert result["format"] == "grib2"
    assert result["message_count"] == 1
    assert result["messages"][0]["parameter"]["abbrev"] == "TMP"
    assert result["integrity"]["all_messages_end_with_7777"] is True


@pytest.mark.asyncio
async def test_inspect_grib2_rejects_non_grib_file(tmp_path):
    from meteora.toolbox import builtin_tools

    path = tmp_path / "not_grib2.txt"
    path.write_text("hello")

    result = await builtin_tools.inspect_grib2(str(path))

    assert result["status"] == "error"
    assert "未识别到 GRIB2" in result["message"]
