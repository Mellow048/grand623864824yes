"""Audio finishing: silence trimming (-35 dB -> 0.30s w/ 35ms pad), capped 1.12x tempo, compressor & 2-pass loudnorm (-18 LUFS)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .align import AlignedWord
from .config import (
    AUDIO_MAX_LRA,
    AUDIO_MAX_TRUE_PEAK_DB,
    AUDIO_TARGET_LUFS,
    MAX_TEMPO_SPEEDUP,
    SILENCE_MAX_KEEP_S,
    SILENCE_PAD_S,
    SILENCE_THRESHOLD_DB,
    TARGET_WPM_MIN,
)


@dataclass
class TimeSpan:
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


@dataclass
class AudioMasterStats:
    raw_duration_s: float
    trimmed_duration_s: float
    final_duration_s: float
    tempo_factor: float
    final_wpm: float
    longest_pause_s: float
    integrated_lufs: float | None = None
    lra_lu: float | None = None
    true_peak_db: float | None = None


def parse_silencedetect_stderr(stderr_text: str, total_duration_s: float) -> list[TimeSpan]:
    """Parse FFmpeg silencedetect output into silence intervals."""
    silences: list[TimeSpan] = []
    cur_start: float | None = None
    for line in stderr_text.splitlines():
        m_start = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m_start:
            cur_start = float(m_start.group(1))
            continue
        m_end = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m_end and cur_start is not None:
            silences.append(TimeSpan(cur_start, float(m_end.group(1))))
            cur_start = None
    if cur_start is not None and cur_start < total_duration_s:
        silences.append(TimeSpan(cur_start, total_duration_s))
    return silences


def compute_keep_segments(
    silences: list[TimeSpan],
    total_duration_s: float,
    max_keep_silence_s: float = SILENCE_MAX_KEEP_S,
    pad_s: float = SILENCE_PAD_S,
) -> list[TimeSpan]:
    """
    Compute exact audio segments to keep so every real silence (> max_keep_silence_s)
    is trimmed down to `max_keep_silence_s`, strictly keeping `pad_s` (35 ms) untouched
    on BOTH sides of the cut so speech transients are never clipped (<build> #4).
    """
    min_cuttable = max_keep_silence_s + 2 * pad_s
    cuts: list[TimeSpan] = []
    for sil in sorted(silences, key=lambda s: s.start_s):
        if sil.duration_s <= max_keep_silence_s:
            continue
        # Remove only the interior middle of the silence, leaving pad_s + half_keep on each side
        excess = sil.duration_s - max_keep_silence_s
        if sil.duration_s < min_cuttable:
            # Keep exact 35ms safety pads even if resulting silence is slightly > 0.30s
            safe_start = sil.start_s + pad_s
            safe_end = sil.end_s - pad_s
            if safe_end > safe_start:
                cuts.append(TimeSpan(safe_start, safe_end))
        else:
            side_keep = max(pad_s, max_keep_silence_s / 2.0)
            cut_start = sil.start_s + side_keep
            cut_end = sil.end_s - side_keep
            if cut_end > cut_start and (cut_end - cut_start) <= excess + 1e-6:
                cuts.append(TimeSpan(cut_start, cut_end))

    keeps: list[TimeSpan] = []
    cursor = 0.0
    for c in cuts:
        if c.start_s > cursor:
            keeps.append(TimeSpan(cursor, c.start_s))
        cursor = max(cursor, c.end_s)
    if cursor < total_duration_s:
        keeps.append(TimeSpan(cursor, total_duration_s))
    return keeps or [TimeSpan(0.0, total_duration_s)]


def compute_tempo_factor(word_count: int, duration_s: float) -> float:
    """
    If pace after silence trimming is under 205 wpm, speed up by AT MOST 1.12x
    and stop there even if it still lands below 205 wpm (<build> #4).
    """
    if duration_s <= 0 or word_count <= 0:
        return 1.0
    wpm = (word_count / duration_s) * 60.0
    if wpm >= TARGET_WPM_MIN:
        return 1.0
    needed = (TARGET_WPM_MIN + 2.0) / wpm  # aim just inside 205-215 band
    return round(min(MAX_TEMPO_SPEEDUP, max(1.0, needed)), 4)


def remap_timestamp(t: float, keeps: list[TimeSpan], tempo_factor: float = 1.0) -> float:
    """Map an original raw audio timestamp through silence-cut segments + tempo speedup."""
    acc = 0.0
    for seg in keeps:
        if t < seg.start_s:
            return round(acc / tempo_factor, 4)
        if t <= seg.end_s:
            return round((acc + (t - seg.start_s)) / tempo_factor, 4)
        acc += seg.duration_s
    return round(acc / tempo_factor, 4)


def remap_aligned_words(
    words: list[AlignedWord],
    keeps: list[TimeSpan],
    tempo_factor: float = 1.0,
) -> list[AlignedWord]:
    """Remap every aligned word timestamp cleanly after silence trimming & tempo scaling."""
    out: list[AlignedWord] = []
    for w in words:
        ns = remap_timestamp(w.start_s, keeps, tempo_factor)
        ne = max(ns + 0.02, remap_timestamp(w.end_s, keeps, tempo_factor))
        out.append(
            AlignedWord(
                word_id=w.word_id,
                section_index=w.section_index,
                line_no=w.line_no,
                text=w.text,
                heard_text=w.heard_text,
                start_s=ns,
                end_s=ne,
                exact_match=w.exact_match,
            )
        )
    return out


def compute_longest_pause(words: list[AlignedWord]) -> float:
    if len(words) < 2:
        return 0.0
    return round(max(max(0.0, b.start_s - a.end_s) for a, b in zip(words, words[1:])), 4)


def build_trim_filter_complex(
    keeps: list[TimeSpan],
    tempo_factor: float,
    pitch_semitones: float = 0.0,
) -> str:
    """Generate FFmpeg filter_complex string for lossless silence-cut + optional pitch/tempo + light compressor."""
    parts = []
    labels = []
    for idx, seg in enumerate(keeps):
        lbl = f"a{idx}"
        parts.append(
            f"[0:a]atrim=start={seg.start_s:.4f}:end={seg.end_s:.4f},asetpts=PTS-STARTPTS[{lbl}]"
        )
        labels.append(f"[{lbl}]")
    chain = f"{''.join(labels)}concat=n={len(keeps)}:v=0:a=1"

    if abs(pitch_semitones) > 0.01:
        # Exact duration-preserving pitch shift combined with desired tempo_factor:
        # asetrate(48000*p) shifts pitch by p & speed by p; atempo(tempo_factor/p) yields net speed = tempo_factor!
        pitch_ratio = 2.0 ** (pitch_semitones / 12.0)
        shifted_sr = max(16000, int(round(48000.0 * pitch_ratio)))
        comp_tempo = max(0.5, min(2.0, tempo_factor / pitch_ratio))
        chain += (
            f",aresample=48000,asetrate={shifted_sr},aresample=48000"
            f",atempo={comp_tempo:.4f}"
            ",equalizer=f=210:width_type=o:width=1.2:g=1.6,highshelf=f=6800:g=-1.6"
        )
    elif tempo_factor > 1.0001 or tempo_factor < 0.9999:
        chain += f",atempo={max(0.5, min(2.0, tempo_factor)):.4f}"

    # Light broadcast vocal compressor before 2-pass loudnorm
    chain += ",acompressor=threshold=-20dB:ratio=2.4:attack=8:release=90:makeup=2dB[aout]"
    parts.append(chain)
    return ";".join(parts)


def parse_loudnorm_json(stderr_text: str) -> dict[str, str]:
    """Extract the JSON measurement block emitted by FFmpeg loudnorm=print_format=json."""
    matches = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr_text, re.DOTALL)
    if not matches:
        raise ValueError("Could not find FFmpeg loudnorm JSON output in stderr.")
    return json.loads(matches[-1])


def master_audio_with_ffmpeg(
    input_wav: Path,
    output_wav: Path,
    words: list[AlignedWord],
    pitch_semitones: float = 0.0,
    speed_override: float | None = None,
    max_keep_silence_s: float = SILENCE_MAX_KEEP_S,
) -> tuple[list[AlignedWord], AudioMasterStats]:
    """
    Full FFmpeg mastering pipeline:
    1. Probe duration & detect real silences (< -35 dB)
    2. Trim silences down to max_keep_silence_s keeping 35ms pads
    3. Compute WPM (capped at 1.12x by default, or explicit speed_override when requested) + optional natural pitch shift
    4. Light compression + true two-pass loudnorm (-18 LUFS, LRA <= 3 LU, TP < -2 dB)
    """
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(input_wav),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    raw_dur = float(probe.stdout.strip())

    # 1. Detect real silences under -35 dB
    det = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(input_wav),
            "-af", f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d={max_keep_silence_s}",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    silences = parse_silencedetect_stderr(det.stderr, raw_dur)
    keeps = compute_keep_segments(silences, raw_dur, max_keep_silence_s=max_keep_silence_s)
    trimmed_dur = sum(seg.duration_s for seg in keeps)
    if speed_override is not None:
        tempo = round(max(0.85, min(1.40, float(speed_override))), 4)
    else:
        tempo = compute_tempo_factor(len(words), trimmed_dur)
    final_dur = trimmed_dur / tempo

    output_wav.parent.mkdir(parents=True, exist_ok=True)
    intermediate = output_wav.with_suffix(".pre_loudnorm.wav")

    # 2. Cut silences + optional pitch/tempo + light compressor
    fc = build_trim_filter_complex(keeps, tempo, pitch_semitones=pitch_semitones)
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-i", str(input_wav),
            "-filter_complex", fc, "-map", "[aout]", "-ar", "48000", "-ac", "1",
            str(intermediate),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    # 3. Two-pass loudnorm (-18 LUFS, LRA=3, TP=-2)
    pass1 = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(intermediate),
            "-af", f"loudnorm=I={AUDIO_TARGET_LUFS}:LRA={AUDIO_MAX_LRA}:TP={AUDIO_MAX_TRUE_PEAK_DB}:print_format=json",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    m = parse_loudnorm_json(pass1.stderr)
    pass2_af = (
        f"loudnorm=I={AUDIO_TARGET_LUFS}:LRA={AUDIO_MAX_LRA}:TP={AUDIO_MAX_TRUE_PEAK_DB}:"
        f"measured_I={m['input_i']}:measured_LRA={m['input_lra']}:"
        f"measured_TP={m['input_tp']}:measured_thresh={m['input_thresh']}:"
        f"offset={m['target_offset']}:linear=true:print_format=json"
    )
    pass2 = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-i", str(intermediate),
            "-af", pass2_af, "-ar", "48000", "-ac", "1", str(output_wav),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    m2 = parse_loudnorm_json(pass2.stderr)
    intermediate.unlink(missing_ok=True)

    remapped = remap_aligned_words(words, keeps, tempo)
    stats = AudioMasterStats(
        raw_duration_s=round(raw_dur, 3),
        trimmed_duration_s=round(trimmed_dur, 3),
        final_duration_s=round(final_dur, 3),
        tempo_factor=tempo,
        final_wpm=round((len(words) / max(final_dur, 1e-3)) * 60.0, 1),
        longest_pause_s=compute_longest_pause(remapped),
        integrated_lufs=float(m2["output_i"]),
        lra_lu=float(m2["output_lra"]),
        true_peak_db=float(m2["output_tp"]),
    )
    return remapped, stats
