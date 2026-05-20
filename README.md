# Resume Agent

A lean, modular job-application pipeline. **Two LLM-driven steps** (`Tailor`, `Responder`) and a set of deterministic Python modules (ingest, filter, validate, render, apply, store, notify). Local-first via Ollama, optional cloud fallback. Auto-submits clean applications, only pings Telegram on real barriers (CAPTCHA, 2FA, ban-detection, low-confidence custom Q).

## Architecture at a glance

```
Scheduler -> ingest -> filter -> Tailor (LLM) -> validate -> Responder (LLM, only if needed)
          -> apply -> [account] -> Submit -> store -> Streamlit dashboard
                  \--- barrier? -> Telegram pause ---/
```

| Folder | Purpose | LLM? |
|---|---|---|
| `pipeline/tailor.py` | JD analysis + project pick + STAR bullets in one step | YES |
| `pipeline/responder.py` | Custom application-question answers | YES (sometimes) |
| `pipeline/validate.py` | Provenance + ATS coverage + length checks | no |
| `ingest/` | Greenhouse/Lever/Ashby JSON; later Workday/LinkedIn/Indeed | no |
| `filters/` | YAML rules + embedding similarity to your prefs | embeddings only |
| `render/` | Markdown -> Typst -> ATS-clean PDF | no |
| `apply/` | Per-platform Playwright form-fillers (Phase 3) | no |
| `store/` | SQLite ledger + per-job output folder | no |
| `notify/` | Telegram `notify()` (FYI) + `barrier_pause()` (blocking) | no |
| `llm/` | Provider router: Ollama default, Claude/GPT-4o opt-in | yes |
| `retrieval/` | Chroma + nomic-embed-text for project library + master resume KB | embeddings |

## Quick start

```bash
# 1. Install dependencies (playwright extra = Patchright, required for Lever/Greenhouse apply)
uv sync --extra playwright
uv run patchright install chromium

# 2. Install Ollama and pull a local model (one-time)
brew install ollama
ollama serve &
ollama pull gemma4:e4b        # chat + rerank (see Ollama library for exact size)
ollama pull nomic-embed-text    # ~275MB embeddings

# 3. Copy and edit your config and inputs
cp .env.example .env
cp config/settings.example.yaml config/settings.yaml
cp config/preferences.example.yaml config/preferences.yaml
cp config/personal_facts.example.yaml config/personal_facts.yaml

# Edit:
#   data/input/master_resume.md           <- your real resume in Markdown
#   data/input/projects/*.md              <- one file per project (see example)
#   config/preferences.yaml               <- target roles, locations, salary, must-have skills
#   config/personal_facts.yaml            <- visa, work-auth, salary band, demographics

# 4. Index your project library + master resume
uv run resume-agent index

# 5. Pull jobs from configured ATS company boards
uv run resume-agent ingest

# 6. Run the full pipeline (filter -> tailor -> validate -> render) in dry-apply mode
uv run resume-agent run --dry-apply

# Per-job artifacts land in data/output/YYYYMMDD_company_role/
# Every application is tracked in data/tracker.db (SQLite).
```

## How the Tailor step actually runs

`config/settings.yaml -> tailor.mode` controls one of two paths:

- **`chained_local`** (default; designed for a small local Ollama model such as `gemma4:e4b`): three small calls — JD extract -> project rerank -> STAR bullets. Each prompt is tiny, which is what compact local models handle reliably.
- **`single_call`** (use with Claude Sonnet / GPT-4o): one structured-output call returning the whole tailored resume JSON.

Output schema is identical either way, so `validate.py` and `render/typst.py` don't care which path ran.

**If Tailor fails with `JSON parse failed` / `tailor crashed`:** ingestion still worked — the failure is in the LLM step. Common causes: (1) **`rerank` routed to Ollama first** — `gemma4:e4b` returns empty on long rerank prompts; use `rerank: [anthropic, ollama]`. (2) **`Unterminated string` on STAR bullets** — Anthropic output was **truncated** because `tailor.star_bullets_max_tokens` was too low (default is now 8000 with automatic retry at 2×). Ensure `ANTHROPIC_API_KEY` is set when using Anthropic.

## Per-job output folder

```
data/output/20260515_acme_ai-engineer/
  resume.pdf            # final ATS-clean PDF
  resume.tex            # the Typst source (for editing)
  resume.md             # the tailored markdown
  cover.md              # cover paragraph
  qa.json               # any custom questions answered (or marked needs-review)
  decisions.md          # which projects, why, which keywords matched, estimates flagged
  diff.html             # diff vs your master resume
  meta.json             # job posting + url + ats_score + final status
```

## Honesty guardrail

Every bullet that the model writes carries provenance: `project:foo`, `experience:acme:2022`, or `estimate`. `pipeline/validate.py` (deterministic, no LLM) rejects:
1. Bullets without traceable provenance.
2. Numeric metrics with no source AND no `[ESTIMATE]` flag.
3. ATS keyword coverage of JD must-haves below threshold (default 70%; re-tailor up to 2x).
4. Resume over 1 page / 700 words.

