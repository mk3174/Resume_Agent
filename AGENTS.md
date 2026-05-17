# AGENTS.md

## Cursor Cloud specific instructions

### Project overview

Resume Agent is a Python CLI pipeline that scrapes ATS job boards, filters jobs, tailors resumes via LLM, validates honesty guardrails, and renders ATS-clean PDFs. See `README.md` for full architecture.

### Key commands

| Action | Command |
|---|---|
| Install deps | `uv sync --all-extras` |
| Lint | `uv run ruff check .` |
| Tests | `uv run pytest` |
| CLI help | `uv run resume-agent --help` |
| Health check | `uv run resume-agent doctor` |
| Index projects | `uv run resume-agent index` |
| Ingest jobs | `uv run resume-agent ingest` |
| Full pipeline | `uv run resume-agent run --dry-apply` |

### Ollama (required local LLM server)

Ollama must be running before any LLM or embedding call. Start it with `ollama serve` (in background or a separate tmux session). Two models are required:

- `qwen3:8b` — chat/rerank (pull with `ollama pull qwen3:8b`)
- `nomic-embed-text` — embeddings (pull with `ollama pull nomic-embed-text`)

Verify with `uv run resume-agent doctor`.

### Config files

- `.env` — copy from `.env.example`; only `OLLAMA_HOST` is needed for local-only mode. Telegram/Anthropic/OpenAI keys are optional.
- `config/settings.yaml`, `config/preferences.yaml`, `config/personal_facts.yaml` — already committed with working defaults.

### Gotchas

- The test suite (`tests/`) is currently empty; `pytest` will report 0 collected but exit code 5 (no tests). This is expected.
- Ruff reports ~41 pre-existing lint warnings in the codebase. These are not blocking.
- The Ashby ingestion for `openai` board throws a non-fatal warning (`'str' object has no attribute 'get'`); this is a known API response format issue and does not block the pipeline.
- SQLite DB and ChromaDB are auto-created on disk under `data/`; no external database setup needed.
- `uv` must be on `$PATH` — install via `curl -LsSf https://astral.sh/uv/install.sh | sh` and ensure `$HOME/.local/bin` is in PATH.
