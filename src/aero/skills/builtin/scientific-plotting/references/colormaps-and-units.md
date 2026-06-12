# Colormaps and Units

Use this reference when selecting colormaps, colorbar levels, units, labels, and variable-specific visual encodings.

## Colormap sources

Supported/default sources:

- Matplotlib built-in colormaps: stable, widely available, good default for scientific Python.
- `cmaps`: Python access to many NCL-style meteorological colormaps, useful for users migrating from NCL or reproducing traditional atmospheric-science figure styles.
- Project/built-in operational colormaps: best for precipitation, radar reflectivity, warning products, and other threshold-driven variables.
- User-defined colormaps: allow when required by a paper, project, agency, or product standard.

Do not treat every `cmaps`/NCL colormap as automatically scientifically ideal. Many are useful and familiar, but some have strong rainbow-like or perceptually uneven transitions. Select by variable semantics, not by appearance alone.

## Variable-to-colormap logic

### Sequential positive fields

Use sequential or ordered discrete colormaps for strictly positive variables:

- Precipitation amount/rate
- Wind speed
- Relative humidity
- Radar reflectivity
- CAPE/CIN magnitude fields
- Aerosol, PM2.5, visibility obstruction concentration
- Cloud water, water vapor content, total column water vapor

Rules:

- Small values should usually be light or transparent.
- Large values should be visually stronger.
- Use discrete thresholds when the variable has operational levels.
- Avoid symmetric red-blue maps for nonnegative variables unless a meaningful central threshold is being emphasized.

### Diverging fields

Use diverging colormaps for variables with meaningful positive and negative signs:

- Temperature anomaly
- Geopotential height anomaly
- Bias/error fields
- Standardized anomaly
- Vertical velocity
- Vorticity and divergence
- Moisture flux divergence
- Correlation/regression coefficients around zero

Rules:

- Center the normalization at zero.
- Use balanced color intensity on both sides.
- Use symmetric limits when scientific comparison requires symmetry.
- Make zero white or nearly neutral unless the project has a different standard.

### Categorical fields

Use qualitative/discrete colors for categories:

- Land cover
- Weather type
- Cloud type
- Precipitation phase
- Clusters/classes/regimes

Rules:

- Do not use continuous gradients for unordered categories.
- Provide a legend or categorical colorbar with names, not just numbers.

### Cyclic fields

Use cyclic colormaps for circular variables:

- Wind direction
- Phase angle
- Diurnal/annual phase

Rules:

- 0 and 360 degrees must meet smoothly.
- Avoid ordinary sequential maps that create false discontinuities at the wrap point.

## Variable-specific defaults

### Precipitation

Use discrete thresholds. For China 24-hour precipitation, common operational thresholds are approximately:

- 0.1, 10, 25, 50, 100, 250 mm

For scientific plots, thresholds may differ by accumulation window, climate, or region, but avoid arbitrary equal intervals when established thresholds are more interpretable.

Always state the accumulation window: 1 h, 3 h, 6 h, 24 h, event-total, monthly mean, etc.

### Radar reflectivity

Use dBZ-specific discrete levels, commonly 5 or 10 dBZ spacing depending on purpose. Clearly label product type:

- Base reflectivity
- Composite reflectivity
- CAPPI
- Mosaic
- Quality-controlled reflectivity
- Interpolated or nowcast frame

Do not smooth convective structures unless there is a clear reason and disclosure.

### Temperature

- Actual temperature: sequential or intuitive warm/cool scale.
- Temperature anomaly: diverging red/blue scale centered at zero.
- Highlight 0 °C, 35 °C, 37 °C, or 40 °C contours when relevant.

### Wind

- Wind speed: sequential color fill.
- Wind vector: arrows/barbs/streamlines overlaid with thinning.
- Always include vector key when arrows are quantitative.

### Geopotential height and pressure

Usually better as contours than filled color when serving as a synoptic background.

Common intervals:

- 500 hPa geopotential height: 40 or 60 gpm.
- 850 hPa geopotential height: 20 or 30 gpm.
- Sea level pressure: 2 or 4 hPa.

## Unit conventions

Use explicit units in labels and colorbars.

Common units:

- Temperature: °C or K
- Wind speed: m/s or m s^-1
- Precipitation amount: mm
- Precipitation rate: mm/h
- Pressure: hPa
- Geopotential height: gpm
- Relative humidity: %
- Specific humidity: g/kg or kg/kg
- Vertical velocity: Pa/s
- Vorticity/divergence: s^-1, often scaled as 10^-5 s^-1
- Radar reflectivity: dBZ
- Visibility: m or km

Do not silently convert units unless the conversion is simple, correct, and stated.

## Common mistakes

- Using `jet`/rainbow as a default for continuous scalar fields.
- Letting each panel choose its own color limits in a comparison.
- Using a non-centered norm for anomaly/bias fields.
- Hiding unit conversions in code without reflecting them in labels.
- Using too many colorbar ticks.
- Coloring zero precipitation as visually meaningful rain.
- Truncating extremes without labeling the top/bottom bin as greater-than/less-than.
