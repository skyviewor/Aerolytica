---
name: scientific-plotting
description: Use this skill when creating, reviewing, improving, or refactoring scientific figures, meteorological and atmospheric-science plots in Python, especially maps, contour/fill maps, precipitation, radar reflectivity, satellite imagery, wind vectors, anomaly fields, vertical cross-sections, station interpolation maps, time series, charts, visualizations, publication-quality graphics, 科研图, 画图, 绘图, 图表, 可视化, 气象地图, 等值线图, 时间序列图, 色标, 单位, or figures from data. It emphasizes physically meaningful variable choices, correct units and valid times, China map boundaries with cnmaps, Matplotlib/cmaps/NCL colormaps, CJK font handling with mplfonts, Cartopy projections, reproducibility, and common plotting taboos.
---

# Scientific Plotting for Meteorology

Use this skill to produce or review meteorological research figures. The goal is not decorative output; the goal is a figure that is physically interpretable, reproducible, publication-ready, and hard to misunderstand.

## Agent guidance

1. Identify the figure type, data source, variable, units, valid time, region, and any level or accumulation window. Choose projection and color scale before writing the script. Keep comparable panels on one scale and disclose interpolation, smoothing, or other processing.
2. Use a sequential scale for positive fields and a zero-centered diverging scale for anomalies. Label the variable and unit on the colorbar. For CJK text, call `from mplfonts import use_font` and `use_font("Noto Sans CJK SC")` before rendering.
3. For maps involving China, load the `cnmaps` skill and its `references/api-cheatsheet.md` and `references/plotting-patterns.md` before coding boundaries. Use cnmaps boundary data. A China place name in a time series or other non-map chart does not require cnmaps.
4. Choose one layout owner: `layout="compressed"` is useful for regular fixed-aspect Cartopy grids; `layout="constrained"` can suit complex GridSpec or mixed panels. Do not combine either with `tight_layout()` or `subplots_adjust()`. PlateCarree geographic ticks and projected-map grid labels need different patterns; see `references/layout-and-export.md` and `references/meteorological-maps.md` when relevant.
5. Save the reproducible script under `scripts/tmp/` and the complete figure under `figures/`. Render once and inspect the exported image with `analyze_image` when available. Fix one concrete scientific, labeling, or clipping defect, then export and inspect once more. Stop after that and report any remaining limitation. Do not iterate for cosmetic whitespace or a target byte count.
6. Keep the original figure at readable quality. If a small chat preview is needed, use `prepare_image_for_vision` or a separate preview copy; never degrade the deliverable to satisfy a 500 KB threshold. For publication output, follow `references/publication-quality.md`.
7. Read only the relevant detail: pressure cross-sections → `references/vertical-cross-sections.md`; vector cross-sections → `references/cross-section-wind-vectors.md`; barbs → `references/wind-barbs.md`; annotated time series → `references/time-series.md`; multi-panel maps → `references/layout-and-export.md`.

## Default workflow

1. Identify the figure type and the scientific message.
   - Map, time series, vertical cross-section, station interpolation, radar/satellite image, verification plot, multi-panel comparison, or animation.
   - Decide the primary variable first. Use filled shading for the primary scalar field when appropriate; use contours, vectors, station markers, masks, or annotations only as supporting layers.
   - For any pressure-coordinate vertical cross-section, read
     `references/vertical-cross-sections.md` before planning data requirements or
     writing plotting code. Its axis, terrain, vector-component, and validation
     rules are mandatory.
   - When the section contains arrows, quivers, barbs, streamlines, vertical
     velocity, or circulation, also read
     `references/cross-section-wind-vectors.md`. Decide whether the vectors show
     horizontal wind, schematic in-plane circulation, or physical trajectories
     before selecting components or scaling.
   - For a time series or scatter plot with maximum/minimum/event labels, read
     `references/time-series.md` before placing annotations. Extreme-point
     labels must use edge-aware placement and rendered checks for canvas,
     legend, and plotted-data collisions rather than a fixed positive `xytext`
     offset.

2. Check the minimum metadata before drawing.
   - Inspect data metadata: variable name, dimensions, coordinates, units, valid time, level, region, and missing values.
   - Variable name and level, such as 2 m temperature, 850 hPa wind, 500 hPa geopotential height, composite reflectivity.
   - Unit, valid time, time zone, forecast initialization time and lead time when applicable.
   - Accumulation or averaging window for precipitation and other accumulated/averaged fields.
   - Dataset name, version/resolution when relevant, and processing method such as interpolation, smoothing, anomaly baseline, or significance test.

