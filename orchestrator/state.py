"""Shared domain models + LangGraph state.

Every node in the orchestrator reads/writes ApplicationState. Keeping all model
definitions here (instead of scattered across modules) means the data contract
is grep-able in one file.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


# ---------------------------------------------------------------------------
# Job + JD
# ---------------------------------------------------------------------------


class JobSource(str, Enum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    LINKEDIN = "linkedin"
    INDEED = "indeed"


class JobPosting(BaseModel):
    """Raw job as fetched from a board, before any LLM analysis."""

    source: JobSource
    external_id: str = Field(description="Stable id from the source board.")
    company: str
    title: str
    location: str | None = None
    employment_type: str | None = None
    department: str | None = None
    salary_min_usd: int | None = None
    salary_max_usd: int | None = None
    description_text: str = ""
    description_html: str | None = None
    apply_url: HttpUrl | str
    posted_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def stable_key(self) -> str:
        return f"{self.source.value}:{self.external_id}"


class JDAnalysis(BaseModel):
    """Output of Tailor step 1 (or extracted from single_call output)."""

    must_have_keywords: list[str] = Field(default_factory=list)
    nice_to_have_keywords: list[str] = Field(default_factory=list)
    semantic_themes: list[str] = Field(
        default_factory=list,
        description="Higher-level themes, e.g. 'production LLM evals', 'agentic RAG'.",
    )
    seniority: str | None = None
    primary_responsibilities: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# User-side: projects, master resume, bullets
# ---------------------------------------------------------------------------


class Project(BaseModel):
    """A single user-uploaded project."""

    id: str
    title: str
    summary: str
    tech_stack: list[str] = Field(default_factory=list)
    impact_metrics: list[str] = Field(
        default_factory=list,
        description="Real numbers the user supplied: '2x throughput', '40% latency drop'.",
    )
    role: str | None = None
    body: str = Field(description="Full Markdown description.")
    url: str | None = None


class WorkExperience(BaseModel):
    company: str
    title: str
    start: str  # YYYY-MM
    end: str | None = None  # YYYY-MM or None for current
    location: str | None = None
    base_bullets: list[str] = Field(default_factory=list)


class MasterResume(BaseModel):
    name: str
    headline: str
    contact: dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    skills: list[str] = Field(default_factory=list)
    experience: list[WorkExperience] = Field(default_factory=list)
    education: list[dict[str, str]] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    publications: list[str] = Field(default_factory=list)
    projects_sections: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict,
        description="## Projects section heading -> [{title, base_bullets}] from master ### subheadings.",
    )
    section_order: list[str] = Field(
        default_factory=lambda: ["Summary", "Experience", "Education", "Certifications"],
        description="## heading order from master_resume.md — controls PDF section layout.",
    )
    layout: dict[str, Any] = Field(
        default_factory=dict,
        description="Typography / layout knobs from master frontmatter (font, sizes, skills placement).",
    )


# ---------------------------------------------------------------------------
# Tailored output
# ---------------------------------------------------------------------------


BulletSource = Literal["project", "experience", "estimate", "summary"]


class ResumeBullet(BaseModel):
    """A single STAR bullet with provenance metadata.

    `source` + `source_ref` is the contract that validate.py enforces.
    """

    text: str
    source: BulletSource
    source_ref: str = Field(
        description="e.g. 'project:rag_eval_harness' or 'experience:acme:2022'."
    )
    star: dict[str, str] = Field(
        default_factory=dict,
        description="Structured S/T/A/R fields for auditing. Optional.",
    )
    keywords_hit: list[str] = Field(default_factory=list)
    has_estimate: bool = False


class TailoredExperience(BaseModel):
    company: str
    title: str
    start: str
    end: str | None = None
    location: str | None = None
    bullets: list[ResumeBullet]


class TailoredResume(BaseModel):
    """The single Tailor output. Same shape regardless of single_call vs chained_local."""

    headline: str
    summary: str
    skills: list[str]
    experience: list[TailoredExperience]
    selected_projects: list[str] = Field(
        default_factory=list, description="Project ids included in the resume."
    )
    project_bullets: list[ResumeBullet] = Field(default_factory=list)
    education: list[dict[str, str]] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    publications: list[str] = Field(default_factory=list)
    cover_paragraph: str = ""
    ats_coverage: float = 0.0  # filled by validate.py


# ---------------------------------------------------------------------------
# Q&A
# ---------------------------------------------------------------------------


class QAEntry(BaseModel):
    question: str
    answer: str
    confidence: float
    source: Literal["personal_facts", "qa_memory", "llm", "human"] = "llm"
    needs_review: bool = False


# ---------------------------------------------------------------------------
# Pipeline status + barriers
# ---------------------------------------------------------------------------


class ApplicationStatus(str, Enum):
    NEW = "new"
    FILTERED_OUT = "filtered_out"
    TAILORED = "tailored"
    NEEDS_REVIEW = "needs_review"
    READY_TO_APPLY = "ready_to_apply"
    APPLIED = "applied"
    BARRIER = "barrier"
    FAILED = "failed"


class BarrierKind(str, Enum):
    CAPTCHA = "captcha"
    TWO_FACTOR = "2fa"
    EMAIL_VERIFY = "email_verify"
    BAN_SIGNAL = "ban_signal"
    LOW_CONF_QA = "low_conf_qa"
    MISSING_FACT = "missing_fact"
    ATS_FAIL = "ats_fail"
    UNKNOWN = "unknown"


class Barrier(BaseModel):
    kind: BarrierKind
    message: str
    context: dict[str, Any] = Field(default_factory=dict)
    raised_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: datetime | None = None
    human_response: str | None = None


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------


class ApplicationState(BaseModel):
    """The single object that flows through the LangGraph pipeline.

    Each node reads what it needs and writes back. Validators don't strip extras
    so checkpoints can persist freely.
    """

    job: JobPosting
    jd_analysis: JDAnalysis | None = None
    candidate_projects: list[Project] = Field(default_factory=list)
    tailored: TailoredResume | None = None
    validation_errors: list[str] = Field(default_factory=list)
    retry_count: int = 0
    qa: list[QAEntry] = Field(default_factory=list)
    barriers: list[Barrier] = Field(default_factory=list)
    status: ApplicationStatus = ApplicationStatus.NEW
    output_dir: str | None = None  # populated by store.py once we know the slug
    artifacts: dict[str, str] = Field(
        default_factory=dict,
        description="Filename -> path. e.g. {'resume.pdf': '...'}",
    )
    estimates_present: bool = False

    model_config = {"arbitrary_types_allowed": True}
