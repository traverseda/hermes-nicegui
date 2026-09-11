"""P2 E2E test: reconnect after disconnect preserves transcript.

Verifies that disconnecting and reconnecting to the session detail page
recovers the transcript from state.db without duplicates.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright

# Chrome binary for NixOS.
CHROME_EXEC = "/var/lib/hermes/.local/bin/google-chrome"

# Nix store Python with all app deps.
PYTHON_BIN = "/nix/store/09x48rzfkm2p5gp46w0hvd37k1nv8yjn-python3-3.12.14-env/bin/python"

# Nix store paths for test deps.
NIX_PKGS = [
    "/nix/store/45i5kic1vf2rpwbbb5bs4pxrhk46yna0-python3.12-pytest-9.1.1/lib/python3.12/site-packages",
    "/nix/store/s1dhvqhb5l0xrkz6yivpnvr7gizm27fx-python3.12-playwright-1.61.0/lib/python3.12/site-packages",
    "/nix/store/3wcazd023xpnx9pv49xaiq82ym09m8c1-playwright-core-1.61.1/lib/python3.12/site-packages",
    "/nix/store/vx9swiahyw12lvxmpfk21jhzgm58z8w9-python3.12-pytest-asyncio-0.26.0/lib/python3.12/site-packages",
    "/nix/store/5gs5xkakgfh6dkcwx0wfvi9mmy9qw1wj-python3.12-pluggy-1.6.0/lib/python3.12/site-packages",
    "/nix/store/pg2y482299zq8g7bpwsa1r2nsqxwyq3z-python3.12-iniconfig-2.3.0/lib/python3.12/site-packages",
    "/nix/store/kc2fvnmrwahh9jn1h9nhm4y6nvkwal1n-python3.12-packaging-26.2/lib/python3.12/site-packages",
    "/nix/store/y11cl0a2xi4n84z7hyxqbjq2865wjr9i-python3.12-pyee-13.0.0/lib/python3.12/site-packages",
    "/nix/store/q345byskmy3b9sdn3p3m2ximgbjixgix-python3.12-greenlet-3.5.3/lib/python3.12/site-packages",
    "/nix/store/sr7ki60yv913cdmzkvknw66l92pslh6q-python3.12-sniffio-1.3.1/lib/python3.12/site-packages",
    "/nix/store/6iiqrify1sy2bch3x7n7l2fhp57nslxk-python3-3.12.14-env/lib/python3.12/site-packages",
]


def _make_nix_pythonpath() -> str:
    parts = NIX_PKGS + [
        "/nix/store/09x48rzfkm2p5gp46w0hvd37k1nv8yjn-python3-3.12.14-env/lib/python3.12/site-packages",
    ]
    return os.pathsep.join(parts)


async def test_reconnect_after_disconnect(tmp_path: Path) -> None:
    """Disconnect browser, reconnect, verify transcript recovered without duplicates."""

    # Find a free port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    data_dir = tmp_path / "e2e-data"
    data_dir.mkdir()

    # Start the test server.
    server_script = Path(__file__).parent / "serve.py"
    env = os.environ.copy()
    env["PYTHONPATH"] = _make_nix_pythonpath() + os.pathsep + str(server_script.parent.parent.parent / "src")
    env.pop("NICEGUI_SCREEN_TEST_PORT", None)
    env.pop("NICEGUI_USER_SIMULATION", None)

    proc = subprocess.Popen(
        [PYTHON_BIN, str(server_script), "--port", str(port), "--data-dir", str(data_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        # Wait for server.
        base_url = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                resp = httpx.get(f"{base_url}/login", timeout=1.0)
                if resp.status_code == 200:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.3)
        else:
            proc.kill()
            stdout, stderr = proc.communicate()
            pytest.fail(f"Server did not start after 30s. stderr: {stderr.decode()[:2000]}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=CHROME_EXEC,
                headless=True,
            )
            context = await browser.new_context()
            try:
                context.set_default_timeout(15000)
                page = await context.new_page()

                # Navigate to login page.
                await page.goto(base_url + "/login")
                await page.wait_for_timeout(500)

                # Login via JS event dispatching.
                await page.evaluate(
                    """() => {
                        const textInp = Array.from(document.querySelectorAll('input'))
                            .find(i => i.type === 'text');
                        const passInp = Array.from(document.querySelectorAll('input'))
                            .find(i => i.type === 'password');
                        if (textInp) {
                            textInp.value = 'admin';
                            textInp.dispatchEvent(new Event('input', {bubbles: true, cancelable: true}));
                        }
                        if (passInp) {
                            passInp.value = 'test1234';
                            passInp.dispatchEvent(new Event('input', {bubbles: true, cancelable: true}));
                        }
                    }"""
                )

                # Click login.
                submit_btn = page.locator('[data-testid="login-submit"]')
                if await submit_btn.count() > 0:
                    await submit_btn.click()
                else:
                    btn = page.locator('button:has-text("Log in")')
                    if await btn.count() > 0:
                        await btn.click()
                    else:
                        btn = page.locator('button:has-text("Create account")')
                        if await btn.count() > 0:
                            await btn.click()

                # Wait for login to complete.
                await page.wait_for_url(base_url, timeout=5000)
                await page.wait_for_timeout(500)

                # Navigate to session detail.
                await page.goto(f"{base_url}/sessions/sess-1")
                await page.wait_for_timeout(1000)

                # Verify transcript is visible before disconnect.
                body_before = await page.inner_text("body")
                assert "How do I access your API?" in body_before, (
                    f"Pre-disconnect: expected 'How do I access your API?' in body. "
                    f"Body preview: {body_before[:500]}"
                )
                assert "Here is the answer." in body_before, (
                    f"Pre-disconnect: expected 'Here is the answer.' in body. "
                    f"Body preview: {body_before[:500]}"
                )

                # Disconnect: close the browser context (simulates network disconnect).
                await context.close()

                # Give the server a moment to register the disconnect.
                await asyncio.sleep(1)

                # Reconnect: create a new context + page and navigate to the same URL.
                context = await browser.new_context()
                try:
                    context.set_default_timeout(15000)
                    page = await context.new_page()

                    # Navigate back to the same session URL.
                    await page.goto(f"{base_url}/sessions/sess-1")
                    await page.wait_for_timeout(2000)

                    # Verify transcript is recovered from state.db.
                    body_after = await page.inner_text("body")
                    assert "How do I access your API?" in body_after, (
                        f"Post-reconnect: expected 'How do I access your API?' in body. "
                        f"Body preview: {body_after[:500]}"
                    )
                    assert "Here is the answer." in body_after, (
                        f"Post-reconnect: expected 'Here is the answer.' in body. "
                        f"Body preview: {body_after[:500]}"
                    )
                    assert "Used a tool to check." in body_after, (
                        f"Post-reconnect: expected 'Used a tool to check.' in body. "
                        f"Body preview: {body_after[:500]}"
                    )

                    # Verify no duplicate messages: count occurrences.
                    full_text = body_after
                    user_msg_count = full_text.count("How do I access your API?")
                    assert user_msg_count == 1, (
                        f"Expected 1 occurrence of 'How do I access your API?' "
                        f"but found {user_msg_count} (possible duplicate after reconnect)"
                    )

                    assistant_count = full_text.count("Here is the answer.")
                    assert assistant_count == 1, (
                        f"Expected 1 occurrence of 'Here is the answer.' "
                        f"but found {assistant_count} (possible duplicate after reconnect)"
                    )

                    print("PASS: reconnect after disconnect preserves transcript without duplicates")

                finally:
                    await context.close()

            finally:
                await browser.close()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