`[ESTIMATE]` bullets pass through but trigger a fire-and-forget Telegram notification post-submit so you can correct your master profile.

## Roadmap status

- **Phase 0 + Phase 1 — DONE:** ATS HTTP ingestion (Greenhouse / Lever / Ashby), filter, Tailor + validate, Typst PDF render, SQLite tracker, CLI end-to-end.
- **Phase 2 — DONE:** Streamlit dashboard + uploaders + Telegram-mirrored barrier queue (`apps/streamlit_ui/`).
- **Phase 3 — DONE:** Patchright form-fillers for Greenhouse / Lever / Ashby / Workday + account creation + Responder for custom questions.
- **Phase 4 — DONE:** LinkedIn Easy Apply + Indeed via Patchright (stealth) + optional residential proxy + ban-detection circuit breaker.
- **Phase 5 — DONE:** FastAPI service layer (`apps/api/`), docker-compose stack, Postgres adapter (via `DATABASE_URL`), `compute/runner.py` Runner protocol with `InProcess`/`SSH`/`Ray` backends + the canonical Mac-mini Ollama offload pattern.

## LinkedIn + Indeed (Phase 4)

Both sources are off by default. To enable:

```bash
# 1. Install the playwright extras (Patchright + stealth Chromium).
uv sync --extra playwright
uv run patchright install chromium

# 2. Save a LinkedIn session (one-time, headed):
uv run resume-agent linkedin-login    # browser pops up; sign in, the agent saves cookies

# 3. Flip on in config/settings.yaml:
#    ingest.linkedin.enabled: true
#    ingest.indeed.enabled:   true  (plus PROXY_URL if available)

# 4. Re-run ingestion or the full pipeline; the LinkedIn/Indeed ingestors honour
#    conservative per-platform rate limits and surface barriers on CAPTCHA / 2FA /
#    "unusual activity" prompts instead of grinding on.
uv run resume-agent run --dry-apply
```

### LinkedIn account safety (read this)

LinkedIn does **not** publish a public automation API or numeric rate limits for
scraping / Easy Apply. The defaults in `config/settings.yaml -> ingest.linkedin`
are deliberately slow-human paced: few job cards per sweep, long random waits
between clicks, slow scroll, at most one keyword search batch per `ingest`
(`max_searches_per_ingest`), and a **15+ minute** enforced gap between real
Easy Apply submits on this machine (`min_seconds_between_easy_apply_submits`).
The global `apply.rate_limit_per_hour` still caps all portals together.

**Never commit** `storage_state/linkedin.json` (gitignored) — it is equivalent
to a password. If this file or its contents ever leaked, sign out all LinkedIn
sessions and run `linkedin-login` again.

## FastAPI control plane (Phase 5)

Same operations as the CLI, exposed over HTTP so a separate front-end (Next.js,
mobile, hosted Streamlit) can drive the pipeline:

```bash
uv sync --extra api
uv run uvicorn apps.api.main:app --reload --port 8000

# Then:
curl http://localhost:8000/health
curl -X POST http://localhost:8000/pipeline/index
curl -X POST http://localhost:8000/pipeline/run -H 'Content-Type: application/json' \
     -d '{"limit": 3, "dry_apply": true}'
curl http://localhost:8000/applications
curl http://localhost:8000/barriers/pending
```

Endpoint set: `/health`, `/applications`, `/applications/{id}`,
`/pipeline/index`, `/pipeline/ingest`, `/pipeline/run`, `/barriers/pending`,
`/barriers/{id}/resolve`.

## Postgres swap (Phase 5)

The SQLAlchemy models are dialect-agnostic. To swap SQLite for Postgres:

```bash
uv sync --extra postgres
export DATABASE_URL=postgresql+psycopg://resume:resume@localhost:5432/resume_agent
uv run resume-agent doctor
```

`store/db.py` honours `DATABASE_URL` over `settings.store.db_url`. No code
changes; the docker-compose stack wires the same env var to a managed Postgres
container automatically.

## Docker stack (Phase 5)

```bash
docker compose up -d        # postgres + ollama + agent-api (8000) + agent-ui (8501)
docker compose logs -f agent-api
docker compose exec agent-api ollama pull gemma4:e4b   # one-time per fresh ollama vol
```

## Compute runner (Phase 5)

`compute/runner.py` defines a `Runner` protocol with four backends:

- `in_process` (default; everything local)
- `ollama_host` (LLM offload — set `OLLAMA_HOST=http://mac-mini.local:11434`, zero code change)
- `ssh` (push non-LLM batch jobs over SSH to a remote host)
- `ray` (full orchestrator offload via Ray actors; opt-in)

Switch via `settings.compute.mode`. The CLI and FastAPI service both honour it.

## Running on a remote Mac mini (compute offload)

The simplest pattern is to keep the orchestrator local and offload only the LLM:

```bash
# On the Mac mini:
OLLAMA_HOST=0.0.0.0:11434 ollama serve

# On your laptop (.env):
OLLAMA_HOST=http://mac-mini.local:11434
```

No code change required — every LLM call now runs on the mini.
