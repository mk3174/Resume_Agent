"""Compact resume + job context strings for Responder / ATS-tune prompts."""

from __future__ import annotations

from orchestrator.state import JobPosting, MasterResume, TailoredResume


def format_education(master: MasterResume) -> str:
    lines: list[str] = []
    for ed in master.education:
        school = ed.get("school", "")
        degree = ed.get("degree", "")
        dates = ed.get("dates", "")
        line = " | ".join(p for p in (school, degree, dates) if p)
        if line:
            lines.append(f"- {line}")
    return "\n".join(lines) if lines else "(none listed)"


def format_experience(master: MasterResume, *, max_roles: int = 4) -> str:
    lines: list[str] = []
    for e in master.experience[:max_roles]:
        end = e.end or "Present"
        lines.append(f"- {e.company} | {e.title} | {e.start} – {end}")
        for b in e.base_bullets[:3]:
            lines.append(f"  • {b}")
    return "\n".join(lines) if lines else "(none listed)"


def format_tailored_highlights(tailored: TailoredResume | None) -> str:
    if tailored is None:
        return ""
    parts: list[str] = []
    if tailored.summary:
        parts.append(f"Summary: {tailored.summary}")
    if tailored.skills:
        parts.append(f"Skills: {', '.join(tailored.skills[:15])}")
    for e in tailored.experience[:2]:
        for b in e.bullets[:2]:
            parts.append(f"• {b.text}")
    for b in tailored.project_bullets[:2]:
        parts.append(f"• {b.text}")
    return "\n".join(parts)


def build_responder_context(
    *,
    master: MasterResume | None = None,
    tailored: TailoredResume | None = None,
    job: JobPosting | None = None,
    job_description: str = "",
) -> str:
    """Resume facts the local model should ground open-ended answers in."""
    sections: list[str] = []
    if master:
        sections.append(f"Candidate: {master.name}")
        if master.headline:
            sections.append(f"Headline: {master.headline}")
        if master.summary:
            sections.append(f"Background: {master.summary}")
        sections.append("Education:\n" + format_education(master))
        if master.certifications:
            sections.append("Certifications: " + "; ".join(master.certifications))
        if master.publications:
            sections.append("Publications: " + "; ".join(master.publications[:3]))
        sections.append("Experience:\n" + format_experience(master))
        if master.skills:
            sections.append(f"Core skills: {', '.join(master.skills[:20])}")
    if tailored:
        highlights = format_tailored_highlights(tailored)
        if highlights:
            sections.append("Tailored resume highlights:\n" + highlights)
    if job:
        sections.append(f"Target role: {job.title} @ {job.company}")
    desc = job_description or (job.description_text if job else "")
    if desc:
        sections.append("Job description excerpt:\n" + desc[:2500])
    return "\n\n".join(sections)
