from aero.data import gfs_products

PRODUCTS_HTML = """
<html><body>
<a href="gfs.t00z.pgrb2.0p25.anl.shtml">ANL</a>
<a href="gfs.t00z.pgrb2.0p25.f000.shtml">FH000</a>
<a href="gfs.t00z.pgrb2.0p25.f003.shtml">FH003</a>
<a href="gfs.t00z.pgrb2b.0p25.f003.shtml">FH003</a>
<a href="gfs.t00z.sfluxgrbf000.grib2.shtml">surface flux</a>
</body></html>
"""

INVENTORY_HTML = """
<html><body>
<h2>Inventory of File <i>gfs.t00z.pgrb2.0p25.f000</i></h2>
<table>
<tr>
<th>Number</th><th>Level/Layer</th><th>Parameter</th>
<th>Forecast Valid</th><th>Description</th>
</tr>
<tr>
<td>001</td><td>mean sea level</td><td>PRMSL</td>
<td>analysis</td><td>Pressure Reduced to MSL [Pa]</td>
</tr>
<tr>
<td>002</td><td>2 m above ground</td><td>TMP</td>
<td>analysis</td><td>Temperature [K]</td>
</tr>
</table>
</body></html>
"""


def test_extract_inventory_links():
    links = gfs_products._extract_inventory_links(PRODUCTS_HTML)

    assert links == [
        "gfs.t00z.pgrb2.0p25.anl.shtml",
        "gfs.t00z.pgrb2.0p25.f000.shtml",
        "gfs.t00z.pgrb2.0p25.f003.shtml",
        "gfs.t00z.pgrb2b.0p25.f003.shtml",
        "gfs.t00z.sfluxgrbf000.grib2.shtml",
    ]


def test_product_meta_from_inventory_href():
    meta = gfs_products.product_meta_from_inventory_href("gfs.t00z.pgrb2b.0p25.f003.shtml")

    assert meta["product"] == "pgrb2b.0p25"
    assert meta["forecast_hour"] == 3
    assert meta["resolution"] == "0.25 degree"
    assert meta["subset"] == "least commonly used parameters"


def test_product_meta_from_sflux_inventory_href():
    meta = gfs_products.product_meta_from_inventory_href("gfs.t00z.sfluxgrbf000.grib2.shtml")

    assert meta["product"] == "sfluxgrb"
    assert meta["forecast_hour"] == 0
    assert meta["subset"] == "surface flux fields"


def test_parse_and_search_inventory_page():
    meta = gfs_products.product_meta_from_inventory_href("gfs.t00z.pgrb2.0p25.f000.shtml")
    records = gfs_products.parse_inventory_page(INVENTORY_HTML, "https://example.com/inv", meta)
    inventory = {"records": records}

    assert records[0]["parameter"] == "PRMSL"
    assert records[0]["description"] == "Pressure Reduced to MSL [Pa]"
    assert records[1]["level"] == "2 m above ground"
    assert (
        gfs_products.search_gfs_inventory(inventory, "TMP")[0]["description"]
        == "Temperature [K]"
    )
    assert gfs_products.search_gfs_inventory(inventory, "mean sea level")[0]["parameter"] == "PRMSL"