3. Choose the visual grammar by variable type.
   - Sequential positive fields: precipitation, wind speed, humidity, reflectivity, aerosol/PM, CAPE.
   - Diverging fields: anomalies, biases, vertical velocity, vorticity, divergence, standardized indices.
   - Categorical fields: land cover, weather type, clusters, precipitation phase.
   - Cyclic fields: wind direction, phase, seasonal angle.
   Read `references/colormaps-and-units.md` when choosing colormaps or units.
   - For any Matplotlib wind-barb plot, read `references/wind-barbs.md` before
     calling `barbs`. Confirm the component units and choose either domestic
     Chinese `m s-1` increments or knot-style increments explicitly.

4. If the figure is a map, choose boundaries and projection deliberately.
   - For China-region maps, prefer `cnmaps` for China borders, provincial/city boundaries, clipping, masking, and whitening. Do not use `cartopy.feature.BORDERS` or `cartopy.feature.COASTLINE` for China's national boundary.
   - Use Cartopy for projections and coordinate transformations. Always pass the correct data CRS/transform when plotting longitude-latitude data.
   - Read `references/china-borders.md` and `references/meteorological-maps.md` for map rules.

5. Keep the figure clean.
   - Use consistent color limits across panels, model comparisons, time sequences, and animations.
   - Thin or subset wind vectors; always include a vector key when arrows are quantitative.
   - Use contours sparingly and label only useful contour levels.
   - Avoid overplotting significance dots, station names, province lines, and decorative basemaps.

6. Make the output publication-safe.
   - Design at the final intended size, not just screen size. For a normal in-app result, prefer a compact, readable figure over a large source-resolution image.
   - Prefer vector output for line/cartographic figures when the requested deliverable supports it. For default raster output, use a modest canvas and DPI; do not choose high-DPI export merely as a publication-quality default.
   - Use readable fonts, line widths, labels, colorbar ticks, and panel labels.
   - For scientific units with exponents or indices, use Matplotlib mathtext
     (`$10^{-6}$`, `$m^2$`, `$kg^{-1}$`) rather than Unicode superscript
     glyphs such as `⁻`, `¹`, `²`, or `⁶`. This avoids missing-glyph boxes in
     CJK-capable display fonts. For example:
     `cbar.set_label(r"PVU ($10^{-6}\,\mathrm{K\,m^2\,kg^{-1}\,s^{-1}}$)")`.
     In a raw string (`r"..."`), MathText commands use one backslash. In an
     ordinary Python string, escape those backslashes:
     `"PVU ($10^{-6}\\,\\mathrm{K\\,m^2\\,kg^{-1}\\,s^{-1}}$)"`.
     Never combine a raw-string prefix with doubled MathText backslashes, and
     keep the complete unit expression inside one balanced `$...$` pair.
     MathText commands outside `$...$` are not interpreted: a label such as
     `r"PVU (10^{-6}\,\mathrm{K\,m^2})"` will print the commands literally.
     Do not reconstruct the PVU unit from memory or edit its tokens piecemeal;
     copy this exact constant into the plotting script:
     `PVU_LABEL = r"PVU ($10^{-6}\,\mathrm{K\,m^2\,kg^{-1}\,s^{-1}}$)"`.
     Before running the script, verify `PVU_LABEL` is passed directly to
     `cbar.set_label(...)`; never write `0^{-6}`, omit either `$`, or wrap only
     part of the unit in math mode.
   - Read `references/publication-quality.md` before final export only when the user explicitly requests a paper, print, publication, high-resolution, or other large-format deliverable.

7. Preserve reproducibility.
   - Record data source, time window, units, projection, interpolation/smoothing method, colormap, color levels, masks, and any manual annotations.
   - Do not silently smooth, clip, interpolate, normalize, or change units.
   - Read `references/reproducibility.md` when the figure will support a paper, report, or product decision.

