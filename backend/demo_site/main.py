"""
Local demo site.

This is NOT the product. It's a small, stable sandbox with a /login, /mail,
/search, /forms, /table and /download page so the general-purpose agent
in app/ has something reliable to practice on that won't change out from
under a demo. The agent's own code has zero knowledge of this site's
structure — it discovers everything through DOM inspection like it would
on any real website.
"""
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

demo = FastAPI()

_SHELL_OPEN = """<!doctype html><html><head><meta charset="utf-8">
<title>{title} — WebOrbit Demo Site</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#eef3ff;margin:0;
     padding:32px;color:#1c2340}}
.card{{max-width:640px;margin:0 auto;background:#fff;border-radius:16px;padding:28px 32px;
       box-shadow:0 6px 24px rgba(40,60,140,.08)}}
h1{{font-size:20px;margin:0 0 4px}}
.tag{{display:inline-block;font-size:11px;background:#e4ecff;color:#3355dd;padding:3px 9px;
      border-radius:999px;margin-bottom:16px}}
a{{color:#d6488f;text-decoration:none;font-weight:600}}
input,textarea{{width:100%;padding:10px 12px;margin:6px 0 14px;border:1px solid #dbe2f5;
       border-radius:10px;font-size:14px;box-sizing:border-box}}
button{{background:linear-gradient(135deg,#3f6bff,#d6488f);color:#fff;border:none;
        padding:10px 20px;border-radius:10px;font-weight:600;cursor:pointer}}
.item{{padding:12px 0;border-bottom:1px solid #eef1fb}}
.item a{{font-weight:600;color:#20264d}}
.muted{{color:#78809e;font-size:13px}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:8px;border-bottom:1px solid #eef1fb;font-size:14px}}
nav{{max-width:640px;margin:0 auto 14px;font-size:13px}}
nav a{{margin-right:14px;color:#3f6bff}}
</style></head><body>
<nav><a href="/demo/">Home</a><a href="/demo/search">Search</a><a href="/demo/mail">Mail</a>
<a href="/demo/forms">Forms</a><a href="/demo/table">Table</a><a href="/demo/download">Download</a>
<a href="/demo/login">Login</a></nav>
<div class="card"><span class="tag">DEMO SITE</span>
"""
_SHELL_CLOSE = "</div></body></html>"


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(_SHELL_OPEN.format(title=title) + f"<h1>{title}</h1>" + body + _SHELL_CLOSE)


@demo.get("/", response_class=HTMLResponse)
async def home():
    return page("WebOrbit Demo Site", """
        <p class="muted">A small general-purpose sandbox: search, mail, forms, a table and a
        download — for the agent to practice DOM inspection and action execution on.</p>
        <div class="item"><a href="/demo/search">/search</a> — a toy search engine</div>
        <div class="item"><a href="/demo/mail">/mail</a> — a toy inbox (requires login)</div>
        <div class="item"><a href="/demo/forms">/forms</a> — a registration form</div>
        <div class="item"><a href="/demo/table">/table</a> — a data table</div>
        <div class="item"><a href="/demo/download">/download</a> — a downloadable file</div>
        <div class="item"><a href="/demo/login">/login</a> — a login page</div>
    """)


# ---------------------------------------------------------------------------
# LOGIN
# ---------------------------------------------------------------------------
@demo.get("/login", response_class=HTMLResponse)
async def login_get():
    return page("Sign in", """
        <form method="post" action="/demo/login">
            <label>Username</label>
            <input name="username" placeholder="you@example.com" aria-label="username field">
            <label>Password</label>
            <input name="password" type="password" placeholder="Password" aria-label="password field">
            <button type="submit">Sign in</button>
        </form>
    """)


@demo.post("/login")
async def login_post(username: str = Form(...), password: str = Form(...)):
    resp = RedirectResponse(url="/demo/mail", status_code=303)
    resp.set_cookie("demo_session", "ok", max_age=3600)
    return resp


def _authed(request: Request) -> bool:
    return request.cookies.get("demo_session") == "ok"


