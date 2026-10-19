"""
DOM inspection.

The agent never guesses blindly at CSS selectors made up by an LLM. Instead,
before every action it asks the *real page* what is actually there right
now, tags each interactive element with a stable `data-woid` attribute, and
only then lets the resolver / guardrail pick a target. This is what the
project brief calls "the DOM is the source of truth".

Targeting-strategy support: for every element we now compute, in priority
order, the same signals a real accessibility tree would expose —
role, accessible name, associated <label>, a test id if the page ships one,
then the older placeholder/name/id/text signals — so guardrail.py can score
candidates in that order rather than a flat keyword match.

Shadow DOM: many real sites (YouTube among them) build their UI out of web
components with `attachShadow`, which plain `document.querySelectorAll`
cannot see into — it only walks the "light" DOM. Without piercing shadow
roots, the inspector would report zero interactive elements on such a page
even though the real controls are right there, nested inside a component.
`queryAllDeep()` below recurses into every *open* shadow root it finds (a
*closed* shadow root hides its contents from JS entirely — there's no way
around that from the page side, so those genuinely can't be inspected).
Playwright's own `page.locator('[data-woid="..."]')`, which the executor
uses to actually click/type, already pierces shadow DOM natively — so
once an element is found and tagged here, acting on it needs no change.
"""

INSPECT_JS = r"""
() => {
  function isVisible(el) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return false;
    return true;
  }

  // crude implicit-role table for the tags/types we care about — a real
  // accessibility API would compute this for us, but Playwright's evaluate
  // only gives us the raw DOM, so we approximate the common cases.
  function implicitRole(el) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      if (['submit', 'button'].includes(type)) return 'button';
      if (type === 'search') return 'searchbox';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      return 'textbox';
    }
    return '';
  }

  function labelFor(el) {
    if (el.id) {
      // search within the element's own root (shadow root or the main
      // document) — a <label for="x"> in a different shadow tree can't
      // legally target it anyway, per the HTML spec.
      const root = el.getRootNode();
      const lbl = root.querySelector ? root.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
      if (lbl) return lbl.innerText.trim().slice(0, 80);
    }
    const parentLabel = el.closest ? el.closest('label') : null;
    if (parentLabel) return parentLabel.innerText.trim().slice(0, 80);
    return '';
  }

  function testId(el) {
    return el.getAttribute('data-testid') || el.getAttribute('data-test-id') ||
           el.getAttribute('data-test') || el.getAttribute('data-qa') || '';
  }

  // Recursively collects every match for `selector`, descending into any
  // open shadow root along the way — this is what lets the inspector see
  // into web-component-based UIs instead of stopping at the light DOM.
  function queryAllDeep(root, selector, out, seen) {
    const matches = root.querySelectorAll(selector);
    for (const el of matches) {
      if (!seen.has(el)) { seen.add(el); out.push(el); }
    }
    const all = root.querySelectorAll('*');
    for (const el of all) {
      if (el.shadowRoot) queryAllDeep(el.shadowRoot, selector, out, seen);
    }
  }

  const SELECTOR = [
    'input', 'textarea', 'select', 'button', 'a[href]',
    '[role="button"]', '[role="link"]', '[role="searchbox"]',
    '[role="textbox"]', '[contenteditable="true"]'
  ].join(',');

  let deepNodes = [];
  queryAllDeep(document, SELECTOR, deepNodes, new Set());
  const nodes = deepNodes.slice(0, 400);
  let counter = 0;
  const out = [];

  for (const el of nodes) {
    if (!el.hasAttribute('data-woid')) {
      el.setAttribute('data-woid', 'wo' + (Date.now() % 100000) + '_' + (counter++));
    }
    const rect = el.getBoundingClientRect();
    const visible = isVisible(el);
    const disabled = !!(el.disabled || el.getAttribute('aria-disabled') === 'true');
    const text = (el.innerText || el.value || '').trim().slice(0, 120);

    out.push({
      woid: el.getAttribute('data-woid'),
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      role: el.getAttribute('role') || implicitRole(el),
      testid: testId(el),
      accessible_name: el.getAttribute('aria-label') ||
                        (el.getAttribute('aria-labelledby')
                          ? (document.getElementById(el.getAttribute('aria-labelledby'))?.innerText || '').trim().slice(0, 80)
                          : '') || '',
      label: labelFor(el),
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      aria_label: el.getAttribute('aria-label') || '',
      title: el.getAttribute('title') || '',
      text: text,
      href: el.getAttribute('href') || '',
      visible: visible,
      enabled: !disabled,
      rect: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
    });
  }

  let headingNodes = [];
  queryAllDeep(document, 'h1,h2,h3', headingNodes, new Set());
  const headings = headingNodes.slice(0, 15).map(h => h.innerText.trim().slice(0, 100)).filter(Boolean);

  let formNodes = [], tableNodes = [], linkNodes = [];
  queryAllDeep(document, 'form', formNodes, new Set());
  queryAllDeep(document, 'table', tableNodes, new Set());
  queryAllDeep(document, 'a[href]', linkNodes, new Set());

  return {
    url: location.href,
    title: document.title,
    elements: out,
    headings: headings,
    forms_count: formNodes.length,
    tables_count: tableNodes.length,
    links_count: linkNodes.length,
  };
}
"""


def summarize_for_llm(dom: dict, max_items: int = 60) -> str:
    """Compact, token-cheap text summary of the page used only when the LLM
    genuinely needs to re-plan against real DOM state."""
    lines = [
        f"URL: {dom.get('url')}", f"TITLE: {dom.get('title')}",
        f"HEADINGS: {', '.join(dom.get('headings', [])[:5]) or '(none)'}",
        f"forms={dom.get('forms_count', 0)} tables={dom.get('tables_count', 0)} links={dom.get('links_count', 0)}",
        "INTERACTIVE ELEMENTS:",
    ]
    for el in dom.get("elements", [])[:max_items]:
        if not el["visible"]:
            continue
        label = (el.get("testid") or el.get("accessible_name") or el.get("label")
                  or el["placeholder"] or el["text"] or el["name"] or el["title"])
        role = el.get("role") or el["tag"]
        lines.append(f"- [{el['woid']}] <{role}{' '+el['type'] if el['type'] else ''}> \"{label}\"")
    return "\n".join(lines)
