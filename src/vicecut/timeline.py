"""Word-keyed timeline solver: hard cuts, bright/dark alternation, card timing, anti-flicker snapping, collision & bare-footage guards (<build> #6)."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .align import AlignedWord, AlignmentResult
from .config import (
    BLUR_RADIUS_PX,
    CARD_DARKEN_ALPHA,
    CARD_FADE_S,
    CARD_HOLDS_S,
    CARD_LEAD_IN_S,
    CARD_MIN_HOLD_S,
    ProjectBrief,
    VIDEO_FPS,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
)
from .media import MediaManifest, ShotClip
from .research import CONFIDENCE_PILL_LABELS, ResearchLedger
from .script_parser import ParsedScript, ScriptLine


def sec_to_frame(t: float, fps: int = VIDEO_FPS) -> int:
    return max(0, int(round(t * fps)))


def frame_to_sec(frame: int, fps: int = VIDEO_FPS) -> float:
    return round(frame / fps, 4)


def caption_font_size(word_count: int) -> int:
    """Exact caption font sizes from <build> #6: 116 (1-2w), 106 (3w), 92 (4-5w), 80 (6w)."""
    if word_count <= 2:
        return 116
    if word_count == 3:
        return 106
    if word_count <= 5:
        return 92
    return 80


@dataclass
class ShotEvent:
    shot_id: str
    source_id: str
    path: str
    start_s: float
    end_s: float
    start_frame: int
    end_frame: int
    is_still: bool = False
    luma_mean: float = 0.5
    crop_filter: str = ""
    face_zones: list[str] = field(default_factory=list)
    pinned_word_id: int | None = None
    still_push_in: bool = False  # 3% push-in over 2.5s for official stills

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    @property
    def is_bright(self) -> bool:
        return self.luma_mean >= 0.45


@dataclass
class CardEvent:
    id: str
    card_type: str       # photo | headline | character | quote | number | chapter
    anchor_word_id: int
    start_s: float
    end_s: float
    start_frame: int
    end_frame: int
    fade_s: float = CARD_FADE_S
    blur_px: int = BLUR_RADIUS_PX
    darken_alpha: float = 0.45
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


@dataclass
class CaptionWordCue:
    word_id: int
    text: str
    reveal_s: float


@dataclass
class CaptionEvent:
    id: str
    start_s: float
    end_s: float
    start_frame: int
    end_frame: int
    font_size_px: int
    y_pct: float = 80.0
    words: list[CaptionWordCue] = field(default_factory=list)


@dataclass
class SourcePillEvent:
    id: str
    label: str
    color_variant: str   # "pink" | "cream" | "peach"
    slot_index: int      # 0, 1, 2 stacked from top-left (66px, 52px, +76px)
    tilt_deg: float
    anchor_word_id: int
    start_s: float
    end_s: float
    start_frame: int
    end_frame: int


@dataclass
class BlurInterval:
    start_frame: int
    end_frame: int
    start_s: float
    end_s: float
    blur_px: int
    darken_alpha: float
    card_id: str


