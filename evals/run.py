from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from threading import Thread
from typing import Any

from werkzeug.serving import make_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from centralign_agent.loop import AgentLoop
from company_app.app import create_app


ENVIRONMENT_KEYS = (
    "APP_BASE_URL",
    "AGENT_MAX_STEPS",
    "AUTO_APPROVE",
    "CENTRALIGN_MODEL",
    "CHAOS_MODE",
    "DEMO_PASSWORD",
    "DEMO_USERNAME",
    "EVAL_DB_PATH",
    "HEADLESS",
    "LLM_PROVIDER",
)


def run_eval_task(task: dict[str, Any]) -> dict[str, Any]:
    """Run one case against a fresh database and isolated run directory."""
    previous_environment = {key: os.environ.get(key) for key in ENVIRONMENT_KEYS}
    with tempfile.TemporaryDirectory(prefix=f"centralign-eval-{task['id']}-") as temp_dir:
        work_dir = Path(temp_dir)
        database_path = work_dir / "company.db"
        os.environ.update(
            {
                "AUTO_APPROVE": "true",
                "AGENT_MAX_STEPS": "16",
                "CENTRALIGN_MODEL": "mock",
                "DEMO_PASSWORD": "demo",
                "DEMO_USERNAME": "demo",
                "EVAL_DB_PATH": str(database_path),
                "HEADLESS": "true",
                "LLM_PROVIDER": "mock",
            }
        )
        if task.get("chaos_mode"):
            os.environ["CHAOS_MODE"] = task["chaos_mode"]
        else:
            os.environ.pop("CHAOS_MODE", None)

        server = None
        thread = None
        try:
            server = make_server("127.0.0.1", 0, create_app())
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            os.environ["APP_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
            state = AgentLoop(work_dir / "runs").run(task["goal"])
        finally:
            if server is not None:
                server.shutdown()
            if thread is not None:
                thread.join(timeout=5)
            for key, value in previous_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        failures = []
        expected_status = task["expected_status"]
        if state.status != expected_status:
            failures.append(f"expected status {expected_status}, got {state.status}")

        for key, expected_value in task.get("expected_memory", {}).items():
            if state.memory.get(key) != expected_value:
                failures.append(
                    f"expected memory[{key!r}]={expected_value!r}, got {state.memory.get(key)!r}"
                )

        invoice = task.get("invoice_number")
        if invoice:
            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    "SELECT vendor, invoice_number, amount, due_date FROM payables WHERE invoice_number = ?",
                    (invoice,),
                ).fetchone()
            expected_present = task.get("invoice_exists", True)
            if bool(row) != expected_present:
                failures.append(
                    f"expected invoice {invoice} {'to exist' if expected_present else 'not to exist'}"
                )

        return {
            "id": task["id"],
            "passed": not failures,
            "status": state.status,
            "run_id": state.run_id,
            "failures": failures,
        }


def main() -> int:
    tasks_path = PROJECT_ROOT / "evals" / "tasks.json"
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    results = []
    for task in tasks:
        print(f"\n[CentrAlign eval] {task['id']}: {task['goal']}", flush=True)
        result = run_eval_task(task)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[CentrAlign eval] {status} {result['id']} (status={result['status']})")
        for failure in result["failures"]:
            print(f"[CentrAlign eval]   {failure}")

    passed = sum(result["passed"] for result in results)
    print(f"\n[CentrAlign eval] Summary: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