# ---------------------------------------------------------------------------
# MAIL (requires "login")
# ---------------------------------------------------------------------------
_EMAILS = [
    {"id": "1", "sender": "careers@cambridgeinstitute.edu", "subject": "Internship placement update",
     "body": "Your internship placement documents have been approved."},
    {"id": "2", "sender": "no-reply@github.com", "subject": "Security alert",
     "body": "A new device signed in to your account."},
    {"id": "3", "sender": "recruiting@techcorp.example", "subject": "Interview scheduled",
     "body": "Your interview is confirmed for Monday at 10am."},
]


@demo.get("/mail", response_class=HTMLResponse)
async def mail(request: Request):
    if not _authed(request):
        return RedirectResponse(url="/demo/login")
    items = "".join(
        f'<div class="item"><a href="/demo/mail/{e["id"]}" aria-label="open email from {e["sender"]}">{e["subject"]}</a>'
        f'<div class="muted">{e["sender"]}</div></div>' for e in _EMAILS
    )
    return page("Inbox", f"""
        <form method="get" action="/demo/mail">
            <input name="q" placeholder="Search mail" aria-label="search box">
        </form>
        {items}
    """)


@demo.get("/mail/{email_id}", response_class=HTMLResponse)
async def mail_detail(request: Request, email_id: str):
    if not _authed(request):
        return RedirectResponse(url="/demo/login")
    e = next((x for x in _EMAILS if x["id"] == email_id), None)
    if not e:
        return page("Not found", "<p>No such email.</p>")
    return page(e["subject"], f"""
        <p class="muted">From: {e['sender']}</p>
        <p>{e['body']}</p>
    """)


# ---------------------------------------------------------------------------
# SEARCH
# ---------------------------------------------------------------------------
@demo.get("/search", response_class=HTMLResponse)
async def search(request: Request):
    q = request.query_params.get("q", "")
    results_html = ""
    if q:
        fake_results = [
            f"{q.title()} — Overview and guide",
            f"Top 10 resources about {q}",
            f"{q.title()} tutorial for beginners",
        ]
        results_html = "".join(
            f'<div class="item"><a href="/demo/search?q={q}&open={i}">{r}</a>'
            f'<div class="muted">demo-results.example/{i}</div></div>'
            for i, r in enumerate(fake_results)
        )
    return page("WebOrbit Demo Search", f"""
        <form method="get" action="/demo/search">
            <input name="q" placeholder="Search the demo web" aria-label="search box" value="{q}">
            <button type="submit">Search</button>
        </form>
        {results_html}
    """)


# ---------------------------------------------------------------------------
# FORMS
# ---------------------------------------------------------------------------
@demo.get("/forms", response_class=HTMLResponse)
async def forms_get():
    return page("Registration", """
        <form method="post" action="/demo/forms">
            <label>Name</label>
            <input name="name" aria-label="name field">
            <label>Email</label>
            <input name="email" aria-label="email field">
            <label>Message</label>
            <textarea name="message" aria-label="message field"></textarea>
            <button type="submit" aria-label="submit button">Submit</button>
        </form>
    """)


@demo.post("/forms", response_class=HTMLResponse)
async def forms_post(name: str = Form(""), email: str = Form(""), message: str = Form("")):
    return page("Thanks!", f"<p>Registered <b>{name}</b> ({email}).</p><p class='muted'>{message}</p>")


# ---------------------------------------------------------------------------
# TABLE
# ---------------------------------------------------------------------------
@demo.get("/table", response_class=HTMLResponse)
async def table():
    rows = [("Region", "Signups", "Growth"), ("APAC", "1,204", "+12%"),
            ("EMEA", "980", "+7%"), ("Americas", "1,540", "+18%")]
    body = "<table>" + "".join(
        "<tr>" + "".join(f"<{'th' if i==0 else 'td'}>{c}</{'th' if i==0 else 'td'}>" for c in r) + "</tr>"
        for i, r in enumerate(rows)
    ) + "</table>"
    return page("Quarterly signups", body)


