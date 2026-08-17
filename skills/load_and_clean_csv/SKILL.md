# load_and_clean_csv

## Responsibility
Load the CSV input referenced by the Work, apply the requested temporal/scope filters,
handle missing values conservatively, and produce a cleaned reusable dataset artifact.

## Boundaries
- Do not perform downstream business analysis.
- Do not infer or fabricate missing business values unless the Work explicitly permits imputation.
- Do not overwrite the original input file.
- Keep file operations inside the configured workspace.
- If the requested source file cannot be found, fail rather than substituting another file.

## Procedure
1. Identify the CSV file from the Work objective/context.
2. Inspect schema, data types, row count, date coverage, and critical columns.
3. Apply only the filters explicitly required by the Work.
4. Handle missing values conservatively and report unresolved issues.
5. Write the cleaned dataset to a new workspace artifact, preferably CSV.
6. Verify the produced artifact can be read back successfully.
7. Return only the structured result required by the output schema.

## Output meaning
`artifact_path` must point to the cleaned dataset artifact that a later Work can consume.
