"""Master resume parser.

Convention for `data/input/master_resume.md`:

    ---
    name: Jane Doe
    headline: AI/ML Engineer
    contact:
      email: jane@example.com
      phone: "+1-555-0100"
      linkedin: https://linkedin.com/in/janedoe
    skills: [Python, PyTorch, LangGraph, RAG, Postgres, AWS]
    ---

    ## Summary
    Two sentences about you.

    ## Experience

    ### Acme Corp | Senior AI Engineer | 2022-08 - Present | Remote
    - Bullet 1
    - Bullet 2

    ### Foo Inc | ML Engineer | 2020-01 - 2022-07 | NYC
    - Bullet 1

    ## Education

    ### Stanford University | B.S. Computer Science | 2016-2020

    ## Certifications
    - AWS Solutions Architect
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from config_loader import PROJECT_ROOT
from orchestrator.state import MasterResume, WorkExperience

MASTER_PATH = PROJECT_ROOT / "data" / "input" / "master_resume.md"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_EXPERIENCE_HEADER_RE = re.compile(
    r"###\s+(?P<company>[^|]+?)\s*\|\s*(?P<title>[^|]+?)\s*\|\s*"
    r"(?P<start>[^\s|]+)\s*-\s*(?P<end>[^\s|]+?)(?:\s*\|\s*(?P<location>[^\n]+))?$",
    re.MULTILINE,
)


def _section(body: str, heading: str) -> str:
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
    m = pattern.search(body)
    return m.group(1).strip() if m else ""


def _bullets(text: str) -> list[str]:
    return [line.lstrip("-* ").strip() for line in text.splitlines() if line.lstrip().startswith(("-", "*"))]


def parse_master_resume(path: Path | None = None) -> MasterResume:
    p = path or MASTER_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"Master resume not found at {p}. See data/input/master_resume.example.md"
        )
    raw = p.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ValueError(f"{p.name}: missing YAML frontmatter (--- ... ---)")
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2)

    summary = _section(body, "Summary")

    experience: list[WorkExperience] = []
    exp_section = _section(body, "Experience")
    # Split exp_section into chunks per `### ...` header.
    for match in _EXPERIENCE_HEADER_RE.finditer(exp_section):
        start_idx = match.end()
        # Body of this experience entry runs until next ### or EOF.
        next_match = _EXPERIENCE_HEADER_RE.search(exp_section, start_idx)
        end_idx = next_match.start() if next_match else len(exp_section)
        chunk = exp_section[start_idx:end_idx]
        end_raw = match.group("end").strip()
        experience.append(
            WorkExperience(
                company=match.group("company").strip(),
                title=match.group("title").strip(),
                start=match.group("start").strip(),
                end=None if end_raw.lower() in {"present", "current", "now"} else end_raw,
                location=(match.group("location") or "").strip() or None,
                base_bullets=_bullets(chunk),
            )
        )

    education_raw = _section(body, "Education")
    education: list[dict[str, str]] = []
    for line in education_raw.splitlines():
        line = line.strip()
        if not line.startswith("###"):
            continue
        parts = [p.strip() for p in line.lstrip("# ").split("|")]
        if len(parts) >= 3:
            education.append({"school": parts[0], "degree": parts[1], "dates": parts[2]})

    certs_raw = _section(body, "Certifications")
    certifications = _bullets(certs_raw)

    return MasterResume(
        name=meta.get("name", ""),
        headline=meta.get("headline", ""),
        contact=meta.get("contact", {}) or {},
        summary=summary,
        skills=list(meta.get("skills") or []),
        experience=experience,
        education=education,
        certifications=certifications,
    )
