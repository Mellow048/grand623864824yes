"""Section-by-section voice synthesis (Kokoro local ONNX, ElevenLabs file-keyed, or Mock) (<build> #4)."""

from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectBrief
from .pronunciation import PronunciationLexicon
from .script_parser import ParsedScript, ScriptSection


@dataclass
class SectionTake:
    section_index: int
    section_title: str
    spoken_text: str
    char_count: int
    audio_path: Path


@dataclass
class TTSCostEstimate:
    provider: str
    total_chars: int
    estimated_credits: int
    per_section: list[tuple[str, int]]
    requires_approval: bool


def estimate_tts_cost(
    script: ParsedScript,
    brief: ProjectBrief,
    lexicon: PronunciationLexicon,
) -> TTSCostEstimate:
    """Compute exact spoken character count (~1 ElevenLabs credit each) before spending."""
    per_section: list[tuple[str, int]] = []
    total = 0
    for sec in script.sections:
        spoken = lexicon.to_spoken(sec.spoken_text)
        n = len(spoken)
        per_section.append((sec.title, n))
        total += n
    is_paid = brief.voice.provider == "elevenlabs"
    return TTSCostEstimate(
        provider=brief.voice.provider,
        total_chars=total,
        estimated_credits=total if is_paid else 0,
        per_section=per_section,
        requires_approval=is_paid,
    )


def _write_mock_wav(path: Path, duration_s: float, sample_rate: int = 24000) -> None:
    """Synthesize a clean low-volume cadence-paced placeholder WAV for offline pipeline tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = max(1, int(duration_s * sample_rate))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            t = i / sample_rate
            # Subtle 220 Hz modulated pulse simulating speech syllables with tiny inter-word dips
            env = 0.25 * (0.5 + 0.5 * math.sin(2 * math.pi * 4.2 * t))
            val = int(32767 * env * math.sin(2 * math.pi * 210.0 * t))
            frames.extend(struct.pack("<h", val))
        wf.writeframes(bytes(frames))


def _record_kokoro(spoken_text: str, out_wav: Path, voice_id: str, speed: float, lang: str) -> None:
    """Record one full section take with free local kokoro-onnx."""
    try:
        from kokoro_onnx import Kokoro  # type: ignore
        import soundfile as sf  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "kokoro-onnx (and soundfile) not installed. Run `vicecut doctor --install` or "
            "`pip install kokoro-onnx soundfile` and download kokoro-v1.0.onnx + voices-v1.0.bin."
        ) from exc

    model_dir = Path.home() / ".local/share/vicecut/kokoro"
    model_path = model_dir / "kokoro-v1.0.onnx"
    voices_path = model_dir / "voices-v1.0.bin"
    if not model_path.exists() or not voices_path.exists():
        raise FileNotFoundError(
            f"Kokoro model files not found in {model_dir}. "
            "Place kokoro-v1.0.onnx and voices-v1.0.bin there."
        )
    kokoro = Kokoro(str(model_path), str(voices_path))
    samples, sample_rate = kokoro.create(spoken_text, voice=voice_id, speed=speed, lang=lang)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_wav), samples, sample_rate)


def _record_elevenlabs(
    spoken_text: str,
    out_audio: Path,
    voice_id: str,
    api_key: str,
) -> None:
    """Record one full section take with ElevenLabs TTS API. Never logs api_key."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_24000"
    payload = json.dumps(
        {
            "text": spoken_text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {
                "stability": 0.62,
                "similarity_boost": 0.78,
                "style": 0.08,
                "use_speaker_boost": True,
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/pcm",
        },
        method="POST",
    )
    out_audio.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=180) as resp:
        pcm_bytes = resp.read()
    # Wrap raw 24kHz 16-bit mono PCM cleanly into WAV
    with wave.open(str(out_audio), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm_bytes)


