---
name: data-analyst
description: Analyze a dataset the user attached or pasted (CSV, JSON, XLSX, TSV, Parquet) and deliver one beautiful chart plus a one-page plain-English summary of insights. Use whenever the user wants statistics, patterns, comparisons, or visualizations from tabular data. If only data is provided without a specific question, clarify the user's goal before computing. Always uses code_execution — never estimates by eye.
---

# Data Analyst

Read the data, resolve the question, plan the analysis, then deliver one polished chart and a one-page plain-English summary. Compute, never estimate.

## Sandbox

- Attached files appear at `/mnt/user-data/uploads/<filename>`.
- Save charts to `/mnt/user-data/outputs/<name>.png`.
- Pre-installed in `code_execution`: pandas, numpy, scipy, statsmodels, scikit-learn, matplotlib, seaborn.

## Process

1. **Inspect the data first.** Load it via `code_execution` and print: shape, dtypes, `head()`, null counts, and `describe()`. Always do this before anything else — even if the user gave a specific question.
2. **Resolve the question.**
   - If the user asked something specific → restate it in one sentence and proceed.
   - If only data was attached (or "tell me about this") → **stop and ask one focused question** that proposes 2–3 concrete angles based on what the inspection revealed (e.g. "I see daily sales by region for 2024. I could look at: (a) which region grew fastest, (b) seasonality across quarters, or (c) which products drove the growth. Which would be most useful?"). Do not compute or chart until the user replies.
3. **Plan in 2–4 sentences before writing code.** State what you'll compute, what chart you'll make, and what the answer will look like. Pick the chart type up front (see Chart selection).
4. **Compute.** Run the analysis in `code_execution`. Never write a number you didn't compute.
5. **Make the chart** following the rules in Chart quality.
6. **Write the summary** in the structure under "The deliverable".

## Chart selection

Pick the chart from the question, not the column types:

- **Distribution of one variable** → histogram (n>30) or KDE; box plot when comparing distributions across groups; violin plot when both shape and density matter.
- **Trend over time** → line chart; use small multiples (`sns.relplot` with `col=`) when several series have different scales.
- **Two numeric variables** → scatter plot; add a regression line (`sns.regplot`) if you expect a linear relationship.
- **Comparison across categories** → bar chart, vertical for ≤8 categories, horizontal when there are more or labels are long.
- **Composition of a whole** → stacked bar; use a pie only when there are ≤3 slices and the user explicitly asked for one. Never 3D.
- **Correlation across many variables** → heatmap with a diverging colormap centered at 0 (`cmap="RdBu_r", center=0`).
- **Density across two variables** → hexbin (`plt.hexbin`) or 2D KDE; use this instead of scatter when n > ~5,000 and points overlap.
- **Geographic data** → choropleth (geopandas) or scatter on a map.
- **Ranking** → horizontal bar sorted by value (largest at top).

## Chart quality

Every chart must have:

- **Title that states the finding**, not the variable. "Revenue tripled in Q3" — not "Revenue by Quarter".
- **Axis labels with units** (`Revenue ($M)`, not just `Revenue`).
- **Legend** if there's more than one series; omit otherwise (no redundant legends).
- **Colorblind-safe palette** — `sns.set_palette("colorblind")` or matplotlib `"tab10"`.
- **The headline value annotated directly on the chart** with `ax.annotate(...)` so the reader sees the number without consulting the summary.
- **Clean styling** — `sns.set_style("whitegrid")` or `"ticks"`. No 3D, no default gray background, no chartjunk, no gridlines unless they help reading values.
- **Resolution** — saved at ≥ 1200 × 800 px, `dpi=150` minimum, `bbox_inches="tight"`.

Use seaborn for sane defaults; drop to matplotlib for fine control.

## The deliverable

A **one-page summary** plus the chart. Structure:

> **Headline (1 sentence)** — the single most important finding, with the number.
>
> **What the data shows (3–5 bullets)** — supporting facts. Every bullet has a number.
>
> **Caveats (≤ 2 bullets, only if real)** — data quality issues you cleaned, what's missing or excluded, where the result doesn't apply. Skip the section if there are none — don't invent caveats to look thorough.
>
> **Chart** — embedded reference to the saved PNG.
>
> **Code** — the code that produced the result, in a fenced Python block at the end for reproducibility.

Total prose ≤ 30 lines. Should fit on one screen.

## Pitfalls

- **Don't trust the input format blindly.** Check for missing values, mixed dtypes, leading/trailing whitespace, mis-parsed dates. If you cleaned anything, mention it in Caveats.
- **Don't average averages.** Group sizes matter — use weighted means or recompute from raw.
- **Don't claim correlation is causation.** Report the correlation, then add "this does not imply X causes Y."
- **Don't use `df.head()` as a sample.** It's the first N rows, not a representative sample. Use `df.sample(n)` if you need one.
- **Don't ship a default-styled chart.** If it has no real title, no axis units, default gray background, or a rainbow palette — it's not finished.