8. Save and report outputs correctly.
   - Save temporary plotting scripts under `scripts/tmp/`.
   - Save generated figures under `figures/`.
   - Start with a readable canvas and raster DPI suited to the actual deliverable, with a layout engine such as `layout="compressed"`/`layout="constrained"`. Do not enlarge the canvas just to fill screen space or use `bbox_inches="tight"` as a substitute for layout.
   - A 500 KB target may be used for a separate chat preview, never as a hard cap on the original scientific figure. If vision needs a smaller image, use `prepare_image_for_vision` or a separate preview copy.
   - Reduce output size only by scaling the **entire figure** proportionally, lowering DPI, or using lossless/format compression. Never crop scientific content to meet the size limit, save only a colorbar axis, or remove the main map/data axes. **Never pass the string `bbox_inches="tight"` for a Cartopy map**, even when one exported image appears correct: Cartopy transforms, colorbars, and inset axes can produce an incomplete tight bounding box.
   - First solve whitespace with the correct geographic extent, an aspect-matched `figsize`, `layout="compressed"`/`layout="constrained"`, and layout-aware title/colorbar placement. If those are correct but the exported file still has uniform **outer canvas** whitespace, use only the safe post-render fallbacks in `references/layout-and-export.md`: alpha-channel autocrop for PNG/WebP, or a manually computed fixed `Bbox` for PDF/SVG. Run the rendered-geometry checks before cropping and visually inspect the final cropped file. Do not use autocrop to hide a wrong extent, oversized axes allocation, or internal panel gaps.
   - Before reporting a figure, visually inspect the exported file (not only the plotting window) and verify that it includes the primary data panel, title/metadata where applicable, and colorbar/legend. If the exported dimensions or aspect ratio are implausible for the requested figure, treat it as a failed export and regenerate it rather than delivering it.
   - Honor a user request for "高清", "高分辨率", "大图", "出版", "印刷", "publication", "print", or an explicit pixel/DPI target. State the resulting file size when useful.
   - When reporting a generated image, use Markdown image syntax such as `![description](figures/name.png)`.
   - For every multi-panel Cartopy figure, read
     `references/layout-and-export.md` before writing the plotting script. It
     defines the single-layout-owner rule, title/colorbar placement, rendered
     geometry checks, and a compact export pattern. These checks are mandatory
     when the user reports clipped titles, excessive top/bottom whitespace, or
     ineffective spacing changes.

## Safe compact export for Cartopy maps

For a normal single-panel Cartopy map, use this pattern instead of manually tuning
`subplots_adjust`, creating a separate colorbar axes, or passing
`bbox_inches="tight"` to `savefig`. `layout="compressed"` automatically reduces
unnecessary whitespace around fixed-aspect map axes while `Figure.colorbar` reserves
space for the colorbar as part of the same layout.

Do not use `fig.add_axes([left, bottom, width, height], projection=...)` for the
main fixed-aspect map unless a deliberately custom editorial layout is required. A
manually allocated axes box that is taller or narrower than the map extent leaves a
large unused white area after Cartopy preserves the map aspect ratio. Instead, use
`plt.subplots(..., layout="compressed")` and choose a canvas whose rough aspect
ratio matches the requested geographic extent. For example, China’s approximate
64° × 40° PlateCarree extent works well with `figsize=(8, 5.4)`; put a figure-wide
title in `fig.suptitle(...)` so the layout engine reserves space for it.

```python
import cartopy.crs as ccrs
from cartopy.util import add_cyclic_point
import matplotlib.pyplot as plt

# `lon`, `lat`, and `field` are the already validated data coordinates and 2-D field.
field_wrapped, lon_wrapped = add_cyclic_point(field, coord=lon)
fig, ax = plt.subplots(
    figsize=(7, 4),
    layout="compressed",
    subplot_kw={"projection": ccrs.PlateCarree(central_longitude=180)},
)

mesh = ax.pcolormesh(
    lon_wrapped, lat, field_wrapped,
    transform=ccrs.PlateCarree(),
    cmap="RdBu_r", vmin=-40, vmax=40,
)
ax.set_global()
ax.coastlines(linewidth=0.5)
ax.set_title("ERA5 daily mean 2 m temperature — 2026-07-01", fontsize=12)

cbar = fig.colorbar(
    mesh, ax=ax, orientation="horizontal", pad=0.05, shrink=0.88, aspect=28,
)
cbar.set_label("2 m temperature (°C)")

# Deliberately omit bbox_inches="tight".  The layout engine owns the spacing.
fig.savefig("figures/era5_t2m.png", dpi=120, facecolor="white")
plt.close(fig)
```

For a **vertical** colorbar, do not use a rotated unit-only axis label such as
`cbar.set_label("°C")`. Put the unit at the top in normal horizontal text instead:

