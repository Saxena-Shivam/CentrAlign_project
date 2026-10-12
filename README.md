# CentrAlign Autonomous AI Task Worker

A small, reliable AI task worker prototype built for the CentrAlign internship assignment.

## What is included

- Flask mock company app with login, separate payables/vendor/activity pages, and a dedicated payable form
- SQLite-backed validation with realistic seeded data
- Browser automation tool definitions for navigation and form interaction
- A custom agent loop with plan, memory, tool execution, and verification
- Generic read-only table analysis for totals, averages, percentages, filters,
  grouping, repeated values, and distinct rankings
- Run-state trace persistence under `runs/<run-id>/`
- CLI entrypoints for the app and the worker
- A small eval harness and demo PowerShell script

## Quick start

1. Create and activate a virtual environment.
2. Install dependencies:
   - `python -m pip install -e .`
3. Copy the environment template if needed:
   - `copy .env.example .env`
   - The project reads `.env` from the project root. Keep the mock app and worker configured with the same credentials.
4. Start the company app:
   - `centralign app`
5. In a second terminal, run the worker:
   - `centralign run --goal 'Log in and inspect all payable records. Calculate each invoice amount as a percentage of the total payable amount. Include the invoice ID, vendor, amount, percentage, and total in a Markdown report. Verify the calculations and report by reading the saved file back. Do not change any financial records.'`
   - Or run `centralign run` and enter a goal when prompted.
6. To execute the isolated offline evaluation suite:
   - `python evals\run.py`

The goal is the task instruction: enter what you want it to do after `--goal`, or enter it at the prompt when running `centralign run` without that option.
To watch the worker's browser actions, set `HEADLESS=false` in `.env`. Visible mode
waits 1.2 seconds after each navigation or form action by default; adjust
`BROWSER_ACTION_DELAY_MS` (0-60000) to make it slower or faster.

After signing in, the app has separate Payables, Vendors, and Activity pages. Use the navigation at the top to switch between them; add a payable from the dedicated Add payable form. The vendor field is plain text (no dropdown); the approved vendor names are shown beside it, and the server validates the entered value before saving.

For an offline run without an API key, use `LLM_PROVIDER=mock`. Login navigation,
credential entry, payable inspection, and the documented read-only table analysis
operations run locally. Credentials are not included in model prompts. Example:

```powershell
centralign run --goal 'Add payable for Contoso, invoice INV-9007, amount $275.50, due date 2026-12-15'
```

If required payable details are missing or the invoice already exists, the worker
pauses rather than inventing values or creating a duplicate. The offline mock is
still intentionally bounded for write workflows; it does not replace a hosted
tool-calling model for arbitrary browser tasks.
Before submitting a payable, the worker displays the exact current form values and asks for approval in an interactive terminal. A non-interactive run pauses without writing. `AUTO_APPROVE=true` is intended only for isolated tests/evals and should not be used for normal runs.

For other natural-language goals, configure a model that supports tool calling:

```powershell
$env:LLM_PROVIDER = "openai"
$env:LLM_MODEL = "gpt-4o-mini"
$env:LLM_API_KEY = "<set your key locally in this terminal>"
centralign run
```

Never paste an API key into chat or commit it. `.env` is gitignored. OpenAI-compatible presets are `openai`, `groq`, and `huggingface`; set `LLM_BASE_URL` when needed. For Groq/Hugging Face, set the provider-specific model name and an API key locally. Tool-calling capability varies by model/provider. These hosted-provider paths are not covered by the no-key automated tests.
Login navigation and configured credential entry are handled locally for all
providers. Read-only analysis is performed from the rendered register after the
complete table is observed. The generic `analyze_table` tool supports:

- `count`, `total`, `average`, `minimum`, and `maximum`
- Per-invoice percentage of the total
- Strict comparisons such as `amount > average`
- Grouped totals such as total amount by vendor
- Repeated amount detection with the matching invoice rows
- Distinct ranking such as the second-highest distinct amount

Analysis results are stored as run evidence and included in Markdown reports.
Reports are written and read back before completion is reported. Saving a
Markdown report is not treated as a request to submit a financial form.

Each run preserves the original goal and derives a per-run completion-criteria set from the model plan. Before a successful status is assigned, the criteria evaluator must mark every criterion complete using concrete run evidence; generic page-inspection messages cannot satisfy an unmet criterion. Hosted-model action selection uses the reusable instructions in `agent/src/centralign_agent/prompts.py`. Existing deterministic branches are compatibility and safety fallbacks, not substitutes for the goal criteria. Runtime safeguards still enforce the configured navigation origin, local credential privacy, payable approval, and post-write verification. The no-key mock provider has a deliberately limited deterministic policy for the demo flows and is not a general natural-language agent.
On the payable form, explicit payable goals use the form-aware workflow for every provider. Chromium verifies each filled value in the live DOM; a fill that does not persist is reported as an error, and the actual observed field values are printed in the run log. A model-selected Save button click is routed through the same approved form-submission path; direct browser clicks cannot bypass approval. After the app confirms a save, the worker performs one fresh register read and does not retry the write if verification fails. For other tasks, a browser action that repeats without changing the observed page is stopped and reported instead of consuming the remaining step limit.

## Default login

The mock app uses the same credentials as the agent environment:

- Username: `demo`
- Password: `demo`

These values are read from environment variables. If terminals are opened separately, they do not share environment state automatically.

## Environment variables

See `.env.example` for the current defaults.

## Validation behavior

The app validates the following server-side rules:

