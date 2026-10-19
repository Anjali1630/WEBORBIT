"""
Integration tests that exercise the real FastAPI app over real HTTP
(Starlette's TestClient runs the actual ASGI app in-process — no mocking
of routes or handlers).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def test_index_serves_dashboard():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "WebOrbit" in resp.text


def test_api_config_reports_llm_mode():
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_mode"] in ("LOCAL DEMO FALLBACK", "OPENROUTER FREE")


def test_api_history_is_a_list():
    resp = client.get("/api/history")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_api_metrics_shape():
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert "tasks" in body and "actions" in body and "planning" in body


def test_history_detail_404_for_unknown_id():
    resp = client.get("/api/history/does-not-exist")
    assert resp.status_code == 404


# ---- demo site pages ------------------------------------------------------

def test_demo_home():
    resp = client.get("/demo/")
    assert resp.status_code == 200
    assert "DEMO SITE" in resp.text


def test_demo_search_page():
    resp = client.get("/demo/search?q=python")
    assert resp.status_code == 200
    assert "python" in resp.text.lower()


def test_demo_table_page_has_real_table():
    resp = client.get("/demo/table")
    assert resp.status_code == 200
    assert "<table>" in resp.text
    assert "APAC" in resp.text


def test_demo_mail_redirects_to_login_when_unauthenticated():
    resp = client.get("/demo/mail", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)
    assert "/demo/login" in resp.headers["location"]


def test_demo_login_then_mail_works():
    resp = client.post("/demo/login", data={"username": "u", "password": "p"}, follow_redirects=False)
    assert resp.status_code == 303
    cookie = resp.cookies.get("demo_session")
    assert cookie == "ok"
    resp2 = client.get("/demo/mail", cookies={"demo_session": "ok"})
    assert resp2.status_code == 200
    assert "Inbox" in resp2.text


def test_demo_download_file_is_nonempty():
    resp = client.get("/demo/download/file")
    assert resp.status_code == 200
    assert len(resp.content) > 0
    assert resp.headers["content-disposition"].startswith("attachment")


def test_demo_delayed_page_starts_disabled():
    resp = client.get("/demo/delayed")
    assert resp.status_code == 200
    assert "disabled" in resp.text
    # the aria-label is only ever applied at runtime by client-side JS —
    # it must not be present as a literal HTML attribute in the initial markup
    # (the string "confirm button" does legitimately appear inside the inline
    # <script> source, so we check for the attribute form specifically)
    assert 'aria-label="confirm button"' not in resp.text


def test_demo_long_page_is_tall_and_has_a_table():
    resp = client.get("/demo/long")
    assert resp.status_code == 200
    assert "<table>" in resp.text
    assert "1400px" in resp.text


def test_demo_shadow_page_defines_the_web_components():
    resp = client.get("/demo/shadow")
    assert resp.status_code == 200
    assert "attachShadow" in resp.text
    assert "demo-search-box" in resp.text
    # the actual <input>/<button>/<table> only exist once the custom
    # elements run client-side and build their shadow trees — the static
    # HTML this endpoint returns has no light-DOM <input> element at all
    # (the markup for one only appears inside the inline <script>'s JS
    # template-literal source, not as real, parseable HTML)
    import re
    html_without_script = re.sub(r"<script>.*?</script>", "", resp.text, flags=re.DOTALL)
    assert "<input" not in html_without_script




def test_demo_forms_submission():
    resp = client.post("/demo/forms", data={"name": "Anjali", "email": "a@example.com", "message": "hi"})
    assert resp.status_code == 200
    assert "Anjali" in resp.text