```python
cbar = fig.colorbar(mesh, ax=ax, orientation="vertical")
cbar.ax.set_title("°C", fontsize=10, pad=5)
```

For a horizontal colorbar, keep the descriptive variable-and-unit label below the
bar with `cbar.set_label("2 m temperature (°C)")`.

For a regular fixed-aspect Cartopy grid, prefer `layout="compressed"`; for a
complex GridSpec or mixed panel types, `layout="constrained"` may fit better.
Do not combine either layout mode with `tight_layout()` or with
`bbox_inches="tight"`. Keep the original export readable regardless of file size.

If correct layout still leaves a uniform blank strip outside the rendered artists,
do not keep guessing `figsize` indefinitely. Follow the post-render safe-crop
decision tree in `references/layout-and-export.md`. The literal string
`bbox_inches="tight"` remains forbidden; a validated fixed `Bbox` and transparent
outer-canvas raster crop are different, controlled operations.

## Regional PlateCarree map ticks and gridlines

For a rectangular regional map on a plain `ccrs.PlateCarree()` axes, prefer
normal GeoAxes ticks over `gridlines(draw_labels=True)`. Cartopy Gridliner labels
can sit outside the layout bounds and be clipped at export. Standard ticks are
known to Matplotlib's layout engine, so `layout="compressed"` or
`layout="constrained"` reserves their space automatically.

This is a required metadata rule, not an optional decoration: a geographic map
must retain visible longitude and latitude labels. In a multi-panel grid, show
latitude labels on every outer-left panel and longitude labels on every
bottom-row panel. Never remove all latitude labels merely to make the layout
more compact.

```python
import numpy as np
import cartopy.crs as ccrs
from cartopy.mpl.ticker import LatitudeFormatter, LongitudeFormatter

extent = (115.3, 117.8, 39.3, 41.4)  # west, east, south, north
ax.set_extent(extent, crs=ccrs.PlateCarree())

lon_ticks = np.arange(np.ceil(extent[0] * 2) / 2, extent[1] + 0.01, 0.5)
lat_ticks = np.arange(np.ceil(extent[2] * 10) / 10, extent[3] + 0.01, 0.3)
ax.set_xticks(lon_ticks, crs=ccrs.PlateCarree())
ax.set_yticks(lat_ticks, crs=ccrs.PlateCarree())
ax.xaxis.set_major_formatter(LongitudeFormatter(number_format=".1f"))
ax.yaxis.set_major_formatter(LatitudeFormatter(number_format=".1f"))
ax.tick_params(axis="both", labelsize=8, pad=3)

# Draw a subdued dashed grid without asking Gridliner to draw duplicate labels.
ax.gridlines(
    xlocs=lon_ticks, ylocs=lat_ticks, draw_labels=False,
    linestyle="--", linewidth=0.4, color="grey", alpha=0.55,
)
```

Choose tick intervals appropriate to the geographic extent. Keep gridlines light
and visually subordinate by default; use a light dashed style (`"--"`) so they do
not compete with geographic boundaries or data contours. Do **not** use this tick
pattern for Lambert, polar, Robinson, or other
non-rectangular projections: use Cartopy Gridliner for those, and keep labels on
the requested sides with a layout-aware margin.

## Aspect-aware multi-panel map layouts

Do not give every regional map a square panel. A narrow/tall province (for
example Shaanxi) placed in a wide square grid wastes most of every axes; a wide
national map has the inverse problem. For `PlateCarree`, derive the Figure size
from the extent, panel grid, and the actual title/colorbar reservations. Do not
blindly change `figsize`, `pad`, or `panel_height` and claim that whitespace is
reduced: use this calculation first.

Before using the calculation below, follow the single-layout-owner rule in
`references/layout-and-export.md`: create the Figure with exactly one layout
engine; do not subsequently call `subplots_adjust`, `tight_layout`, or put a
suptitle outside the canvas with `y > 1`. Pass layout options only to Figure
creation (`plt.subplots`/`plt.figure`), never to `savefig`.

