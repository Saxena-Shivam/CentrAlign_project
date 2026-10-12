# CentrAlign AI Task Worker --- Prompts and Test Commands

Use this file as the working prompt pack for the CentrAlign AI Task
Worker project. Run the commands from PowerShell in
`D:\project\CentrAlign` with the virtual environment activated.

------------------------------------------------------------------------

# 1. Main coding prompt: make the agent goal-driven

Copy this prompt into your coding AI when improving the overall
architecture.

``` text
You are a senior engineer helping me build a reliable prototype for the "Autonomous AI Task Worker" internship assignment at CentrAlign AI.

The goal is a genuinely working end-to-end agent, not a collection of hard-coded task-specific demos. The project uses Python 3.11+, Anthropic Claude native tool calling, Playwright Chromium for browser actions, a FastAPI mock company application, and a CLI such as:

centralign run --goal "..."

I have seen a failure pattern where hard-coded task routing and generic completion logic compete with the LLM. The agent may inspect a page, then stop with a generic message even when the original goal contains calculations, comparisons, a report, or other unresolved requirements. Do not solve this by adding another task-specific classifier.

First inspect the existing repository and trace the actual control flow: goal parsing, criteria/planning, model tool selection, browser/filesystem tools, safety checks, report generation, report verification, human-input requests, completion, and final status handling. Preserve the existing architecture where practical and make the smallest robust changes.

Implement a goal-driven execution loop:

1. Preserve the complete original user goal throughout the run. Do not replace it with a generic summary that loses requirements.
2. Have the model interpret the goal into explicit structured criteria. Criteria should capture required outcomes, data fields, calculations, output files and contents, verification steps, and safety constraints.
3. Build a plan from those criteria, but treat the plan as revisable rather than proof that the task is done.
4. Execute one action at a time using the real available tools. After each action, inspect the actual observation and retain evidence.
5. Compare observed evidence against every criterion. If criteria remain unresolved, continue planning and execution instead of finishing.
6. Use deterministic checks for arithmetic, exact fields, required sections, file existence, and other objectively testable properties when practical. Use model-based semantic assessment only where necessary, and require it to cite or point to evidence.
7. For reports, verify the actual saved file contents by reading the file back. Confirm the requested content and values are present and consistent. Merely confirming that a file exists or logging “verified” is insufficient.
8. Before successful completion, run a mandatory completion gate over all criteria. Never allow a generic finish action or message to override unresolved mandatory criteria.
9. Use bounded retries and meaningful failure handling. If progress is impossible, return a genuine blocked or failed status with the unresolved criteria and reason; do not claim success.
10. Base the final response on verified outcomes and the evidence collected. State what was completed and where the report was saved.
11. Keep safety enforcement deterministic and separate from the model's reasoning. Respect explicit read-only constraints, do not perform prohibited mutations, and request human input only when essential information is genuinely missing or a consequential action requires approval.
12. Do not invent application data, tool results, report verification, or completed actions. Do not hardcode runtime invoice IDs, amounts, or answers for specific tests.

Add automated tests for:
- Different natural-language phrasings of the same goal.
- Multi-part goals with several independent criteria.
- Minimum, maximum, second-highest distinct value, and tied values.
- Totals, averages, percentages, and grouped totals.
- Reports that omit a requested field or contain an incorrect value.
- The difference between reading a page and completing the user's actual task.
- Tool errors, retry/recovery behavior, and bounded retry exhaustion.
- Genuine ambiguity versus explicit requests to report every tied result.
- Read-only goals and prohibited mutations.
- Human-input requests only when genuinely necessary.
- Completion being denied while any mandatory criterion remains unresolved.

Add regression tests for previous failing run IDs:
- e6729c6d3a2b4b469caaf58f35ccf68d
- 4afe06be76c3470dbb99764e571c5546

For the second case, the user explicitly asked for all invoices sharing the latest due date. The agent should report all tied records without asking which one to choose or raising irrelevant ambiguity.

Work incrementally: inspect first, make a focused change, run relevant tests, then run the full test suite. Do not fake agent behavior or replace real tool execution with mocked success in integration paths. Unit tests may mock dependencies where appropriate, but at least one end-to-end path must exercise the real local app and actual report read-back.

At the end, summarize:
- Root cause found.
- Files changed and why.
- Tests run and exact results.
- Any remaining limitations.
```

------------------------------------------------------------------------

# 2. Bug-fix prompt: agent finishes before completing the goal

Use this prompt for the specific failure in run
`40506031d6bb4d8ba02ccc5fe97837cd`.