@dataclass
class Timeline:
    project_slug: str
    fps: int
    width: int
    height: int
    duration_s: float
    total_frames: int
    theme: dict[str, Any]
    shots: list[ShotEvent] = field(default_factory=list)
    cards: list[CardEvent] = field(default_factory=list)
    captions: list[CaptionEvent] = field(default_factory=list)
    pills: list[SourcePillEvent] = field(default_factory=list)
    blur_intervals: list[BlurInterval] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_slug": self.project_slug,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "duration_s": round(self.duration_s, 4),
            "total_frames": self.total_frames,
            "theme": self.theme,
            "shots": [asdict(s) for s in self.shots],
            "cards": [asdict(c) for c in self.cards],
            "captions": [asdict(c) for c in self.captions],
            "pills": [asdict(p) for p in self.pills],
            "blur_intervals": [asdict(b) for b in self.blur_intervals],
            "warnings": self.warnings,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Timeline":
        raw = json.loads(path.read_text(encoding="utf-8"))
        captions = []
        for c in raw.get("captions", []):
            words = [CaptionWordCue(**w) for w in c.get("words", [])]
            captions.append(CaptionEvent(**{k: v for k, v in c.items() if k != "words"}, words=words))
        return cls(
            project_slug=raw["project_slug"],
            fps=raw["fps"],
            width=raw["width"],
            height=raw["height"],
            duration_s=raw["duration_s"],
            total_frames=raw["total_frames"],
            theme=raw["theme"],
            shots=[ShotEvent(**s) for s in raw.get("shots", [])],
            cards=[CardEvent(**c) for c in raw.get("cards", [])],
            captions=captions,
            pills=[SourcePillEvent(**p) for p in raw.get("pills", [])],
            blur_intervals=[BlurInterval(**b) for b in raw.get("blur_intervals", [])],
            warnings=list(raw.get("warnings", [])),
        )


def _line_words(line_no: int, words_by_line: dict[int, list[AlignedWord]]) -> list[AlignedWord]:
    return words_by_line.get(line_no, [])


