# Perform Causal Analysis

## Purpose

Work out which parts of the business account for the measured change, and how
much of it each one explains — separating what the numbers show from what is
still a hypothesis.

## Inputs

`sales_trend_table` arrives in your typed inputs as an object carrying `path`,
`period`, `baseline_period` and the headline `metrics`. Read the full table
from `path`. The measurement is already done; do not recompute it.

## Instructions

1. Start from the aggregate change, then decompose it: which categories,
   regions or channels moved, and by how much. A factor is worth listing when
   it accounts for a visible share of the total.
2. For each factor, record the evidence from the table — the specific figures
   that make the case. A factor with no numbers behind it is a hypothesis, and
   belongs in `notes` rather than in `factors`.
3. Give each factor a contribution share where the arithmetic supports one, so
   the factors can be ranked rather than merely listed.
4. Account for what is left over in `unexplained_share`. A decomposition that
   silently adds up to less than the whole change overstates its own certainty.
5. Say what you ruled out and why. A checked-and-rejected explanation is a
   result, not wasted work.

## Boundaries

- Correlation within one table is not proof of cause. Confidence must reflect
  that, and a plausible story with thin evidence gets a low number.
- Do not recommend actions or write the report; this step ends at the ranked
  factors and their evidence.
- Do not reach for data outside the trend table. If the decisive evidence is
  not there, say which data would settle it in `notes`.
- Do not manufacture a single dominant cause when the table shows a diffuse
  decline across many segments.
