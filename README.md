# Editorial Approval Agent — runbook deploy Railway

Servizio FastAPI che gira su Railway 24/7. Sostituisce il bottleneck umano del Content Pipeline con un flusso **mobile-first 2-tap**: dal phone vedi il brief del giorno, tap "Approva", ricevi preview reale della pagina, tap "Pubblica". Niente Mac coinvolto.

## Architettura

```
                        ┌──────────────────┐
                        │ News Scanner     │ (gira su Mac/Mac Mini)
                        │ alle 7:00 IT     │ → entry Notion "Da Fare"
                        └────────┬─────────┘
                                 │
        ┌────────────────────────▼────────────────────────┐
        │  Editorial Approval Agent (Railway, 24/7)       │
        │  ─────────────────────────────────────────────  │
        │  GET  /today          (pagina HTML mobile)      │
        │  POST /approve-brief  → Gemini image + WP draft │
        │  GET  /preview/<id>   (pagina HTML mobile)      │
        │  POST /promote/<id>   → flip draft → publish    │
        │  POST /delete/<id>    → cancella draft          │
        │  POST /twilio-webhook (opzionale: WhatsApp)     │
        └────────┬────────────────────────────────────────┘
                 │
                 ├─→ Notion API (legge brief, scrive Stato + Note)
                 ├─→ gemini-3.1-flash-image (Gemini API: genera visual)
                 └─→ WP albeni/v1/* (upload media + create/promote/delete pagine)
                                ↓
                       merinouniversity.com  ← live
```

## Flusso utente — 2 tap dal phone

**Tap 1**: apri `https://<railway-url>/today` sul phone.
- Vedi il brief del giorno (titolo, dominio, tema, shelf life, body)
- Tappi "Approva e crea draft"
- Aspetti ~10 secondi (generazione immagine + upload + WP create)

**Tap 2**: vieni rediretto a `/preview/<notion-id>`.
- Vedi il visual generato e il link alla preview reale della pagina IT su MU
- 3 bottoni: "Pubblica live", "Mantieni come draft", "Cancella"
- Tap "Pubblica" → in 5 secondi la pagina è online

Nessun Terminal, nessun comando, nessun Mac.

## Stato Phase 1 (MVP)

✅ Visual generation cloud via gemini-3.1-flash-image
✅ Frontend mobile-first responsive
✅ Backend orchestration (today/approve/promote/delete)
✅ Notion come state store (marker `[DRAFT-PENDING]`)
✅ Webhook Twilio attivo (canale alternativo per OK/STOP via WhatsApp)
✅ TTL drafts: hot 7gg / warm 20gg / evergreen 30gg
✅ Endpoint WP `promote-draft-to-publish` e `delete-drafts` deployati su MU
✅ Smoke test endpoints MU verificato

⚠️ **Limiti Phase 1**:
- Solo dominio `merinouniversity.com` (WoM in Phase 2)
- Solo lingua IT (EN/DE/FR in Phase 2 con MT translator)
- WhatsApp push notifications opzionali (per ora apri `/today` manualmente)

## File del repository

```
editorial-approval-agent/
├── agent.py            # FastAPI app + scheduler + Notion/WP/Twilio helpers (~620 righe)
├── visual_gen.py       # gemini-3.1-flash-image → WP Media Library upload (~190 righe)
├── orchestrator.py     # Endpoint /today /approve-brief /promote /delete (~290 righe)
├── frontend.py         # Pagine HTML mobile /today e /preview (~330 righe)
├── requirements.txt    # 10 dependencies Python
├── Procfile            # web: uvicorn agent:app
├── railway.toml        # config Railway (NIXPACKS, healthcheck, restart)
└── README.md           # this file
```

Ogni modulo è single-file e auto-contenuto. Niente database esterno.

## Deploy su Railway — runbook

### Prerequisiti

Account Railway già attivo (lo usi per il dashboard approval). Subscription stesso, niente costi aggiuntivi a Hobby plan ($5/mese — già pagato).

### 1. Push del codice su GitHub

Da Mac/Mac Mini:
```
cd ~/Documents/Claude/Projects/Merino\ News/editorial-approval-agent
git init
git add .
git commit -m "Initial: editorial-approval-agent v1 conservative"
# ... push verso un repo privato GitHub
```

Oppure se preferisci CLI Railway diretta (saltando GitHub):
```
brew install railway
cd ~/Documents/Claude/Projects/Merino\ News/editorial-approval-agent
railway login
railway link        # collega al tuo progetto Railway esistente
railway up          # build & deploy
```

### 2. Crea il service Railway

