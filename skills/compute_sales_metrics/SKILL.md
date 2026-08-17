# compute_sales_metrics

## Responsibility
Compute the quantitative sales metrics requested by the Work from the cleaned dataset,
compare the requested periods, and produce a reusable sales trend table.

## Boundaries
- Use the supplied cleaned dataset; do not silently replace it with unrelated data.
- Do not claim causal explanations. This Skill quantifies changes only.
- Do not fabricate metrics when source columns are insufficient.
- Preserve enough grouping detail for downstream diagnostic analysis.

## Procedure
1. Resolve the `cleaned_dataframe` input artifact.
2. Confirm which requested metrics are computable.
3. Compute requested measures such as revenue, unit sales, and average transaction value when supported.
4. Aggregate at the requested temporal granularity and dimensions.
5. Compare the requested target period against its baseline.
6. Cross-check totals against the cleaned source data.
7. Persist the trend table as a workspace artifact, preferably CSV.
8. Return only the structured result required by the output schema.

## Output meaning
`artifact_path` must point to the trend-table artifact for downstream analysis.
