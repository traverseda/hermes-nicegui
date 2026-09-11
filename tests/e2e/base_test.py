"""Base test class for hermes-nicegui E2E tests.

Provides helpers for starting the test server, logging in, and
common assertions that work across all E2E test cases.
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.async_api import Page


class BaseE2ETest:
    """Test base that starts the E2E server fixture and provides helpers.

    Inherit from this class and use `async def test_xxx(self, page)`
    methods. The fixture will start the server on a random port.
    """

    @pytest.fixture(autouse=True)
    async def _start_server(self, e2e_data_dir: Path, chrome_executable: str, context_page: Page):
        """Start the serve.py server on a random port and wait for it."""
        import httpx

        # Find a free port
        sock = __import__("socket").socket(__import__("socket").AF_INET, __import__("socket").SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        # Start the server process
        server_script = Path(__file__).parent / "serve.py"
        self._proc = subprocess.Popen(
            [
                __import__("sys").executable,
                str(server_script),
                "--port", str(port),
                "--data-dir", str(e2e_data_dir),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Wait for server to be ready
        base_url = f"http://127.0.0.1:{port}"
        for _ in range(30):
            try:
                resp = httpx.get(f"{base_url}/login", timeout=1.0)
                if resp.status_code == 200:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.3)
        else:
            self._proc.kill()
            stdout, stderr = self._proc.communicate()
            pytest.fail(f"Server did not start: {stderr.decode()[:500]}")

        # Navigate to login page
        await context_page.goto(base_url + "/login")

        # Login via JS event dispatching (NiceGUI inputs don't respond to fill())
        await self._login(context_page, "admin", "test1234")

        # Wait for login to complete
        await context_page.wait_for_timeout(500)

        # Store for use in tests
        self._base_url = base_url
        self._page = context_page
        self._port = port

        yield context_page

        # Cleanup: stop server
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()

    @staticmethod
    async def _login(page: Page, username: str, password: str) -> None:
        """Log in using JS event dispatching to trigger NiceGUI/Vue input bindings.

        NiceGUI inputs maintain their internal .value property via @input events,
        not via DOM value assignment. Playwright's fill() changes the DOM value
        but does NOT fire the events that NiceGUI listens on.
        """
        await page.evaluate(
            f"""() => {{
                const textInp = Array.from(document.querySelectorAll('input'))
                    .find(i => i.type === 'text');
                const passInp = Array.from(document.querySelectorAll('input'))
                    .find(i => i.type === 'password');
                if (textInp) {{
                    textInp.value = '{username}';
                    textInp.dispatchEvent(new Event('input', {{bubbles: true, cancelable: true}}));
                }}
                if (passInp) {{
                    passInp.value = '{password}';
                    passInp.dispatchEvent(new Event('input', {{bubbles: true, cancelable: true}}));
                }}
            }}"""
        )

        # Click the submit button
        submit_btn = page.locator('[data-testid="login-submit"]')
        if await submit_btn.count() > 0:
            await submit_btn.click()
        else:
            # Fallback: find button by text content
            btn = page.locator('button:has-text("Log in")')
            if await btn.count() > 0:
                await btn.click()

        await page.wait_for_timeout(1000)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def page(self) -> Page:
        return self._page

    async def should_see(self, text: str, *, timeout: int = 5000) -> None:
        """Wait until the page contains the given text."""
        await self.page.wait_for_function(
            f"() => document.body.innerText.includes('{text}')",
            timeout=timeout,
        )

    async def should_not_see(self, text: str, *, timeout: int = 3000) -> None:
        """Wait until the page does NOT contain the given text."""
        await self.page.wait_for_function(
            f"() => !document.body.innerText.includes('{text}')",
            timeout=timeout,
        )

    async def navigate_to_session(self, session_id: str = "sess-1") -> None:
        """Navigate to a session detail page."""
        await self.page.goto(f"{self.base_url}/sessions/{session_id}")
        await self.page.wait_for_timeout(500)

    def get_chrome_path(self) -> str:
        return __import__("os").path.join(
            __import__("pathlib").Path(__file__).parent.parent.parent.parent,
            "tests",
            "e2e",
            "conftest.py",
        )  # placeholder — see conftest
