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
from claude_writer import write_article_from_brief
from knowledge_base import find_companion as kb_find_companion

log = logging.getLogger("orchestrator")

router = APIRouter()

# ─── Helpers (importati pigramente da agent.py per evitare cicli) ──────────


def _agent():
    """Lazy import di agent (per evitare circular imports)."""
    import agent

    return agent


# ─── Brief parsing ──────────────────────────────────────────────────────────


def extract_mu_content(notion_blocks_text: str, fallback_title: str = "") -> dict:
    """
    Dato il body markdown del brief Notion (concatenato), estrae:
      - title: prima opzione tra "Titolo proposto (opzione 1)" o fallback Contenuto Notion
      - slug: derivato dal title
      - body_html: corpo dell'Angolo MU formattato come HTML Gutenberg
      - focus_keyword: prima keyword DE menzionata
    """
    # Title — opzione 1 nel body
    title_match = re.search(
        r'\*\*Titolo proposto \(opzione 1\)\*\*\s*[:|]\s*"?([^"\n]+)"?',
        notion_blocks_text,
    )
    if not title_match:
        title_match = re.search(r'Titolo proposto[^\n]*opzione 1[^\n]*[:|]\s*"?([^"\n]+)"?', notion_blocks_text)
    if title_match:
        title = title_match.group(1).strip().strip('"').strip("«»").strip()
    else:
        # Fallback al Contenuto Notion: "[NEWS] Topic — Tipo MU/WoM: Titolo"
        clean = re.sub(r"^\[NEWS\][^—]*—\s*[^:]+:\s*", "", fallback_title or "").strip()
        title = clean if clean else (fallback_title or "Untitled")

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



def extract_key_concepts(brief_text: str, max_items: int = 6) -> str:
    """
    Estrae concetti chiave dal body del brief Notion per arricchire il prompt
    visuale: numeri/percentuali/date, sostantivi tecnici ricorrenti, location.
    """
    concepts = []
    # 1. Date in vari formati
    for m in re.finditer(r"\b(\d{1,2}\s+(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre|january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{4}|\d{4}-\d{2}-\d{2}|mid-?20\d{2})\b", brief_text, flags=re.I):
        concepts.append(m.group(1))
        if len(concepts) >= 2:
            break
    # 2. Numeri rilevanti (percentuali, costi, micron, ecc.)
    for m in re.finditer(r"\b(\d+[.,]?\d*\s*(?:%|micron|μm|km|kg|°C|GB|euro|c/kg|g/m²|YoY|TWh))\b", brief_text):
        concepts.append(m.group(1))
        if len(concepts) >= 4:
            break
    # 3. Acronimi tecnici (DPP, ESPR, IWTO, ecc.)
    acronyms = set(re.findall(r"\b([A-Z]{3,6})\b", brief_text))
    # Filtra acronimi rilevanti del dominio merino/sostenibilità
    relevant = {"DPP","ESPR","IWTO","REACH","PFAS","LCA","UID","QR","NFC","RFID","EMI","AWEX","ZQ","UE","UV"}
    for ac in acronyms:
        if ac in relevant:
            concepts.append(ac)
            if len([c for c in concepts if c.isupper()]) >= 3:
                break
    # 4. Location notable
    for m in re.finditer(r"\b(Biella|Milano|Bruxelles|Amsterdam|New Zealand|Australia|Italia|Europa|EU)\b", brief_text):
        concepts.append(m.group(1))
        if len(concepts) >= max_items:
            break
    # Dedupe + limit
    seen = set()
    unique = []
    for c in concepts:
        cl = c.lower().strip()
        if cl not in seen:
            seen.add(cl)
            unique.append(c.strip())
    return ", ".join(unique[:max_items]) if unique else ""

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



