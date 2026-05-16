"""Render a TailoredResume to ATS-clean PDF via Typst.

Uses the `typst` Python package, which bundles the Typst binary. No system
dependency required.
"""

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
TEMPLATE_FILE = "ats_resume.typ"


def _build_data(t: TailoredResume, master: MasterResume) -> dict[str, Any]:
    contact = master.contact or {}
    parts = [contact.get(k) for k in ("email", "phone", "linkedin", "github", "portfolio")]
    contact_line = " | ".join([p for p in parts if p])

    experience = []
    for e in t.experience:
        end = e.end or "Present"
        experience.append(
            {
                "company": e.company,
                "title": e.title,
                "dates": f"{e.start} – {end}",
                "bullets": [_clean(b.text) for b in e.bullets],
            }
        )

    return {
        "name": master.name,
        "headline": t.headline or master.headline,
        "contact_line": contact_line,
        "summary": _clean(t.summary),
        "skills": list(t.skills),
        "experience": experience,
        "project_bullets": [_clean(b.text) for b in t.project_bullets],
        "education": [
            {"school": e.get("school", ""), "degree": e.get("degree", ""), "dates": e.get("dates", "")}
            for e in t.education
        ],
    }


def _clean(s: str) -> str:
    """Strip [ESTIMATE] markers from rendered text. Provenance still lives in JSON."""
    return s.replace("[ESTIMATE]", "").replace("  ", " ").strip()


def render_pdf(t: TailoredResume, master: MasterResume, out_dir: Path) -> dict[str, Path]:
    """Render resume.pdf + resume.typ + resume.json into out_dir.

    Returns a dict of artifact paths.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # Copy template into the working dir and write data.json next to it,
    # because Typst's `json()` reads relative to the source file.
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

    artifacts = {
        "resume.pdf": pdf_path,
        "resume.typ": template_dst,
        "resume.json": out_dir / "data.json",
    }
    return artifacts
