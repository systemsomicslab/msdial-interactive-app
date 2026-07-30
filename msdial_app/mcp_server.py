from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


try:
    from mcp.server import MCPServer

    mcp = MCPServer("MS-DIAL Interactive")
except ImportError as error:
    raise RuntimeError(
        "The MCP Python SDK is required for the MS-DIAL Interactive MCP server. "
        "Install it with: python -m pip install \"mcp[cli]\""
    ) from error


def _base_url(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"http://{host}:{int(port)}"


def _request_json(
    method: str,
    path: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    body: dict[str, Any] | None = None,
    timeout: float = 5,
) -> dict[str, Any]:
    url = _base_url(host, port) + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not connect to MS-DIAL Interactive at {url}: {error.reason}") from error


def _status_or_error(host: str, port: int) -> dict[str, Any]:
    try:
        status = _request_json("GET", "/api/agent/status", host=host, port=port, timeout=2)
        return {"running": True, "url": _base_url(host, port), "status": status}
    except RuntimeError as error:
        return {"running": False, "url": _base_url(host, port), "error": str(error)}


@mcp.tool()
def msdial_interactive_status(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Return current MS-DIAL Interactive status and recent analysis jobs."""
    return _status_or_error(host, port)


@mcp.tool()
def msdial_interactive_launch(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Launch the local MS-DIAL Interactive web app if it is not already running."""
    current = _status_or_error(host, port)
    if current["running"]:
        if open_browser:
            webbrowser.open(current["url"])
        return {"launched": False, **current}

    command = [sys.executable, str(ROOT / "app.py"), "--host", host, "--port", str(port)]
    if not open_browser:
        command.append("--no-browser")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    deadline = time.time() + 10
    last = current
    while time.time() < deadline:
        time.sleep(0.5)
        last = _status_or_error(host, port)
        if last["running"]:
            return {"launched": True, **last}
    return {"launched": True, **last}


@mcp.tool()
def msdial_interactive_open(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Open the MS-DIAL Interactive web UI in the user's browser."""
    url = _base_url(host, port)
    webbrowser.open(url)
    return {"opened": True, "url": url, "status": _status_or_error(host, port)}


@mcp.tool()
def msdial_interactive_wait_for_completion(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout_seconds: int = 3600,
    poll_seconds: int = 10,
) -> dict[str, Any]:
    """Poll until the latest MS-DIAL Console job completes or fails."""
    deadline = time.time() + max(1, timeout_seconds)
    while True:
        status = _request_json("GET", "/api/agent/status", host=host, port=port, timeout=5)
        latest = status.get("latest_job") or {}
        if latest.get("status") in {"completed", "failed"}:
            return {"finished": True, "status": status}
        if time.time() >= deadline:
            return {"finished": False, "status": status}
        time.sleep(max(1, poll_seconds))


@mcp.tool()
def msdial_interactive_create_handoff(
    job_id: str = "",
    run_directory: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Create a data-mining handoff JSON for a completed MS-DIAL job or output folder."""
    payload = {"job_id": job_id, "run_directory": run_directory}
    return _request_json(
        "POST",
        "/api/agent/handoff",
        host=host,
        port=port,
        body=payload,
        timeout=30,
    )


@mcp.tool()
def msdial_interactive_validate_mztab(
    run_directory: str = "",
    file_path: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Validate mzTab-M files in an output folder, or one selected mzTab-M file."""
    return _request_json(
        "POST",
        "/api/mztab/validate",
        host=host,
        port=port,
        body={"run_directory": run_directory, "file_path": file_path},
        timeout=30,
    )


@mcp.tool()
def msdial_interactive_preview_mztab(
    run_directory: str,
    file_path: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Preview mzTab-M metadata, sections, first rows, and numeric columns."""
    return _request_json(
        "POST",
        "/api/mztab/preview",
        host=host,
        port=port,
        body={"run_directory": run_directory, "file_path": file_path},
        timeout=30,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