# ---------------------------------------------------------------------------
# DOWNLOAD
# ---------------------------------------------------------------------------
@demo.get("/download", response_class=HTMLResponse)
async def download_page():
    return page("Sample report", """
        <p class="muted">A tiny text file, standing in for a PDF/report.</p>
        <a href="/demo/download/file" aria-label="download link" download>
            <button>Download report.txt</button>
        </a>
    """)


@demo.get("/download/file")
async def download_file():
    content = b"WebOrbit demo report\n---------------------\nGenerated for agent download testing.\n"
    return Response(content, media_type="text/plain",
                     headers={"Content-Disposition": "attachment; filename=report.txt"})


# ---------------------------------------------------------------------------
# DELAYED / CHANGING CONTENT — exercises the guardrail's retry path: the
# first inspection sees a disabled, unlabeled button; shortly after, JS
# relabels and enables it (simulating a page whose button text/state
# changes after initial load, e.g. content that finishes loading late).
# ---------------------------------------------------------------------------
@demo.get("/delayed", response_class=HTMLResponse)
async def delayed_page():
    return page("Confirm purchase", """
        <p class="muted">The button below starts disabled and unlabeled, then
        relabels itself to "Confirm purchase" after a short delay — for testing
        safe re-targeting when a page's content changes underneath the agent.</p>
        <button id="dyn-btn" disabled>Loading…</button>
        <script>
          setTimeout(function () {
            var b = document.getElementById('dyn-btn');
            b.removeAttribute('disabled');
            b.setAttribute('aria-label', 'confirm button');
            b.innerText = 'Confirm purchase';
          }, 1300);
        </script>
    """)

@demo.get("/long", response_class=HTMLResponse)
async def long_page():
    rows = "".join(f"<tr><td>{i}</td><td>Row {i} of demo content</td></tr>" for i in range(1, 61))
    body = f"""
        <p class="muted">A tall page for testing real scrolling.</p>
        <div style="height:1400px">Scroll target — lots of space above the table.</div>
        <table>{rows}</table>
    """
    return page("Long scrollable page", body)


# ---------------------------------------------------------------------------
# SHADOW DOM — a real web component UI, two shadow roots deep, like the
# search boxes on many modern sites (YouTube among them). Plain
# document.querySelectorAll cannot see inside these; this page exists to
# prove the inspector's shadow-piercing traversal actually works, not just
# hope it does on a real external site.
# ---------------------------------------------------------------------------
@demo.get("/shadow", response_class=HTMLResponse)
async def shadow_page():
    return page("Shadow DOM search widget", """
        <p class="muted">The search box and table below are rendered inside two
        nested shadow roots by real custom elements — nothing here is reachable
        via a plain document.querySelectorAll on the main document.</p>
        <div id="results" class="muted">No search yet.</div>
        <demo-search-box></demo-search-box>
        <script>
          customElements.define('demo-inner-box', class extends HTMLElement {
            connectedCallback() {
              const shadow = this.attachShadow({mode: 'open'});
              shadow.innerHTML = `
                <input type="text" aria-label="search box" placeholder="Search demo">
                <button aria-label="search button">Search</button>
                <table>
                  <tr><th>Item</th><th>Qty</th></tr>
                  <tr><td>Widget</td><td>3</td></tr>
                  <tr><td>Gadget</td><td>7</td></tr>
                </table>
              `;
              const doSearch = () => {
                const val = shadow.querySelector('input').value;
                document.getElementById('results').innerText = 'Searched for: ' + val;
              };
              shadow.querySelector('button').addEventListener('click', doSearch);
              shadow.querySelector('input').addEventListener('keydown', (e) => {
                if (e.key === 'Enter') doSearch();
              });
            }
          });
          customElements.define('demo-search-box', class extends HTMLElement {
            connectedCallback() {
              const shadow = this.attachShadow({mode: 'open'});
              shadow.innerHTML = '<demo-inner-box></demo-inner-box>';
            }
          });
        </script>
    """)