```python
import numpy as np

def compact_platecarree_grid_size(
    extent, nrows, ncols, *, panel_height=2.4,
    has_suptitle=True, shared_horizontal_colorbar=True,
):
    west, east, south, north = extent
    # Longitude is shorter in rendered distance away from the equator.
    map_aspect = (
        (east - west) * np.cos(np.deg2rad((south + north) / 2))
        / (north - south)
    )
    # Reserve only the components that are actually drawn; this controls the
    # figure's outer top/bottom whitespace without relying on tight-bbox export.
    title_reserve = 0.24 if has_suptitle else 0.05
    colorbar_reserve = 0.30 if shared_horizontal_colorbar else 0.05
    outer_reserve = 0.08
    width = np.clip(ncols * panel_height * map_aspect + 0.35, 4.2, 11.0)
    height = nrows * panel_height + title_reserve + colorbar_reserve + outer_reserve
    return float(width), float(height)

figsize = compact_platecarree_grid_size(
    extent, nrows=3, ncols=3,
    has_suptitle=True, shared_horizontal_colorbar=True,
)
fig, axes = plt.subplots(
    3, 3, figsize=figsize, layout="compressed",
    subplot_kw={"projection": ccrs.PlateCarree()},
)
```

When a multi-panel figure has both `fig.suptitle(...)` and per-panel
`ax.set_title(...)`, do not run a second helper that changes Figure height or
axes positions after compressed layout. Such a helper can read non-finite
intermediate Cartopy geometry and make the canvas infinitely tall. Add all
artists, draw once, then run `assert_artists_inside_canvas(fig)` from
`references/layout-and-export.md`. It checks both canvas bounds and the gap
between the suptitle and top-row panel titles.

Never set the title/colorbar reservation to zero merely to reduce whitespace.
If the acceptance guard raises, change the initial compact `figsize` or font
sizes and render again rather than deleting titles, manually moving axes, or
using `bbox_inches="tight"`.

Set `extent` from the study boundary's bounds plus modest geographic padding
(normally 3–6% of longitude and latitude spans) before calling this function;
do not keep a previous province/country extent when the target region changes.
Use `layout="compressed"` with this pattern: it removes unused layout space
around fixed-aspect GeoAxes without cropping pixels. Then make geographic tick
density match each panel's available width/height. Use a 1/2/2.5/5 × 10^n
"nice" step, targeting at most about four longitude intervals and six latitude
intervals for a compact 3×3 grid. Do not retain dense half-degree labels merely
because they fit a single-panel version of the same map.

This automates **outer Figure margins**, not empty areas inside a geographic axes.
An oblique/narrow shape such as Japan can legitimately leave blank corners inside
its rectangular lon/lat extent. If that dominates the exported figure, explain
the cause and offer a user-visible choice of a different projection/extent; do
not distort coordinates, crop geographic content, or falsely report that a
`pad` adjustment fixed it.

This is an estimate, not a replacement for visual verification: inspect the
export, compare its pixel dimensions and visible outer margins with the prior
export, and report only a verified change. Increase the minimum canvas width or
reduce tick density if labels overlap. Do not restore a wide square canvas just
to avoid calculating the map aspect; it reintroduces the original empty-space
problem.

For a fixed-aspect Cartopy multi-panel map, do **not** expect
`layout="constrained"` plus `fig.get_layout_engine().set(wspace=..., hspace=...)`
to force the visible gaps to those percentages. Constrained layout treats them
as lower bounds, then the map aspect, titles, ticks, and colorbar may expand the
gaps again. Use `layout="compressed"`; only then apply modest spacing preferences
if needed:

```python
fig.get_layout_engine().set(wspace=0.02, hspace=0.04)
```

Treat these as preferences, not exact visual percentages. Re-open the exported
PNG to verify the visible gap; never claim an exact gap reduction without that
check.

## Multi-panel China comparison maps

For a China map with two or more comparable panels (for example different times
or ensemble members), apply the complete map contract to **every** panel rather
than treating the first panel as the only complete map:

- Use one shared `levels`, `cmap`, and `norm` for all panels, and pass
  `extend="both"` to **every** `contourf` call. Build the shared colorbar from
  that same mappable so its two endpoint triangles describe the actual panel
  encoding.
- Draw the applicable national, provincial, or study-area boundary and a light
  dashed longitude/latitude grid in every panel. Use ordinary GeoAxes ticks and
  show labels only on the outer left and bottom edges, so interior panels retain
  the grid without duplicated labels. Apply `LongitudeFormatter` and
  `LatitudeFormatter`; `set_xticks`/`set_yticks` alone otherwise leave plain
  numbers rather than geographic labels.
