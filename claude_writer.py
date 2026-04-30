"""
Claude Writer — riscrive l'articolo finito a partire dal brief Notion.

Il brief Notion è un documento progettuale ("Titoli proposti", "Formato",
"Cluster rilevanti", "Azione richiesta") prodotto dal merino-news-scanner.
Quello NON è l'articolo da pubblicare — è una scaletta per il copywriter.

Questo modulo passa il brief a Claude (Anthropic API) e ottiene l'articolo
redatto, formattato in HTML Gutenberg, pronto per la pubblicazione su WP.

Env vars:
    ANTHROPIC_API_KEY    — chiave API Anthropic (https://console.anthropic.com)
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

log = logging.getLogger("claude-writer")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Modello da usare. claude-sonnet-4-6 è un ottimo bilanciamento qualità/costo.
DEFAULT_MODEL = "claude-sonnet-4-6"


SYSTEM_PROMPT_MU = """Sei l'editor scientifico di Merino University Osservatorio, il dipartimento news-driven della rivista tecnica di Albeni 1905. Il tuo registro è quello di una pubblicazione tecnico-scientifica seria — Nature, MIT Technology Review, Heritage Sustainability Review.

Il tuo compito: trasformare un BRIEF EDITORIALE (un documento progettuale con titoli proposti, dati chiave, angolo narrativo) in un ARTICOLO FINITO pronto per la pubblicazione.

Linee guida di stile:
- Tono: tecnico-scientifico ma accessibile, mai accademico né freddo. Cita dati con precisione, ma collega ai pattern dell'industria laniera (IWTO, ZQ, eBale, Reda, Biella).
- Lunghezza: 800-1200 parole.
- Struttura: introduzione (1 paragrafo che ancora il fatto), 2-3 sezioni con heading H2, conclusione operativa (cosa cambia per il settore).
- Voce: terza persona, presente, italiano colto e tecnico.
- Mai: marketing, claim Albeni esplicito, SEO stuffing, refusi.
- Linkato sempre a "Invisible Luxury": il merino come fibra che opera secondo standard tecnici verificabili che precedono le mode normative.

Output: SOLO blocchi Gutenberg WordPress validi. Niente meta-commenti, niente "ecco l'articolo:", niente preamboli.

Format Gutenberg:
- <!-- wp:paragraph --><p>...</p><!-- /wp:paragraph -->
- <!-- wp:heading {"level":2} --><h2 class="wp-block-heading">...</h2><!-- /wp:heading -->
- <!-- wp:list --><ul>...</ul><!-- /wp:list --> per liste
- <!-- wp:quote --><blockquote class="wp-block-quote"><p>...</p></blockquote><!-- /wp:quote --> per pull quote
"""


SYSTEM_PROMPT_WOM = """Sei il caporedattore di World of Merino, il magazine lifestyle di Albeni 1905. Il tuo registro è contemplativo, sussurrato, da newsroom curata — Monocle, The Gentlewoman, Cereal.

Il tuo compito: trasformare un BRIEF EDITORIALE in un ARTICOLO FINITO pronto per la pubblicazione.

Linee guida di stile:
- Tono: contemplativo, sensoriale, mai promozionale. La materia parla, non i loghi.
- Lunghezza: 600-900 parole.
- Struttura: incipit evocativo (1 paragrafo), corpo che alterna riflessione e dato, finale che apre prospettiva.
- Voce: terza persona, italiano elegante e mai retorico.
- Mai: marketing, claim Albeni esplicito, citazione di prodotti.
- "Invisible Luxury": il vero lusso è la coerenza verificabile, non il logo.

Output: SOLO blocchi Gutenberg WordPress validi. Niente preamboli."""


async def write_article_from_brief(
    brief_text: str,
    title: str,
    domain: str,
    topic: str = "",
    key_concepts: str = "",
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Genera l'articolo Gutenberg HTML dal brief Notion via Claude API.

    Ritorna SOLO il body articolo (no hero image, no H1 titolo, no CTA —
    quelli li aggiunge l'orchestrator esterno).
    """
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — falling back to brief-as-article")
        return _fallback_format(brief_text)

    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:
        raise RuntimeError(
            "anthropic SDK non installato. Aggiungi 'anthropic' a requirements.txt"
        ) from e

    is_mu = domain == "merinouniversity.com"
    system = SYSTEM_PROMPT_MU if is_mu else SYSTEM_PROMPT_WOM

    user_prompt = (
        f"BRIEF EDITORIALE da trasformare in articolo finito:\n\n"
        f"---\n"
        f"Titolo proposto: {title}\n"
        f"Tema: {topic or '(non specificato)'}\n"
        f"Concetti chiave estratti: {key_concepts or '(non disponibili)'}\n"
        f"Dominio: {domain}\n"
        f"\n"
        f"--- BRIEF COMPLETO ---\n"
        f"{brief_text}\n"
        f"--- FINE BRIEF ---\n\n"
        f"Scrivi l'articolo finito in italiano, formattato in blocchi Gutenberg WordPress. "
        f"Estrai i dati e l'angolo narrativo dal brief, ma scrivi un articolo VERO da pubblicare, "
        f"non un riassunto del brief. NON includere il titolo H1 (lo aggiunge il sistema). "
        f"Inizia direttamente con il primo paragrafo dell'articolo."
    )

    log.info(f"Claude request: model={model} domain={domain} topic={topic!r}")

    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        log.error(f"Claude API call failed: {e}")
        return _fallback_format(brief_text)

    # Estrae il testo
    content = "".join(
        block.text for block in resp.content if hasattr(block, "text")
    )
    log.info(f"Claude response: {len(content)} chars, {resp.usage.input_tokens}+{resp.usage.output_tokens} tokens")

    # Sanitize: rimuove eventuali wrapper markdown se Claude ha sbagliato
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```\w*\n", "", content)
        content = re.sub(r"\n```\s*$", "", content)
    return content.strip()


def _fallback_format(brief_text: str) -> str:
    """Fallback se Claude non disponibile: stripping minimo + paragrafi."""
    lines = brief_text.strip().split("\n")
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Skip metadata-style lines
        if re.match(r"^(Titoli proposti|Formato|Cluster rilevanti|Azione richiesta|Dati|Tema):", line, re.I):
            continue
        if line.startswith("- "):
            continue
        out.append(
            f'<!-- wp:paragraph -->\n<p>{_escape(line)}</p>\n<!-- /wp:paragraph -->'
        )
    return "\n\n".join(out[:30])  # cap a 30 paragrafi


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
