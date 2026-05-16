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
# 1. Install dependencies
uv sync

# 2. Install Ollama and pull a local model (one-time)
brew install ollama
ollama serve &
ollama pull qwen3:8b            # ~5GB. Fits on an 8GB Mac. Use qwen3:14b/27b if you have more RAM.
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

- **`chained_local`** (default; designed for Ollama Qwen 3 8B): three small calls — JD extract -> project rerank -> STAR bullets. Each prompt is tiny which is what local 8B models do reliably.
- **`single_call`** (use with Claude Sonnet / GPT-4o): one structured-output call returning the whole tailored resume JSON.

Output schema is identical either way, so `validate.py` and `render/typst.py` don't care which path ran.

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

## Roadmap

- **Phase 0 + Phase 1 (this repo today):** ATS HTTP ingestion, filter, Tailor + validate, Typst PDF render, SQLite tracker, CLI end-to-end.
- **Phase 2:** Streamlit dashboard + uploaders + approval queue.
- **Phase 3:** Playwright form-fillers for Greenhouse/Lever/Ashby/Workday + account creation + Responder for custom questions.
- **Phase 4:** LinkedIn Easy Apply + Indeed via Patchright (stealth) + optional residential proxy.
- **Phase 5:** FastAPI service layer, docker-compose, Postgres adapter, Mac-mini compute offload.

## Running on a remote Mac mini (compute offload)

The simplest pattern is to keep the orchestrator local and offload only the LLM:

```bash
# On the Mac mini:
OLLAMA_HOST=0.0.0.0:11434 ollama serve

# On your laptop (.env):
OLLAMA_HOST=http://mac-mini.local:11434
```

No code change required — every LLM call now runs on the mini.
