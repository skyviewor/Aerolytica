# Cartopy multi-panel layout and export

Use this reference for every multi-panel Cartopy figure and whenever a title is
clipped, top/bottom whitespace is excessive, or a spacing edit appears to have
no effect.

## Why the failure happens

Matplotlib layout becomes unstable when several independent mechanisms try to
own the same margins. The common failure pattern is:

- create the Figure with `layout="compressed"` or `layout="constrained"`;
- later call `fig.subplots_adjust(...)` or `plt.tight_layout()`;
- manually place `fig.suptitle(..., y=1.02)` outside the canvas;
- create one horizontal colorbar per panel, each reserving another bottom row;
- pass layout-like arguments to `savefig`, where they are invalid.

The result is not a Cartopy rendering bug. The layout engine and manual
positions overwrite one another. A title placed above `y=1` can be clipped,
while repeated colorbar reservations create excess bottom whitespace.

## Single-layout-owner rule

Choose exactly one layout owner:

```python
fig, axes = plt.subplots(
    1, 2,
    figsize=(8.4, 3.8),
    layout="compressed",
    subplot_kw={"projection": ccrs.PlateCarree()},
)
```

After this call:

- do not call `subplots_adjust`, `tight_layout`, or `set_position`;
- do not pass `y` to `fig.suptitle`; let the layout engine reserve its space;
- do not pass `layout` to `savefig`;
- create colorbars with `fig.colorbar(..., ax=...)` so they participate in the
  same layout;
- use one shared colorbar for panels that share the same variable, units,
  levels, colormap, and normalization;
- if panels genuinely use different scales, keep separate colorbars but attach
  each one to its panel and use the same orientation, `pad`, and `shrink`.

Do not pass the string `bbox_inches="tight"` for Cartopy maps. If correct layout
still leaves uniform outer-canvas whitespace, use the controlled post-render
fallbacks below instead of asking `savefig` to rediscover the Cartopy tight box.

## Geographic labels survive layout

For rectangular `PlateCarree` panels, do not use
`gridlines(draw_labels=True)` as the source of longitude/latitude labels.
Gridliner labels are not reliably included in Matplotlib's layout calculation.
Use ordinary GeoAxes ticks plus `LongitudeFormatter` and `LatitudeFormatter`,
and use Gridliner only to draw the dashed lines:

```python
from cartopy.mpl.ticker import LatitudeFormatter, LongitudeFormatter

for index, ax in enumerate(axes.flat):
    row, col = divmod(index, ncols)
    ax.set_xticks(lon_ticks, crs=ccrs.PlateCarree())
    ax.set_yticks(lat_ticks, crs=ccrs.PlateCarree())
    ax.xaxis.set_major_formatter(LongitudeFormatter())
    ax.yaxis.set_major_formatter(LatitudeFormatter())
    ax.tick_params(axis="x", labelbottom=(row == nrows - 1))
    ax.tick_params(axis="y", labelleft=(col == 0))
    ax.gridlines(
        xlocs=lon_ticks, ylocs=lat_ticks, draw_labels=False,
        linestyle="--", linewidth=0.4, color="grey", alpha=0.45,
    )
```

Acceptance requires at least one visible latitude-label column on the outer
left and one visible longitude-label row along the bottom. Interior labels may
be hidden, but all labels for either coordinate must never disappear.

## Canvas sizing

Size the canvas from the panel grid and geographic aspect, not from a desired
pixel count. For a normal in-app 1×2 regional comparison, start near
`figsize=(8.4, 3.8)` and `dpi=120`. Increase only when labels are unreadable.
Do not start with figures such as `14×6` at 200 DPI for a default result.

Use a single shared colorbar when the panels are directly comparable:

```python
fig.suptitle("ERA5 potential vorticity — 2026-06-15 00:00 UTC", fontsize=12)
cbar = fig.colorbar(
    mappable,
    ax=np.ravel(axes).tolist(),
    orientation="horizontal",
    pad=0.06,
    shrink=0.86,
    aspect=32,
)
cbar.set_label(
    r"PVU ($10^{-6}\,\mathrm{K\,m^2\,kg^{-1}\,s^{-1}}$)"
)
```

If pressure levels use intentionally different color limits, a shared colorbar
would be misleading. In that case create one colorbar per axes, but keep the
canvas compact and do not add an extra manually positioned colorbar axes.

## Cartopy panel-title fallback

With some Matplotlib/Cartopy combinations, Gridliner can make the transform used
by `GeoAxes.set_title()` non-finite (`NaN`/`inf`). Symptoms include a missing
panel title, a title apparently clipped at the top, or an automatic height
calculation becoming infinite. Do not keep changing `top`, `bottom`, or canvas
height in this state.

