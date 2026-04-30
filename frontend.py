"""
Frontend mobile-first — 2-tap flow per approvazione editoriale.

Due pagine HTML servite direttamente da FastAPI:
  GET /today              → mostra il brief più recente "Da Fare"
                            con bottone "Approva e crea draft"
  GET /preview/{id}       → mostra la pagina draft + visual
                            con bottoni "Pubblica" e "Cancella"

Design: vanilla HTML5 + CSS, no framework. Mobile-first.
Tutta la dinamica passa da fetch() verso gli endpoint dell'orchestrator.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


# ─── /today ──────────────────────────────────────────────────────────────

TODAY_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<meta name="theme-color" content="#1a1a1a">
<title>Albeni Editorial — Today</title>
<style>
  :root {
    --bg: #f6f3ee;
    --text: #1a1a1a;
    --muted: #6b6b6b;
    --accent: #2c4a3e;
    --accent-hover: #1f3530;
    --danger: #8b2929;
    --border: #d8d2c8;
    --card: #ffffff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  header {
    padding: 24px 20px 12px;
    border-bottom: 1px solid var(--border);
  }
  header .date {
    color: var(--muted);
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  header h1 {
    margin: 4px 0 0;
    font-size: 22px;
    font-weight: 500;
  }
  main {
    padding: 20px;
    max-width: 720px;
    margin: 0 auto;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 16px;
  }
  .meta {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }
  .pill {
    display: inline-block;
    background: var(--bg);
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 12px;
    color: var(--muted);
    border: 1px solid var(--border);
  }
  .pill.shelf-hot { color: #b03a3a; }
  .pill.shelf-warm { color: #b06b3a; }
  .pill.shelf-evergreen { color: #2c4a3e; }
  h2 {
    margin: 0 0 12px;
    font-size: 19px;
    line-height: 1.3;
    font-weight: 500;
  }
  .body {
    color: #2a2a2a;
    font-size: 15px;
    max-height: 380px;
    overflow-y: auto;
    padding: 12px;
    background: var(--bg);
    border-radius: 6px;
    margin-bottom: 16px;
  }
  .body strong { color: var(--text); }
  button.action {
    display: block;
    width: 100%;
    padding: 16px;
    margin: 8px 0;
    border: none;
    border-radius: 10px;
    font-size: 17px;
    font-weight: 500;
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
    transition: background 0.15s;
  }
  button.primary {
    background: var(--accent);
    color: #fff;
  }
  button.primary:hover, button.primary:active {
    background: var(--accent-hover);
  }
  button.secondary {
    background: transparent;
    color: var(--muted);
    border: 1px solid var(--border);
  }
  .empty {
    text-align: center;
    padding: 60px 20px;
    color: var(--muted);
  }
  .empty .icon { font-size: 48px; margin-bottom: 16px; opacity: 0.4; }
  .loading {
    text-align: center;
    padding: 40px;
    color: var(--muted);
  }
  .spinner {
    display: inline-block;
    width: 20px;
    height: 20px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
    margin-right: 8px;
    vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .toast {
    position: fixed;
    bottom: 20px;
    left: 50%;
    transform: translateX(-50%);
    background: #1a1a1a;
    color: #fff;
    padding: 12px 20px;
    border-radius: 6px;
    font-size: 14px;
    z-index: 100;
    max-width: 90vw;
  }
  .toast.error { background: var(--danger); }
  .footer-note {
    text-align: center;
    padding: 24px;
    color: var(--muted);
    font-size: 12px;
  }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>

<header>
  <div class="date" id="today-date">—</div>
  <h1>Albeni Editorial</h1>
</header>

<main id="root">
  <div class="loading">
    <span class="spinner"></span> Caricamento brief…
  </div>
</main>

<div class="footer-note">
  Albeni 1905 — Invisible Luxury Editorial Pipeline
</div>

<script>
  const root = document.getElementById('root');
  const dateEl = document.getElementById('today-date');

  // Set date in locale italiana
  const now = new Date();
  const dateStr = now.toLocaleDateString('it-IT', {
    weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'
  });
  dateEl.textContent = dateStr;

  function showToast(msg, isError = false) {
    const t = document.createElement('div');
    t.className = 'toast' + (isError ? ' error' : '');
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  }

  async function loadBrief() {
    try {
      const r = await fetch('/today/data');
      const data = await r.json();
      if (data.empty) {
        root.innerHTML = `
          <div class="empty">
            <div class="icon">✨</div>
            <p>Nessun brief in attesa.<br>Lo scanner è schedulato per le 7:00.</p>
          </div>
        `;
        return;
      }
      const shelfClass = `shelf-${(data.shelf_life || '').toLowerCase()}`;
      root.innerHTML = `
        <div class="card">
          <div class="meta">
            <span class="pill">${esc(data.dominio || '?')}</span>
            <span class="pill">${esc(data.tipo_contenuto || '?')}</span>
            <span class="pill ${shelfClass}">${esc(data.shelf_life || '?')}</span>
            <span class="pill">${esc(data.cluster || '?')}</span>
          </div>
          <h2>${esc(data.extracted.title)}</h2>
          <div class="body">${formatBody(data.extracted.body_html)}</div>
          <div style="margin-bottom: 12px; font-size: 13px; color: var(--muted);">
            Tema: <strong>${esc(data.tema || 'general')}</strong> ·
            Keyword DE: <code>${esc(data.extracted.focus_keyword || '—')}</code>
          </div>
          <button class="action primary" id="approveBtn">
            Approva e crea draft
          </button>
          <a href="${esc(data.notion_url)}" target="_blank">
            <button class="action secondary">Apri brief su Notion</button>
          </a>
        </div>
      `;
      document.getElementById('approveBtn').addEventListener('click', () => approve(data.notion_id));
    } catch (e) {
      root.innerHTML = `<div class="empty">⚠️ Errore: ${esc(e.message)}</div>`;
    }
  }

  async function approve(notionId) {
    const btn = document.getElementById('approveBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Genero visual e creo draft…';
    try {
      const r = await fetch(`/approve-brief/${notionId}`, {method: 'POST'});
      if (!r.ok) {
        const err = await r.text();
        throw new Error(`HTTP ${r.status}: ${err.slice(0, 200)}`);
      }
      const out = await r.json();
      // Redirect alla preview
      window.location.href = `/preview/${notionId}`;
    } catch (e) {
      showToast(e.message, true);
      btn.disabled = false;
      btn.innerHTML = 'Approva e crea draft';
    }
  }

  function esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function formatBody(html) {
    // Se è già HTML, lo lasciamo (vienne da Notion già strippato di tag)
    return html || '<em>Brief vuoto</em>';
  }

  loadBrief();
</script>
</body>
</html>"""


