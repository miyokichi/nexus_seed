# Load and Clean CSV

## Purpose

Turn a raw CSV file into a dataset the later analysis steps can rely on, and
say plainly what was changed on the way.

## Inputs

`csv_file` names the source. It arrives either as a workspace-relative path in
your typed inputs, or named in the objective text. Read it from the workspace;
nothing is transferred to you as file content.

## Instructions

1. Load the file and read its real shape before deciding anything — column
   names, row count, dtypes, and how much is actually missing.
2. Apply only the filter the objective states (a date range, a segment, a
   status). Do not narrow the data further on your own judgement.
3. Resolve missing values explicitly. Dropping a row and imputing a value are
   different decisions, and each one belongs in `missing_value_policy`.
4. Write the cleaned dataset to a **new** workspace file. Never overwrite the
   source.
5. Return the path you wrote, plus the row count, the column list, and how many
   rows were dropped.

## Boundaries

- The cleaned data goes in a file; the returned object carries the *reference*
  and the shape, not the rows themselves.
- Do not compute metrics, trends or comparisons — that is the next step's work
  and doing it here would hide it from the record.
- Do not delete or modify the source file.
- If the file is missing or unreadable, fail and say so. An empty dataset that
  looks successful is worse than a clear failure.