Dal dashboard:
- **New** → **GitHub Repo** → seleziona il repo
- O da CLI il `railway up` di sopra crea il service automaticamente
- Service name: `editorial-approval-agent`

### 3. Variabili d'ambiente

Dal Railway dashboard → service → **Variables** → aggiungi tutte queste:

| Variable | Valore | Note |
|---|---|---|
| `NOTION_API_KEY` | `ntn_...` | Dal `.env` Mac |
| `NOTION_DB_ID` | `98a46d37-3b7d-4455-ad86-0da224b00b01` | Content Pipeline |
| `WP_UPLOAD_SECRET` | (dal `.env`) | Stesso secret WoM/MU |
| `WP_BASE_MU` | `https://merinouniversity.com` | |
| `WP_BASE_WOM` | `https://worldofmerino.com` | |
| `GEMINI_API_KEY` | (dal `.env`) | Per gemini-3.1-flash-image |
| `TWILIO_ACCOUNT_SID` | `ACbd9ebba...` | |
| `TWILIO_AUTH_TOKEN` | (dal `.env`) | |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+14155238886` | |
| `ROBERTO_WHATSAPP` | `+39335271008` | |
| `CONSERVATIVE_MODE` | `true` | |
| `AGENT_VERSION` | `v1.0.0-mobile` | |

### 4. Verifica deploy

Railway ti dà un URL pubblico tipo `https://editorial-approval-agent-production.up.railway.app`. Apri:

```
https://<railway-url>/
```

Atteso: JSON con `service`, `version`, `scheduler_running: true`, `next_runs`.

### 5. Apri sul phone

```
https://<railway-url>/today
```

Vedi il brief più recente in stato "Da Fare". Se non c'è nulla, vedrai "Nessun brief in attesa" (lo scanner del giorno deve ancora girare).

### 6. (Opzionale) WhatsApp push

Se vuoi ricevere un WhatsApp con il link `/today` ogni mattina dopo che lo scanner ha generato il brief, aggiungeremo in Phase 2 una scheduled task Railway che alle 7:30 IT manda:

```
📰 Brief del giorno pronto.
Apri: https://<railway-url>/today
```

Per ora apri tu il link al mattino dal phone.

### 7. (Opzionale) Webhook Twilio per comandi rapidi WhatsApp

Console Twilio → **Sandbox configuration** → "WHEN A MESSAGE COMES IN":
```
https://<railway-url>/twilio-webhook  (HTTP POST)
```

Da quel momento, se mandi `OK` o `STOP` al sandbox WhatsApp da phone, l'agent agisce sulla draft più recente. Utile come scorciatoia se hai aperto il brief sul phone e vuoi "OKare" senza tornare al browser.

## Smoke test post-deploy

Sull'URL pubblico:

```bash
curl https://<railway-url>/
# → JSON con scheduler_running: true

curl https://<railway-url>/today/data
# → JSON con il brief più recente "Da Fare"
```

Se entrambi rispondono OK, apri il phone su `/today` e fai il primo flow reale.

## Troubleshooting

| Sintomo | Diagnosi | Fix |
|---|---|---|
| `/today/data` ritorna `empty: true` | Nessuna entry Notion in stato "Da Fare" | Lascia girare lo scanner mattutino |
| `/approve-brief/<id>` 500 "Visual generation failed" | Gemini image API down o quota esaurita | Vedi log Railway |
| `/approve-brief/<id>` 500 "WP create-draft failed" | Endpoint MU non risponde o secret errato | Lancia `pipeline/smoke-test-endpoints.sh mu` da Mac |
| Pagina draft creata ma `/preview` errore | Marker `[DRAFT-PENDING]` non scritto in Notion | Vedi log dell'endpoint approve |
| Bottone "Pubblica" 400 "Marker non trovato" | Note Notion modificato manualmente | Riavvia il flow da `/today` |
| Visual non appare in `/preview` | URL nel marker malformato | Verifica `Visual:` line nella property Note di Notion |

## Roadmap Phase 2

- [ ] Migrazione `merino-news-scanner` da scheduled task Cowork (Mac) a Railway cron
- [ ] Migrazione `albeni-mt-translator` (genera EN/DE/FR) come endpoint cloud
- [ ] WhatsApp push automatico ogni mattina (apri `/today` con un tap dalla notifica)
- [ ] Backport pipeline su WoM (`wpcode-albeni-v1-endpoints.php` + `deploy_radar_post.py`)
- [ ] Whitelist auto-publish per topic verdi (dopo 30gg di osservazione conservativa)
- [ ] Production WhatsApp number (esce da sandbox 24h)
- [ ] Digest giornaliero 08:00 a Roberto via WhatsApp con riepilogo decisioni 24h
