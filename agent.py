"""
Editorial Approval Agent — Albeni 1905
=======================================

Servizio FastAPI che gira su Railway 24/7. Tre responsabilità:

  1. Polling Notion ogni 60 minuti per le entry "Da Fare":
     - Applica policy editoriale (vedi editorial-policy.md)
     - Approve  → deploy in DRAFT su WP, manda WhatsApp a Roberto
     - Reject   → archive Notion + commento
     - Hold     → lascia Da Fare + commento

  2. Endpoint POST /twilio-webhook — riceve risposte di Roberto:
     - "OK"   → flip draft → publish (chiamata albeni/v1/promote-draft-to-publish)
     - "STOP" → cancella draft (chiamata albeni/v1/delete-drafts)
     - tutto il resto → commento Notion "comando non riconosciuto"

  3. Polling 1×/giorno per scadenze TTL delle draft pending:
     - hot       → auto-cancella dopo 7 giorni
     - warm      → auto-cancella dopo 20 giorni
     - evergreen → auto-cancella dopo 30 giorni

Lo state è memorizzato dentro Notion stesso, nella property `Note` con
prefix `[DRAFT-PENDING]`. Niente database esterno.

Env vars richieste (Railway):
  NOTION_API_KEY          — integration token Albeni AI Stack
  NOTION_DB_ID            — id del Content Pipeline (98a46d37-3b7d-4455-ad86-0da224b00b01)
  WP_UPLOAD_SECRET        — secret X-Upload-Key, identico al .env locale
  TWILIO_ACCOUNT_SID      — Twilio account SID
  TWILIO_AUTH_TOKEN       — Twilio auth token
  TWILIO_WHATSAPP_FROM    — es: whatsapp:+14155238886
  ROBERTO_WHATSAPP        — destinatario, es: +39335271008
  CONSERVATIVE_MODE       — "true" → ignora whitelist auto-publish, tutto va in draft+veto
  AGENT_VERSION           — string per logging (es: "v1.0.0")
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, JSONResponse

# ─── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("editorial-agent")

# ─── Env config ───────────────────────────────────────────────────────────

NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DB_ID = os.environ.get(
    "NOTION_DB_ID", "98a46d37-3b7d-4455-ad86-0da224b00b01"
)
WP_UPLOAD_SECRET = os.environ.get("WP_UPLOAD_SECRET", "")
WP_BASE_MU = os.environ.get("WP_BASE_MU", "https://merinouniversity.com")
WP_BASE_WOM = os.environ.get("WP_BASE_WOM", "https://worldofmerino.com")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
ROBERTO_PHONE = os.environ.get("ROBERTO_WHATSAPP", "")
ROBERTO_WHATSAPP_FORMATTED = (
    f"whatsapp:{ROBERTO_PHONE}" if ROBERTO_PHONE else ""
)
CONSERVATIVE_MODE = os.environ.get("CONSERVATIVE_MODE", "true").lower() == "true"
AGENT_VERSION = os.environ.get("AGENT_VERSION", "v1.0.0-conservative")

# ─── Notion API ───────────────────────────────────────────────────────────

NOTION_API = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


async def notion_query_da_fare(client: httpx.AsyncClient) -> list[dict]:
    """Legge tutte le entry Notion con Stato = 'Da Fare', ordinate per Created."""
    body = {
        "filter": {"property": "Stato", "select": {"equals": "Da Fare"}},
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        "page_size": 50,
    }
    r = await client.post(
        f"{NOTION_API}/databases/{NOTION_DB_ID}/query",
        headers=NOTION_HEADERS,
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("results", [])


async def notion_query_draft_pending(client: httpx.AsyncClient) -> list[dict]:
    """Entry con Stato='In Pubblicazione' che hanno un marker [DRAFT-PENDING] nel Note."""
    body = {
        "filter": {
            "and": [
                {"property": "Stato", "select": {"equals": "In Pubblicazione"}},
                {"property": "Note", "rich_text": {"contains": "[DRAFT-PENDING]"}},
            ]
        },
        "page_size": 50,
    }
    r = await client.post(
        f"{NOTION_API}/databases/{NOTION_DB_ID}/query",
        headers=NOTION_HEADERS,
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("results", [])


async def notion_update_stato(
    client: httpx.AsyncClient, page_id: str, new_stato: str
) -> None:
    body = {"properties": {"Stato": {"select": {"name": new_stato}}}}
    r = await client.patch(
        f"{NOTION_API}/pages/{page_id}", headers=NOTION_HEADERS, json=body, timeout=20
    )
    r.raise_for_status()


async def notion_set_note(
    client: httpx.AsyncClient, page_id: str, note_text: str
) -> None:
    """Sostituisce completamente il property Note (max 2000 char)."""
    body = {
        "properties": {
            "Note": {"rich_text": [{"type": "text", "text": {"content": note_text[:2000]}}]}
        }
    }
    r = await client.patch(
        f"{NOTION_API}/pages/{page_id}", headers=NOTION_HEADERS, json=body, timeout=20
    )
    r.raise_for_status()


async def notion_add_comment(
    client: httpx.AsyncClient, page_id: str, text: str
) -> None:
    body = {
        "parent": {"page_id": page_id},
        "rich_text": [{"type": "text", "text": {"content": text[:2000]}}],
    }
    r = await client.post(
        f"{NOTION_API}/comments", headers=NOTION_HEADERS, json=body, timeout=20
    )
    if not r.is_success:
        log.warning(f"Failed to add Notion comment: {r.status_code} {r.text[:200]}")


def get_prop(page: dict, prop_name: str) -> Any:
    """Estrae il valore di una property Notion, qualunque sia il tipo."""
    p = page.get("properties", {}).get(prop_name, {})
    t = p.get("type")
    if t == "select":
        return (p.get("select") or {}).get("name")
    if t == "multi_select":
        return [x.get("name") for x in (p.get("multi_select") or [])]
    if t == "title":
        return "".join([x.get("plain_text", "") for x in p.get("title", [])])
    if t == "rich_text":
        return "".join([x.get("plain_text", "") for x in p.get("rich_text", [])])
    if t == "checkbox":
        return p.get("checkbox", False)
    if t == "number":
        return p.get("number")
    return None


# ─── WP albeni/v1 client ──────────────────────────────────────────────────

WP_HEADERS = {
    "X-Upload-Key": WP_UPLOAD_SECRET,
    "Content-Type": "application/json",
}


def wp_base_for_domain(domain: str) -> str:
    if domain == "merinouniversity.com":
        return WP_BASE_MU
    if domain == "worldofmerino.com":
        return WP_BASE_WOM
    raise ValueError(f"Unsupported domain: {domain}")


async def wp_promote_drafts(
    client: httpx.AsyncClient, domain: str, page_ids: list[int]
) -> dict:
    base = wp_base_for_domain(domain)
    r = await client.post(
        f"{base}/wp-json/albeni/v1/promote-draft-to-publish",
        headers=WP_HEADERS,
        json={"ids": page_ids},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


async def wp_delete_drafts(
    client: httpx.AsyncClient,
    domain: str,
    page_ids: list[int],
    force: bool = False,
) -> dict:
    base = wp_base_for_domain(domain)
    r = await client.post(
        f"{base}/wp-json/albeni/v1/delete-drafts",
        headers=WP_HEADERS,
        json={"ids": page_ids, "force": force},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


# ─── Twilio ───────────────────────────────────────────────────────────────


async def twilio_send_whatsapp(
    client: httpx.AsyncClient, to: str, body: str
) -> dict:
    """Invia un WhatsApp via Twilio API. Ritorna il response JSON."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    data = {"To": to, "From": TWILIO_FROM, "Body": body[:1500]}
    r = await client.post(
        url,
        data=data,
        auth=(TWILIO_SID, TWILIO_TOKEN),
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


# ─── State markers nel Note Notion ────────────────────────────────────────

# Marker format inside Notion Note property:
#   [DRAFT-PENDING] domain=<d> shelf=<s> ttl_until=<iso> sent_at=<iso> page_ids=<json>


def parse_draft_marker(note: str) -> Optional[dict]:
    """Estrae i campi dal marker [DRAFT-PENDING] nel Note. Restituisce None se assente."""
    m = re.search(r"\[DRAFT-PENDING\]\s+(.*?)(?=\n|\Z)", note, flags=re.S)
    if not m:
        return None
    body = m.group(1).strip()
    out = {}
    for kv in re.finditer(r"(\w+)=(\{[^}]*\}|[^\s]+)", body):
        k, v = kv.group(1), kv.group(2)
        if v.startswith("{"):
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = v
        else:
            out[k] = v
    return out


def build_draft_marker(
    domain: str, shelf: str, ttl_until: datetime, page_ids: dict[str, int]
) -> str:
    return (
        f"[DRAFT-PENDING] domain={domain} shelf={shelf} "
        f"ttl_until={ttl_until.isoformat()} "
        f"sent_at={datetime.now(timezone.utc).isoformat()} "
        f"page_ids={json.dumps(page_ids)}"
    )


# ─── Policy decision (semplice, da espandere) ─────────────────────────────


def shelf_ttl_days(shelf: str) -> int:
    return {"hot": 7, "warm": 20, "evergreen": 30}.get(shelf or "warm", 20)


def is_in_quiet_hours(now: Optional[datetime] = None) -> bool:
    """22:00–07:00 Europe/Rome (approssimato come UTC+1/+2)."""
    n = now or datetime.now(timezone.utc) + timedelta(hours=2)  # CEST
    return n.hour >= 22 or n.hour < 7


def policy_decide(page: dict) -> tuple[str, str]:
    """
    Applica la policy editoriale a un'entry Notion.
    Ritorna (decision, reason) dove decision ∈ {APPROVE, REJECT, HOLD}.

    NOTE: questa è la versione minima v1. In modalità conservativa, applica
    solo guardrail di base e lascia in HOLD qualsiasi caso ambiguo.
    """
    dominio = get_prop(page, "Dominio") or ""
    tipo = get_prop(page, "Tipo Contenuto") or ""
    eccezione = get_prop(page, "Eccezione") or False
    shelf = get_prop(page, "Shelf Life") or ""
    titolo = get_prop(page, "Contenuto") or ""

    # Guardrail dominio
    if dominio not in ("worldofmerino.com", "merinouniversity.com"):
        return ("HOLD", f"Dominio fuori scope agente v1: {dominio}")

    # Eccezioni richiedono sempre review umano
    if eccezione:
        return ("HOLD", "Eccezione=true, review umano obbligatorio")

    # Red flag (semplificato — euristica per v1)
    titolo_lower = titolo.lower()
    red_flag_terms = [
        "cura", "previene", "guarisce", "tratta", "smartwool", "icebreaker",
        "allbirds", "asket", "uniqlo", "loropiana"
    ]
    for term in red_flag_terms:
        if term in titolo_lower:
            return ("HOLD", f"Red flag rilevato nel titolo: '{term}'")

    # Shelf life mancante
    if not shelf:
        return ("HOLD", "Shelf life mancante")

    # Tipo contenuto coerente
    valid_wom_types = ["Field Note", "Story", "Blog Post", "Radar Weekly"]
    valid_mu_types = ["Data Brief", "Approfondimento", "Blog Post", "Guide"]
    if dominio == "worldofmerino.com" and tipo not in valid_wom_types:
        return ("HOLD", f"Tipo '{tipo}' non valido per WoM")
    if dominio == "merinouniversity.com" and tipo not in valid_mu_types:
        return ("HOLD", f"Tipo '{tipo}' non valido per MU")

    # Tutti i guardrail superati → APPROVE
    return ("APPROVE", "Guardrail policy v1 superati")


# ─── Agent runs ───────────────────────────────────────────────────────────


async def run_polling_da_fare() -> None:
    """Cron job: ogni ora, processa le entry "Da Fare"."""
    if is_in_quiet_hours():
        log.info("Quiet hours, skipping polling")
        return
    log.info("=== Polling cycle: Da Fare ===")
    async with httpx.AsyncClient() as client:
        try:
            entries = await notion_query_da_fare(client)
        except Exception as e:
            log.error(f"Notion query failed: {e}")
            return
        log.info(f"Found {len(entries)} entries Da Fare")

        for entry in entries:
            page_id = entry["id"]
            titolo = get_prop(entry, "Contenuto") or "(no title)"
            log.info(f"  → Processing: {titolo[:60]}")
            decision, reason = policy_decide(entry)
            log.info(f"    Decision: {decision} — {reason}")

            try:
                if decision == "REJECT":
                    await notion_update_stato(client, page_id, "Archiviato")
                    await notion_add_comment(
                        client, page_id, f"❌ Auto-archived [agent {AGENT_VERSION}]\nMotivo: {reason}"
                    )
                elif decision == "HOLD":
                    await notion_add_comment(
                        client, page_id,
                        f"🟡 Hold per review umano [agent {AGENT_VERSION}]\nMotivo: {reason}"
                    )
                elif decision == "APPROVE":
                    # In v1 conservativa: deploy as-draft + WUP
                    # PER ORA: solo annota la decisione, NON deploya ancora.
                    # Il deploy reale richiede la chiamata a deploy_*.py (che gira su Mac, NON qui).
                    # In Phase 2 il deploy partirà da qui via subprocess o via endpoint custom.
                    await notion_add_comment(
                        client, page_id,
                        f"✅ Approved by agent [{AGENT_VERSION}] — pending deploy.\n"
                        f"NOTA: in modalità conservativa Phase 1, il deploy reale richiede "
                        f"interazione del Mac (deploy_*.py). Phase 2 wireup pending."
                    )
            except Exception as e:
                log.error(f"    Failed to process {page_id}: {e}")


async def run_polling_ttl() -> None:
    """Cron job: 1×/giorno, cancella le draft pending scadute."""
    log.info("=== Polling cycle: TTL drafts ===")
    now = datetime.now(timezone.utc)
    async with httpx.AsyncClient() as client:
        try:
            entries = await notion_query_draft_pending(client)
        except Exception as e:
            log.error(f"Notion query (pending) failed: {e}")
            return
        log.info(f"Found {len(entries)} draft-pending entries")

        for entry in entries:
            page_id = entry["id"]
            note = get_prop(entry, "Note") or ""
            marker = parse_draft_marker(note)
            if not marker:
                continue
            ttl_str = marker.get("ttl_until")
            if not ttl_str:
                continue
            try:
                ttl = datetime.fromisoformat(ttl_str)
            except Exception:
                continue
            if now < ttl:
                continue
            domain = marker.get("domain", "")
            page_ids_map = marker.get("page_ids", {})
            ids_list = list(page_ids_map.values()) if isinstance(page_ids_map, dict) else []
            if not ids_list:
                continue
            try:
                await wp_delete_drafts(client, domain, ids_list, force=False)
                await notion_update_stato(client, page_id, "Archiviato")
                await notion_add_comment(
                    client, page_id,
                    f"⏱️ TTL scaduto, draft auto-cancellate [agent {AGENT_VERSION}]\n"
                    f"shelf={marker.get('shelf')} ttl_until={ttl_str}"
                )
            except Exception as e:
                log.error(f"TTL delete failed for {page_id}: {e}")


# ─── FastAPI app ──────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler(timezone="Europe/Rome")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"Editorial Approval Agent {AGENT_VERSION} starting up")
    log.info(f"  Conservative mode: {CONSERVATIVE_MODE}")
    log.info(f"  Notion DB:         {NOTION_DB_ID}")
    log.info(f"  Twilio from:       {TWILIO_FROM}")
    log.info(f"  Roberto:           {ROBERTO_PHONE}")
    # Schedule polling
    scheduler.add_job(
        run_polling_da_fare,
        trigger=CronTrigger(minute=15, timezone="Europe/Rome"),
        id="poll_da_fare",
        replace_existing=True,
    )
    scheduler.add_job(
        run_polling_ttl,
        trigger=CronTrigger(hour=4, minute=30, timezone="Europe/Rome"),
        id="poll_ttl",
        replace_existing=True,
    )
    scheduler.start()
    log.info("Scheduler started")
    yield
    log.info("Shutting down scheduler")
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="Editorial Approval Agent",
    version=AGENT_VERSION,
    lifespan=lifespan,
)

