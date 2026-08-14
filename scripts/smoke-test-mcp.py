from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parent.parent


async def smoke_test(
    input_path: str, port: int, restart_incompatible: bool, restart: bool, job_id: str
) -> dict[str, object]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "scripts" / "msdial-interactive-mcp.py")],
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            names = [tool.name for tool in listing.tools]
            status_result = await session.call_tool(
                "msdial_interactive_status", {"port": port}
            )
            status = json.loads(status_result.content[0].text)
            restarted = False
            if not status.get("running"):
                launch_result = await session.call_tool(
                    "msdial_interactive_launch",
                    {"port": port, "open_browser": False},
                )
                status = json.loads(launch_result.content[0].text)
            if status.get("running") and (restart or not status.get("compatible")):
                if not restart_incompatible:
                    raise RuntimeError(
                        "An app restart was requested or required. Pass --restart-incompatible to confirm replacement."
                    )
                restart_result = await session.call_tool(
                    "msdial_interactive_restart",
                    {"port": port, "confirmed": True, "open_browser": False},
                )
                restart = json.loads(restart_result.content[0].text)
                restarted = bool(restart.get("restarted"))
            result = await session.call_tool(
                "msdial_guided_analysis_plan",
                {"input_path": input_path, "answers": {}, "port": port},
            )
            payload = json.loads(result.content[0].text)
            console_result = await session.call_tool(
                "msdial_check_console_path", {"port": port}
            )
            console = json.loads(console_result.content[0].text)
            waited_job = None
            if job_id:
                wait_result = await session.call_tool(
                    "msdial_interactive_wait_for_completion",
                    {
                        "job_id": job_id,
                        "port": port,
                        "timeout_seconds": 5,
                        "poll_seconds": 1,
                    },
                )
                waited_job = json.loads(wait_result.content[0].text)
            return {
                "tool_count": len(names),
                "compatible": True,
                "restarted": restarted,
                "has_guided_plan": "msdial_guided_analysis_plan" in names,
                "has_publication": "msdial_generate_publication_report" in names,
                "has_peak_height_estimate": "msdial_estimate_peak_height" in names,
                "has_legacy_peak_height_tool": any("recommend" in name.casefold() for name in names),
                "has_console_discovery": "msdial_check_console_path" in names,
                "has_job_scoped_completion": "msdial_complete_guided_analysis" in names,
                "console_candidate_count": len(console.get("candidates", [])),
                "file_count": payload["input"]["file_count"],
                "next_question": payload["next_question"]["id"],
                "question_presentation": payload["next_question"].get("presentation"),
                "waited_job_id": (waited_job or {}).get("job", {}).get("id", ""),
                "waited_job_finished": (waited_job or {}).get("finished"),
            }


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the local MS-DIAL MCP server.")
    parser.add_argument("input_path")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--restart-incompatible", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--job-id", default="")
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                smoke_test(
                    args.input_path,
                    args.port,
                    args.restart_incompatible,
                    args.restart,
                    args.job_id,
                )
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
