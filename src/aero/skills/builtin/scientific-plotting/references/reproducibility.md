# Reproducibility and Scientific QA

Use this reference when a figure supports a paper, report, product claim, model comparison, or scientific decision.

## File organization

- Put generated plotting scripts under `scripts/tmp/`.
- Put generated figures under `figures/`.
- Use deterministic filenames that include dataset, variable, region or level, and date when practical.
- Keep source data in `data/`; do not duplicate large data into `figures/`.
- Report the generated figure path and the script path when useful.

## Required provenance

Record enough information to reproduce the plot:

- Dataset name and version/source.
- Variable names and derived-variable formulas.
- Time range, valid time, time zone, and forecast initialization/lead time.
- Spatial domain and projection.
- Interpolation, regridding, smoothing, masking, clipping, and unit conversion.
- Color levels and colormap.
- Statistical method and baseline period for anomalies.
- Significance test method and threshold if used.

## Reanalysis and truth language

Do not call reanalysis an absolute truth. Use terms such as:

- reference dataset
- reanalysis reference
- observation-based estimate
- benchmark

If a product is described as near-truth, state the source and limitations.

## Anomaly fields

Rules:

- State baseline period, such as 1991-2020.
- State whether anomaly is absolute, percentage, standardized, or normalized.
- Center diverging colormap at zero.
- Use symmetric limits when comparing positive and negative departures.

## Significance marking

Rules:

- State the test: t-test, Mann-Kendall, bootstrap, permutation, FDR, etc.
- Thin dots/hatching so the meteorological signal remains visible.
- Account for autocorrelation or effective sample size when relevant.
- Avoid overstating field significance from pointwise tests on spatially correlated data.

## Smoothing and filtering

Rules:

- Disclose smoothing/filtering type and scale/window.
- Do not smooth away extremes or convective structures without a reason.
- Keep raw and smoothed interpretations separate.

## Station interpolation QA

Rules:

- State station count and interpolation method.
- Mark stations when possible.
- Mask or flag low-support regions.
- Do not imply fine spatial resolution beyond station support.
- In sparse regions, returning no data or masked output can be more honest than showing a smooth invented field.

## Model comparison QA

Rules:

- Same domain, projection, variable, units, time, and color levels.
- Same regridding/interpolation method before difference maps.
- Same verification sample and thresholds.
- Show bias and error metrics, not correlation alone.

## Final QA checklist

Before finalizing, check:

- Variable, level, unit, time, and data source are visible or caption-ready.
- Projection and transform are correct.
- Boundaries align with data.
- Colormap matches variable semantics.
- Color levels are fixed for comparisons.
- Vectors/contours/significance markers are not too dense.
- Processing steps are disclosed.
- Export size and resolution are adequate.