After a draw, check that the title bounding box is finite. If it is not, remove
the axes title and use a compact in-panel label:

```python
fig.canvas.draw()
title_box = ax.title.get_window_extent(fig.canvas.get_renderer())
if not np.isfinite(title_box.extents).all():
    ax.set_title("")
    ax.text(
        0.02, 0.98, panel_name,
        transform=ax.transAxes,
        ha="left", va="top",
        fontsize=10, fontweight="bold",
        bbox={
            "facecolor": "white", "alpha": 0.75,
            "edgecolor": "none", "pad": 2,
        },
        gid="panel-title",
    )
```

An in-panel fallback is for the short panel identifier or level only. Keep the
scientific Figure title in `fig.suptitle`.

## Rendered geometry acceptance

Run this check after all titles, labels, legends, inset axes, and colorbars are
created. It detects artists extending outside the canvas and a suptitle
colliding with panel titles before export:

```python
import numpy as np

def assert_artists_inside_canvas(fig, *, pad_px=2):
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    canvas = fig.bbox
    failures = []

    artists = []
    panel_title_boxes = []
    if fig._suptitle is not None:
        artists.append(("suptitle", fig._suptitle))
    for index, ax in enumerate(fig.axes):
        artists.extend([
            (f"axes[{index}].xaxis", ax.xaxis),
            (f"axes[{index}].yaxis", ax.yaxis),
        ])
        if ax.title.get_visible() and ax.title.get_text():
            artists.append((f"axes[{index}].title", ax.title))
            panel_title_boxes.append(ax.title.get_window_extent(renderer))
        artists.extend(
            (f"axes[{index}].panel-title", text)
            for text in ax.texts if text.get_gid() == "panel-title"
        )

    for name, artist in artists:
        if not artist.get_visible():
            continue
        bbox = artist.get_tightbbox(renderer)
        if bbox is None:
            continue
        if not np.isfinite(bbox.extents).all():
            failures.append(name + " (non-finite geometry)")
            continue
        if (
            bbox.x0 < canvas.x0 + pad_px
            or bbox.y0 < canvas.y0 + pad_px
            or bbox.x1 > canvas.x1 - pad_px
            or bbox.y1 > canvas.y1 - pad_px
        ):
            failures.append(name)

    if fig._suptitle is not None and panel_title_boxes:
        suptitle_box = fig._suptitle.get_window_extent(renderer)
        top_panel = max(panel_title_boxes, key=lambda box: box.y1)
        if suptitle_box.y0 - top_panel.y1 < 6:
            failures.append("suptitle/panel-title gap")

    if failures:
        raise RuntimeError(
            "Artists touch or exceed the canvas: " + ", ".join(failures)
        )
```

This is an acceptance check, not a crop operation. If it fails, adjust the
Figure size, font size, or layout-aware colorbar parameters and render again.
Never “fix” it by deleting metadata or trimming pixels.

## Export and visual verification

```python
assert_artists_inside_canvas(fig)
fig.savefig(output, dpi=120, facecolor="white")
plt.close(fig)
```

Open the exported raster and verify all of the following:

- the full suptitle is visible with a small top margin;
- panel titles do not collide with the suptitle;
- the colorbar label and ticks are visible;
- longitude labels remain on the bottom outer edge and latitude labels remain
  on the left outer edge;
- top and bottom whitespace are visually balanced;
- the main data axes remain the dominant area;
- the original file remains readable; create a separate compact preview only if needed.

Do not claim an improvement merely because `figsize`, `pad`, `top`, or `bottom`
changed in source code. Compare the exported image dimensions and visible
geometry.

## Safe outer-canvas trimming after layout

Use this section only after all of the following are true:

1. the geographic extent is the requested domain rather than an unnecessarily
   large data extent;
2. the Figure aspect approximately matches the rendered map/panel grid;
3. one layout owner controls titles, panels, insets, legends, and colorbars;
4. `assert_artists_inside_canvas(fig)` passes;
5. the remaining problem is a uniform blank strip at the **outside of the
   Figure**, not whitespace inside a map axes or between panels.

Cropping is not a layout engine. It must not be used to conceal a wrong extent,
an oversized axes rectangle, a manually misplaced colorbar, or missing labels.
Never use it merely to satisfy a preview-size target.

### PNG/WebP: crop only the transparent outer canvas

For raster delivery, render the complete Figure to an RGBA buffer with transparent
Figure and axes patches, then crop to the alpha-channel bounds. This operates on
the final pixels and never calls `GeoAxes.get_tightbbox()`, so it avoids the
Cartopy failure where `bbox_inches="tight"` can leave only a colorbar.