- Add a bottom-right South China Sea inset to **every panel only when the main
  view is a national China map**. Clip those national main panels to the
  mainland polygon, but render each inset with the full China polygon collection
  so South China Sea islands are not lost. Do **not** add a South China Sea inset
  to a provincial, city, or other regional map such as Shaanxi.
- When an inset is present, the main national panel must use a compact extent
  such as `[73, 136, 18, 54]`; the inset alone uses the South China Sea extent
  such as `[105, 123, 2, 25]`. Never show the remote South China Sea islands in
  both places and never derive the main-panel extent from the full China
  multi-polygon bounds. That duplication is cartographically redundant and
  makes the mainland map smaller while increasing whitespace.
- Use `layout="compressed"` for the fixed-aspect panel grid and one external shared
  colorbar; do not add a separate colorbar for each panel.

```python
# Shared setup before the loop.
extent, scs_extent = [73, 136, 18, 54], [105, 123, 2, 25]
lon_ticks, lat_ticks = np.arange(80, 136, 10), np.arange(20, 56, 10)

for index, (ax, field) in enumerate(zip(axes.flat, fields)):
    row, col = divmod(index, 2)
    cs = ax.contourf(
        lon, lat, field, levels=levels, cmap=cmap, norm=norm,
        extend="both", transform=ccrs.PlateCarree(),
    )
    clip_contours_by_map(cs, china_mainland, ax=ax)
    draw_map(china_mainland, ax=ax, color="#333333", linewidth=0.6)
    ax.set_extent(extent, crs=ccrs.PlateCarree())
    ax.set_xticks(lon_ticks, crs=ccrs.PlateCarree())
    ax.set_yticks(lat_ticks, crs=ccrs.PlateCarree())
    ax.xaxis.set_major_formatter(LongitudeFormatter())
    ax.yaxis.set_major_formatter(LatitudeFormatter())
    ax.tick_params(axis="x", labelbottom=(row == 1), labelsize=7)
    ax.tick_params(axis="y", labelleft=(col == 0), labelsize=7)
    ax.gridlines(xlocs=lon_ticks, ylocs=lat_ticks, draw_labels=False,
                 linestyle="--", linewidth=0.3, color="grey", alpha=0.4)

# National-China maps only: repeat the inset for every panel.
if is_national_china_map:
    for ax, field in zip(axes.flat, fields):
        inset = ax.inset_axes([0.78, 0.05, 0.20, 0.25], transform=ax.transAxes,
                              projection=ccrs.PlateCarree())
        inset_cs = inset.contourf(
            lon, lat, field, levels=levels, cmap=cmap, norm=norm,
            extend="both", transform=ccrs.PlateCarree(),
        )
        clip_contours_by_map(inset_cs, china_full, ax=inset)
        draw_maps(china_full, ax=inset, color="#333333", linewidth=0.35)
        inset.set_extent(scs_extent, crs=ccrs.PlateCarree())
        inset.set_xticks([]); inset.set_yticks([])

fig.colorbar(cs, ax=axes.ravel().tolist(), orientation="horizontal", ticks=levels)
```

## Reference loading guide

Read only the files needed for the task:

- `references/china-borders.md`: China boundaries, cnmaps, South China Sea/inset considerations, clipping/masking rules.
- `references/colormaps-and-units.md`: Matplotlib colormaps, cmaps/NCL colormaps, variable-specific color logic, units, colorbar conventions.
- `references/meteorological-maps.md`: Map projections, map layers, contours, wind vectors, precipitation/radar/satellite/cross-section rules.
- `references/vertical-cross-sections.md`: **Required for every atmospheric vertical cross-section.** Pressure-axis orientation, terrain conversion and masking, along-section/vertical wind components, interpolation, and image-level acceptance checks.
- `references/cross-section-wind-vectors.md`: **Required when a vertical cross-section contains wind arrows, quivers, barbs, streamlines, or vertical velocity.** Along-section projection, ERA5 omega versus height velocity, vertical exaggeration, normalization, thinning, robust scaling, and truthful vector-key labels.
- `references/wind-barbs.md`: **Required for every Matplotlib wind-barb plot.** Unit-aware `barb_increments`, Chinese public-meteorological `2/4/20 m s-1` encoding, international `5/10/50 kt` encoding, rounding, flag limitations, and synthetic-speed validation.
- `examples/era5-meridional-pressure-cross-section.py`: **Priority read and starting point** for an ERA5 north-south pressure cross-section with surface-geopotential terrain, underground masking, wind-speed shading, and explicitly labeled horizontal `(u, v)` barbs. Adapt its paths, longitude, time, variables, and labels; retain its coordinate-based selection, order-independent terrain interpolation, axis assertion, and acceptance diagnostics.
- `Safe compact export for Cartopy maps` above: Default pattern for a one-panel Cartopy map with a colorbar. Use it before introducing manual axes positions or any tight-bbox export.
- `Regional PlateCarree map ticks and gridlines` above: Default label/grid pattern for a rectangular local PlateCarree map; use it to avoid clipped Gridliner labels.
- `references/publication-quality.md`: Multi-panel layout, journal/export quality, typography, CJK font handling with mplfonts, labels, colorbar placement, accessibility.
- `references/layout-and-export.md`: **Required for every multi-panel Cartopy
  figure and every clipped-title/excess-whitespace correction.** Use one layout
  owner, reserve titles and colorbars through layout-aware artists, validate
  rendered artist bounds before export, and inspect the exported raster.