def _materialize_prebuilt_take(takes_dir: Path, section_index: int, out_wav: Path) -> Path:
    """
    Use pre-recorded voice takes (e.g. synthesized via Arena TTS `generate_speech`).
    Supports `section_XX.wav`, `section_XX.mp3`, or multi-part `section_XX_partNN.{wav,mp3}`.
    """
    if out_wav.exists() and out_wav.stat().st_size > 44:
        return out_wav

    part_files = sorted(
        p for p in takes_dir.glob(f"section_{section_index:02d}_part*")
        if p.suffix.lower() in {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac"}
    )
    single_alt = next(
        (
            takes_dir / f"section_{section_index:02d}{ext}"
            for ext in (".mp3", ".m4a", ".ogg", ".flac", ".aac")
            if (takes_dir / f"section_{section_index:02d}{ext}").exists()
        ),
        None,
    )
    sources = part_files if part_files else ([single_alt] if single_alt else [])
    if not sources:
        raise FileNotFoundError(
            f"Prebuilt/Arena voice take missing for section {section_index:02d} in {takes_dir}. "
            f"Expected section_{section_index:02d}.wav/.mp3 or section_{section_index:02d}_part*.mp3."
        )

    ffmpeg = shutil.which("ffmpeg")
    if len(sources) == 1 and sources[0].suffix.lower() == ".wav":
        shutil.copy2(sources[0], out_wav)
        return out_wav

    if ffmpeg:
        if len(sources) == 1:
            subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(sources[0]), "-ac", "1", "-ar", "24000", str(out_wav)],
                check=True,
            )
        else:
            # Decode each MP3/WAV part independently via filter_complex concat so ID3 tags never glitch!
            ff_inputs: list[str] = []
            tags: list[str] = []
            for idx, p in enumerate(sources):
                ff_inputs += ["-i", str(p)]
                tags.append(f"[{idx}:a]")
            fc = f"{''.join(tags)}concat=n={len(sources)}:v=0:a=1[aout]"
            subprocess.run(
                [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    *ff_inputs, "-filter_complex", fc, "-map", "[aout]",
                    "-ac", "1", "-ar", "24000", str(out_wav),
                ],
                check=True,
            )
        return out_wav

    # Pure-Python fallback when all parts are uncompressed WAVs
    if all(p.suffix.lower() == ".wav" for p in sources):
        with wave.open(str(sources[0]), "rb") as w0:
            params = w0.getparams()
        with wave.open(str(out_wav), "wb") as wout:
            wout.setparams(params)
            for p in sources:
                with wave.open(str(p), "rb") as wi:
                    wout.writeframes(wi.readframes(wi.getnframes()))
        return out_wav

    raise RuntimeError(
        f"FFmpeg is required to decode prebuilt audio {[p.name for p in sources]} into {out_wav.name}."
    )


def record_sections(
    script: ParsedScript,
    brief: ProjectBrief,
    lexicon: PronunciationLexicon,
    takes_dir: Path,
    project_dir: Path | None = None,
    only_sections: list[int] | None = None,
    enforce_approval_gates: bool = True,
) -> list[SectionTake]:
    """
    Record one continuous take per section (never per sentence).
    Enforces Gate 1 (script approval) and Gate 2 (paid ElevenLabs credit approval).
    """
    if enforce_approval_gates and not brief.approvals.script_approved:
        raise PermissionError(
            "Gate 1 blocked: show the sourced script, source list, and checker report to the user "
            "and record approval (`approvals.script_approved: true`) before recording voice."
        )
    if enforce_approval_gates and brief.voice.provider == "elevenlabs" and not brief.approvals.tts_cost_approved:
        est = estimate_tts_cost(script, brief, lexicon)
        raise PermissionError(
            f"Gate 2 blocked: ElevenLabs synthesis will use {est.total_chars:,} characters "
            f"(~{est.estimated_credits:,} credits). Get explicit user confirmation and set "
            "`approvals.tts_cost_approved: true` before recording."
        )

    api_key = brief.voice.load_api_key(project_dir) if brief.voice.provider == "elevenlabs" else ""
    takes_dir.mkdir(parents=True, exist_ok=True)
    takes: list[SectionTake] = []

    for sec in script.sections:
        if only_sections is not None and sec.index not in only_sections:
            continue
        spoken = lexicon.to_spoken(sec.spoken_text)
        out_wav = takes_dir / f"section_{sec.index:02d}.wav"
        if brief.voice.provider == "kokoro":
            _record_kokoro(spoken, out_wav, brief.voice.voice_id, brief.voice.speed, brief.voice.language)
        elif brief.voice.provider == "elevenlabs":
            _record_elevenlabs(spoken, out_wav, brief.voice.voice_id, api_key)
        elif brief.voice.provider in {"arena", "prebuilt"}:
            _materialize_prebuilt_take(takes_dir, sec.index, out_wav)
        elif brief.voice.provider == "mock":
            # Target ~210 WPM cadence for mock tests
            est_sec = max(1.0, (sec.word_count / 210.0) * 60.0)
            _write_mock_wav(out_wav, est_sec)
        else:
            raise ValueError(f"Unsupported voice provider: {brief.voice.provider}")

        takes.append(
            SectionTake(
                section_index=sec.index,
                section_title=sec.title,
                spoken_text=spoken,
                char_count=len(spoken),
                audio_path=out_wav,
            )
        )
    return takes