```python
from io import BytesIO

from PIL import Image


def save_cartopy_raster_autocrop(
    fig,
    output,
    *,
    dpi=120,
    padding_px=8,
    background="white",
):
    assert_artists_inside_canvas(fig)
    fig.canvas.draw()

    buffer = BytesIO()
    fig.savefig(
        buffer,
        format="png",
        dpi=dpi,
        transparent=True,
    )
    buffer.seek(0)
    image = Image.open(buffer).convert("RGBA")
    content_bbox = image.getchannel("A").getbbox()
    if content_bbox is None:
        raise RuntimeError("Rendered figure has no visible pixels")

    left, top, right, bottom = content_bbox
    crop_bbox = (
        max(0, left - padding_px),
        max(0, top - padding_px),
        min(image.width, right + padding_px),
        min(image.height, bottom + padding_px),
    )
    image = image.crop(crop_bbox)

    if background is not None:
        canvas = Image.new("RGBA", image.size, background)
        canvas.alpha_composite(image)
        image = canvas.convert("RGB")

    image.save(output)
```

This is appropriate for PNG and can be adapted to WebP after cropping. Inspect
the final file, not the pre-crop buffer. Confirm that the suptitle, panel titles,
ordinary longitude/latitude ticks or Gridliner labels, legend, inset map, and
colorbar label remain present. A crop that changes the scientific domain or
removes any of those artists has failed.

Do not crop by comparing pixels to pure white: white is legitimate map/data
content, and antialiasing makes background-color thresholds fragile. Prefer the
alpha channel generated by `transparent=True`.

### PDF/SVG: pass a rendered fixed Bbox, never `"tight"`

Vector output cannot use Pillow without rasterizing the result. Render once,
collect stable artist bounds, transform their union from display pixels to
Figure inches, and pass that fixed `Bbox` to `savefig`. For `GeoAxes`, use
`get_window_extent()`; do not call its problematic `get_tightbbox()`. Ordinary
axes such as colorbars may use `get_tightbbox()`. Add figure titles, legends,
annotations, and Gridliner label artists explicitly.

```python
import numpy as np
from cartopy.mpl.geoaxes import GeoAxes
from matplotlib.transforms import Bbox


def rendered_fixed_bbox(
    fig,
    *,
    extra_artists=(),
    pad_inches=0.04,
):
    assert_artists_inside_canvas(fig)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    boxes = []

    for ax in fig.axes:
        if not ax.get_visible():
            continue
        box = (
            ax.get_window_extent(renderer)
            if isinstance(ax, GeoAxes)
            else ax.get_tightbbox(renderer)
        )
        if box is not None and np.isfinite(box.extents).all():
            boxes.append(box)

        # GeoAxes window bounds exclude ordinary tick labels and titles.
        if isinstance(ax, GeoAxes):
            for artist in (ax.xaxis, ax.yaxis, ax.title):
                if not artist.get_visible():
                    continue
                artist_box = artist.get_tightbbox(renderer)
                if (
                    artist_box is not None
                    and np.isfinite(artist_box.extents).all()
                ):
                    boxes.append(artist_box)

    if fig._suptitle is not None and fig._suptitle.get_visible():
        boxes.append(fig._suptitle.get_window_extent(renderer))

    for artist in (*fig.legends, *fig.texts, *extra_artists):
        if artist is None or not artist.get_visible():
            continue
        box = artist.get_window_extent(renderer)
        if box is not None and np.isfinite(box.extents).all():
            boxes.append(box)

    if not boxes:
        raise RuntimeError("No finite rendered bounds found")

    bbox = Bbox.union(boxes).transformed(fig.dpi_scale_trans.inverted())
    return Bbox.from_extents(
        bbox.x0 - pad_inches,
        bbox.y0 - pad_inches,
        bbox.x1 + pad_inches,
        bbox.y1 + pad_inches,
    )
```

Include non-rectangular-projection Gridliner labels explicitly because they may
live outside the map axes:

```python
gridliner_artists = []
for name in ("xlabel_artists", "ylabel_artists"):
    gridliner_artists.extend(getattr(gl, name, ()))

fixed_bbox = rendered_fixed_bbox(
    fig,
    extra_artists=gridliner_artists,
)
fig.savefig(
    "figures/map.pdf",
    bbox_inches=fixed_bbox,  # a fixed Bbox, never the string "tight"
    facecolor="white",
)
```

### Decision order

Use the least invasive successful method:

1. correct domain, aspect-matched canvas, and one layout owner;
2. normal full-Figure export;
3. for raster only, transparent outer-canvas autocrop;
4. for vector only, rendered fixed-`Bbox` export.

If either fallback removes the map, produces an implausible aspect ratio, or
leaves only the colorbar, reject the export and return to the normal full-Figure
layout. Do not retry the literal `bbox_inches="tight"`.
