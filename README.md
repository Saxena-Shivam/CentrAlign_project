# CentrAlign Autonomous AI Task Worker

A small, reliable AI task worker prototype built for the CentrAlign internship assignment.

## What is included

- Flask mock company app with login, separate payables/vendor/activity pages, and a dedicated payable form
- SQLite-backed validation with realistic seeded data
- Browser automation tool definitions for navigation and form interaction
- A custom agent loop with plan, memory, tool execution, and verification
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
   - `centralign run --goal 'Add payable for Contoso, invoice INV-9007, amount $275.50, due date 2026-12-15'`
   - Or run `centralign run` and enter a goal when prompted.
6. To execute the isolated offline evaluation suite:
   - `python evals\run.py`

The goal is the task instruction: enter what you want it to do after `--goal`, or enter it at the prompt when running `centralign run` without that option.
To watch the worker's browser actions, set `HEADLESS=false`. Visible mode waits 1.2 seconds after each navigation or form action by default; adjust `BROWSER_ACTION_DELAY_MS` (0-60000) to make it slower or faster.

After signing in, the app has separate Payables, Vendors, and Activity pages. Use the navigation at the top to switch between them; add a payable from the dedicated Add payable form. The vendor field is plain text (no dropdown); the approved vendor names are shown beside it, and the server validates the entered value before saving.

For an offline run without an API key, use `LLM_PROVIDER=mock`. The deterministic mock supports the login-and-inspect flow and payable creation goals that explicitly include a listed vendor, invoice number, amount, and due date (`YYYY-MM-DD`). Login navigation and entry of locally configured credentials are handled by the worker for all providers; credentials are not included in model prompts. Example:

```powershell
centralign run --goal 'Add payable for Contoso, invoice INV-9007, amount $275.50, due date 2026-12-15'
```

If required payable details are missing or the invoice already exists, the worker pauses rather than inventing values or creating a duplicate. The offline mock is intentionally limited; it does not understand arbitrary goals.
Before submitting a payable, the worker displays the exact current form values and asks for approval in an interactive terminal. A non-interactive run pauses without writing. `AUTO_APPROVE=true` is intended only for isolated tests/evals and should not be used for normal runs.

For other natural-language goals, configure a model that supports tool calling:

```powershell
$env:LLM_PROVIDER = "openai"
$env:LLM_MODEL = "gpt-4o-mini"
$env:LLM_API_KEY = "<set your key locally in this terminal>"
centralign run
```

Never paste an API key into chat or commit it. `.env` is gitignored. OpenAI-compatible presets are `openai`, `groq`, and `huggingface`; set `LLM_BASE_URL` when needed. For Groq/Hugging Face, set the provider-specific model name and an API key locally. Tool-calling capability varies by model/provider. These hosted-provider paths are not covered by the no-key automated tests.
Login navigation and configured credential entry are handled locally for all providers. Read-only payable count, total, and average requests are answered from the rendered register, including requests for only one metric. Activity-page requests follow the observed navigation link, read the visible entries, and summarize the latest entries without changing records. Observation-report requests save the captured payables table once and finish instead of repeating reads or writes.

Hosted-model action selection uses the reusable instructions in `agent/src/centralign_agent/prompts.py`; task-specific hosted-model decisions are not hard-coded in the run loop. Runtime safeguards still enforce the configured navigation origin, local credential privacy, payable approval, and post-write verification. The no-key mock provider has a deliberately limited deterministic policy for the demo flows and is not a general natural-language agent.
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

The loop is ordinary Python so each decision, observation, tool call, approval, and failure is inspectable. It is not based on LangGraph or `AgentExecutor`; those abstractions are intentionally avoided for this small prototype. Payable approval happens at the actual submit boundary, after the form values are read from the live page. A browser submit is not sufficient proof of success: the worker checks the app confirmation and then re-reads the rendered table before declaring a payable task complete.

## Notes

- The loop intentionally uses a small, testable design rather than a broad fake automation.
- Browser operations are constrained to the configured company app origin.
- Browser state persists across tool calls during a run; steps and compact observations are shown in the terminal and stored in the trace.
- Password values are redacted from observations and persisted traces.
- Browser automation can run in headless or visible mode with `HEADLESS`; requested screenshots are saved in the current run's `screenshots/` folder.
- In visible mode, `BROWSER_ACTION_DELAY_MS` controls the pause after navigation and form actions, making each step easier to watch.
- OpenAI-compatible model calls use `langchain-openai` structured tool calling. The no-key mock is limited to the small supported demo flows above.
- Payable creation is not considered complete just because the submit action worked: the app confirmation and invoice row must both be observed before the worker reports success.
- This remains an internship prototype, not a complete general-purpose agent. The deterministic offline mock handles the explicitly documented demo flows and does not interpret arbitrary natural-language tasks; broader goals require a configured tool-calling model and should be evaluated before production use.