- vendor must exist
- invoice number must not be blank or duplicated
- amount must be a valid positive number
- due date must be a real date in `YYYY-MM-DD` format
- error banners display and entered values are preserved on failure

## Chaos mode

Set `CHAOS_MODE` to one of the supported values:

- `rename_field`
- `extra_required`
- `first_submit_fails`
- `move_submit`

This intentionally changes the form behavior to exercise robustness.
`python evals\run.py` runs each case against a temporary database, verifies its expected status and database/memory result, prints each visible agent trace, and exits nonzero if any case fails. The suite covers successful creation, inspection, missing/duplicate input pauses, renamed fields, a moved submit button, recovery after a failed first submit, and an unknown required field.

## Run evidence

Each run stores its state under `runs/<run-id>/` with files such as:

- `state.json`
- `trace.jsonl`
- `screenshots/` (when requested by the goal)
- `observation.md` (when the goal asks to write/save/document observations)

The payables form is available at `/payables/new`; `/payables` shows only the payable register.

For example, `centralign run --goal 'Log in and inspect the payables list and write your observation'` writes a read-only Markdown report containing the observed table to `runs/<run-id>/observation.md` and prints the report path.

## Architecture

```text
User goal
   |
   v
Planner --> Custom loop <--> Model adapter
                 |
                 v
        Validated tool registry
          |             |
          v             v
    Playwright       Inbox files
          |
          v
    Flask app --> SQLite
          |
          v
 Independent verification --> final status + run evidence
```

The loop is ordinary Python so each decision, observation, tool call, approval, and
failure is inspectable. It is not based on LangGraph or `AgentExecutor`; the
state-machine loop provides the required observation, analysis, evidence,
reporting, and verification stages without adding another framework dependency.
Payable approval happens at the actual submit boundary, after the form values
are read from the live page. A browser submit is not sufficient proof of
success: the worker checks the app confirmation and then re-reads the rendered
table before declaring a payable task complete.

## Notes

- The loop intentionally uses a small, testable design rather than a broad fake automation.
- Browser operations are constrained to the configured company app origin.
- Browser state persists across tool calls during a run; steps and compact observations are shown in the terminal and stored in the trace.
- Password values are redacted from observations and persisted traces.
- Browser automation can run in headless or visible mode with `HEADLESS`; requested screenshots are saved in the current run's `screenshots/` folder.
- In visible mode, `BROWSER_ACTION_DELAY_MS` controls the pause after navigation and form actions, making each step easier to watch.
- OpenAI-compatible model calls use `langchain-openai` structured tool calling.
- Complex analytical goals are routed through the generic table-analysis path so
  model retries or repeated `read_page` actions do not replace the calculation.
- Payable creation is not considered complete just because the submit action worked: the app confirmation and invoice row must both be observed before the worker reports success.
- This remains an internship prototype, not a production accounting system.
  Financial writes remain approval-gated and read-only analysis never changes
  records.

## Demo video walkthrough

Use the following sequence for a visible end-to-end demonstration.

### Terminal 1: configure and start the company app

```powershell
cd D:\project\CentrAlign
.\.venv\Scripts\Activate.ps1
centralign app
```

Keep this terminal open. The mock company app runs at
`http://127.0.0.1:3000` by default.

### Terminal 2: run a percentage-analysis task

```powershell
cd D:\project\CentrAlign
.\.venv\Scripts\Activate.ps1
$env:LLM_PROVIDER = "mock"
centralign run --goal 'Log in and inspect all payable records. Calculate each invoice amount as a percentage of the total payable amount. Include the invoice ID, vendor, amount, percentage, and total in a Markdown report. Verify the calculations and report by reading the saved file back. Do not change any financial records.'
```

The terminal visibly prints the plan, every browser step, observations, the
analysis result, report verification, and the final status. Copy the printed
run ID from the output.

### Terminal 2: open the generated report

Replace `<RUN_ID>` with the ID printed by `centralign run`:

```powershell
Get-Content .\runs\<RUN_ID>\task_report.md
Get-Content .\runs\<RUN_ID>\state.json
Get-Content .\runs\<RUN_ID>\trace.jsonl
```

The report contains the observed invoice table, total, derived percentages,
formula/evidence output, and the read-back verification result.

### Additional demo prompts

Strictly above-average invoices:

```powershell
centralign run --goal 'Log in and inspect every payable record. Calculate the average invoice amount, then list every invoice whose amount is strictly greater than that average. Explain the comparison, include the invoice ID, vendor, and amount, and save a verified Markdown report containing all invoice details. Do not change financial records.'
```

Grouped vendor totals:

```powershell
centralign run --goal 'Log in and inspect all payable records. Group the invoices by vendor, calculate each vendor total and the overall payable total, and identify the vendor with the highest combined payable amount. Include all invoice IDs and calculations in a Markdown report, then read the report back and verify the results. Do not change financial records.'
```

Read-only safety:

```powershell
centralign run --goal 'Log in and inspect all payable records. Identify the highest-amount invoice and prepare a Markdown report. Do not approve, reject, edit, delete, or otherwise modify any financial record. Read the report back and verify its contents.'
```

### Optional payable-creation demo

This demo intentionally pauses for approval before changing the database:

```powershell
centralign run --goal 'Add payable for Contoso, invoice INV-9007, amount $275.50, due date 2026-12-15'
```

Review the displayed form values and approve only if they are correct. Do not
set `AUTO_APPROVE=true` during a recorded demonstration unless the demo uses a
temporary isolated database.
