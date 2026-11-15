"""Annotated one-sentence-per-line script parser (<build> #3)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

TAG_RE = re.compile(r"\[(?P<key>[A-Z_]+)(?::(?P<val>[^\]]+))?\]")
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*|\$\d[\d,]*(?:\.\d+)?[BMK]?")


@dataclass
class InlineCardCue:
    card_type: str       # photo | headline | character | quote | number
    asset_or_value: str  # asset id / number / quote text
    meta: str = ""       # pill label / attribution / subline / side


@dataclass
class ScriptLine:
    """One spoken sentence with semantic production annotations."""

    line_no: int
    section_index: int
    section_title: str
    raw_text: str
    text: str  # Clean spoken sentence without tags
    words: list[str]
    tags: dict[str, list[str]] = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        return len(self.words)

    def has_tag(self, key: str) -> bool:
        return key in self.tags

    def tag_values(self, key: str) -> list[str]:
        return self.tags.get(key, [])

    @property
    def card_cues(self) -> list[InlineCardCue]:
        cues: list[InlineCardCue] = []
        for raw in self.tags.get("CARD", []):
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) >= 2:
                cues.append(
                    InlineCardCue(
                        card_type=parts[0].lower(),
                        asset_or_value=parts[1],
                        meta="|".join(parts[2:]) if len(parts) > 2 else "",
                    )
                )
        return cues


@dataclass
class ScriptSection:
    index: int
    title: str
    kind: str  # "intro" | "body" | "close"
    chapter_word: str
    lines: list[ScriptLine] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return sum(line.word_count for line in self.lines)

    @property
    def spoken_text(self) -> str:
        return " ".join(line.text for line in self.lines if line.text)


@dataclass
class ParsedScript:
    title: str
    sections: list[ScriptSection] = field(default_factory=list)

    @property
    def all_lines(self) -> list[ScriptLine]:
        return [line for sec in self.sections for line in sec.lines]

    @property
    def total_words(self) -> int:
        return sum(sec.word_count for sec in self.sections)

    @property
    def intro_section(self) -> ScriptSection | None:
        for sec in self.sections:
            if sec.kind == "intro":
                return sec
        return self.sections[0] if self.sections else None

    @property
    def close_section(self) -> ScriptSection | None:
        for sec in reversed(self.sections):
            if sec.kind == "close":
                return sec
        return self.sections[-1] if len(self.sections) > 1 else None

    @property
    def body_sections(self) -> list[ScriptSection]:
        return [sec for sec in self.sections if sec.kind == "body"]


def _classify_section(title: str, idx: int) -> str:
    low = title.lower().strip()
    if low.startswith("intro") or idx == 0 and "close" not in low:
        return "intro"
    if low.startswith(("close", "outro", "ending", "wrap")):
        return "close"
    return "body"


def _default_chapter_word(title: str) -> str:
    # Strip leading "Section 1:" prefix
    cleaned = re.sub(r"^section\s*\d+\s*[:\-–]\s*", "", title, flags=re.IGNORECASE).strip()
    words = [w for w in re.split(r"\s+", cleaned) if w]
    return " ".join(words[:3]).upper() if words else title.upper()


def parse_script_text(raw: str) -> ParsedScript:
    """Parse annotated markdown script into structured sections and sentences."""
    title = ""
    sections: list[ScriptSection] = []
    current: ScriptSection | None = None

    for lineno, raw_line in enumerate(raw.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("<!--") or stripped.startswith("//"):
            continue
        if stripped.startswith("# ") and not title:
            title = stripped[2:].strip()
            continue
        if stripped.startswith("## "):
            heading = stripped[3:].strip()
            # Optional inline [CHAPTER:WORD] on heading line
            chap_override = ""
            m_chap = re.search(r"\[CHAPTER:([^\]]+)\]", heading)
            if m_chap:
                chap_override = m_chap.group(1).strip().upper()
                heading = TAG_RE.sub("", heading).strip()
            sec_idx = len(sections)
            kind = _classify_section(heading, sec_idx)
            current = ScriptSection(
                index=sec_idx,
                title=heading,
                kind=kind,
                chapter_word=chap_override or _default_chapter_word(heading),
            )
            sections.append(current)
            continue

        if current is None:
            current = ScriptSection(index=0, title="Intro", kind="intro", chapter_word="INTRO")
            sections.append(current)

        tags: dict[str, list[str]] = {}
        for m in TAG_RE.finditer(stripped):
            key = m.group("key").upper()
            val = (m.group("val") or "true").strip()
            tags.setdefault(key, []).append(val)

        clean_text = TAG_RE.sub("", stripped)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()
        if not clean_text:
            continue

        words = WORD_RE.findall(clean_text)
        current.lines.append(
            ScriptLine(
                line_no=lineno,
                section_index=current.index,
                section_title=current.title,
                raw_text=stripped,
                text=clean_text,
                words=words,
                tags=tags,
            )
        )

    return ParsedScript(title=title, sections=sections)


def parse_script_file(path: Path) -> ParsedScript:
    return parse_script_text(path.read_text(encoding="utf-8"))