- `references/reproducibility.md`: Metadata, processing disclosure, station interpolation, anomaly baselines, significance marking, QA checklist.
- `references/time-series.md`: Time-axis rules, UTC/local time, accumulations, dual axes, verification plots, ensemble/uncertainty display.
- `references/cjk-fonts.md`: Quick reference for CJK tofu-box font rendering issues with mplfonts.
- `examples/china-clipped-with-scs-inset.py`: **Priority read** when the user's requirements match **both** (1) a filled-contour scalar field clipped to China's boundary, and (2) a South China Sea inset rendered as an overlay inside the main map. This example uses **PlateCarree** projection (simple rectangular lon-lat). Read this example first, then adapt data loading, levels, colormap, extent, and inset position.
- `examples/china-lambert-scs-inset-right.py`: **Lambert Conformal** variant with the SCS inset in the **bottom-right** corner. Use when the user prefers a Lambert projection (curved boundaries, more professional cartographic look) or explicitly asks for Lambert.
- `examples/china-lambert-scs-inset-left.py`: Same Lambert pattern but with the SCS inset in the **bottom-left** corner. Use when the bottom-right area contains important data that shouldn't be obscured (e.g. precipitation over Fujian/Taiwan). The bottom-left covers the sparsely-populated Tibetan Plateau region.

## Hard rules (MANDATORY — violation is an error)

These rules must be followed without exception. They override convenience, aesthetics, and any other defaults.

- **Colormaps**: Never use `jet`/rainbow for continuous scalar fields. Positive fields (precip, wind speed, humidity, reflectivity) must use sequential colormaps. Anomaly/bias fields must use diverging colormaps centered at zero.
- **Metadata**: Never omit units, valid time, variable level, or accumulation window when they matter scientifically. Colorbar must always be labeled with variable name and unit.
- **Locked scales**: Never let each panel in a comparison auto-scale independently unless the user explicitly asks and the caption clearly says so. Same variable = same `vmin`/`vmax` across panels, times, and models.
- **Finite contour levels**: When using `contourf` with an explicitly finite `levels` range, pass `extend="both"` by default so valid values below/above the selected display range use the end colors rather than becoming blank. The colorbar must show the corresponding end triangles. Do not use `extend` to conceal NaN/masked data: investigate and represent genuine missing data separately.
- **Preview size**: A 500 KB target applies only to an optional chat/vision preview. Preserve the readable original figure.
- **Figure-integrity check**: A colorbar, legend, title, or blank canvas alone is never a valid scientific figure. Before delivering an export, confirm that the primary data axes were saved and occupy a meaningful part of the image. Never trade away the data panel to satisfy the default file-size cap.
- **Vertical cross-section check**: For pressure coordinates, high pressure must
  appear at the bottom and low pressure at the top. Underground cells must be
  masked and the same terrain region must be visibly filled. Never plot `(u, v)`
  as if it were an in-plane latitude/longitude-versus-pressure circulation
  vector; use along-section wind plus vertical velocity, or label horizontal
  wind barbs explicitly.
- **Cross-section vector disclosure**: Never present a vertically exaggerated,
  normalized, clipped, or screen-coordinate quiver as a true airflow angle or
  true two-dimensional speed. State the vertical variable, units, multiplier,
  normalization/clipping, and the meaning of the reference arrow on the figure.