# Mount orchestrator + frontend routers (mobile-first 2-tap flow)
from orchestrator import router as orch_router  # noqa: E402
from frontend import router as fe_router  # noqa: E402

app.include_router(fe_router)
app.include_router(orch_router)


@app.get("/")
async def root() -> dict:
    return {
        "service": "editorial-approval-agent",
        "version": AGENT_VERSION,
        "conservative": CONSERVATIVE_MODE,
        "scheduler_running": scheduler.running,
        "next_runs": {
            j.id: j.next_run_time.isoformat() if j.next_run_time else None
            for j in scheduler.get_jobs()
        },
    }


@app.post("/run-now/{job_id}")
async def run_now(job_id: str) -> dict:
    """Trigger manuale di un job. Utile per debug e primo test."""
    if job_id == "poll_da_fare":
        await run_polling_da_fare()
        return {"ok": True, "ran": "poll_da_fare"}
    if job_id == "poll_ttl":
        await run_polling_ttl()
        return {"ok": True, "ran": "poll_ttl"}
    return JSONResponse(
        {"ok": False, "error": f"unknown job: {job_id}"}, status_code=400
    )


@app.post("/twilio-webhook")
async def twilio_webhook(
    request: Request,
    From: str = Form(""),
    Body: str = Form(""),
) -> PlainTextResponse:
    """
    Endpoint POST chiamato da Twilio quando Roberto risponde a un WhatsApp.
    Twilio manda body urlencoded con campi:
      - From: whatsapp:+39...
      - Body: testo della risposta
      - MessageSid, AccountSid, ecc.

    Risponde con TwiML vuoto (200 OK) — Twilio si aspetta XML ma anche
    una risposta vuota va bene per non auto-rispondere.
    """
    log.info(f"Webhook received from {From}: {Body[:100]}")

    # Verifica che venga davvero da Roberto
    if From != ROBERTO_WHATSAPP_FORMATTED:
        log.warning(f"Webhook from unauthorized number: {From}")
        return PlainTextResponse("ignored", status_code=200)

    cmd = (Body or "").strip().upper()

    async with httpx.AsyncClient() as client:
        # Trova le draft-pending in Notion (la più recente sarà l'oggetto della risposta)
        try:
            pending = await notion_query_draft_pending(client)
        except Exception as e:
            log.error(f"Failed to fetch pending: {e}")
            await twilio_send_whatsapp(
                client, From, f"⚠️ Errore lettura Notion: {e}"
            )
            return PlainTextResponse("error", status_code=200)

        if not pending:
            await twilio_send_whatsapp(
                client, From,
                "Nessuna draft pendente. Ricontrolla quando l'agente avrà processato il prossimo brief."
            )
            return PlainTextResponse("ok", status_code=200)

        # Per v1: la risposta si applica alla MOST RECENT draft pending
        target = pending[0]
        page_id = target["id"]
        titolo = get_prop(target, "Contenuto") or "(no title)"
        note = get_prop(target, "Note") or ""
        marker = parse_draft_marker(note)
        if not marker:
            await twilio_send_whatsapp(
                client, From, f"⚠️ Marker DRAFT-PENDING non leggibile su: {titolo[:60]}"
            )
            return PlainTextResponse("error", status_code=200)
        domain = marker.get("domain", "")
        page_ids_map = marker.get("page_ids", {})
        ids_list = list(page_ids_map.values()) if isinstance(page_ids_map, dict) else []

        if cmd == "OK":
            try:
                result = await wp_promote_drafts(client, domain, ids_list)
                await notion_update_stato(client, page_id, "Pubblicato")
                await notion_add_comment(
                    client, page_id,
                    f"✅ Pubblicato dopo OK Roberto [agent {AGENT_VERSION}]\n"
                    f"Result: {json.dumps(result.get('results', []))[:1500]}"
                )
                permalinks = "\n".join(
                    [str(r.get("permalink", "")) for r in result.get("results", []) if r.get("ok")]
                )
                await twilio_send_whatsapp(
                    client, From,
                    f"✅ Pubblicato: {titolo[:80]}\n{permalinks[:1200]}"
                )
            except Exception as e:
                log.error(f"Promote failed: {e}")
                await twilio_send_whatsapp(client, From, f"⚠️ Promote failed: {e}")
            return PlainTextResponse("ok", status_code=200)

        if cmd == "STOP":
            try:
                await wp_delete_drafts(client, domain, ids_list, force=False)
                await notion_update_stato(client, page_id, "Archiviato")
                await notion_add_comment(
                    client, page_id,
                    f"❌ Stop di Roberto, draft cancellate [agent {AGENT_VERSION}]"
                )
                await twilio_send_whatsapp(
                    client, From, f"❌ Bloccato e archiviato: {titolo[:80]}"
                )
            except Exception as e:
                log.error(f"Delete failed: {e}")
                await twilio_send_whatsapp(client, From, f"⚠️ Delete failed: {e}")
            return PlainTextResponse("ok", status_code=200)

        # Comando non riconosciuto
        await twilio_send_whatsapp(
            client, From,
            f"❓ Comando non riconosciuto: '{Body[:60]}'\n"
            f"Rispondi 'OK' per pubblicare, 'STOP' per archiviare.\n"
            f"Ultima draft pendente: {titolo[:80]}"
        )
        return PlainTextResponse("ok", status_code=200)
