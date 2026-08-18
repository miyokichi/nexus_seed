# Work Generation

## Purpose

Find the gap between what the Goal requires and what the World State plus
existing Work already covers, and propose the Work that closes it.

## Instructions

1. Read the Goal's success criteria as the target condition.
2. Read the World State for what is already satisfied.
3. Read the existing Work inventory — including Work that is expected, blocked
   or in progress — and treat anything already covered as covered.
4. Propose only the missing Work. Each candidate states `work_type`, the
   capabilities it needs, and the `reason` it is necessary.
5. Prefer few, well-justified candidates over exhaustive decomposition.

## One Work per outcome, not per step

A Work is a **need with an outcome**, not a single action. When several
competences only make sense as a chain — each one consuming what the last
produced — they belong to *one* Work that lists them all in
`required_capabilities`, in the order the data flows.

Splitting such a chain into separate Work items looks tidier and is wrong: each
piece then stands alone, with nothing to say that one must precede another and
nothing to carry the intermediate result across. Work items are matched and
started independently, so a chain split across them will run out of order and
each part will start from nothing.

Judge it by the data, not by the number of verbs:

    csv_file → cleaned_dataframe → sales_trend_table → causal_factors_list

is one Work with three capabilities, because no intermediate type is useful on
its own and none of it can start before the previous part finishes.

Two candidates are genuinely separate Work when either could run first, or when
one's outcome stands on its own even if the other never happens.

## Declaring the data flow

`available_input_types` and `required_output_types` are how a chain becomes
executable — they are read to work out the order and to carry each result to
the next step. State them as data type names:

- `available_input_types`: what exists before this Work starts.
- `required_output_types`: what must exist for it to count as done.

Name the *thing*, not the activity: `cleaned_dataframe`, `sales_trend_table`,
`analysis_report`. Reuse a type name exactly when it is the same thing, since
that is what links one capability's output to the next one's input.

## Boundaries

- Do not duplicate existing Work under a new name.
- Do not decide who executes the Work; that is a separate step.
- A capability that nothing provides is still a legitimate requirement — state
  it plainly rather than routing around it.
- Do not order the capabilities by convenience. The order is the data flow, and
  anything else will not compose.
