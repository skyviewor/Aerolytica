"""
China boundary-clipped filled-contour map with South China Sea inset.

Use this example when the user wants:
  1. A filled-contour scalar field (precip, temperature, anomaly, etc.)
     clipped to China's national boundary using cnmaps.
  2. A South China Sea inset rendered as a small overlay inside the
     main map (bottom-right corner).

Adapt the data loading block to your dataset, then tune levels,
colormap, extent, and the SCS inset position as needed.
"""

import numpy as np
import xarray as xr
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import cartopy.crs as ccrs
from cnmaps import clip_contours_by_map, draw_map, draw_maps, get_adm_maps
from mplfonts import use_font

use_font('Noto Sans CJK SC')

# ── 1. Load & prepare data ──
ds = xr.open_dataset('data/your_file.nc')
field = ds['your_var']

# Example: monthly sum, convert units (m → mm)
if 'expver' in field.dims:
    field = field.sel(expver=1).squeeze()
if 'number' in field.dims:
    field = field.sel(number=0).squeeze()
field = field.sum(dim='time') * 1000.0

field_crop = field.sel(latitude=slice(58, 8), longitude=slice(68, 148))

# ── 2. cnmaps boundaries ──
china_mainland = get_adm_maps(country='中国', level='国', record='first', only_polygon=True)
china_full     = get_adm_maps(country='中国', level='国', only_polygon=True)

# ── 3. Colormap & levels ──
levels = [0, 1, 10, 25, 50, 100, 150, 200, 300, 400, 500, 600, 800]
cmap_colors = [
    '#ffffff', '#d4eeff', '#a0d8ef', '#6fc7e1',
    '#3aafd2', '#1b85b8', '#1a5276',
    '#f4a460', '#e67e22', '#d35400',
    '#8b0000', '#4a0000', '#1a0000',
]
cmap = mcolors.ListedColormap(cmap_colors)
norm = mcolors.BoundaryNorm(levels, cmap.N, extend='max')
cb_ticks = [0, 10, 50, 100, 200, 400, 600, 800]

# ── 4. Figure & main map ──
fig = plt.figure(figsize=(10, 8), facecolor='white')
ax = fig.add_axes([0.06, 0.10, 0.68, 0.82], projection=ccrs.PlateCarree())

cs = ax.contourf(
    field_crop.longitude, field_crop.latitude, field_crop.values,
    levels=levels, cmap=cmap, norm=norm, transform=ccrs.PlateCarree(),
)
clip_contours_by_map(cs, china_mainland, ax=ax)
draw_map(china_mainland, ax=ax, color='#333333', linewidth=0.8)

ax.set_extent([72, 136, 15, 55], crs=ccrs.PlateCarree())
ax.set_title('Your Title Here', fontsize=14, fontweight='bold', pad=8)

gl = ax.gridlines(
    draw_labels=True, linewidth=0.3, color='gray', alpha=0.35,
    xlocs=np.arange(70, 141, 10), ylocs=np.arange(10, 56, 10),
)
gl.top_labels = False
gl.right_labels = False
gl.xlabel_style = {'size': 8}
gl.ylabel_style = {'size': 8}

# ── 5. Colorbar (auto-aligned with axes height) ──
cbar = fig.colorbar(cs, ax=ax, ticks=cb_ticks, fraction=0.022, pad=0.03)
cbar.set_label('mm', fontsize=10, labelpad=6)
cbar.ax.tick_params(labelsize=8, length=2, pad=2)

# ── 6. South China Sea inset ──
INSET_POS = [0.80, 0.08, 0.21, 0.28]   # [left, bottom, width, height] in ax coords
SCS_EXTENT = [105, 123, 2, 25]

ax_inset = ax.inset_axes(INSET_POS, transform=ax.transAxes,
                         projection=ccrs.PlateCarree())

cs_inset = ax_inset.contourf(
    field_crop.longitude, field_crop.latitude, field_crop.values,
    levels=levels, cmap=cmap, norm=norm, transform=ccrs.PlateCarree(),
)
clip_contours_by_map(cs_inset, china_full, ax=ax_inset)
draw_maps(china_full, ax=ax_inset, color='#333333', linewidth=0.5)

ax_inset.set_extent(SCS_EXTENT, crs=ccrs.PlateCarree())
ax_inset.set_xticks([])
ax_inset.set_yticks([])

for spine in ax_inset.spines.values():
    spine.set_linewidth(0.6)
    spine.set_color('#555555')

ax_inset.text(0.5, -0.10, 'South China Sea',
              transform=ax_inset.transAxes,
              ha='center', va='top', fontsize=8, color='#444444')

# ── 7. Save ──
plt.savefig('output.png', dpi=300, bbox_inches='tight', facecolor='white')
plt.close()