async def find_wom_companion(client, tema: str) -> dict | None:
    """
    Cerca in Notion un'entry pubblicata WoM con lo stesso tema (gemello editoriale).
    Ritorna {"title": str, "url": str} o None.
    """
    if not tema:
        return None
    a = _agent()
    body = {
        "filter": {
            "and": [
                {"property": "Stato", "select": {"equals": "Pubblicato"}},
                {"property": "Dominio", "select": {"equals": "worldofmerino.com"}},
                {"property": "Tema", "rich_text": {"equals": tema}},
            ]
        },
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        "page_size": 1,
    }
    try:
        r = await client.post(
            f"{a.NOTION_API}/databases/{a.NOTION_DB_ID}/query",
            headers=a.NOTION_HEADERS,
            json=body,
            timeout=20,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return None
        entry = results[0]
        title = a.get_prop(entry, "Contenuto") or ""
        # Cerca URL nel campo Note (potrebbe contenere il permalink dopo publish)
        note = a.get_prop(entry, "Note") or ""
        url_match = re.search(r"https?://worldofmerino\.com/[\w\-/]+", note)
        permalink = url_match.group(0) if url_match else "https://worldofmerino.com/"
        # Pulisci titolo dal pattern "[NEWS] Topic — Tipo WoM: Titolo"
        clean = re.sub(r"^\[NEWS\][^—]*—\s*[^:]+:\s*", "", title).strip() or title
        return {"title": clean, "url": permalink}
    except Exception as e:
        log.warning(f"find_wom_companion failed: {e}")
        return None


# ─── Endpoint: GET /today ───────────────────────────────────────────────────


@router.get("/today/data")
async def today_data() -> dict:
    """JSON: brief più recente Stato=Da Fare, con corpo + meta estratti."""
    a = _agent()
    async with httpx.AsyncClient() as client:
        entries = await a.notion_query_da_fare(client)
        # Phase 1: filtra solo MU (WoM in Phase 2 con backport endpoint)
        entries = [e for e in entries if a.get_prop(e, "Dominio") == "merinouniversity.com"]
        if not entries:
            return {
                "ok": True,
                "empty": True,
                "message": "Nessun brief MU in attesa di approvazione (Phase 1 — WoM in Phase 2).",
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
        extracted = extract_mu_content(body_text, fallback_title=titolo)

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
        cluster = a.get_prop(entry, "Cluster") or ""

        if dominio != "merinouniversity.com":
            raise HTTPException(
                400, f"Phase 1 supporta solo merinouniversity.com (received: {dominio})"
            )

        # Body brief
        body_text = await fetch_notion_page_body(client, notion_id)
        extracted = extract_mu_content(body_text, fallback_title=titolo)

        # 1) Genera visual con prompt arricchito da concetti del body
        subject_phrase = extracted["title"][:120]
        key_concepts = extract_key_concepts(body_text)
        log.info(f"Visual prompt key_concepts: {key_concepts!r}")
        # MU: studio still-life o macro materico legato al tema dell'articolo
        if dominio == "merinouniversity.com":
            subject_descriptor = (
                f"a tactile material photograph related to '{subject_phrase}' — "
                f"showing natural wool fibers, textile samples, or laboratory objects "
                f"that directly evoke the article's specific subject matter"
            )
        else:
            subject_descriptor = (
                f"a still life evoking '{subject_phrase}', subtle and contemplative"
            )
        prompt = compose_prompt(
            subject_phrase=subject_descriptor,
            destination="MU" if dominio == "merinouniversity.com" else "WoM",
            key_concepts=key_concepts,
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
        # Hero image — allineamento default (dentro al container, non bleed laterale)
        hero_block = (
            f'<!-- wp:image {{"id":{visual["media_id"]},"sizeSlug":"large","linkDestination":"none"}} -->\n'
            f'<figure class="wp-block-image size-large">'
            f'<img src="{visual["image_url"]}" alt="{extracted["title"]}" class="wp-image-{visual["media_id"]}"/>'
            f'</figure>\n<!-- /wp:image -->'
        )
        # Heading H1 (sotto l'hero)
        sub_block = (
            f'<!-- wp:heading {{"level":1}} -->\n'
            f'<h1 class="wp-block-heading">{extracted["title"]}</h1>\n'
            f'<!-- /wp:heading -->'
        )
        # Riscrivi il body con Claude (il brief grezzo non è un articolo)
        try:
            article_html = await write_article_from_brief(
                brief_text=body_text,
                title=extracted["title"],
                domain=dominio,
                topic=tema,
                key_concepts=key_concepts,
            )
            log.info(f"Claude article generated: {len(article_html)} chars")
        except Exception as e:
            log.error(f"Claude article generation failed: {e}, falling back to brief body")
            article_html = extracted["body_html"]
        # CTA box editoriale al termine — collegamento WoM tramite knowledge base
        target = "worldofmerino.com" if dominio == "merinouniversity.com" else "merinouniversity.com"
        kb_match = await kb_find_companion(
            client=client,
            source_topic=tema,
            source_title=extracted["title"],
            target_domain=target,
            source_cluster=cluster if 'cluster' in dir() else "",
        )
        wom_companion = (
            {"title": kb_match.title, "url": kb_match.url} if kb_match else None
        )
        target_name = "World of Merino" if target == "worldofmerino.com" else "Merino University"
        if wom_companion:
            cta_heading = f"Continua su {target_name}"
            cta_intro = (
                f"Un approfondimento dello stesso tema, con un altro registro."
            )
            cta_url = wom_companion["url"]
            cta_label_primary = wom_companion["title"][:80]
        else:
            cta_heading = f"Vai a {target_name}"
            cta_intro = (
                f"Esplora il magazine."
            )
            cta_url = "https://worldofmerino.com/"
            cta_label_primary = "Vai a World of Merino"

        cta_block = (
            '<!-- wp:group {"className":"osservatorio-cta","layout":{"type":"constrained"}} -->\n'
            '<div class="wp-block-group osservatorio-cta">\n'
            f'<!-- wp:heading {{"level":3}} --><h3 class="wp-block-heading">{cta_heading}</h3><!-- /wp:heading -->\n'
            f'<!-- wp:paragraph --><p>{cta_intro}</p><!-- /wp:paragraph -->\n'
            '<!-- wp:buttons --><div class="wp-block-buttons">\n'
            f'<!-- wp:button {{"className":"is-style-fill"}} --><div class="wp-block-button"><a class="wp-block-button__link wp-element-button" href="{cta_url}">{cta_label_primary}</a></div><!-- /wp:button -->\n'
            '<!-- wp:button {"className":"is-style-outline"} --><div class="wp-block-button is-style-outline"><a class="wp-block-button__link wp-element-button" href="/osservatorio/">Tutti gli articoli Osservatorio</a></div><!-- /wp:button -->\n'
            '</div><!-- /wp:buttons -->\n'
            '</div>\n<!-- /wp:group -->'
        )
        # Banner approval — visibile finché la pagina è in draft, JS chiama Railway
        agent_base = os.environ.get("AGENT_PUBLIC_URL", "https://editorial-approval-agent-production.up.railway.app")
        approval_banner = (
            '<!-- wp:html -->\n'
            '<div id="albeni-approval-banner" data-notion-id="' + notion_id + '" '
            'style="background:#FEF6E4;border:1px solid #E0C38A;border-radius:6px;'
            'padding:14px 18px;margin-bottom:24px;font-family:system-ui,sans-serif;'
            'display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">\n'
            '<div style="font-size:14px;color:#5C4A1F;line-height:1.4;">'
            '<strong>Bozza in attesa di approvazione</strong> · '
            'Verifica testo, immagine e CTA, poi pubblica o cancella la draft.</div>\n'
            '<div style="display:flex;gap:8px;">\n'
            '<button id="albeni-approve-btn" style="background:#1F3A5F;color:#fff;border:none;'
            'padding:8px 16px;border-radius:4px;font-weight:600;cursor:pointer;font-size:13px;">'
            'Approva e pubblica</button>\n'
            '<button id="albeni-delete-btn" style="background:transparent;color:#7A2E2E;'
            'border:1px solid #C9876B;padding:8px 16px;border-radius:4px;cursor:pointer;font-size:13px;">'
            'Cancella draft</button>\n'
            '</div></div>\n'
            '<script>\n'
            '(function(){\n'
            '  var banner=document.getElementById("albeni-approval-banner");\n'
            '  if(!banner) return;\n'
            '  var nid=banner.dataset.notionId;\n'
            '  var base="' + agent_base + '";\n'
            '  function setMsg(t,c){banner.innerHTML=\'<div style="font-size:14px;color:\'+(c||"#5C4A1F")+\';">\'+t+\'</div>\';}\n'
            '  document.getElementById("albeni-approve-btn").onclick=function(){\n'
            '    setMsg("Pubblicazione in corso…");\n'
            '    fetch(base+"/promote/"+nid,{method:"POST"}).then(function(r){return r.json();}).then(function(j){\n'
            '      if(j.ok){setMsg("Articolo pubblicato — la pagina sarà ricaricata","#2D5A2D");setTimeout(function(){location.reload();},1200);}\n'
            '      else{setMsg("Errore: "+(j.error||"sconosciuto"),"#7A2E2E");}\n'
            '    }).catch(function(e){setMsg("Errore di rete: "+e,"#7A2E2E");});\n'
            '  };\n'
            '  document.getElementById("albeni-delete-btn").onclick=function(){\n'
            '    if(!confirm("Cancellare definitivamente la draft?")) return;\n'
            '    setMsg("Cancellazione in corso…");\n'
            '    fetch(base+"/delete/"+nid,{method:"POST"}).then(function(r){return r.json();}).then(function(j){\n'
            '      if(j.ok){setMsg("Draft cancellata","#7A2E2E");setTimeout(function(){location.href="/osservatorio/";},1200);}\n'
            '      else{setMsg("Errore: "+(j.error||"sconosciuto"),"#7A2E2E");}\n'
            '    }).catch(function(e){setMsg("Errore di rete: "+e,"#7A2E2E");});\n'
            '  };\n'
            '})();\n'
            '</script>\n'
            '<!-- /wp:html -->'
        )
        full_body = "\n\n".join([approval_banner, hero_block, sub_block, article_html, cta_block])
        wp_payload = {
            "lang": "it",
            "title": extracted["title"],
            "slug": extracted["slug"],
            "content": full_body,
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
