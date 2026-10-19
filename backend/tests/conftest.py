import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_PORT = 8123


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def live_server():
    """Starts the real WebOrbit app with a real uvicorn worker in a
    subprocess, so end-to-end tests exercise the actual process a user
    would run — not an in-memory stub."""
    env = os.environ.copy()
    env["WEBORBIT_PORT"] = str(TEST_PORT)
    env["WEBORBIT_HOST"] = "127.0.0.1"
    # Bound any login/confirm wait tightly during tests. In real usage these
    # default to 180s/60s so a human has time to act; a pytest run should
    # never silently block for minutes if a test triggers one by mistake —
    # that is exactly what an earlier debugging pass surfaced as an
    # apparent "hang". A genuine automated login-handoff test still passes
    # comfortably inside 20s.
    env["WEBORBIT_LOGIN_TIMEOUT"] = "20"
    env["WEBORBIT_CONFIRM_TIMEOUT"] = "10"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(TEST_PORT)],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if _port_open(TEST_PORT):
                try:
                    r = httpx.get(f"http://127.0.0.1:{TEST_PORT}/api/config", timeout=1)
                    if r.status_code == 200:
                        break
                except Exception:
                    pass
            time.sleep(0.3)
        else:
            proc.terminate()
            raise RuntimeError("server did not start in time")
        yield f"http://127.0.0.1:{TEST_PORT}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
