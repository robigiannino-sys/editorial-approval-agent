"""
Knowledge Base — agente che conosce i contenuti pubblicati su WoM e MU.

Phase 1 (oggi): query Notion per articoli "Pubblicato" del dominio target +
chiamata Claude API per match semantico tra topic MU e articoli WoM.

Phase 2 (futuro, quando articoli pubblicati > 200): vector store + embeddings
+ RAG. L'interfaccia di questo modulo resta stabile, cambia solo l'implementazione.

API pubblica (stable):
    list_published(client, domain) -> list[Article]
    find_companion(client, topic, target_domain, source_title) -> Article | None
    # Phase 2:
    # gap_analysis(client, topic) -> list[str]
    # interlink_suggest(client, article) -> list[Article]
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger("knowledge-base")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


@dataclass
class Article:
    """Rappresentazione di un articolo pubblicato (sia WoM sia MU)."""
    notion_id: str
    title: str
    domain: str
    tema: str
    cluster: str
    url: str  # permalink WP, fallback all'URL Notion se non pubblicato

    def to_compact(self) -> str:
        """Formato compatto per passaggio a Claude come context."""
        return f"- [{self.tema or '?'}] {self.title} → {self.url}"


def _agent():
    import agent
    return agent


# ─── Phase 1: query Notion ad ogni chiamata (no cache) ─────────────────────


async def list_published(
    client: httpx.AsyncClient,
    domain: str,
    limit: int = 100,
) -> list[Article]:
    """
    Ritorna gli articoli con Stato='Pubblicato' del dominio specificato.
    Phase 1: query Notion ogni volta. Phase 2: cache + invalidation on new publish.
    """
    a = _agent()
    body = {
        "filter": {
            "and": [
                {"property": "Stato", "select": {"equals": "Pubblicato"}},
                {"property": "Dominio", "select": {"equals": domain}},
            ]
        },
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        "page_size": min(limit, 100),
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
    except Exception as e:
        log.error(f"list_published failed: {e}")
        return []

    articles = []
    for entry in results:
        title_raw = a.get_prop(entry, "Contenuto") or ""
        # Pulisci pattern "[NEWS] Topic — Tipo MU/WoM: Titolo"
        title_clean = re.sub(r"^\[NEWS\][^—]*—\s*[^:]+:\s*", "", title_raw).strip() or title_raw

        # Estrai URL dal Note (commento "Pubblicato dopo OK..." spesso contiene il permalink)
        note = a.get_prop(entry, "Note") or ""
        url_match = re.search(rf"https?://{re.escape(domain)}/[\w\-/]+", note)
        url = url_match.group(0) if url_match else f"https://{domain}/"

        articles.append(Article(
            notion_id=entry["id"],
            title=title_clean[:120],
            domain=domain,
            tema=a.get_prop(entry, "Tema") or "",
            cluster=a.get_prop(entry, "Cluster") or "",
            url=url,
        ))
    return articles


async def find_companion(
    client: httpx.AsyncClient,
    source_topic: str,
    source_title: str,
    target_domain: str,
    source_cluster: str = "",
) -> Optional[Article]:
    """
    Trova l'articolo del target_domain più affine all'articolo sorgente.

    Logica:
    1. Lista articoli pubblicati su target_domain
    2. Se ANTHROPIC_API_KEY è settata: ranking semantico via Claude
    3. Altrimenti: match per Tema esatto, fallback al cluster, fallback a None

    Ritorna Article o None se nessun articolo pubblicato esiste.
    """
    articles = await list_published(client, target_domain, limit=50)
    if not articles:
        log.info(f"No published articles in {target_domain}")
        return None

    # Tenta match esatto per Tema (senza chiamare Claude — rapido e gratis)
    if source_topic:
        for art in articles:
            if art.tema and art.tema.lower() == source_topic.lower():
                log.info(f"Tema-match found: {art.title!r}")
                return art

    # Fallback: cluster match
    if source_cluster:
        for art in articles:
            if art.cluster and art.cluster.lower() == source_cluster.lower():
                log.info(f"Cluster-match found: {art.title!r}")
                return art

    # Phase 1.5: se Claude API è disponibile, ranking semantico
    if ANTHROPIC_API_KEY and len(articles) > 0:
        ranked = await _rank_with_claude(
            source_topic=source_topic,
            source_title=source_title,
            candidates=articles,
        )
        if ranked:
            return ranked

    # Fallback finale: il più recente
    return articles[0] if articles else None


async def _rank_with_claude(
    source_topic: str,
    source_title: str,
    candidates: list[Article],
) -> Optional[Article]:
    """
    Chiede a Claude di scegliere l'articolo più affine al source.
    Ritorna l'Article scelto, o None se Claude non riesce a decidere.
    """
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        log.warning("anthropic SDK not installed, skipping semantic ranking")
        return None

    listing = "\n".join(
        f"{i + 1}. [{a.tema or '?'}] {a.title}"
        for i, a in enumerate(candidates[:30])
    )

    prompt = (
        f"Sei un editor che cura la circolarità editoriale tra Merino University (taglio "
        f"tecnico-scientifico) e World of Merino (taglio lifestyle).\n\n"
        f"Articolo sorgente:\n"
        f"  Titolo: {source_title}\n"
        f"  Tema: {source_topic}\n\n"
        f"Lista articoli candidati per la CTA cross-magazine:\n{listing}\n\n"
        f"Scegli l'articolo della lista più affine al sorgente per affinità di TEMA, "
        f"non per tipologia. Rispondi SOLO con il numero dell'articolo (es. '7'), "
        f"oppure '0' se nessuno è davvero pertinente."
    )

    try:
        client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        m = re.search(r"\d+", text)
        if not m:
            return None
        idx = int(m.group(0)) - 1
        if 0 <= idx < len(candidates):
            log.info(f"Claude picked: #{idx + 1} = {candidates[idx].title!r}")
            return candidates[idx]
    except Exception as e:
        log.warning(f"Claude ranking failed: {e}")
    return None
