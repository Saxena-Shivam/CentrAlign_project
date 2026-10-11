from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from centralign_agent.config import get_settings
from centralign_agent.loop import AgentLoop

cli_app = typer.Typer(help="Autonomous AI Task Worker prototype for CentrAlign")


@cli_app.command("run")
def run_command(
    goal: str | None = typer.Option(
        None,
        "--goal",
        help="Natural-language task for the worker; omit to enter it interactively",
    )
):
    """Run the autonomous task loop against the demo app and inbox."""
    if goal is None:
        goal = typer.prompt("What would you like the worker to do?")
    if not goal.strip():
        raise typer.BadParameter("The goal must not be blank.", param_hint="--goal")
    settings = get_settings()
    print(f"[CentrAlign] Starting run with app origin {settings.app_origin}")
    loop = AgentLoop(Path("runs"))
    final_state = loop.run(goal)
    print(f"[CentrAlign] Run finished with status: {final_state.status}")
    print(f"[CentrAlign] Final message: {final_state.final_message}")


@cli_app.command("app")
def serve_app():
    """Launch the mock company Flask server."""
    from company_app.app import create_app

    settings = get_settings()
    flask_app = create_app()
    print(f"Starting CentrAlign company app at {settings.app_origin}")
    flask_app.run(host=settings.app_host, port=settings.app_port, debug=False)


if __name__ == "__main__":
    cli_app()
