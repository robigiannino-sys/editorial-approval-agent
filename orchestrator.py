"""
Orchestrator — endpoint backend per il flusso 2-tap mobile-first.

Tre endpoint principali:
  GET  /today                 → mostra brief più recente "Da Fare"
  POST /approve-brief/{id}    → genera visual + crea draft pagina WP IT
  POST /promote/{id}          → flip draft→publish
  POST /delete/{id}           → cancella le draft + archive Notion

NOTA Phase 1 (MVP):
  - Solo lingua IT (Italian master). Le 4 lingue arriveranno in Phase 2 con
    integrazione del traduttore (skill albeni-mt-translator).
  - Solo dominio merinouniversity.com (MU). WoM in Phase 2.
  - Estrae il corpo dall'angolo MU del brief Notion via parsing semplice.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# import locali — agent.py li ri-esporta o si fanno lazy
from visual_gen import generate_and_upload_visual, compose_prompt

log = logging.getLogger("orchestrator")

router = APIRouter()

# ─── Helpers (importati pigramente da agent.py per evitare cicli) ──────────


def _agent():
    """Lazy import di agent (per evitare circular imports)."""
    import agent

    return agent


# ─── Brief parsing ──────────────────────────────────────────────────────────


def extract_mu_content(notion_blocks_text: str) -> dict:
    """
    Dato il body markdown del brief Notion (concatenato), estrae:
      - title: prima opzione tra "Titolo proposto (opzione 1)"
      - slug: derivato dal title
      - body_html: corpo dell'Angolo MU formattato come HTML Gutenberg
      - focus_keyword: prima keyword DE menzionata
    """
    # Title — opzione 1
    title_match = re.search(
        r'\*\*Titolo proposto \(opzione 1\)\*\*\s*[:|]\s*"?([^"\n]+)"?',
        notion_blocks_text,
    )
    if not title_match:
        # Fallback: cerca "Titolo:" o "Titolo proposto opzione 1:"
        title_match = re.search(r'Titolo proposto[^\n]*opzione 1[^\n]*[:|]\s*"?([^"\n]+)"?', notion_blocks_text)
    title = title_match.group(1).strip().strip('"').strip("«»").strip() if title_match else "Untitled"

    # Slug (sanitize)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]

    # Body MU — la sezione "## Angolo MU" o "Angolo MU"
    mu_section_match = re.search(
        r"##?\s*Angolo MU[^\n]*\n(.+?)(?=\n##\s|\n---|\Z)",
        notion_blocks_text,
        flags=re.S,
    )
    if mu_section_match:
        mu_body = mu_section_match.group(1).strip()
    else:
        # Fallback: prendi una porzione centrale del brief
        mu_body = notion_blocks_text[500:3000]

    # Convert markdown chunks to Gutenberg HTML blocks
    body_html = _markdown_to_gutenberg(mu_body)

    # Focus keyword DE
    kw_match = re.search(r"\*\*Keyword.*?DE.*?\*\*[:|]?\s*`?([^`\n]+?)`?\s*[\n;]", notion_blocks_text, flags=re.I)
    focus_kw = kw_match.group(1).strip() if kw_match else ""

    return {
        "title": title,
        "slug": slug,
        "body_html": body_html,
        "focus_keyword": focus_kw[:60],
    }


def _markdown_to_gutenberg(md: str) -> str:
    """Converter molto basico markdown → blocchi Gutenberg minimi."""
    out = []
    paragraphs = re.split(r"\n\s*\n", md.strip())
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        # Heading
        h_match = re.match(r"^(#{2,4})\s+(.+)", p)
        if h_match:
            level = len(h_match.group(1))
            text = h_match.group(2).strip()
            out.append(
                f'<!-- wp:heading {{"level":{level}}} -->\n'
                f'<h{level}>{_escape_html(text)}</h{level}>\n'
                f'<!-- /wp:heading -->'
            )
            continue
        # Bold opening as paragraph
        text = _md_inline_to_html(p)
        out.append(
            f'<!-- wp:paragraph -->\n<p>{text}</p>\n<!-- /wp:paragraph -->'
        )
    return "\n\n".join(out)


def _md_inline_to_html(text: str) -> str:
    text = _escape_html(text)
    # Bold
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    # Italic
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    # Inline code
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ─── Notion helpers (read brief body) ───────────────────────────────────────


async def fetch_notion_page_body(client: httpx.AsyncClient, page_id: str) -> str:
    """Concatena tutti i blocchi text di una pagina Notion in un singolo markdown-ish."""
    a = _agent()
    out_lines = []
    cursor = None
    while True:
        url = f"{a.NOTION_API}/blocks/{page_id}/children"
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        r = await client.get(url, headers=a.NOTION_HEADERS, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        for blk in data.get("results", []):
            text = _block_to_text(blk)
            if text:
                out_lines.append(text)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return "\n\n".join(out_lines)


def _block_to_text(blk: dict) -> str:
    btype = blk.get("type", "")
    rich = (blk.get(btype, {}) or {}).get("rich_text", []) or []
    text = "".join(x.get("plain_text", "") for x in rich)
    if btype == "heading_2":
        return f"## {text}"
    if btype == "heading_3":
        return f"### {text}"
    if btype == "heading_1":
        return f"# {text}"
    if btype == "bulleted_list_item":
        return f"- {text}"
    return text


# ─── Endpoint: GET /today ───────────────────────────────────────────────────


@router.get("/today/data")
async def today_data() -> dict:
    """JSON: brief più recente Stato=Da Fare, con corpo + meta estratti."""
    a = _agent()
    async with httpx.AsyncClient() as client:
        entries = await a.notion_query_da_fare(client)
        if not entries:
            return {
                "ok": True,
                "empty": True,
                "message": "Nessun brief in attesa di approvazione.",
            }
        # Prendiamo il più recente
        entry = entries[0]
        page_id = entry["id"]
        titolo = a.get_prop(entry, "Contenuto") or ""
        dominio = a.get_prop(entry, "Dominio") or ""
        tipo = a.get_prop(entry, "Tipo Contenuto") or ""
        cluster = a.get_prop(entry, "Cluster") or ""
        shelf = a.get_prop(entry, "Shelf Life") or ""
        tema = a.get_prop(entry, "Tema") or ""
        lingua = a.get_prop(entry, "Lingua") or []
        keyword = a.get_prop(entry, "Keyword Target") or ""

        # Body
        body_text = await fetch_notion_page_body(client, page_id)
        extracted = extract_mu_content(body_text)

        return {
            "ok": True,
            "empty": False,
            "notion_id": page_id,
            "notion_url": entry.get("url", ""),
            "titolo": titolo,
            "dominio": dominio,
            "tipo_contenuto": tipo,
            "cluster": cluster,
            "shelf_life": shelf,
            "tema": tema,
            "lingue": lingua,
            "keyword_target": keyword,
            "extracted": extracted,
        }


# ─── Endpoint: POST /approve-brief/{id} ────────────────────────────────────


class ApproveResponse(BaseModel):
    ok: bool
    notion_id: str
    media_id: int
    image_url: str
    page_id: int
    preview_url: str
    permalink: str
    title: str


@router.post("/approve-brief/{notion_id}")
async def approve_brief(notion_id: str) -> ApproveResponse:
    """
    1) Legge il brief Notion (titolo, body, tema)
    2) Genera visual MU via Imagen, upload a WP
    3) Crea draft pagina IT via /albeni/v1/create-osservatorio-page
    4) Aggiorna Notion: Stato=In Pubblicazione + marker [DRAFT-PENDING]
    """
    a = _agent()
    async with httpx.AsyncClient() as client:
        # Fetch Notion entry
        page_url = f"{a.NOTION_API}/pages/{notion_id}"
        r = await client.get(page_url, headers=a.NOTION_HEADERS, timeout=20)
        r.raise_for_status()
        entry = r.json()

        titolo = a.get_prop(entry, "Contenuto") or ""
        dominio = a.get_prop(entry, "Dominio") or ""
        tema = a.get_prop(entry, "Tema") or "general"
        shelf = a.get_prop(entry, "Shelf Life") or "warm"

        if dominio != "merinouniversity.com":
            raise HTTPException(
                400, f"Phase 1 supporta solo merinouniversity.com (received: {dominio})"
            )

        # Body brief
        body_text = await fetch_notion_page_body(client, notion_id)
        extracted = extract_mu_content(body_text)

        # 1) Genera visual
        subject_phrase = extracted["title"][:120]
        prompt = compose_prompt(
            subject_phrase=f"a still life evoking '{subject_phrase}', subtle and contemplative",
            destination="MU",
            extra=f"Topic: {tema}.",
        )
        try:
            visual = await generate_and_upload_visual(
                client=client,
                prompt=prompt,
                topic=tema,
                destination="MU",
                aspect_ratio="16:9",
                alt_text=extracted["title"],
                caption=f"Albeni 1905 — {extracted['title']}",
            )
        except Exception as e:
            log.error(f"Visual generation failed: {e}")
            raise HTTPException(500, f"Visual generation failed: {e}")

        # 2) Crea draft pagina IT
        wp_payload = {
            "lang": "it",
            "title": extracted["title"],
            "slug": extracted["slug"],
            "content": extracted["body_html"],
            "featured_media": visual["media_id"],
            "seo_title": extracted["title"][:60],
            "seo_description": (extracted["title"] + ". " + extracted["body_html"][:300])[:155],
            "seo_focus_keyword": extracted["focus_keyword"],
            "status": "draft",
        }
        wp_url = f"{a.WP_BASE_MU}/wp-json/albeni/v1/create-osservatorio-page"
        r = await client.post(wp_url, headers=a.WP_HEADERS, json=wp_payload, timeout=60)
        if not r.is_success:
            log.error(f"WP create-draft failed: {r.status_code} {r.text[:300]}")
            raise HTTPException(500, f"WP create-draft failed HTTP {r.status_code}: {r.text[:200]}")
        draft = r.json()

        # 3) Aggiorna Notion: stato + marker
        await a.notion_update_stato(client, notion_id, "In Pubblicazione")
        ttl_days = a.shelf_ttl_days(shelf)
        ttl_until = datetime.now(timezone.utc) + timedelta(days=ttl_days)
        marker = a.build_draft_marker(
            domain=dominio,
            shelf=shelf,
            ttl_until=ttl_until,
            page_ids={"it": draft["id"]},
        )
        await a.notion_set_note(
            client, notion_id,
            f"{marker}\n\nVisual: {visual['image_url']}\nPreview: {draft.get('preview_link', draft['link'])}"
        )
        await a.notion_add_comment(
            client, notion_id,
            f"📝 Approved via /today — draft IT live, in attesa di promote.\n"
            f"Visual: {visual['image_url']}\n"
            f"Preview: {draft.get('preview_link', draft['link'])}"
        )

        return ApproveResponse(
            ok=True,
            notion_id=notion_id,
            media_id=visual["media_id"],
            image_url=visual["image_url"],
            page_id=draft["id"],
            preview_url=draft.get("preview_link", draft["link"]),
            permalink=draft.get("permalink", ""),
            title=extracted["title"],
        )


# ─── Endpoint: POST /promote/{notion_id} ───────────────────────────────────


@router.post("/promote/{notion_id}")
async def promote_draft(notion_id: str) -> dict:
    a = _agent()
    async with httpx.AsyncClient() as client:
        # Fetch Notion entry per leggere il marker
        page_url = f"{a.NOTION_API}/pages/{notion_id}"
        r = await client.get(page_url, headers=a.NOTION_HEADERS, timeout=20)
        r.raise_for_status()
        entry = r.json()
        note = a.get_prop(entry, "Note") or ""
        marker = a.parse_draft_marker(note)
        if not marker:
            raise HTTPException(400, "Marker [DRAFT-PENDING] non trovato nel Note")
        domain = marker.get("domain", "")
        page_ids_map = marker.get("page_ids", {})
        ids_list = list(page_ids_map.values()) if isinstance(page_ids_map, dict) else []
        if not ids_list:
            raise HTTPException(400, "Nessun page id nel marker")

        result = await a.wp_promote_drafts(client, domain, ids_list)
        await a.notion_update_stato(client, notion_id, "Pubblicato")
        await a.notion_add_comment(
            client, notion_id,
            f"✅ Pubblicato via /promote.\nResults: {json.dumps(result.get('results', []))[:1500]}"
        )
        return {"ok": True, "result": result}


# ─── Endpoint: POST /delete/{notion_id} ────────────────────────────────────


@router.post("/delete/{notion_id}")
async def delete_draft(notion_id: str) -> dict:
    a = _agent()
    async with httpx.AsyncClient() as client:
        page_url = f"{a.NOTION_API}/pages/{notion_id}"
        r = await client.get(page_url, headers=a.NOTION_HEADERS, timeout=20)
        r.raise_for_status()
        entry = r.json()
        note = a.get_prop(entry, "Note") or ""
        marker = a.parse_draft_marker(note)
        if not marker:
            raise HTTPException(400, "Marker [DRAFT-PENDING] non trovato nel Note")
        domain = marker.get("domain", "")
        page_ids_map = marker.get("page_ids", {})
        ids_list = list(page_ids_map.values()) if isinstance(page_ids_map, dict) else []
        if not ids_list:
            raise HTTPException(400, "Nessun page id nel marker")

        result = await a.wp_delete_drafts(client, domain, ids_list, force=False)
        await a.notion_update_stato(client, notion_id, "Archiviato")
        await a.notion_add_comment(
            client, notion_id,
            f"❌ Cancellato via /delete.\nResults: {json.dumps(result.get('results', []))[:1500]}"
        )
        return {"ok": True, "result": result}
