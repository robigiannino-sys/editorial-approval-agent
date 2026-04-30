"""
Visual Generator (cloud) — Imagen 4 → WP Media Library
=======================================================

Modulo helper per il flusso editoriale mobile-first.
Sostituisce la pipeline locale `generate_visuals.py` con una versione
streaming-only: l'immagine non tocca mai il filesystem locale, vive solo
in RAM finché non è uploadata sul WP Media Library.

Uso (chiamato dal backend `orchestrator.py`):
    media = await generate_and_upload_visual(
        client=httpx_client,
        prompt="editorial fashion photography, ...",
        topic="DPP",
        destination="MU",
        aspect_ratio="16:9",
    )
    # media è un dict: {"id": 1234, "source_url": "https://...wp-content/.../visual-...png"}

Env vars richieste:
    GEMINI_API_KEY        — Google Gemini / Imagen API key
    WP_UPLOAD_SECRET      — X-Upload-Key per albeni/v1/upload-visual
    WP_BASE_MU            — default https://merinouniversity.com
    WP_BASE_WOM           — default https://worldofmerino.com
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime
from typing import Optional

import httpx

log = logging.getLogger("visual-gen")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
WP_UPLOAD_SECRET = os.environ.get("WP_UPLOAD_SECRET", "")
WP_BASE_MU = os.environ.get("WP_BASE_MU", "https://merinouniversity.com")
WP_BASE_WOM = os.environ.get("WP_BASE_WOM", "https://worldofmerino.com")

DEFAULT_MODEL = "imagen-4.0-generate-001"


def _wp_base_for(destination: str) -> str:
    if destination.upper() == "MU":
        return WP_BASE_MU
    if destination.upper() == "WOM":
        return WP_BASE_WOM
    raise ValueError(f"Unknown destination '{destination}', expected MU or WoM")


async def generate_image_bytes(
    prompt: str,
    aspect_ratio: str = "16:9",
    model: str = DEFAULT_MODEL,
) -> bytes:
    """
    Chiama Imagen 4 via Google Gemini SDK e ritorna i bytes PNG.
    SDK sincrono usato in run_in_executor sarebbe più pulito, ma
    qui il SDK è veloce e la chiamata è una sola — accettiamo il
    blocco temporaneo del loop async (~3-8 secondi).
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set in env")

    # Import lazy per evitare costo all'avvio se il modulo non viene mai usato
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise RuntimeError(
            "google-genai SDK non installato. Aggiungi 'google-genai' a requirements.txt"
        ) from e

    client = genai.Client(api_key=GEMINI_API_KEY)

    log.info(
        f"Imagen request: model={model} ar={aspect_ratio} "
        f"prompt={prompt[:100]!r}..."
    )

    result = client.models.generate_images(
        model=model,
        prompt=prompt,
        config=types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio=aspect_ratio,
            safety_filter_level="BLOCK_LOW_AND_ABOVE",
        ),
    )

    if not result.generated_images:
        raise RuntimeError(
            "Imagen returned no images (likely safety filter triggered)"
        )

    img_bytes = result.generated_images[0].image.image_bytes
    log.info(f"Imagen OK: {len(img_bytes) / 1024:.0f} KB")
    return img_bytes


async def upload_to_wp(
    client: httpx.AsyncClient,
    image_bytes: bytes,
    filename: str,
    alt_text: str,
    caption: str,
    destination: str = "MU",
) -> dict:
    """
    Upload PNG bytes a WP Media Library tramite albeni/v1/upload-visual.
    Ritorna {"id": int, "source_url": str, "link": str}.
    """
    base = _wp_base_for(destination)
    url = f"{base}/wp-json/albeni/v1/upload-visual"
    payload = {
        "image_data": base64.b64encode(image_bytes).decode("ascii"),
        "filename": filename,
        "alt_text": alt_text,
        "caption": caption,
    }
    headers = {
        "X-Upload-Key": WP_UPLOAD_SECRET,
        "Content-Type": "application/json",
    }
    log.info(f"Upload to WP: {url} ({len(image_bytes) / 1024:.0f} KB)")
    r = await client.post(url, headers=headers, json=payload, timeout=120)
    if r.status_code == 404:
        raise RuntimeError(
            f"Endpoint not found at {url}. "
            "Verifica che il snippet WPCode 'Albeni — v1 REST endpoints' sia attivo."
        )
    if not r.is_success:
        raise RuntimeError(f"WP upload failed HTTP {r.status_code}: {r.text[:300]}")
    out = r.json()
    log.info(f"WP media id={out.get('id')} url={out.get('source_url')}")
    return out


async def generate_and_upload_visual(
    client: httpx.AsyncClient,
    prompt: str,
    topic: str,
    destination: str = "MU",
    aspect_ratio: str = "16:9",
    alt_text: Optional[str] = None,
    caption: Optional[str] = None,
) -> dict:
    """
    Pipeline completa one-shot: genera con Imagen + carica su WP.
    Ritorna lo stesso dict di upload_to_wp + il prompt usato.
    """
    img_bytes = await generate_image_bytes(prompt=prompt, aspect_ratio=aspect_ratio)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_tag = destination.lower()
    topic_tag = (topic or "untagged").lower().replace(" ", "-")[:30]
    filename = f"visual-{timestamp}-{topic_tag}-{dest_tag}.png"

    media = await upload_to_wp(
        client=client,
        image_bytes=img_bytes,
        filename=filename,
        alt_text=alt_text or f"Albeni 1905 — {topic}",
        caption=caption or f"Albeni 1905 editorial visual ({topic})",
        destination=destination,
    )

    return {
        "ok": True,
        "media_id": media["id"],
        "image_url": media["source_url"],
        "wp_link": media.get("link"),
        "prompt_used": prompt,
        "destination": destination,
        "topic": topic,
        "filename": filename,
    }


# ─── Style guide / prompt scaffolding ────────────────────────────────────
# Versione MVP: prompt template inline, senza template engine esterno.
# Phase 2: caricare references/visual-prompt-templates.json dal news scanner.

STYLE_GUIDE_MU = (
    "editorial documentary photography, neutral lighting, technical and scientific aesthetic, "
    "macro detail, shallow depth of field, 50mm. Editorial photograph for Merino University: "
    "{subject_phrase}. Color palette: deep navy, charcoal grey, warm beige, muted terracotta. "
    "Documentary editorial style, authentic, no models, no logos, no text overlay."
)

STYLE_GUIDE_WOM = (
    "editorial fashion photography, warm natural light, muted earth tones, quiet luxury aesthetic, "
    "Italian elegance, no visible logos, soft depth of field, 35mm film grain. Editorial photograph "
    "for World of Merino: {subject_phrase}. Color palette: warm beige, deep navy blue, stone grey, "
    "cream white, soft terracotta. Editorial context: contemplative newsroom voice."
)

NEGATIVE_PROMPT = (
    "neon colors, artificial lighting, studio backdrop, bold text overlay, clipart, cartoon style, "
    "stock photo generic poses, bright saturated colors, logos, brand names, watermarks"
)


def compose_prompt(subject_phrase: str, destination: str, extra: str = "") -> str:
    """Compone un prompt per Imagen 4 a partire da un soggetto e destinazione."""
    style = STYLE_GUIDE_MU if destination.upper() == "MU" else STYLE_GUIDE_WOM
    base = style.format(subject_phrase=subject_phrase)
    if extra:
        base = f"{base}. {extra}"
    return f"{base}\n\nNegative: {NEGATIVE_PROMPT}"
