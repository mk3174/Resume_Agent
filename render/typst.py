"""Render a TailoredResume to PDF using the master resume layout (font, section order, headings)."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import typst as typst_lib

from orchestrator.state import MasterResume, TailoredResume

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_FILE = "master_resume.typ"

_DEFAULT_LAYOUT = {
    "font": "Helvetica",
    "font_size": 10.5,
    "name_size": 18,
    "headline_size": 11,
    "contact_size": 9,
    "section_heading_size": 11,
    "skills_in_summary": True,
}


def _layout(master: MasterResume) -> dict[str, Any]:
    merged = dict(_DEFAULT_LAYOUT)
    merged.update(master.layout or {})
    return merged


def _contact_line(master: MasterResume) -> str:
    contact = master.contact or {}
    parts = [
        contact.get("email"),
        contact.get("phone"),
        contact.get("location"),
        contact.get("linkedin"),
        contact.get("github"),
        contact.get("portfolio"),
    ]
    return " | ".join(p for p in parts if p)


def _clean(s: str) -> str:
    return s.replace("[ESTIMATE]", "").replace("  ", " ").strip()


def _is_projects_section(heading: str) -> bool:
    key = heading.strip().lower()
    return "project" in key and key != "experience"


def _project_id_from_ref(ref: str) -> str:
    if ref.startswith("project:"):
        return ref.split(":", 1)[1]
    return ""


def _project_library_titles() -> dict[str, str]:
    from retrieval.project_library import load_projects_from_disk

    return {p.id: p.title for p in load_projects_from_disk()}


def _project_title_for_id(pid: str, outline: list[dict[str, Any]], lib: dict[str, str]) -> str:
    lib_title = lib.get(pid, "")
    if lib_title:
        lib_key = lib_title.lower()
        for entry in outline:
            entry_title = (entry.get("title") or "").lower()
            if entry_title == lib_key or lib_key in entry_title or entry_title in lib_key:
                return entry["title"]
        return lib_title
    if outline:
        return outline[0]["title"]
    return pid.replace("_", " ").title()


def _project_base_bullets_for_title(title: str, outline: list[dict[str, Any]]) -> list[str]:
    title_key = title.lower()
    for entry in outline:
        entry_title = (entry.get("title") or "").lower()
        if entry_title == title_key or title_key in entry_title or entry_title in title_key:
            return list(entry.get("base_bullets") or [])
    return []


def _project_entries(section: str, t: TailoredResume, master: MasterResume) -> list[dict[str, Any]]:
    """Group tailored project bullets under locked ### subheadings from the master resume."""
    from config_loader import load_settings

    lib = _project_library_titles()
    outline = (master.projects_sections or {}).get(section, [])
    min_proj = int(load_settings()["tailor"].get("min_project_bullets", 2))
    max_proj = int(load_settings()["tailor"].get("max_project_bullets", 3))

    by_id: dict[str, list[str]] = {}
    for b in t.project_bullets:
        pid = _project_id_from_ref(b.source_ref)
        if not pid:
            continue
        by_id.setdefault(pid, []).append(_clean(b.text))

    if not by_id and not outline:
        return []

    ids = list(t.selected_projects) if t.selected_projects else list(by_id.keys())
    seen: set[str] = set()
    ordered_ids: list[str] = []
    for pid in ids:
        if pid in by_id and pid not in seen:
            ordered_ids.append(pid)
            seen.add(pid)
    for pid in by_id:
        if pid not in seen:
            ordered_ids.append(pid)

    entries: list[dict[str, Any]] = []
    for pid in ordered_ids:
        title = _project_title_for_id(pid, outline, lib)
        bullets = list(by_id.get(pid) or [])
        seen_text = {b.lower() for b in bullets}
        for base in _project_base_bullets_for_title(title, outline):
            if len(bullets) >= min_proj:
                break
            cleaned = _clean(base)
            if cleaned and cleaned.lower() not in seen_text:
                bullets.append(cleaned)
                seen_text.add(cleaned.lower())
        bullets = bullets[:max_proj]
        if not bullets:
            continue
        entries.append({"title": title, "bullets": bullets})
    return entries


def _experience_blocks(t: TailoredResume, master: MasterResume) -> list[dict[str, Any]]:
    loc_by_key = {(e.company.lower(), e.start): e.location for e in master.experience}
    blocks: list[dict[str, Any]] = []
    for e in t.experience:
        end = e.end or "Present"
        blocks.append(
            {
                "company": e.company,
                "title": e.title,
                "dates": f"{e.start} - {end}",
                "location": e.location or loc_by_key.get((e.company.lower(), e.start)) or "",
                "bullets": [_clean(b.text) for b in e.bullets],
            }
        )
    return blocks


def _section_payload(section: str, t: TailoredResume, master: MasterResume, layout: dict[str, Any]) -> dict[str, Any] | None:
    key = section.strip().lower()
    if key == "summary":
        summary = _clean(t.summary)
        skills_line = ""
        if layout.get("skills_in_summary") and t.skills:
            skills_line = " · ".join(t.skills)
        if not summary and not skills_line:
            return None
        return {"type": "summary", "title": section, "summary": summary, "skills_line": skills_line}
    if key == "skills":
        if not t.skills:
            return None
        return {"type": "skills", "title": section, "skills": list(t.skills)}
    if key == "experience":
        exp = _experience_blocks(t, master)
        if not exp:
            return None
        return {"type": "experience", "title": section, "entries": exp}
    if key == "education":
        edu = t.education or master.education
        if not edu:
            return None
        return {
            "type": "education",
            "title": section,
            "entries": [
                {
                    "school": e.get("school", ""),
                    "degree": e.get("degree", ""),
                    "dates": e.get("dates", ""),
                }
                for e in edu
            ],
        }
    if key == "certifications":
        certs = t.certifications or master.certifications
        if not certs:
            return None
        return {"type": "bullets", "title": section, "bullets": [_clean(c) for c in certs]}
    if key == "publications":
        pubs = t.publications or master.publications
        if not pubs:
            return None
        return {"type": "bullets", "title": section, "bullets": [_clean(p) for p in pubs]}
    if _is_projects_section(section):
        entries = _project_entries(section, t, master)
        if not entries:
            return None
        return {"type": "projects", "title": section, "entries": entries}
    return None


def _build_data(t: TailoredResume, master: MasterResume) -> dict[str, Any]:
    layout = _layout(master)
    sections: list[dict[str, Any]] = []
    for heading in master.section_order:
        payload = _section_payload(heading, t, master, layout)
        if payload:
            sections.append(payload)

    return {
        "layout": layout,
        "name": master.name,
        "headline": master.headline,
        "contact_line": _contact_line(master),
        "sections": sections,
    }


def render_pdf(t: TailoredResume, master: MasterResume, out_dir: Path) -> dict[str, Path]:
    """Render resume.pdf using master resume typography and section order."""
    out_dir.mkdir(parents=True, exist_ok=True)

    template_dst = out_dir / "resume.typ"
    shutil.copyfile(TEMPLATE_DIR / TEMPLATE_FILE, template_dst)

    data = _build_data(t, master)
    (out_dir / "data.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    pdf_path = out_dir / "resume.pdf"
    try:
        typst_lib.compile(str(template_dst), output=str(pdf_path), format="pdf")
    except Exception as e:  # noqa: BLE001
        log.error("Typst compile failed: %s", e)
        raise

    return {
        "resume.pdf": pdf_path,
        "resume.typ": template_dst,
        "resume.json": out_dir / "data.json",
    }
