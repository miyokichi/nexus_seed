# Compute Sales Metrics

## Purpose

Quantify what the sales figures actually did over the period, against a
baseline, so the next step can reason about *why* from numbers rather than
impressions.

## Inputs

`cleaned_dataframe` arrives in your typed inputs as an object carrying `path`,
`row_count` and `columns`. Read the dataset from `path` in the workspace. Do
not re-clean it and do not go back to the raw source — the cleaning decisions
were already made and recorded upstream.

## Instructions

1. Read the objective for the period, the baseline to compare against, and the
   grain (daily, weekly, monthly). If the baseline is not stated, use the
   immediately preceding period of the same length and say so in `notes`.
2. Compute the metrics the objective asks for. Revenue, unit sales and average
   transaction value are the usual three; add one only if the objective names
   it.
3. Compute each metric for both periods, then the absolute and percentage
   change. A metric without its baseline is not yet a comparison.
4. Break the metrics down by the dimensions the objective names (product
   category, region, channel). Record which ones in `dimensions`.
5. Write the full trend table to a workspace file and return its path together
   with the headline metrics.

## Boundaries

- Measure, do not explain. Naming a cause is the next step's work.
- Do not drop or re-impute rows. If the data cannot support a metric, return
  the metric absent and put the reason in `notes` rather than substituting a
  guess.
- State the denominator whenever a percentage could be read two ways.
- If `path` cannot be read, fail rather than falling back to the raw CSV.
