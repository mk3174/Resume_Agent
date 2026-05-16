"""Project library: load Markdown projects from disk, embed them into Chroma,
and expose a top-K query for the Tailor pipeline.

Convention: every file in `data/input/projects/*.md` is one project. YAML
frontmatter is parsed; the rest is the project body.

Example file:

    ---
    id: rag_eval_harness
    title: RAG Evaluation Harness
    tech_stack: [Python, Ragas, LangChain, Postgres]
    impact_metrics:
      - "Reduced eval cycle time from 3 days to 4 hours"
      - "Caught 12 regressions in last 6 weeks of releases"
    role: Tech lead
    url: https://github.com/me/rag-eval-harness
    ---
    Built an automated RAG evaluation harness that runs Ragas faithfulness,
    answer_relevancy, and context_precision against every PR ...
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import chromadb
import yaml
from chromadb.config import Settings as ChromaSettings

from config_loader import PROJECT_ROOT, load_settings
from orchestrator.state import Project
from retrieval.embeddings import embed

log = logging.getLogger(__name__)

PROJECTS_DIR = PROJECT_ROOT / "data" / "input" / "projects"
COLLECTION_NAME = "projects"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_markdown_project(path: Path) -> Project:
    raw = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ValueError(
            f"{path.name}: missing YAML frontmatter. Wrap metadata between ---"
        )
    meta_raw, body = m.group(1), m.group(2).strip()
    meta = yaml.safe_load(meta_raw) or {}
    pid = str(meta.get("id") or path.stem)
    title = meta.get("title") or pid
    summary = meta.get("summary") or _first_paragraph(body)
    return Project(
        id=pid,
        title=title,
        summary=summary,
        tech_stack=list(meta.get("tech_stack") or []),
        impact_metrics=list(meta.get("impact_metrics") or []),
        role=meta.get("role"),
        body=body,
        url=meta.get("url"),
    )


def _first_paragraph(body: str) -> str:
    for chunk in body.split("\n\n"):
        c = chunk.strip()
        if c:
            return c
    return body[:300]


# ---------------------------------------------------------------------------
# Chroma
# ---------------------------------------------------------------------------


def _client() -> chromadb.ClientAPI:
    chroma_dir = PROJECT_ROOT / load_settings()["store"]["chroma_dir"]
    chroma_dir.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(chroma_dir),
        settings=ChromaSettings(anonymized_telemetry=False),
    )


def _collection() -> chromadb.Collection:
    return _client().get_or_create_collection(name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"})


def load_projects_from_disk() -> list[Project]:
    if not PROJECTS_DIR.exists():
        return []
    out = []
    for path in sorted(PROJECTS_DIR.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            out.append(_parse_markdown_project(path))
        except Exception as e:  # noqa: BLE001
            log.warning("Skipping %s: %s", path.name, e)
    return out


def reindex() -> int:
    """Rebuild the Chroma collection from disk. Returns the number of projects indexed."""
    projects = load_projects_from_disk()
    coll = _collection()
    # Reset by deleting + recreating.
    client = _client()
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:  # noqa: BLE001
        pass
    coll = client.get_or_create_collection(name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"})

    if not projects:
        return 0

    texts = [_index_text(p) for p in projects]
    vectors = embed(texts)
    coll.add(
        ids=[p.id for p in projects],
        documents=texts,
        embeddings=vectors,
        metadatas=[
            {
                "title": p.title,
                "tech_stack": ", ".join(p.tech_stack),
            }
            for p in projects
        ],
    )
    return len(projects)


def _index_text(p: Project) -> str:
    parts = [
        p.title,
        p.summary,
        " ".join(p.tech_stack),
        " ".join(p.impact_metrics),
        p.body,
    ]
    return "\n".join(parts)


def query_top_k(jd_text: str, k: int = 10) -> list[Project]:
    """Return up to k projects ranked by cosine similarity to the JD text.

    If the collection is empty (e.g. user hasn't indexed yet), return all
    on-disk projects in their natural order so the pipeline still runs.
    """
    on_disk = {p.id: p for p in load_projects_from_disk()}
    if not on_disk:
        return []

    coll = _collection()
    if coll.count() == 0:
        return list(on_disk.values())[:k]

    qv = embed([jd_text])[0]
    res = coll.query(query_embeddings=[qv], n_results=min(k, coll.count()))
    ordered_ids: list[str] = res["ids"][0]
    return [on_disk[i] for i in ordered_ids if i in on_disk]
