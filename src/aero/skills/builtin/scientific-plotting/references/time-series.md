# Time Series and Verification Plots

Use this reference for meteorological time series, station comparisons, model verification curves, ensemble plots, and accumulated variables.

## Time axis

Rules:

- State UTC, local time, or Beijing Time clearly.
- Use consistent tick formatting.
- Avoid overcrowded timestamps.
- For forecasts, distinguish initialization time, valid time, and lead time.
- For accumulated variables, show the accumulation interval.

## Variable display conventions

Common choices:

- Temperature, pressure, wind speed: line chart.
- Precipitation amount: bar chart or step-like accumulated curve.
- Accumulated precipitation: cumulative line with clear reset/window logic.
- Wind direction: special handling because it is circular.
- Visibility: consider log scale only if clearly labeled.

## Multiple variables

Rules:

- Avoid dual y-axes unless necessary.
- If using dual axes, label both units clearly and avoid implying false correlation.
- Prefer separate aligned panels when variables differ strongly.
- Use line styles and markers, not color alone.

## Observations vs forecasts

Rules:

- Distinguish observed, analysis, nowcast, forecast, and reanalysis.
- Use consistent line style across figures.
- Include sample count or missing-data handling when relevant.
- Do not join long gaps without marking missing values.
- For averages, state the averaging region and whether missing values were skipped.

## Ensemble and uncertainty

Rules:

- Use median/mean plus percentile bands when showing ensembles.
- Keep uncertainty shading transparent and subordinate.
- State ensemble size and percentile range.

## Verification plots

Rules:

- Scatter plots should include 1:1 line.
- Dense scatter should use density/hexbin or transparency.
- Taylor diagrams should define standard deviation normalization and reference dataset.
- Boxplots must define whiskers and outliers.
- Skill scores should state baseline/reference.

## Common mistakes

- Mixing UTC and local time on the same figure.
- Not stating precipitation accumulation windows.
- Overusing dual y-axes.
- Hiding missing data by interpolation.
- Reporting correlation without bias/error context.