- **Multi-panel title clearance**: Inspect the rendered export for title collisions and clipped artists. Use `assert_artists_inside_canvas(fig)` from `references/layout-and-export.md` when the figure uses that helper; do not add an undefined call merely to satisfy a textual check.
- Never use a diverging colormap for a strictly positive scalar field unless it encodes a meaningful threshold.
- Never use a sequential colormap for an anomaly/bias field that needs positive/negative symmetry.
- Never use dense wind arrows or dense significance dots that cover the actual meteorological field.
- **Wind-barb units**: Never pass `m s-1` components to Matplotlib's default
  `barbs` and describe the result as Chinese domestic wind-barb notation.
  Matplotlib does not infer or convert units. Set the required increments
  explicitly, or convert both vector components to knots and label them.
- Never present interpolated station fields, reanalysis fields, smoothed fields, or AI-generated/infilled radar frames as raw observations. All processing methods must be disclosed.
- **China boundaries (HIGHEST PRIORITY)**: Whenever a map involves China's territory (Mainland, Taiwan, Hong Kong, Macau, South China Sea islands, etc.), you MUST use `cnmaps` for boundary data. Using `cartopy.feature.BORDERS`, `cartopy.feature.COASTLINE`, or any NaturalEarth-based global boundary for China's border is FORBIDDEN. Before writing any plotting code for a China-involved map, you MUST first read `skills/builtin/cnmaps/references/api-cheatsheet.md` and `skills/builtin/cnmaps/references/plotting-patterns.md`. This rule applies even if the user does not explicitly mention `cnmaps` or China boundaries.
- **Global fill map seam**: Whenever plotting a global `contourf` or `pcolormesh` map where longitude spans 0°–360° (or -180°–180°) with `ax.set_global()`, you MUST call `cartopy.util.add_cyclic_point` to close the longitude seam. A white-line gap at the 0°/360° meridian is unacceptable for publication-quality output. See `references/meteorological-maps.md` for the exact usage pattern.
- **Display domain must match the clip domain**: Never use a broad regional extent (for example East Asia) while clipping the plotted field to one country (for example China), unless the user explicitly requests blank surroundings. Regional synoptic fields remain un-clipped with administrative boundaries drawn as overlays. Country-only thematic maps may be clipped, but the axes extent must then fit that country. See `references/meteorological-maps.md`.
- **Map canvas must match rendered aspect**: After the domain and clip are correct, do not estimate the final canvas solely with `longitude_span / latitude_span` or add arbitrary side-width constants. Cartopy's fixed-aspect GeoAxes, the actual `set_extent(...)`, and layout-aware decorations determine the rendered panel size. Make at most one Figure-width correction when the exported main map is clearly too small; do not chase a fixed occupancy percentage. See `references/meteorological-maps.md`.
- Never use decorative basemaps, 3D effects, glow effects, or busy backgrounds for scientific defaults.

## Python ecosystem defaults

- Use Matplotlib as the default plotting backend.
- For every Matplotlib figure containing Chinese/Japanese/Korean text, explicitly call `from mplfonts import use_font` and `use_font("Noto Sans CJK SC")` before rendering. Do not rely only on a previous `mplfonts init`, manually selected system font paths, or ad-hoc `font.sans-serif` lists.
- Use Cartopy for map projections and geospatial axes.
- Use `cnmaps` by default for China-region administrative boundaries and map clipping/whitening.
- Support Matplotlib built-in colormaps and `cmaps` for NCL-style meteorological colormaps.
- Use xarray/netCDF-aware workflows when handling gridded meteorological datasets.
- Prefer explicit `levels`, `vmin`, `vmax`, `norm`, and colorbar labels over hidden automatic scaling.

## Output expectations

When generating code, include the parts needed to make the plot scientifically complete:

- Data variable selection and unit conversion if needed.
- Fixed levels/color limits appropriate to the variable.
- Projection and transform declarations for maps.
- Boundary layers and optional clipping/masking.
- Colorbar label with units.
- Title or caption-ready metadata including valid time and level.
- Export settings suitable for papers or reports.

When reviewing a figure or code, report issues in this order:

1. Scientific correctness and metadata omissions.
2. Map/projection/boundary problems.
3. Colormap and colorbar problems.
4. Overplotting and visual hierarchy problems.
5. Publication/export/reproducibility problems.
