"""Responder agent for custom application questions.

Lookup order:
  1. personal_facts.yaml   (deterministic; no LLM)
  2. qa_memory.json        (cosine similarity >= settings.responder.similarity_threshold)
  3. LLM (Tailor's router, task='responder')

If confidence < threshold OR question contains an "always_escalate" keyword
AND no personal_facts entry covers it, return needs_review=True so the
orchestrator pings Telegram.

This module is wired but minimally exercised in Phase 1 (form-filling lives in
Phase 3). It's importable today so unit tests and the eventual Streamlit
"answer this question" UI both have a stable surface.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from config_loader import PROJECT_ROOT, load_personal_facts, load_settings
from llm.router import get_router
from orchestrator.state import QAEntry
from retrieval.embeddings import cosine, embed

log = logging.getLogger(__name__)


def answer(question: str, *, job_title: str, company: str) -> QAEntry:
    settings = load_settings()["responder"]
    threshold_sim = float(settings["similarity_threshold"])
    threshold_conf = float(settings["confidence_threshold"])
    escalate_kw = [k.lower() for k in settings.get("always_escalate_keywords") or []]

    q_lower = question.lower()
    must_escalate = any(k in q_lower for k in escalate_kw)

    # 1) personal_facts
    facts = load_personal_facts()
    fact_answer = _from_personal_facts(question, facts)
    if fact_answer is not None:
        return QAEntry(question=question, answer=fact_answer, confidence=0.99, source="personal_facts")

    # 2) qa_memory
    cached = _from_qa_memory(question, threshold=threshold_sim)
    if cached is not None and not must_escalate:
        return cached

    # 3) LLM
    router = get_router()
    sys = (
        "You answer custom job-application questions on the candidate's behalf. "
        "Be concise (<=4 sentences), professional, and specific to the role/company."
    )
    prompt = (
        f"Job: {job_title} @ {company}\n\n"
        f"Question: {question}\n\n"
        "If you don't have enough info to answer truthfully and specifically, "
        "respond with the literal string NEEDS_HUMAN."
    )
    try:
        raw, _ = router.chat(task="responder", prompt=prompt, system=sys, max_tokens=400)
    except Exception as e:  # noqa: BLE001
        log.warning("responder LLM failed: %s", e)
        return QAEntry(
            question=question, answer="", confidence=0.0, source="llm", needs_review=True
        )
    text = raw.strip()
    needs_review = must_escalate or text.upper().startswith("NEEDS_HUMAN") or len(text) < 8
    confidence = 0.0 if needs_review else _heuristic_confidence(text, question)
    if confidence < threshold_conf:
        needs_review = True
    return QAEntry(
        question=question,
        answer="" if needs_review else text,
        confidence=confidence,
        source="llm",
        needs_review=needs_review,
    )


def remember(entry: QAEntry) -> None:
    """Append a confirmed (non-needs_review) answer to qa_memory.json."""
    if entry.needs_review or not entry.answer:
        return
    path = _qa_memory_path()
    data = _load_qa_memory()
    vec = embed([entry.question])[0]
    data.append({"q": entry.question, "a": entry.answer, "v": vec})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _qa_memory_path() -> Path:
    return PROJECT_ROOT / load_settings()["responder"]["qa_memory_path"]


def _load_qa_memory() -> list[dict[str, Any]]:
    path = _qa_memory_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("qa_memory.json corrupt; ignoring")
        return []


def _from_qa_memory(q: str, *, threshold: float) -> QAEntry | None:
    data = _load_qa_memory()
    if not data:
        return None
    qv = embed([q])[0]
    best_score = -1.0
    best = None
    for item in data:
        if "v" not in item:
            continue
        s = cosine(qv, item["v"])
        if s > best_score:
            best_score = s
            best = item
    if best and best_score >= threshold:
        return QAEntry(question=q, answer=best["a"], confidence=best_score, source="qa_memory")
    return None


def _from_personal_facts(question: str, facts: dict[str, Any]) -> str | None:
    """Naive keyword-routing into the structured facts file. Extended ad-hoc."""
    q = question.lower()

    # Visa / sponsorship
    if any(k in q for k in ("sponsor", "visa", "h1b", "work authorization", "authorized to work")):
        wa = facts.get("work_authorization") or {}
        cit = wa.get("citizen_or_pr_in") or []
        visa = wa.get("visa_required_in") or []
        if cit:
            return (
                f"I am authorized to work in {', '.join(cit)} without sponsorship. "
                + (f"I would require sponsorship in {', '.join(visa)}." if visa else "")
            ).strip()

    # Salary
    if any(k in q for k in ("salary", "compensation", "pay expectation", "expected salary")):
        comp = facts.get("compensation") or {}
        if comp.get("base_target_usd"):
            return (
                f"My target base is around ${comp['base_target_usd']:,} USD, with flexibility "
                f"between ${comp.get('base_min_usd', 0):,} and ${comp.get('base_max_usd', 0):,} "
                "depending on total package."
            )

    # Notice / start date
    if "notice" in q or "start" in q or "available" in q:
        av = facts.get("availability") or {}
        if av.get("notice_period_weeks") is not None:
            return (
                f"I can give {av['notice_period_weeks']} weeks of notice; "
                f"earliest start {av.get('earliest_start_date', 'flexible')}."
            )

    # Years experience
    yr = facts.get("experience_years") or {}
    for skill, years in yr.items():
        if skill.lower() in q and ("year" in q or "experience" in q):
            return f"{years}+ years of hands-on {skill} experience."

    # Demographics
    if any(k in q for k in ("gender", "race", "ethnicity", "veteran", "disability")):
        dem = facts.get("demographics") or {}
        for key, val in dem.items():
            if key in q:
                return val.replace("_", " ")

    # Canned answers
    canned = facts.get("canned_answers") or {}
    for key, val in canned.items():
        if key.replace("_", " ") in q:
            return val.strip()

    return None


def _heuristic_confidence(text: str, question: str) -> float:
    """Very rough: longer thoughtful answers get higher score, ultra-short = low."""
    n = len(text.split())
    if n < 10:
        return 0.4
    if n < 25:
        return 0.7
    return 0.85