PREVIEW_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<meta name="theme-color" content="#1a1a1a">
<title>Albeni Editorial — Preview</title>
<style>
  :root {
    --bg: #f6f3ee;
    --text: #1a1a1a;
    --muted: #6b6b6b;
    --accent: #2c4a3e;
    --accent-hover: #1f3530;
    --danger: #8b2929;
    --danger-hover: #6b1f1f;
    --border: #d8d2c8;
    --card: #ffffff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }
  header {
    padding: 24px 20px 12px;
    border-bottom: 1px solid var(--border);
  }
  header .step {
    color: var(--muted);
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  header h1 {
    margin: 4px 0 0;
    font-size: 22px;
    font-weight: 500;
  }
  main {
    padding: 20px;
    max-width: 720px;
    margin: 0 auto;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 16px;
  }
  .visual-img {
    width: 100%;
    height: auto;
    display: block;
    border-radius: 6px;
    margin-bottom: 16px;
  }
  .preview-link {
    display: block;
    width: 100%;
    padding: 14px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    text-align: center;
    color: var(--accent);
    font-weight: 500;
    margin-bottom: 16px;
    text-decoration: none;
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  .preview-link:active { background: #ebe7df; }
  button.action {
    display: block;
    width: 100%;
    padding: 16px;
    margin: 8px 0;
    border: none;
    border-radius: 10px;
    font-size: 17px;
    font-weight: 500;
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  button.publish { background: var(--accent); color: #fff; }
  button.publish:active { background: var(--accent-hover); }
  button.cancel { background: var(--danger); color: #fff; }
  button.cancel:active { background: var(--danger-hover); }
  button.keep { background: transparent; color: var(--muted); border: 1px solid var(--border); }
  .loading { text-align: center; padding: 40px; color: var(--muted); }
  .spinner {
    display: inline-block;
    width: 20px; height: 20px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
    margin-right: 8px;
    vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .toast {
    position: fixed;
    bottom: 20px;
    left: 50%;
    transform: translateX(-50%);
    background: #1a1a1a;
    color: #fff;
    padding: 12px 20px;
    border-radius: 6px;
    font-size: 14px;
    z-index: 100;
    max-width: 90vw;
  }
  .toast.error { background: var(--danger); }
  .toast.success { background: var(--accent); }
  .success-screen { text-align: center; padding: 40px 20px; }
  .success-screen .icon { font-size: 56px; margin-bottom: 16px; }
  h2 { margin: 0 0 12px; font-size: 19px; line-height: 1.3; font-weight: 500; }
</style>
</head>
<body>

<header>
  <div class="step">Step 2 di 2 — Preview</div>
  <h1 id="title">Caricamento…</h1>
</header>

<main id="root">
  <div class="loading"><span class="spinner"></span> Carico draft…</div>
</main>

<script>
  const NOTION_ID = "{{NOTION_ID}}";
  const root = document.getElementById('root');
  const titleEl = document.getElementById('title');

  function showToast(msg, kind='') {
    const t = document.createElement('div');
    t.className = 'toast ' + kind;
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  }

  function esc(s) {
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  async function loadDraft() {
    try {
      const r = await fetch(`/preview/${NOTION_ID}/data`);
      const data = await r.json();
      if (!data.ok) {
        root.innerHTML = `<div class="loading">⚠️ ${esc(data.error || 'Errore')}</div>`;
        return;
      }
      titleEl.textContent = data.title || 'Untitled';
      root.innerHTML = `
        <div class="card">
          ${data.image_url ? `<img class="visual-img" src="${esc(data.image_url)}" alt="${esc(data.title)}">` : ''}
          <h2>${esc(data.title)}</h2>
          <p style="color: var(--muted); font-size: 13px;">
            Pagina draft creata su MU. Aprila in una nuova tab per vedere il rendering esatto:
          </p>
          <a href="${esc(data.preview_url)}" target="_blank" class="preview-link">
            Apri preview pagina (IT)
          </a>
          <button class="action publish" id="publishBtn">
            ✓ Pubblica live (4 lingue Phase 2 — solo IT in Phase 1)
          </button>
          <button class="action keep" id="keepBtn">
            Mantieni come draft, decido dopo
          </button>
          <button class="action cancel" id="cancelBtn">
            ✕ Cancella draft
          </button>
        </div>
      `;
      document.getElementById('publishBtn').addEventListener('click', () => action('promote'));
      document.getElementById('cancelBtn').addEventListener('click', () => {
        if (confirm('Sicuro di voler cancellare la draft?')) action('delete');
      });
      document.getElementById('keepBtn').addEventListener('click', () => {
        showToast('Draft mantenuta. Tornerai più tardi.', 'success');
        setTimeout(() => window.location.href = '/today', 1500);
      });
    } catch (e) {
      root.innerHTML = `<div class="loading">⚠️ ${esc(e.message)}</div>`;
    }
  }

  async function action(kind) {
    const btn = document.getElementById(kind === 'promote' ? 'publishBtn' : 'cancelBtn');
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span> ${kind === 'promote' ? 'Pubblico…' : 'Cancello…'}`;
    try {
      const r = await fetch(`/${kind}/${NOTION_ID}`, {method: 'POST'});
      if (!r.ok) {
        const err = await r.text();
        throw new Error(`HTTP ${r.status}: ${err.slice(0, 200)}`);
      }
      const out = await r.json();
      const okMsg = kind === 'promote' ? 'Pubblicato live!' : 'Draft cancellata.';
      root.innerHTML = `
        <div class="success-screen">
          <div class="icon">${kind === 'promote' ? '✅' : '🗑️'}</div>
          <h2>${okMsg}</h2>
          <p style="color: var(--muted);">${kind === 'promote' ? 'L\\'articolo è ora visibile online.' : 'La pagina è stata mossa nel cestino di WP.'}</p>
          <button class="action publish" onclick="window.location.href='/today'">Torna al brief del giorno</button>
        </div>
      `;
    } catch (e) {
      showToast(e.message, 'error');
      btn.disabled = false;
      btn.innerHTML = kind === 'promote' ? '✓ Pubblica live' : '✕ Cancella draft';
    }
  }

  loadDraft();
</script>
</body>
</html>"""


@router.get("/today", response_class=HTMLResponse)
async def today_page() -> str:
    return TODAY_HTML


@router.get("/preview/{notion_id}", response_class=HTMLResponse)
async def preview_page(notion_id: str) -> str:
    return PREVIEW_HTML.replace("{{NOTION_ID}}", notion_id)


# ─── /preview/{id}/data — JSON per il frontend ──────────────────────────────


@router.get("/preview/{notion_id}/data")
async def preview_data(notion_id: str) -> dict:
    """JSON con i dati per la pagina /preview: image_url, preview_url, title."""
    import httpx

    def _agent():
        import agent

        return agent

    a = _agent()
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{a.NOTION_API}/pages/{notion_id}", headers=a.NOTION_HEADERS, timeout=20
        )
        if not r.is_success:
            return {"ok": False, "error": f"Notion fetch failed: {r.status_code}"}
        entry = r.json()
        note = a.get_prop(entry, "Note") or ""
        marker = a.parse_draft_marker(note)
        if not marker:
            return {"ok": False, "error": "Marker [DRAFT-PENDING] non trovato"}
        page_ids_map = marker.get("page_ids", {})
        if not isinstance(page_ids_map, dict) or not page_ids_map:
            return {"ok": False, "error": "Page ids vuoti"}

        # Estrae image_url e preview_url dal Note (li avevamo scritti lì)
        image_url = ""
        preview_url = ""
        m = re.search(r"Visual:\s*(\S+)", note)
        if m:
            image_url = m.group(1)
        m = re.search(r"Preview:\s*(\S+)", note)
        if m:
            preview_url = m.group(1)

        title = a.get_prop(entry, "Contenuto") or "Untitled"
        # Pulisci eventuale prefix "[NEWS] ..."
        title_clean = re.sub(r"^\[NEWS\]\s*[^—]+\s*—\s*", "", title)

        return {
            "ok": True,
            "title": title_clean,
            "image_url": image_url,
            "preview_url": preview_url,
            "domain": marker.get("domain", ""),
            "page_ids": page_ids_map,
        }


# Re need import re for the function above
import re