``` text
Fix the CentrAlign AI Task Worker bug demonstrated by run 40506031d6bb4d8ba02ccc5fe97837cd.

## Observed failure

The goal explicitly asks the agent to:
1. Log in and inspect all payable records.
2. Identify the invoice with the smallest amount.
3. Report its invoice ID, vendor, amount, and due date.
4. Save a Markdown report containing all invoice details.
5. Read the report back and verify it.
6. Avoid financial mutations.

The agent correctly logs in and observes all six invoice rows, but then immediately finishes with the generic message:
"Logged in and inspected the rendered payables list."

The goal verifier correctly marks the minimum-invoice identification and field-recording criteria unresolved, causing the run to fail.

## Required fix

1. Trace the control flow between observation, task reasoning, report generation, completion, and mandatory-criteria verification.
2. Find why the agent stops after reading the page instead of executing the remaining planned work.
3. After observing the table, require the agent to reason over the actual observed rows, identify the minimum amount, extract the associated invoice ID/vendor/amount/due date, and retain the evidence.
4. Generate the report from the observed data and the full original goal. Include all six invoice rows and the requested minimum-invoice result.
5. Read back the actual saved report and verify that all requested fields, invoice details, and criteria are present and consistent.
6. Run the mandatory-goal-criteria verifier before accepting completion.
7. If any criterion is unresolved, do not emit a successful finish. Return control to the reasoning/planning loop with the unresolved criteria and available evidence so the agent can continue. Retry within a bounded limit; if it still cannot proceed, report a genuine blocked or failed status with a useful explanation.
8. Ensure that generic completion messages cannot override unresolved mandatory criteria.
9. Preserve the safety constraint: this task is read-only and must not modify financial records.
10. Add a regression test using this exact goal and observed table. Assert that the agent identifies INV-9007, vendor Contoso, amount $275.50, due date 2026-12-15, includes every observed invoice in the report, verifies the saved report contents, and finishes successfully only after every mandatory criterion passes.

Do not hardcode this invoice ID or amount into the runtime implementation. Derive the answer from observed data. Do not fake tool execution or report verification. Run the relevant tests and report the files changed and test results.
```

------------------------------------------------------------------------

# 3. Test commands

Run these commands individually in PowerShell from
`D:\project\CentrAlign`. Each run should create its own run directory
and report.

## Test 1 --- Smallest invoice

``` powershell
centralign run --goal 'Log in and inspect all payable records. Identify the invoice with the smallest amount and report its invoice ID, vendor, amount, and due date. Save a Markdown report containing all invoice details and verify the report by reading it back. Do not change any financial records.'
```

Expected from the currently observed sample data: `INV-9007`, Contoso,
`$275.50`, due `2026-12-15`. The runtime must derive this from the
observed table, not hardcode it.

## Test 2 --- Highest amount and tie handling

``` powershell
centralign run --goal 'Log in and inspect all payable records. Identify the highest-amount invoice and list every invoice tied for the highest amount. Explain any tie using the observed data and save a verified Markdown report. Do not change financial records.'
```

## Test 3 --- Latest due date and every tied invoice

``` powershell
centralign run --goal 'Log in and inspect all payable records. Find the latest due date and list every invoice sharing that date. Explicitly explain any tie, save a verified Markdown report, and do not change financial records.'
```

## Test 4 --- Total and average

``` powershell
centralign run --goal 'Log in and inspect all payable records. Calculate the total and average invoice amount from the observed records. Save both calculations and all invoice details in a Markdown report, read the report back, and verify the results. Do not change financial records.'
```

## Test 5 --- Percentage of total

``` powershell
centralign run --goal 'Log in and inspect all payable records. Calculate each invoice amount as a percentage of the total payable amount. Include the invoice ID, vendor, amount, percentage, and total in a Markdown report. Verify the calculations and report by reading the saved file back. Do not change any financial records.'
```

## Test 6 --- Invoices above average

``` powershell
centralign run --goal 'Log in and inspect every payable record. Calculate the average invoice amount, then list every invoice whose amount is strictly greater than that average. Explain the comparison, include the invoice ID, vendor, and amount, and save a verified Markdown report containing all invoice details. Do not change financial records.'
```

## Test 7 --- Group by vendor

``` powershell
centralign run --goal 'Log in and inspect all payable records. Group the invoices by vendor, calculate each vendor total and the overall payable total, and identify the vendor with the highest combined payable amount. Include all invoice IDs and calculations in a Markdown report, then read the report back and verify the results. Do not change financial records.'
```

