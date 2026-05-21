"""Responder agent for custom application questions.

Lookup order:
  1. personal_facts.yaml   (deterministic; no LLM)
  2. qa_memory.json        (cosine similarity >= settings.responder.similarity_threshold)
  3. Local Ollama with full resume + job context (open-ended / essay questions)
  4. LLM fallback (same router, task='responder')

If confidence < threshold OR question contains an "always_escalate" keyword
AND no personal_facts entry covers it, return needs_review=True so the
orchestrator pings Telegram.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from config_loader import PROJECT_ROOT, load_personal_facts, load_settings
from llm.prompts import render as render_prompt
from llm.router import get_router
from orchestrator.state import MasterResume, QAEntry, TailoredResume
from pipeline.resume_context import build_responder_context
from retrieval.embeddings import cosine, embed

log = logging.getLogger(__name__)

_OPEN_ENDED_MARKERS = (
    "why",
    "interest",
    "motivat",
    "5 year",
    "five year",
    "yourself",
    "tell us",
    "describe",
    "what excites",
    "what attracts",
    "passion",
    "cover letter",
    "anything else",
    "greatest strength",
    "biggest challenge",
    "proud",
    "mission",
    "culture",
    "fit",
)


def answer(
    question: str,
    *,
    job_title: str,
    company: str,
    master: MasterResume | None = None,
    tailored: TailoredResume | None = None,
    job_description: str = "",
) -> QAEntry:
    settings = load_settings()["responder"]
    threshold_sim = float(settings["similarity_threshold"])
    threshold_conf = float(settings["confidence_threshold"])
    escalate_kw = [k.lower() for k in settings.get("always_escalate_keywords") or []]

    q_lower = question.lower()
    must_escalate = any(k in q_lower for k in escalate_kw)
    open_ended = _is_open_ended(q_lower)

    # 1) personal_facts
    facts = load_personal_facts()
    fact_answer = _from_personal_facts(question, facts, company=company)
    if fact_answer is not None:
        return QAEntry(question=question, answer=fact_answer, confidence=0.99, source="personal_facts")

    # 2) qa_memory
    cached = _from_qa_memory(question, threshold=threshold_sim)
    if cached is not None and not must_escalate:
        return cached

    # 3) Local Ollama with resume context (preferred for essay / why-company questions)
    resume_context = build_responder_context(
        master=master,
        tailored=tailored,
        job_description=job_description,
    )
    if resume_context and (open_ended or master is not None):
        entry = _answer_with_resume_context(
            question,
            job_title=job_title,
            company=company,
            resume_context=resume_context,
            must_escalate=must_escalate,
            open_ended=open_ended,
            threshold_conf=threshold_conf,
        )
        if entry.answer or entry.needs_review:
            return entry

    # 4) Generic LLM fallback (no resume block)
    return _answer_generic(
        question,
        job_title=job_title,
        company=company,
        must_escalate=must_escalate,
        threshold_conf=threshold_conf,
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


def _is_open_ended(q_lower: str) -> bool:
    return any(m in q_lower for m in _OPEN_ENDED_MARKERS)


def _answer_with_resume_context(
    question: str,
    *,
    job_title: str,
    company: str,
    resume_context: str,
    must_escalate: bool,
    open_ended: bool,
    threshold_conf: float,
) -> QAEntry:
    router = get_router()
    prompt = render_prompt(
        "responder_application.j2",
        question=question,
        job_title=job_title,
        company=company,
        resume_context=resume_context,
    )
    sys = (
        "You write authentic job-application answers grounded in the candidate's real resume. "
        "Plain text only."
    )
    try:
        raw, prov = router.chat(
            task="responder",
            prompt=prompt,
            system=sys,
            max_tokens=512 if open_ended else 256,
            temperature=0.35,
            think=False,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("contextual responder failed: %s", e)
        return QAEntry(question=question, answer="", confidence=0.0, source="llm", needs_review=True)

    text = raw.strip()
    if text.upper().startswith("NEEDS_HUMAN") or len(text) < 8:
        return QAEntry(question=question, answer="", confidence=0.0, source="llm", needs_review=True)

    needs_review = must_escalate
    confidence = _heuristic_confidence(text, question)
    if open_ended and not must_escalate:
        confidence = max(confidence, 0.75)
        needs_review = False
    elif confidence < threshold_conf:
        needs_review = True

    return QAEntry(
        question=question,
        answer="" if needs_review else text,
        confidence=confidence,
        source="llm",
        needs_review=needs_review,
    )


def _answer_generic(
    question: str,
    *,
    job_title: str,
    company: str,
    must_escalate: bool,
    threshold_conf: float,
) -> QAEntry:
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
        raw, prov = router.chat(task="responder", prompt=prompt, system=sys, max_tokens=400)
    except Exception as e:  # noqa: BLE001
        log.warning("responder LLM failed: %s", e)
        return QAEntry(question=question, answer="", confidence=0.0, source="llm", needs_review=True)
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


def _from_personal_facts(question: str, facts: dict[str, Any], *, company: str = "") -> str | None:
    """Naive keyword-routing into the structured facts file. Extended ad-hoc."""
    q = question.lower()

    # Why this company / role (use canned + company name)
    if any(k in q for k in ("why", "interest", "motivat", "what excites", "what attracts")):
        if company.lower() in q or "company" in q or "role" in q or "work at" in q:
            canned = (facts.get("canned_answers") or {}).get("why_interested_in_ai") or ""
            if canned:
                return f"{canned.strip()} I'm especially interested in {company} because the {company} team ships production AI systems at scale."

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

    # 5-year vision
    if "5 year" in q or "five year" in q:
        canned = (facts.get("canned_answers") or {}).get("five_year_plan") or ""
        if canned:
            return canned.strip()
        return (
            "In five years I want to be a senior IC leading production LLM systems end-to-end — "
            "architecture, evals, and reliable delivery — while mentoring engineers on applied AI."
        )

    # Relocation
    if "relocat" in q:
        wa = facts.get("work_authorization") or {}
        rel = wa.get("open_to_relocation")
        if rel is True:
            return "Yes"
        if rel is False:
            return "No"

    # Source / referral
    if "how did you hear" in q or "how did you find" in q or "where did you hear" in q:
        return "LinkedIn"

    # Voluntary self-identification acknowledgement (checkbox)
    if ("voluntary self" in q or "self-identify" in q) and "gender" not in q:
        return "Yes"

    # Location (common Lever required dropdown)
    if q.strip().lower().startswith("location"):
        return "United States"

    # Demographics (including Lever eeo[...] field names)
    if any(k in q for k in ("gender", "race", "ethnicity", "veteran", "disability", "eeo[")):
        dem = facts.get("demographics") or {}
        if "veteran" in q:
            vs = dem.get("veteran_status")
            if vs and "not" in str(vs).lower():
                return "I am not a veteran"
            return str(vs or "").replace("_", " ")
        if "disability" in q:
            ds = dem.get("disability_status")
            if ds and str(ds).lower() in {"no", "false"}:
                return "No, I don't have a disability"
            return str(ds or "").replace("_", " ")
        if "gender" in q:
            g = dem.get("gender")
            if g and "prefer" not in str(g).lower():
                return str(g).capitalize() if str(g).lower() in {"male", "female"} else str(g).replace("_", " ")
        if "race" in q or "ethnicity" in q:
            eth = dem.get("ethnicity")
            if eth and "prefer" not in str(eth).lower():
                return str(eth).replace("_", " ").title()
        for key, val in dem.items():
            if key.replace("_", " ") in q or key in q:
                human = str(val).replace("_", " ")
                if "prefer not" in human.lower():
                    return "Decline to self-identify"
                if key == "veteran_status" and "not" in human.lower():
                    return "I am not a veteran"
                return human

    # Canned answers
    canned = facts.get("canned_answers") or {}
    for key, val in canned.items():
        if key.replace("_", " ") in q:
            return val.strip()

    return None


def _heuristic_confidence(text: str, question: str) -> float:
    """Very rough: longer thoughtful answers get higher score, ultra-short = low."""
    _ = question
    n = len(text.split())
    if n < 10:
        return 0.4
    if n < 25:
        return 0.7
    return 0.85