def _pick_key_phrase_words(line_words: list[AlignedWord], explicit_phrase: str | None = None) -> list[AlignedWord]:
    """Select 1-6 punchy key words for an on-screen caption (never subtitling every word)."""
    if not line_words:
        return []
    if explicit_phrase:
        raw_tokens = [t.strip() for t in explicit_phrase.split() if t.strip()][:6]
        target = [t.lower().strip(".,!?;:\"'") for t in raw_tokens]
        if target:
            n = len(target)
            # 1) Exact contiguous substring match in line words
            for i in range(len(line_words) - n + 1):
                window = [w.text.lower().strip(".,!?;:\"'") for w in line_words[i : i + n]]
                if window == target:
                    return line_words[i : i + n]
            # 2) Subsequence match (e.g. "WAIT FOR PROOF" matching "wait for real proof")
            matched: list[AlignedWord] = []
            cursor = 0
            for raw_tok, norm_tok in zip(raw_tokens, target):
                found = None
                for j in range(cursor, len(line_words)):
                    if line_words[j].text.lower().strip(".,!?;:\"'") == norm_tok:
                        found = line_words[j]
                        cursor = j + 1
                        break
                if found is not None:
                    matched.append(replace(found, text=raw_tok))
            if len(matched) == len(raw_tokens):
                return matched
            # 3) Explicit editorial punch phrase synced across the middle of the line
            mid = max(0, len(line_words) // 2 - n // 2)
            span = line_words[mid : mid + n]
            while len(span) < n:
                span.append(line_words[-1])
            return [replace(base, text=raw_tok) for raw_tok, base in zip(raw_tokens, span)]

    # Automatic key-phrase picker: grab 2-4 content words near the end/climax of the sentence
    stop = {"and", "so", "but", "the", "a", "an", "to", "of", "in", "on", "for", "that", "this", "is", "are", "it", "you", "your"}
    best_idx = max(0, len(line_words) - 4)
    for i in range(len(line_words) - 1, -1, -1):
        if line_words[i].text.lower().strip(".,!?;:") not in stop:
            best_idx = max(0, i - 2)
            break
    chosen = line_words[best_idx : best_idx + 4]
    return chosen[:6]


def compile_timeline(
    script: ParsedScript,
    alignment: AlignmentResult,
    manifest: MediaManifest,
    brief: ProjectBrief,
    ledger: ResearchLedger | None = None,
) -> Timeline:
    """
    Build a complete word-keyed Timeline strictly obeying every rule in <direction>,
    <build> #6, and <gotchas>.
    """
    warnings: list[str] = []
    words = alignment.words
    if not words:
        raise ValueError("AlignmentResult contains no words; run alignment first.")

    duration_s = round(words[-1].end_s + 0.45, 4)
    total_frames = sec_to_frame(duration_s)

    words_by_line: dict[int, list[AlignedWord]] = {}
    for w in words:
        words_by_line.setdefault(w.line_no, []).append(w)

    # -----------------------------------------------------------------------
    # 1. Build Chapter Words (one per body section, 1-3 words, 2.0s hold) &
    #    Inline Proof Cards (photo, headline, character, quote, number)
    # -----------------------------------------------------------------------
    pending_cards: list[tuple[float, str, AlignedWord, float, dict[str, Any]]] = []
    tilt_toggle = 0

    # Queue chapter word at start of each body section (one per section)
    for sec in script.body_sections:
        if not sec.lines:
            continue
        sec_words = _line_words(sec.lines[0].line_no, words_by_line)
        if not sec_words:
            continue
        pending_cards.append(
            (
                sec_words[0].start_s,
                "chapter",
                sec_words[0],
                CARD_HOLDS_S["chapter"],
                {"text": sec.chapter_word, "section_title": sec.title},
            )
        )

    # Queue explicit [CARD:...] cues from script lines
    for ln in script.all_lines:
        lwords = _line_words(ln.line_no, words_by_line)
        if not lwords:
            continue
        anchor_w = lwords[min(1, len(lwords) - 1)]
        for cue in ln.card_cues:
            ctype = cue.card_type
            if ctype == "photo":
                photo = manifest.photo_by_id(cue.asset_or_value)
                payload = {
                    "asset_id": cue.asset_or_value,
                    "image_path": photo.image_path if photo else "",
                    "label": cue.meta or (photo.label if photo else cue.asset_or_value),
                    "width_px": photo.width_px if photo else 1280,
                    "height_px": photo.height_px if photo else 720,
                    "is_portrait": (photo.height_px > photo.width_px) if photo else False,
                    "tilt_deg": -1.4 if (tilt_toggle % 2 == 0) else 1.4,
                    "is_mockup": photo.is_mockup if photo else False,
                }
                tilt_toggle += 1
                pending_cards.append((anchor_w.start_s, "photo", anchor_w, CARD_HOLDS_S["photo"], payload))
            elif ctype == "headline":
                hl = manifest.headline_by_id(cue.asset_or_value)
                payload = {
                    "asset_id": cue.asset_or_value,
                    "outlet": (hl.outlet if hl else cue.meta) or "SOURCE",
                    "headline_text": hl.headline_text if hl else cue.asset_or_value,
                    "date": hl.date if hl else "",
                    "image_path": hl.image_path if (hl and not hl.use_text_fallback) else "",
                    "use_text_fallback": hl.use_text_fallback if hl else True,
                    "tilt_deg": 1.1 if (tilt_toggle % 2 == 0) else -1.1,
                    "is_mockup": hl.is_mockup if hl else False,
                }
                tilt_toggle += 1
                pending_cards.append((anchor_w.start_s, "headline", anchor_w, CARD_HOLDS_S["headline"], payload))
            elif ctype == "character":
                photo = manifest.photo_by_id(cue.asset_or_value)
                meta_parts = [p.strip() for p in cue.meta.split(";")] if cue.meta else []
                name = meta_parts[0] if len(meta_parts) >= 1 else (photo.label if photo else cue.asset_or_value)
                line1 = meta_parts[1] if len(meta_parts) >= 2 else ""
                line2 = meta_parts[2] if len(meta_parts) >= 3 else ""
                side = meta_parts[3].lower() if len(meta_parts) >= 4 else "right"
                payload = {
                    "asset_id": cue.asset_or_value,
                    "image_path": photo.image_path if photo else "",
                    "name": name,
                    "line1": line1,
                    "line2": line2,
                    "side": side,  # "right" (x=64.5%) or "left" (x=10.5%)
                }
                pending_cards.append((anchor_w.start_s, "character", anchor_w, CARD_HOLDS_S["character"], payload))
            elif ctype == "quote":
                # Typed word by word with the voice, holds until 0.9s after last word!
                last_w = lwords[-1]
                quote_dur = (last_w.end_s - anchor_w.start_s) + CARD_HOLDS_S["quote_tail"] + CARD_LEAD_IN_S
                payload = {
                    "attribution": cue.meta or (ln.tag_values("SOURCE")[0] if ln.has_tag("SOURCE") else "OFFICIAL"),
                    "quote_text": cue.asset_or_value if cue.asset_or_value != "line" else ln.text,
                    "typed_words": [
                        {"word_id": w.word_id, "text": w.text, "reveal_s": w.start_s}
                        for w in lwords
                    ],
                }
                pending_cards.append((anchor_w.start_s, "quote", anchor_w, quote_dur, payload))
            elif ctype == "number":
                payload = {
                    "figure": cue.asset_or_value,
                    "subline": cue.meta.upper(),
                }
                pending_cards.append((anchor_w.start_s, "number", anchor_w, CARD_HOLDS_S["number"], payload))
            elif ctype == "prop":
                # Animated white-background studio object prop (NO people — smooth 30fps float/swing/bounce!)
                photo = manifest.photo_by_id(cue.asset_or_value)
                meta_parts = [p.strip() for p in cue.meta.split(";")] if cue.meta else []
                label = meta_parts[0] if len(meta_parts) >= 1 and meta_parts[0] else (photo.label if photo else cue.asset_or_value)
                subline = meta_parts[1] if len(meta_parts) >= 2 else ""
                anim_cycle = ("float_zoom", "slide_swing", "bounce_pop")
                animation = (
                    meta_parts[2].lower()
                    if len(meta_parts) >= 3 and meta_parts[2].strip()
                    else anim_cycle[tilt_toggle % len(anim_cycle)]
                )
                tilt_toggle += 1
                payload = {
                    "asset_id": cue.asset_or_value,
                    "image_path": photo.image_path if photo else "",
                    "label": label.upper(),
                    "subline": subline.upper(),
                    "animation": animation,
                    "position": "right",
                }
                pending_cards.append((anchor_w.start_s, "prop", anchor_w, CARD_HOLDS_S["prop"], payload))

    # Sort ALL cards chronologically before resolving consecutive overlaps!
    pending_cards.sort(key=lambda item: (item[0], 0 if item[1] == "chapter" else 1))

    cards: list[CardEvent] = []
    card_idx = 0
    for _, card_type, anchor_w, hold_s, payload in pending_cards:
        start_s = max(0.0, anchor_w.start_s - CARD_LEAD_IN_S)
        hold_s = max(CARD_MIN_HOLD_S, hold_s)
        end_s = min(duration_s, start_s + hold_s)
        # Prevent overlapping consecutive cards cleanly
        if cards and start_s < cards[-1].end_s + 0.15:
            prev = cards[-1]
            if start_s - prev.start_s >= CARD_MIN_HOLD_S:
                prev.end_s = round(start_s - 0.06, 4)
                prev.end_frame = sec_to_frame(prev.end_s)
            else:
                start_s = prev.end_s + 0.08
                end_s = min(duration_s, start_s + hold_s)
        if end_s - start_s < CARD_MIN_HOLD_S:
            continue
        blur = 0 if card_type in {"character", "prop"} else BLUR_RADIUS_PX
        darken = CARD_DARKEN_ALPHA.get(card_type, 0.45)
        sf = sec_to_frame(start_s)
        ef = sec_to_frame(end_s)
        card_idx += 1
        cards.append(
            CardEvent(
                id=f"card_{card_idx:03d}",
                card_type=card_type,
                anchor_word_id=anchor_w.word_id,
                start_s=frame_to_sec(sf),
                end_s=frame_to_sec(ef),
                start_frame=sf,
                end_frame=ef,
                blur_px=blur,
                darken_alpha=darken,
                payload=payload,
            )
        )

    # -----------------------------------------------------------------------
    # 2. Build Source & Confidence Pills (first time each source is named in a section)
    # -----------------------------------------------------------------------
    pills: list[SourcePillEvent] = []
    pill_colors = ("pink", "cream", "peach")
    pill_tilts = (-2.2, 1.8, -1.5)
    seen_in_section: dict[int, set[str]] = {}
    pill_idx = 0

    for ln in script.all_lines:
        lwords = _line_words(ln.line_no, words_by_line)
        if not lwords:
            continue
        sec_seen = seen_in_section.setdefault(ln.section_index, set())
        labels_to_emit: list[str] = []

        for src in ln.tag_values("SOURCE"):
            norm = src.strip().upper()
            if norm and norm not in sec_seen:
                sec_seen.add(norm)
                labels_to_emit.append(norm)

        # Confidence pill if non-official fact
        if ledger is not None:
            for fid in ln.tag_values("FACT"):
                fact = ledger.fact_by_id(fid)
                if fact and fact.pill_label:
                    lbl = fact.pill_label.upper()
                    if lbl not in labels_to_emit:
                        labels_to_emit.append(lbl)
        for conf in ln.tag_values("CONFIDENCE"):
            lbl = conf.strip().upper()
            if lbl in {"REPORTED", "ESTIMATE", "BUSTED", "DISPUTED", "UNCONFIRMED", "MOCK-UP"} and lbl not in labels_to_emit:
                labels_to_emit.append(lbl)

        if not labels_to_emit:
            continue

        p_start = max(0.0, lwords[0].start_s - CARD_LEAD_IN_S)
        p_end = min(duration_s, max(p_start + 2.8, lwords[-1].end_s + 0.5))
        sf, ef = sec_to_frame(p_start), sec_to_frame(p_end)

        # Determine currently active stack slots so stacked pills never overlap
        active_slots = {
            p.slot_index for p in pills if not (p.end_frame <= sf or p.start_frame >= ef)
        }
        for lbl in labels_to_emit:
            slot = 0
            while slot in active_slots:
                slot += 1
            active_slots.add(slot)
            color = "peach" if lbl in {"REPORTED", "ESTIMATE", "BUSTED", "DISPUTED", "MOCK-UP"} else pill_colors[pill_idx % len(pill_colors)]
            tilt = pill_tilts[pill_idx % len(pill_tilts)]
            pill_idx += 1
            pills.append(
                SourcePillEvent(
                    id=f"pill_{pill_idx:03d}",
                    label=lbl,
                    color_variant=color,
                    slot_index=slot,
                    tilt_deg=tilt,
                    anchor_word_id=lwords[0].word_id,
                    start_s=frame_to_sec(sf),
                    end_s=frame_to_sec(ef),
                    start_frame=sf,
                    end_frame=ef,
                )
            )

    # -----------------------------------------------------------------------
    # 3. Build Hard-Cut Shot Track (1.7-3.7s, aim 2.3s, bright/dark alternating,
    #    mid-sentence cuts, zero repeated shots, cut-to-card anti-flicker snapping!)
    # -----------------------------------------------------------------------
    usable_pool = manifest.usable_shots()
    motion_pool = [s for s in usable_pool if not s.is_still]
    still_pool = [s for s in usable_pool if s.is_still]
    max_stills = int((duration_s * brief.media.max_stills_ratio) // 2.8)

    # Build map of pinned shot cues from script lines
    pinned_by_time: list[tuple[float, int, str]] = []
    for ln in script.all_lines:
        lwords = _line_words(ln.line_no, words_by_line)
        if not lwords:
            continue
        for sid in ln.tag_values("SHOT"):
            pinned_by_time.append((lwords[0].start_s, lwords[0].word_id, sid))

    # Build cut times targeting ~2.25s median (26-28 cuts/min), preferring mid-sentence words
    cut_times: list[float] = [0.0]
    card_starts = sorted(c.start_s for c in cards)
    t_cur = 0.0
    step_pattern = [2.1, 2.5, 1.9, 2.4, 2.2, 2.7, 2.0, 2.3]
    pat_i = 0

    while t_cur < duration_s - 1.7:
        desired = t_cur + step_pattern[pat_i % len(step_pattern)]
        pat_i += 1
        if duration_s - desired < 1.5:
            break

        # Snap cut cleanly onto a nearby card start (within 0.45s) to prevent blur flicker (<gotchas>)!
        snapped = None
        for cs in card_starts:
            if abs(cs - desired) <= 0.45 and (cs - t_cur) >= 1.45:
                snapped = cs
                break
        if snapped is None:
            # Prefer landing mid-sentence (on an interior word rather than a pause boundary)
            best_w = min(words, key=lambda w: abs(w.start_s - desired))
            if abs(best_w.start_s - desired) <= 0.35 and (best_w.start_s - t_cur) >= 1.65:
                snapped = best_w.start_s
            else:
                snapped = desired

        # Anti-flicker second pass: never leave a cut within (0, 0.25s) of any card start
        for cs in card_starts:
            if 0.01 < abs(cs - snapped) <= 0.25 and (cs - t_cur) >= 1.35:
                snapped = cs
                break

        snapped_f = sec_to_frame(snapped)
        snapped_q = frame_to_sec(snapped_f)
        if snapped_q > t_cur + 1.2 and snapped_q < duration_s - 0.8:
            cut_times.append(snapped_q)
            t_cur = snapped_q
        else:
            t_cur += 2.2

    cut_times.append(duration_s)

    # Assign UNIQUE shots alternating bright & dark, respecting pinned shots & face safety
    used_ids: set[str] = set()
    stills_used = 0
    last_bright: bool | None = None
    shots: list[ShotEvent] = []

    def _active_cards_for(t0: float, t1: float) -> list[CardEvent]:
        return [c for c in cards if not (c.end_s <= t0 or c.start_s >= t1)]

    def _pick_shot(t0: float, t1: float) -> tuple[ShotClip | None, int | None]:
        nonlocal stills_used
        # 1. Explicit pinned shot inside this window
        for pt, pwid, psid in pinned_by_time:
            if t0 - 0.15 <= pt < t1 and psid not in used_ids:
                sc = manifest.shot_by_id(psid)
                if sc and not sc.dropped:
                    return sc, pwid

        active_c = _active_cards_for(t0, t1)
        needs_center_clear = any(c.card_type in {"photo", "headline", "quote", "number", "prop"} for c in active_c)

        candidates = [s for s in motion_pool if s.id not in used_ids]
        if not candidates and stills_used < max_stills:
            candidates = [s for s in still_pool if s.id not in used_ids]

        if not candidates:
            return None, None

        def score(sc: ShotClip) -> tuple[int, int, float]:
            # Prefer shots that clear centered faces when a card is up, and alternate bright/dark!
            face_penalty = 1 if (needs_center_clear and "center" in sc.face_zones) else 0
            alt_penalty = 0 if (last_bright is None or sc.is_bright != last_bright) else 1
            dur_fit = abs(sc.duration_s - (t1 - t0))
            return (face_penalty, alt_penalty, dur_fit)

        best = min(candidates, key=score)
        if best.is_still:
            stills_used += 1
        return best, None

    for seg_i in range(len(cut_times) - 1):
        t0, t1 = cut_times[seg_i], cut_times[seg_i + 1]
        sf, ef = sec_to_frame(t0), sec_to_frame(t1)
        if ef <= sf:
            continue
        chosen, pwid = _pick_shot(t0, t1)
        if chosen is None:
            warnings.append(
                f"Shot pool exhausted at {t0:.1f}s-{t1:.1f}s (NEVER repeat shots! Add more official scenes)."
            )
            shots.append(
                ShotEvent(
                    shot_id=f"MISSING_SHOT_{seg_i+1:03d}",
                    source_id="MISSING",
                    path="",
                    start_s=frame_to_sec(sf),
                    end_s=frame_to_sec(ef),
                    start_frame=sf,
                    end_frame=ef,
                )
            )
            continue

        used_ids.add(chosen.id)
        last_bright = chosen.is_bright

        # Face-safe character card side flip (<gotchas> & <direction>)
        for c in _active_cards_for(t0, t1):
            if c.card_type == "character":
                cur_side = c.payload.get("side", "right")
                if cur_side in chosen.face_zones:
                    other = "left" if cur_side == "right" else "right"
                    if other not in chosen.face_zones:
                        c.payload["side"] = other
                if c.payload.get("side", "right") in chosen.face_zones:
                    warnings.append(f"Character card {c.id} may overlap face on shot {chosen.id}; swap shot.")
            elif c.card_type in {"photo", "headline", "quote", "number"} and "center" in chosen.face_zones:
                warnings.append(f"Centered card {c.id} sits over centered face on shot {chosen.id}; pin another shot.")

        shots.append(
            ShotEvent(
                shot_id=chosen.id,
                source_id=chosen.source_id,
                path=chosen.path,
                start_s=frame_to_sec(sf),
                end_s=frame_to_sec(ef),
                start_frame=sf,
                end_frame=ef,
                is_still=chosen.is_still,
                luma_mean=chosen.luma_mean,
                crop_filter=chosen.crop_filter,
                face_zones=list(chosen.face_zones),
                pinned_word_id=pwid,
                still_push_in=chosen.is_still,
            )
        )

    # -----------------------------------------------------------------------
    # 4. Build Key-Word Captions (1-6 key words, fade word-by-word, no card
    #    collision, gone at next cut or 0.6s after last word, bare-footage guard!)
    # -----------------------------------------------------------------------
    captions: list[CaptionEvent] = []
    cap_idx = 0

    def _centered_card_at(t0: float, t1: float) -> CardEvent | None:
        for c in cards:
            if c.card_type in {"photo", "headline", "quote", "number", "chapter", "prop"}:
                if not (c.end_s <= t0 or c.start_s >= t1):
                    return c
        return None

    def _next_cut_after(t: float) -> float:
        for ct in cut_times:
            if ct > t + 0.15:
                return ct
        return duration_s

    def _try_add_caption_words(picked: list[AlignedWord], force: bool = False) -> bool:
        nonlocal cap_idx
        if not picked:
            return False
        c_start = picked[0].start_s
        natural_end = min(duration_s, picked[-1].end_s + 0.6)
        cut_end = _next_cut_after(picked[-1].start_s)
        c_end = min(natural_end, cut_end)

        # Card-caption collision prevention (<gotchas>)
        conflict = _centered_card_at(c_start, c_end)
        y_pct = 80.0
        if conflict is not None:
            if conflict.start_s > c_start + 0.45:
                c_end = conflict.start_s - 0.04
            elif force and conflict.card_type in {"photo", "headline"}:
                y_pct = 87.5  # tuck safely below card
            else:
                return False

        if c_end - c_start < 0.35:
            return False
        # Check against ALL existing captions (not just the last one!)
        if any(not (c_end <= cap.start_s - 0.12 or c_start >= cap.end_s + 0.12) for cap in captions):
            return False

        sf, ef = sec_to_frame(c_start), sec_to_frame(c_end)
        cap_idx += 1
        captions.append(
            CaptionEvent(
                id=f"cap_{cap_idx:03d}",
                start_s=frame_to_sec(sf),
                end_s=frame_to_sec(ef),
                start_frame=sf,
                end_frame=ef,
                font_size_px=caption_font_size(len(picked)),
                y_pct=y_pct,
                words=[
                    CaptionWordCue(word_id=w.word_id, text=w.text.upper(), reveal_s=w.start_s)
                    for w in picked
                ],
            )
        )
        return True

    def _try_add_caption(line: ScriptLine, explicit: str | None = None, force: bool = False) -> bool:
        lwords = _line_words(line.line_no, words_by_line)
        picked = _pick_key_phrase_words(lwords, explicit)
        return _try_add_caption_words(picked, force=force)

    # First place explicit [CAPTION:...] cues
    last_cap_s = -10.0
    intro_end_s = (
        _line_words(script.intro_section.lines[-1].line_no, words_by_line)[-1].end_s
        if (script.intro_section and script.intro_section.lines and _line_words(script.intro_section.lines[-1].line_no, words_by_line))
        else 45.0
    )

    for ln in script.all_lines:
        lwords = _line_words(ln.line_no, words_by_line)
        if not lwords:
            continue
        t_ln = lwords[0].start_s
        if ln.has_tag("CAPTION"):
            if _try_add_caption(ln, explicit=ln.tag_values("CAPTION")[0]):
                last_cap_s = captions[-1].start_s
        else:
            cadence = 6.2 if t_ln <= intro_end_s else 8.5
            if t_ln - last_cap_s >= cadence:
                if _try_add_caption(ln):
                    last_cap_s = captions[-1].start_s

    # Bare-footage guard: never > 5s bare in first 60s, never > 7s after (<build> #6)!
    def _busy_intervals() -> list[tuple[float, float]]:
        spans = [(c.start_s, c.end_s) for c in cards]
        spans += [(c.start_s, c.end_s) for c in captions]
        spans += [(p.start_s, p.end_s) for p in pills]
        if not spans:
            return []
        spans.sort()
        merged = [spans[0]]
        for a, b in spans[1:]:
            if a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    for _ in range(24):  # bounded fill passes until every bare gap respects <=5s / <=7s
        busy = _busy_intervals()
        gaps: list[tuple[float, float]] = []
        prev_end = 0.0
        for a, b in busy:
            if a > prev_end:
                gaps.append((prev_end, a))
            prev_end = max(prev_end, b)
        if prev_end < duration_s:
            gaps.append((prev_end, duration_s))

        filled_any = False
        for g0, g1 in gaps:
            limit = 4.6 if g0 < 60.0 else 6.6
            if (g1 - g0) > limit:
                mid = (g0 + g1) / 2.0
                # Select 2-4 actual words INSIDE the bare gap near its midpoint!
                in_gap = [w for w in words if (g0 + 0.25) <= w.start_s <= (g1 - 0.35)]
                if in_gap:
                    anchor_i = min(range(len(in_gap)), key=lambda i: abs(in_gap[i].start_s - mid))
                    lo = max(0, anchor_i - 1)
                    hi = min(len(in_gap), lo + 3)
                    picked = in_gap[lo:hi]
                    if _try_add_caption_words(picked, force=True):
                        filled_any = True
        if not filled_any:
            break

    captions.sort(key=lambda c: c.start_frame)

    # -----------------------------------------------------------------------
    # 5. Build exact Blur + Darken Frame Intervals for FFmpeg
    # -----------------------------------------------------------------------
    blur_intervals: list[BlurInterval] = []
    for c in cards:
        if c.blur_px > 0 or c.darken_alpha > 0.0:
            blur_intervals.append(
                BlurInterval(
                    start_frame=c.start_frame,
                    end_frame=c.end_frame,
                    start_s=c.start_s,
                    end_s=c.end_s,
                    blur_px=c.blur_px,
                    darken_alpha=c.darken_alpha,
                    card_id=c.id,
                )
            )

    return Timeline(
        project_slug=brief.slug,
        fps=VIDEO_FPS,
        width=VIDEO_WIDTH,
        height=VIDEO_HEIGHT,
        duration_s=duration_s,
        total_frames=total_frames,
        theme=brief.theme.to_css_vars(),
        shots=shots,
        cards=cards,
        captions=captions,
        pills=pills,
        blur_intervals=blur_intervals,
        warnings=warnings,
    )