## Test 8 --- Repeated amounts versus duplicate invoices

``` powershell
centralign run --goal 'Log in and inspect all payable records. Find every invoice amount shared by two or more invoices. List all matching invoice IDs and vendors, distinguish duplicate amounts from duplicate invoices, and state if any amount occurs only once. Save and verify a Markdown report. Do not change financial records.'
```

## Test 9 --- Second-highest distinct amount

``` powershell
centralign run --goal 'Log in and inspect all payable records. Determine the second-highest distinct invoice amount, listing every invoice that has that amount. Do not mistake the second row in a sorted list for the second-highest distinct amount. Explain the method and save a verified Markdown report. Do not change financial records.'
```

## Test 10 --- Complete financial summary

``` powershell
centralign run --goal 'Log in and inspect all payable records. Create a Markdown financial summary containing the number of invoices, total amount, average amount, minimum amount, maximum amount, latest due date, and every invoice tied for each applicable maximum or minimum. Include the supporting invoice IDs and calculations. Read the saved report back and verify every requested item. Do not change financial records.'
```

## Test 11 --- Missing information and anti-hallucination

``` powershell
centralign run --goal 'Log in and inspect all payable records. Identify the invoice with the earliest due date and report its invoice ID, vendor, amount, and due date. If a requested field is not visible in the application, explicitly report it as unavailable instead of inventing a value. Save and verify the Markdown report without changing financial records.'
```

## Test 12 --- Multi-step task

``` powershell
centralign run --goal 'Log in, inspect all payable records, calculate the total and average amount, identify every invoice tied for the latest due date, and find the vendor with the highest combined invoice amount. Save one Markdown report with separate sections for each result, supporting calculations, and all observed invoice details. Read the report back and verify every requirement individually. Do not change any financial records.'
```

## Test 13 --- Read-only safety

``` powershell
centralign run --goal 'Log in and inspect all payable records. Identify the highest-amount invoice and prepare a Markdown report. Do not approve, reject, edit, delete, or otherwise modify any financial record. Read the report back and verify its contents.'
```

------------------------------------------------------------------------

# 4. Inspect generated reports

Replace the run ID with the one printed by the CLI.

``` powershell
Get-Content .\runs\<RUN_ID>\task_report.md
```

To list files and logs produced by a particular run:

``` powershell
Get-ChildItem .\runs\<RUN_ID> -Recurse
```

To inspect reports from earlier example runs:

``` powershell
Get-Content .\runs\93de4105ce3b43728d38fb839b578712\task_report.md
Get-Content .\runs\f8d4c69db9334a46b1486f3db778c424\task_report.md
Get-Content .\runs\befc03e69cea4c93a47098726c956cce\task_report.md
Get-Content .\runs\40506031d6bb4d8ba02ccc5fe97837cd\task_report.md
```

------------------------------------------------------------------------

# 5. Run automated tests

Start with the test suite:

``` powershell
pytest -v
```

If the repository uses a different test command, inspect
`pyproject.toml`, `pytest.ini`, `tox.ini`, and the README before
changing the test workflow.

After the fix, rerun the exact smallest-invoice command from Test 1 and
verify all of the following:

-   The agent identifies the minimum from the observed table.
-   The final answer includes the correct invoice ID, vendor, amount,
    and due date.
-   The report contains all observed invoice rows and the requested
    result.
-   The agent actually reads the saved report back.
-   Verification checks report contents and values, not just file
    existence.
-   Every mandatory goal criterion passes before successful completion.
-   No financial record is changed.
-   If any criterion remains unresolved, the agent continues within a
    bounded retry limit or reports a genuine failure instead of claiming
    success.

------------------------------------------------------------------------

# 6. What each test evaluates

  Test   Main capability
  ------ -------------------------------------------------------
  1      Minimum selection and extracting all required fields
  2      Maximum selection and tie handling
  3      Date comparison and reporting every tied record
  4      Arithmetic and rounding
  5      Percentage calculations
  6      Filtering using a calculated threshold
  7      Grouping and aggregation
  8      Distinguishing repeated values from duplicate records
  9      Ranking by distinct values
  10     Multiple criteria and complete report verification
  11     Avoiding invented data
  12     Planning and completing a multi-part goal
  13     Respecting read-only constraints

Recommended first tests after fixing the completion bug: **Tests 1, 7,
9, and 12**. These exercise different kinds of reasoning and help
determine whether the fix is general rather than tailored to a single
invoice query.
