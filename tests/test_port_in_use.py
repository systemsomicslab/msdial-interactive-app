"""A second start on a port that is already taken.

Claude's MCP server starts MS-DIAL Interactive on port 8765. A person who then double-clicks the
launcher used to get a window that printed "Another process is already using this port" and closed
at once, so nothing seemed to happen. A start that finds an MS-DIAL Interactive on its port now opens
that instance in the browser instead; any other holder of the port still stops the start, with a
message that says so, and the Windows launcher keeps its window open on a failure.
"""

from __future__ import annotations

import contextlib
import io
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from msdial_app import server

ROOT = Path(__file__).resolve().parents[1]


class _NotInteractive(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - the http.server interface
        body = b'{"service": "something else"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@contextlib.contextmanager
def serving(handler):
    """A server on a free loopback port, with the exclusive binding the app itself uses."""
    holder = server.ExclusiveThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=holder.serve_forever, daemon=True)
    thread.start()
    try:
        yield holder.server_address[1]
    finally:
        holder.shutdown()
        holder.server_close()


def start(*arguments: str) -> tuple[int | str | None, list[str], str, str]:
    """Run main() as `app.py <arguments>`: its exit, the URLs it opened, stdout and stderr."""
    opened: list[str] = []
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", ["app.py", *arguments]), mock.patch.object(
        server.webbrowser, "open", side_effect=opened.append
    ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        with unittest.TestCase().assertRaises(SystemExit) as caught:
            server.main()
    return caught.exception.code, opened, out.getvalue(), err.getvalue()


class PortInUseTests(unittest.TestCase):
    def test_a_running_interactive_is_opened_instead_of_failing(self) -> None:
        with serving(server.Handler) as port:
            code, opened, out, _ = start("--port", str(port))
        self.assertEqual(0, code)
        self.assertEqual([f"http://127.0.0.1:{port}/"], opened)
        self.assertIn(f"MS-DIAL Interactive {server.__version__} is already running", out)

    def test_no_browser_reports_it_without_opening(self) -> None:
        with serving(server.Handler) as port:
            code, opened, out, _ = start("--port", str(port), "--no-browser")
        self.assertEqual(0, code)
        self.assertEqual([], opened)
        self.assertIn(f"http://127.0.0.1:{port}/", out)

    def test_the_rt_correction_start_opens_its_own_page(self) -> None:
        with serving(server.Handler) as port:
            code, opened, _, _ = start("--port", str(port), "--rt-correction")
        self.assertEqual(0, code)
        self.assertEqual([f"http://127.0.0.1:{port}/rt-correction"], opened)

    def test_another_version_is_opened_but_the_start_says_so_and_fails(self) -> None:
        with serving(server.Handler) as port, mock.patch.object(server, "__version__", "0.0.1"):
            code, opened, _, err = start("--port", str(port))
        self.assertEqual(1, code)
        self.assertEqual([f"http://127.0.0.1:{port}/"], opened)
        self.assertIn("this folder holds 0.0.1", err)

    def test_a_lab_start_does_not_take_a_local_instance_for_the_lab_server(self) -> None:
        with serving(server.Handler) as port:
            code, opened, _, err = start("--port", str(port), "--lab")
        self.assertEqual(1, code)
        self.assertEqual([], opened)
        self.assertIn("lab server cannot start there", err)

    def test_any_other_holder_of_the_port_stops_the_start(self) -> None:
        with serving(_NotInteractive) as port:
            code, opened, _, _ = start("--port", str(port))
        self.assertIsInstance(code, str)
        self.assertIn(f"Could not start MS-DIAL Interactive on 127.0.0.1:{port}", code)
        self.assertIn("no MS-DIAL Interactive answers there", code)
        self.assertEqual([], opened)

    def test_the_probe_reaches_a_wildcard_bind_through_the_loopback(self) -> None:
        self.assertEqual("127.0.0.1", server._loopback("0.0.0.0"))
        self.assertEqual("[::1]", server._loopback("::"))
        self.assertEqual("[::1]", server._loopback("::1"))


class WindowsLauncherTests(unittest.TestCase):
    def test_the_launcher_keeps_its_window_open_on_a_failure(self) -> None:
        lines = (ROOT / "scripts" / "start-local-windows.cmd").read_text(encoding="utf-8").splitlines()
        self.assertTrue(any(line.endswith("start-local-windows.ps1\" %*") for line in lines))
        self.assertIn('if not "%EXIT_CODE%"=="0" pause', lines)
        self.assertEqual("endlocal & exit /b %EXIT_CODE%", lines[-1])

    def test_each_windows_script_passes_on_the_app_s_exit_code(self) -> None:
        for name in ("start-local-windows.ps1", "start-rt-correction-windows.ps1", "start-lab-windows.ps1"):
            lines = [line for line in (ROOT / "scripts" / name).read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual("exit $LASTEXITCODE", lines[-1], name)


if __name__ == "__main__":
    unittest.main()
