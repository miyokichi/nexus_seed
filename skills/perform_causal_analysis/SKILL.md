# perform_causal_analysis

## Responsibility
Analyze the supplied `sales_trend_table` and identify the strongest evidence-supported
drivers of the observed sales decline.

## Boundaries
- Distinguish contribution/association from proven causality.
- Do not invent external causes unless evidence is present in supplied context/data.
- Do not rerun upstream cleaning unless the provided input is unusable.
- Prefer transparent calculations and comparisons over unsupported narrative.

## Procedure
1. Resolve the `sales_trend_table` input artifact.
2. Identify the largest negative changes by dimensions actually present in the data.
3. Quantify each candidate factor's contribution when possible.
4. Check for aggregation effects or missing coverage.
5. Rank the strongest factors.
6. State evidence for every factor and list material limitations.
7. Return only the structured result required by the output schema.

## Interpretation rule
Use "driver", "contributor", or "associated factor" unless the evidence genuinely supports causality.
