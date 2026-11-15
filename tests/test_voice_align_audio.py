"""Tests for voice recording approval gates, difflib alignment, silence-trim math & 1.12x tempo cap."""

from __future__ import annotations

from pathlib import Path

import pytest

from vicecut.align import HeardWord, align_section_words, synthesize_synthetic_alignment
from vicecut.audio import TimeSpan, compute_keep_segments, compute_tempo_factor, remap_aligned_words
from vicecut.config import ApprovalState, ProjectBrief, VoiceConfig
from vicecut.pronunciation import PronunciationLexicon
from vicecut.script_parser import parse_script_text
from vicecut.voice import estimate_tts_cost, record_sections


def test_voice_gates_and_mock_section_recording(tmp_path: Path) -> None:
    md = """# Demo
## Intro
You might want GTA 6 right now.
## Close
Wait for real proof.
"""
    script = parse_script_text(md)
    lex = PronunciationLexicon()

    # Gate 1 blocks recording until script_approved=True
    brief = ProjectBrief(
        slug="gate-test",
        topic="everything you need to know about GTA 6 before you buy",
        subject="GTA 6",
        action="buy",
        viewer="Gamers",
        voice=VoiceConfig(provider="mock"),
        approvals=ApprovalState(script_approved=False),
    )
    with pytest.raises(PermissionError, match="Gate 1"):
        record_sections(script, brief, lex, tmp_path / "takes")

    # Gate 2 blocks ElevenLabs until tts_cost_approved=True
    brief_el = ProjectBrief(
        slug="gate-el",
        topic="everything you need to know about GTA 6 before you buy",
        subject="GTA 6",
        action="buy",
        viewer="Gamers",
        voice=VoiceConfig(provider="elevenlabs", api_key_file=str(tmp_path / "k.txt")),
        approvals=ApprovalState(script_approved=True, tts_cost_approved=False),
    )
    est = estimate_tts_cost(script, brief_el, lex)
    assert est.estimated_credits == est.total_chars > 0
    with pytest.raises(PermissionError, match="Gate 2"):
        record_sections(script, brief_el, lex, tmp_path / "takes_el")

    # Approved mock recording succeeds & supports single-section re-record
    brief.approvals.script_approved = True
    takes = record_sections(script, brief, lex, tmp_path / "takes_ok")
    assert len(takes) == 2
    assert all(t.audio_path.exists() and t.audio_path.stat().st_size > 44 for t in takes)

    redo = record_sections(script, brief, lex, tmp_path / "takes_ok", only_sections=[1])
    assert len(redo) == 1 and redo[0].section_index == 1


def test_difflib_alignment_and_redo_detection() -> None:
    md = """# Demo
## Intro
Rockstar lists PS5 and Xbox right now.
"""
    script = parse_script_text(md)
    sec = script.sections[0]

    # Good Whisper tokens -> no redo needed
    good_tokens = [
        HeardWord("Rockstar", 0.0, 0.35),
        HeardWord("lists", 0.36, 0.58),
        HeardWord("PS5", 0.60, 0.90),
        HeardWord("and", 0.92, 1.05),
        HeardWord("Xbox", 1.07, 1.38),
        HeardWord("right", 1.40, 1.60),
        HeardWord("now", 1.62, 1.90),
    ]
    words, mismatches, needs_redo = align_section_words(sec, good_tokens, start_word_id=0)
    assert len(words) == 7
    assert needs_redo is False
    assert all(w.exact_match for w in words)

    # Skipped chunk of words -> flagged for section re-record!
    bad_tokens = [
        HeardWord("Rockstar", 0.0, 0.35),
        HeardWord("now", 1.60, 1.90),
    ]
    _, mismatches_bad, needs_redo_bad = align_section_words(sec, bad_tokens, start_word_id=0)
    assert needs_redo_bad is True
    assert len(mismatches_bad) >= 1


def test_audio_silence_trim_padding_timestamp_remap_and_112x_cap() -> None:
    # A 1.2s silence between 2.0s and 3.2s gets trimmed down to 0.30s (35ms pad on both sides!)
    keep = compute_keep_segments(
        silences=[TimeSpan(2.0, 3.2)],
        total_duration_s=5.0,
        max_keep_silence_s=0.30,
        pad_s=0.035,
    )
    assert len(keep) == 2
    assert abs(keep[0].start_s - 0.0) < 1e-4
    # Kept silence gap = (2.15 - 2.0) + (3.2 - 3.05) = 0.30s; plus >=35ms safety pad on each side!
    removed = keep[1].start_s - keep[0].end_s
    kept_pause = (3.2 - 2.0) - removed
    assert abs(kept_pause - 0.30) < 0.01
    assert keep[0].end_s >= 2.035 and keep[1].start_s <= 3.165

    md = """# Demo
## Intro
One two three four five six seven eight nine ten eleven twelve.
"""
    script = parse_script_text(md)
    syn = synthesize_synthetic_alignment(script, wpm=210.0)
    remapped = remap_aligned_words(syn.words, keeps=[TimeSpan(0.0, 1.0), TimeSpan(1.5, 5.0)], tempo_factor=1.10)
    # Any word starting after 1.5s shifted left by 0.5s and divided by 1.10
    late_orig = [w for w in syn.words if w.start_s >= 1.5]
    late_new = [w for w in remapped if w.word_id == late_orig[0].word_id][0]
    expected = (late_orig[0].start_s - 0.5) / 1.10
    assert abs(late_new.start_s - expected) < 1e-3

    # Speedup MUST strictly cap at 1.12x even if raw speaking rate is very slow!
    assert compute_tempo_factor(word_count=100, duration_s=60.0) == 1.12
    assert compute_tempo_factor(word_count=210, duration_s=60.0) == 1.0
