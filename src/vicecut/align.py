"""Whisper (large-v3-turbo, int8 CPU) transcription + difflib forced word alignment (<build> #4)."""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .pronunciation import int_to_spoken
from .script_parser import ParsedScript, ScriptSection


@dataclass
class HeardWord:
    word: str
    start_s: float
    end_s: float
    probability: float = 1.0


@dataclass
class AlignedWord:
    word_id: int
    section_index: int
    line_no: int
    text: str            # Exact script display text (never respelled!)
    heard_text: str      # What faster-whisper heard (or "<interpolated>")
    start_s: float
    end_s: float
    exact_match: bool = True


@dataclass
class MismatchRecord:
    section_index: int
    section_title: str
    script_phrase: str
    heard_phrase: str
    severity: str  # "benign" (currency/spelling) | "redo_section" (missing/wrong word)
    reason: str


@dataclass
class AlignmentResult:
    words: list[AlignedWord] = field(default_factory=list)
    mismatches: list[MismatchRecord] = field(default_factory=list)
    sections_needing_redo: list[int] = field(default_factory=list)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "words": [asdict(w) for w in self.words],
                    "mismatches": [asdict(m) for m in self.mismatches],
                    "sections_needing_redo": self.sections_needing_redo,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "AlignmentResult":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            words=[AlignedWord(**w) for w in raw.get("words", [])],
            mismatches=[MismatchRecord(**m) for m in raw.get("mismatches", [])],
            sections_needing_redo=list(raw.get("sections_needing_redo", [])),
        )


def _norm_token(tok: str) -> str:
    """Normalize punctuation & common currency/number surface forms for fair comparison."""
    s = tok.lower().strip().replace("’", "'")
    # Convert $80 -> 80 bucks equivalence key
    m_cur = re.fullmatch(r"\$(\d+)", s)
    if m_cur:
        return int_to_spoken(int(m_cur.group(1)))
    s = re.sub(r"[^a-z0-9']", "", s)
    if s.isdigit():
        return int_to_spoken(int(s))
    return s


def _is_benign_mismatch(script_tokens: list[str], heard_tokens: list[str]) -> tuple[bool, str]:
    """
    Distinguish benign STT formatting differences (e.g. '80 bucks' vs '$80', proper noun
    respellings like 'Leonida' vs 'Leonita') from genuine wrong/dropped words (<gotchas>).
    """
    s_join = " ".join(script_tokens).lower().strip()
    h_join = " ".join(heard_tokens).lower().strip()

    # Currency formatting: "$80" <-> "80 bucks" / "eighty dollars"
    if ("$" in s_join or "$" in h_join) and any(c.isdigit() for c in s_join + h_join):
        s_nums = re.findall(r"\d+", s_join)
        h_nums = re.findall(r"\d+", h_join)
        if s_nums and s_nums == h_nums:
            return True, "currency/number formatting difference"

    # Normalized spoken equality (e.g. "6" vs "six", "GTA 6" vs "GTA VI")
    s_norm = re.sub(r"\bvi\b", "six", " ".join(_norm_token(t) for t in script_tokens))
    h_norm = re.sub(r"\bvi\b", "six", " ".join(_norm_token(t) for t in heard_tokens))
    s_norm = re.sub(r"\b(bucks|dollars)\b", "", s_norm).strip()
    h_norm = re.sub(r"\b(bucks|dollars)\b", "", h_norm).strip()
    if s_norm == h_norm and s_norm:
        return True, "equivalent number/unit wording"

    # Single proper-noun spelling variation (high phonetic/string similarity >= 0.72)
    if len(script_tokens) == 1 and len(heard_tokens) == 1:
        is_proper = script_tokens[0][:1].isupper()
        sim = difflib.SequenceMatcher(None, s_norm, h_norm).ratio()
        if sim >= 0.78 or (is_proper and sim >= 0.68):
            return True, f"proper-noun spelling variant (sim={sim:.2f})"

    return False, "different or missing spoken word"


