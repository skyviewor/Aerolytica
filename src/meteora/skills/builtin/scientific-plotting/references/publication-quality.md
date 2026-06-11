# Publication Quality

Use this reference before exporting figures for papers, reports, slides, documentation, or public product pages.

## Design for final size

Do not judge readability only on a large monitor. Design for the final output size.

Typical targets:

- Single-column paper figure: roughly 8-9 cm wide.
- Double-column paper figure: roughly 17-18 cm wide.
- Slides: readable from distance, fewer labels, stronger hierarchy.
- Web product figure: responsive layout and readable colorbar/legend.

## Typography

Rules:

- Use consistent fonts.
- Keep final label size readable, usually not below 7-8 pt in papers.
- Use proper superscripts/subscripts for units.
- Ensure Chinese characters and minus signs render correctly.
- Avoid mixing many font families.

## Line widths and markers

Rules:

- Do not use hairline boundaries that disappear after export.
- Do not use overly thick administrative boundaries.
- Keep contour lines readable but subordinate to filled data when data are primary.
- Use marker sizes that remain visible after downscaling.

## Multi-panel figures

Rules:

- Use consistent extent, projection, colormap, and levels for comparable panels.
- Use one shared colorbar for same-variable panels when possible.
- Use panel labels `(a)`, `(b)`, `(c)` consistently.
- Avoid repeating every longitude/latitude label on every panel.
- Keep subplot spacing tight but not crowded.

## Colorbar and legends

Rules:

- Colorbar label must include variable and unit.
- Tick intervals should be readable and meaningful.
- Use extend arrows or explicit labels for clipped extremes.
- Keep legends outside the core meteorological signal when possible.
- Do not let legends cover storm centers, rain maxima, frontal zones, or key stations.

## Accessibility

Rules:

- Avoid relying on red/green contrast alone.
- Use line styles, markers, hatches, or labels in addition to color when distinction is critical.
- Check whether the plot remains interpretable in grayscale for important line charts.
- Avoid rainbow maps for continuous quantitative fields unless required by a domain convention and justified.

## Export

Recommended defaults:

- Vector: PDF/SVG for contour/line/map-boundary-heavy figures.
- Raster: PNG/TIFF at 300-600 dpi for radar, satellite, dense rasters, or journal requirements.
- For quick review: at least 150 DPI; for publication-oriented output: at least 300 DPI.
- Save with tight bounding boxes only after checking that labels, colorbars, and inset maps are not clipped.
- Avoid screenshot-based figure creation.

## Caption readiness

The figure or caption should answer:

- What variable is shown?
- What level/height is shown?
- What time and time zone?
- What forecast initialization and lead time, if applicable?
- What accumulation/averaging window?
- What data source?
- What units?
- What processing was applied?

## CJK fonts in Matplotlib

When a figure contains Chinese, Japanese, or Korean text, treat font configuration as part of figure reproducibility. Matplotlib often fails to render CJK glyphs correctly out of the box, causing missing-glyph warnings, square boxes, or incorrect minus signs. For Python workflows, prefer `mplfonts` to manage Matplotlib fonts rather than relying on ad-hoc `rcParams` copied from a local machine.

Recommended setup:

```bash
pip install mplfonts
mplfonts init
```

`mplfonts init` configures Matplotlib's font cache/rc settings so CJK text can render normally. After initialization, keep plotting code simple and explicit. When a script needs a specific CJK font, set it near the top of the script:

```python
from mplfonts import use_font

use_font("Noto Sans CJK SC")
```

Use `mplfonts list` to inspect available fonts before choosing a font name. When a project requires a custom font, install a font file or a directory of fonts with:

```bash
mplfonts install --update /path/to/font_or_font_dir
```

For a custom Matplotlib configuration file, use `mplfonts updaterc <matplotlibrc path>` after arranging the preferred font order. Do not bundle or redistribute commercial font files inside a skill, package, paper repository, or generated artifact unless the license clearly permits it.

Practical rules:

- If the plot contains Chinese titles, labels, station names, province names, or map annotations, use `mplfonts` before rendering.
- Prefer open-source CJK fonts such as Noto Sans CJK SC, Noto Serif CJK SC, or Source Han families when available.
- Keep Chinese/English mixed typography consistent across panels.
- Verify that the minus sign in tick labels renders correctly; CJK font fixes often also need `axes.unicode_minus: False` or equivalent configuration.
- Export a test figure and inspect it after PDF/SVG/PNG export, not only inside a notebook.

Common CJK mistakes:

- Setting `font.sans-serif` to a font that exists only on the developer's machine.
- Ignoring Matplotlib missing-glyph warnings.
- Fixing Chinese labels but breaking the Unicode minus sign.
- Mixing several unrelated Chinese fonts in one multi-panel figure.
- Embedding restricted font files in shared code or artifacts.

## Common mistakes

- Figure looks good on screen but becomes unreadable when inserted into a paper.
- Colorbar numbers overlap or have too many decimals.
- Panel labels move around across subplots.
- Exported raster is low resolution.
- White/transparent map areas disappear on nonwhite page backgrounds.
