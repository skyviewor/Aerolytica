---
name: scientific-plotting
description: Use this skill when creating, reviewing, improving, or refactoring scientific figures, meteorological and atmospheric-science plots in Python, especially maps, contour/fill maps, precipitation, radar reflectivity, satellite imagery, wind vectors, anomaly fields, vertical cross-sections, station interpolation maps, time series, charts, visualizations, publication-quality graphics, 科研图, 画图, 绘图, 图表, 可视化, 气象地图, 等值线图, 时间序列图, 色标, 单位, or figures from data. It emphasizes physically meaningful variable choices, correct units and valid times, China map boundaries with cnmaps, Matplotlib/cmaps/NCL colormaps, CJK font handling with mplfonts, Cartopy projections, reproducibility, and common plotting taboos.
---

# Scientific Plotting for Meteorology

Use this skill to produce or review meteorological research figures. The goal is not decorative output; the goal is a figure that is physically interpretable, reproducible, publication-ready, and hard to misunderstand.

## Default workflow

1. Identify the figure type and the scientific message.
   - Map, time series, vertical cross-section, station interpolation, radar/satellite image, verification plot, multi-panel comparison, or animation.
   - Decide the primary variable first. Use filled shading for the primary scalar field when appropriate; use contours, vectors, station markers, masks, or annotations only as supporting layers.

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
   - Design at the final intended size, not just screen size.
   - Prefer vector output for line/cartographic figures and high-DPI raster output for image-like fields.
   - Use readable fonts, line widths, labels, colorbar ticks, and panel labels.
   - Read `references/publication-quality.md` before final export.

7. Preserve reproducibility.
   - Record data source, time window, units, projection, interpolation/smoothing method, colormap, color levels, masks, and any manual annotations.
   - Do not silently smooth, clip, interpolate, normalize, or change units.
   - Read `references/reproducibility.md` when the figure will support a paper, report, or product decision.

8. Save and report outputs correctly.
   - Save temporary plotting scripts under `scripts/tmp/`.
   - Save generated figures under `figures/`.
   - When reporting a generated image, use Markdown image syntax such as `![description](figures/name.png)`.

## Reference loading guide

Read only the files needed for the task:

- `references/china-borders.md`: China boundaries, cnmaps, South China Sea/inset considerations, clipping/masking rules.
- `references/colormaps-and-units.md`: Matplotlib colormaps, cmaps/NCL colormaps, variable-specific color logic, units, colorbar conventions.
- `references/meteorological-maps.md`: Map projections, map layers, contours, wind vectors, precipitation/radar/satellite/cross-section rules.
- `references/publication-quality.md`: Multi-panel layout, journal/export quality, typography, CJK font handling with mplfonts, labels, colorbar placement, accessibility.
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
- Never use a diverging colormap for a strictly positive scalar field unless it encodes a meaningful threshold.
- Never use a sequential colormap for an anomaly/bias field that needs positive/negative symmetry.
- Never use dense wind arrows or dense significance dots that cover the actual meteorological field.
- Never present interpolated station fields, reanalysis fields, smoothed fields, or AI-generated/infilled radar frames as raw observations. All processing methods must be disclosed.
- **China boundaries (HIGHEST PRIORITY)**: Whenever a map involves China's territory (Mainland, Taiwan, Hong Kong, Macau, South China Sea islands, etc.), you MUST use `cnmaps` for boundary data. Using `cartopy.feature.BORDERS`, `cartopy.feature.COASTLINE`, or any NaturalEarth-based global boundary for China's border is FORBIDDEN. Before writing any plotting code for a China-involved map, you MUST first read `skills/builtin/cnmaps/references/api-cheatsheet.md` and `skills/builtin/cnmaps/references/plotting-patterns.md`. This rule applies even if the user does not explicitly mention `cnmaps` or China boundaries.
- **Global fill map seam**: Whenever plotting a global `contourf` or `pcolormesh` map where longitude spans 0°–360° (or -180°–180°) with `ax.set_global()`, you MUST call `cartopy.util.add_cyclic_point` to close the longitude seam. A white-line gap at the 0°/360° meridian is unacceptable for publication-quality output. See `references/meteorological-maps.md` for the exact usage pattern.
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