def align_section_words(
    section: ScriptSection,
    heard_words: list[HeardWord],
    start_word_id: int = 0,
    time_offset_s: float = 0.0,
) -> tuple[list[AlignedWord], list[MismatchRecord], bool]:
    """
    Match faster-whisper words back to exact ground-truth script words via difflib.
    Always takes timestamps from the matched audio (NEVER from TTS service timestamps).
    """
    script_entries: list[tuple[int, str]] = []
    for ln in section.lines:
        for w in ln.words:
            script_entries.append((ln.line_no, w))

    if not script_entries:
        return [], [], False

    script_norm = [_norm_token(w) for _, w in script_entries]
    heard_norm = [_norm_token(hw.word) for hw in heard_words]

    matcher = difflib.SequenceMatcher(a=script_norm, b=heard_norm, autojunk=False)
    aligned: list[AlignedWord | None] = [None] * len(script_entries)
    mismatches: list[MismatchRecord] = []
    needs_redo = False

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                si = i1 + offset
                hi = j1 + offset
                line_no, raw_w = script_entries[si]
                hw = heard_words[hi]
                aligned[si] = AlignedWord(
                    word_id=start_word_id + si,
                    section_index=section.index,
                    line_no=line_no,
                    text=raw_w,
                    heard_text=hw.word,
                    start_s=round(time_offset_s + hw.start_s, 4),
                    end_s=round(time_offset_s + hw.end_s, 4),
                    exact_match=True,
                )
        elif tag == "replace":
            s_slice = [script_entries[k][1] for k in range(i1, i2)]
            h_slice = [heard_words[k].word for k in range(j1, j2)]
            benign, reason = _is_benign_mismatch(s_slice, h_slice)
            severity = "benign" if benign else "redo_section"
            if not benign:
                needs_redo = True
            mismatches.append(
                MismatchRecord(
                    section_index=section.index,
                    section_title=section.title,
                    script_phrase=" ".join(s_slice),
                    heard_phrase=" ".join(h_slice),
                    severity=severity,
                    reason=reason,
                )
            )
            # Distribute heard span evenly across script tokens in this replace block
            span_start = heard_words[j1].start_s
            span_end = heard_words[j2 - 1].end_s
            count = i2 - i1
            step = max(0.04, (span_end - span_start) / max(count, 1))
            for idx_off, si in enumerate(range(i1, i2)):
                line_no, raw_w = script_entries[si]
                t0 = time_offset_s + span_start + idx_off * step
                t1 = time_offset_s + min(span_end, span_start + (idx_off + 1) * step)
                aligned[si] = AlignedWord(
                    word_id=start_word_id + si,
                    section_index=section.index,
                    line_no=line_no,
                    text=raw_w,
                    heard_text=" ".join(h_slice),
                    start_s=round(t0, 4),
                    end_s=round(max(t0 + 0.03, t1), 4),
                    exact_match=benign,
                )
        elif tag == "delete":
            s_slice = [script_entries[k][1] for k in range(i1, i2)]
            needs_redo = True
            mismatches.append(
                MismatchRecord(
                    section_index=section.index,
                    section_title=section.title,
                    script_phrase=" ".join(s_slice),
                    heard_phrase="<missing>",
                    severity="redo_section",
                    reason="word(s) omitted in recording",
                )
            )
        elif tag == "insert":
            h_slice = [heard_words[k].word for k in range(j1, j2)]
            # Ignore tiny filler artifacts like single-char noise, flag genuine extra words
            if any(len(re.sub(r"[^a-zA-Z0-9]", "", t)) >= 2 for t in h_slice):
                needs_redo = True
                mismatches.append(
                    MismatchRecord(
                        section_index=section.index,
                        section_title=section.title,
                        script_phrase="<none>",
                        heard_phrase=" ".join(h_slice),
                        severity="redo_section",
                        reason="extra spoken word(s) not in script",
                    )
                )

    # Interpolate any deleted/unassigned script words cleanly between neighbours
    total_dur = heard_words[-1].end_s if heard_words else len(script_entries) * 0.285
    for idx in range(len(aligned)):
        if aligned[idx] is not None:
            continue
        prev_end = time_offset_s
        for p in range(idx - 1, -1, -1):
            if aligned[p] is not None:
                prev_end = aligned[p].end_s  # type: ignore[union-attr]
                break
        next_start = time_offset_s + total_dur
        gap_len = 1
        for nxt in range(idx + 1, len(aligned)):
            if aligned[nxt] is not None:
                next_start = aligned[nxt].start_s  # type: ignore[union-attr]
                gap_len = nxt - idx
                break
        step = max(0.05, (next_start - prev_end) / max(gap_len, 1))
        line_no, raw_w = script_entries[idx]
        aligned[idx] = AlignedWord(
            word_id=start_word_id + idx,
            section_index=section.index,
            line_no=line_no,
            text=raw_w,
            heard_text="<interpolated>",
            start_s=round(prev_end, 4),
            end_s=round(prev_end + step, 4),
            exact_match=False,
        )

    final_words = [w for w in aligned if w is not None]
    return final_words, mismatches, needs_redo


def transcribe_with_faster_whisper(
    audio_path: Path,
    model_name: str = "large-v3-turbo",
    compute_type: str = "int8",
) -> list[HeardWord]:
    """Transcribe a section take using faster-whisper large-v3-turbo int8 on CPU."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper not installed. Run `pip install faster-whisper`."
        ) from exc

    model = WhisperModel(model_name, device="cpu", compute_type=compute_type)
    segments, _ = model.transcribe(str(audio_path), word_timestamps=True, vad_filter=False)
    heard: list[HeardWord] = []
    for seg in segments:
        for w in seg.words or []:
            token = (w.word or "").strip()
            if token:
                heard.append(
                    HeardWord(
                        word=token,
                        start_s=float(w.start),
                        end_s=float(w.end),
                        probability=float(getattr(w, "probability", 1.0)),
                    )
                )
    return heard


def synthesize_synthetic_alignment(script: ParsedScript, wpm: float = 210.0) -> AlignmentResult:
    """
    Generate realistic word-level timestamps directly from a script at `wpm`
    (with natural 0.14s sentence pauses and 0.30s section transitions) for dry-run
    timeline compilation and automated testing without waiting on Whisper.
    """
    sec_per_word = 60.0 / wpm
    words: list[AlignedWord] = []
    wid = 0
    cursor = 0.18
    for sec in script.sections:
        for ln in sec.lines:
            for w in ln.words:
                dur = max(0.11, sec_per_word * (0.65 + 0.08 * min(len(w), 10)))
                words.append(
                    AlignedWord(
                        word_id=wid,
                        section_index=sec.index,
                        line_no=ln.line_no,
                        text=w,
                        heard_text=w,
                        start_s=round(cursor, 4),
                        end_s=round(cursor + dur * 0.88, 4),
                        exact_match=True,
                    )
                )
                wid += 1
                cursor += dur
            cursor += 0.16  # crisp natural breath between sentences
        cursor += 0.28  # tight section pause (< 0.30s)
    return AlignmentResult(words=words, mismatches=[], sections_needing_redo=[])
