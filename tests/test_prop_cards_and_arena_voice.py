"""Tests for compact floating white-BG motion prop cards, fallback URLs, Arena/prebuilt voice takes, and the real GTA 6 launch-week project."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from vicecut.align import synthesize_synthetic_alignment
from vicecut.config import ProjectBrief, project_dirs
from vicecut.graphics import build_overlay_html, compute_frame_render_key
from vicecut.media import FetchPlan, MediaManifest, VideoFetchItem, download_official_video, execute_fetch_plan
from vicecut.pronunciation import PronunciationLexicon
from vicecut.research import ResearchLedger
from vicecut.script_checker import check_script
from vicecut.script_parser import parse_script_file
from vicecut.timeline import compile_timeline
from vicecut.voice import record_sections


REPO_ROOT = Path(__file__).resolve().parents[1]
GTA6_LAUNCH_PROJ = REPO_ROOT / "projects" / "gta6-before-you-play"


def test_prop_card_is_compact_unblurred_pip_and_never_static_deduped() -> None:
    """Compact white-BG prop cards must keep background gameplay crisp (blur=0, darken=0) and render every 30fps frame."""
    brief = ProjectBrief.load(GTA6_LAUNCH_PROJ / "project.yaml")
    dirs = project_dirs(GTA6_LAUNCH_PROJ)
    script = parse_script_file(dirs["script"] / "script.md")
    manifest = MediaManifest.load(dirs["media"] / "manifest.yaml")
    ledger = ResearchLedger.load(dirs["research"] / "facts.yaml")

    alignment = synthesize_synthetic_alignment(script)
    tl = compile_timeline(script, alignment, manifest, brief, ledger)

    prop_cards = [c for c in tl.cards if c.card_type == "prop"]
    assert len(prop_cards) >= 8
    for pc in prop_cards:
        # Must NEVER blur or darken background gameplay (user requirement: small PiP over moving gameplay)
        assert pc.blur_px == 0
        assert pc.darken_alpha == 0.0
        assert pc.payload["position"] == "right"
        assert pc.payload["animation"] in {"float_zoom", "slide_swing", "bounce_pop"}
        assert pc.payload["image_path"].endswith(".png")

        # Must disable static-frame dedup so 30fps levitation/swing/bounce stays buttery smooth!
        mid_frame = (pc.start_frame + pc.end_frame) // 2
        assert compute_frame_render_key(tl, mid_frame) is None

    # Verify compiled overlay HTML renders compact floating PiP (520x370, NOT fullscreen!)
    html_out = build_overlay_html(tl, dirs["renders"] / "overlay_test_tmp.html", project_dir=GTA6_LAUNCH_PROJ)
    try:
        html_text = html_out.read_text(encoding="utf-8")
        assert ".card-prop-wrap" in html_text
        assert "width: 520px;" in html_text
        assert "height: 370px;" in html_text
        assert "card.card_type === 'prop'" in html_text
    finally:
        html_out.unlink(missing_ok=True)


def test_gta6_before_you_play_project_passes_research_script_and_assets() -> None:
    """Our real launch-week GTA 6 project must pass research audit, all 33 script rules, and ship all 8 zero-people prop PNGs."""
    brief = ProjectBrief.load(GTA6_LAUNCH_PROJ / "project.yaml")
    dirs = project_dirs(GTA6_LAUNCH_PROJ)
    ledger = ResearchLedger.load(dirs["research"] / "facts.yaml")
    audit = ledger.audit(brief)
    assert audit.passed, f"Research audit failed: {audit.errors}"

    script = parse_script_file(dirs["script"] / "script.md")
    report = check_script(script, brief, ledger)
    assert report.passed, f"Script checker failed: {report.errors}"
    assert not report.warnings, f"Unexpected script warnings: {report.warnings}"

    manifest = MediaManifest.load(dirs["media"] / "manifest.yaml")
    assert len(manifest.photos) == 8
    for ph in manifest.photos:
        img_file = GTA6_LAUNCH_PROJ / ph.image_path
        assert img_file.exists(), f"Missing studio prop PNG: {img_file}"
        assert img_file.stat().st_size > 10_000

    plan = FetchPlan.load(dirs["media"] / "fetch_plan.yaml")
    assert len(plan.videos) == 5
    assert any(v.fallback_url for v in plan.videos)


def test_video_fetch_fallback_url_and_partial_resilience(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When primary URL fails, download_official_video retries fallback_url; execute_fetch_plan continues if 1 video fails."""
    local_mp4 = tmp_path / "fallback_trailer.mp4"
    local_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 1024)

    def fake_urlopen(*args, **kwargs):
        raise OSError("Simulated network failure on primary URL")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    item = VideoFetchItem(
        id="src_test_fb",
        title="Official Trailer",
        channel_or_outlet="Rockstar Games",
        url="https://example.invalid/primary.mp4",
        fallback_url=str(local_mp4),
    )
    out_mp4, rec = download_official_video(item, tmp_path / "raw")
    assert out_mp4.exists()
    assert out_mp4.stat().st_size == local_mp4.stat().st_size
    assert rec.id == "src_test_fb"

    # Partial failure test: 1 failing video + 1 succeeding local fallback video
    monkeypatch.setattr(
        "vicecut.media.split_video_into_scenes",
        lambda **kwargs: ([], None),
    )
    plan = FetchPlan(
        project="test",
        videos=[
            VideoFetchItem(id="v_fail", title="Broken", channel_or_outlet="Rockstar Games", url="https://example.invalid/bad.mp4"),
            item,
        ],
    )
    manifest = MediaManifest(project="test")
    dirs = {
        "media": tmp_path / "media",
        "media_raw": tmp_path / "raw2",
        "media_scenes": tmp_path / "scenes",
        "media_photos": tmp_path / "photos",
        "media_headlines": tmp_path / "headlines",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    summary = execute_fetch_plan(plan, manifest, tmp_path, dirs, skip_headlines=True)
    assert summary["videos_fetched"] == 1
    assert len(summary["errors"]) == 1
    assert "v_fail" in summary["errors"][0]


def test_arena_prebuilt_voice_provider_concatenates_wav_parts(tmp_path: Path) -> None:
    """voice.provider='arena' reuses pre-recorded section WAV takes cleanly (including multi-part concatenation)."""
    brief = ProjectBrief.load(GTA6_LAUNCH_PROJ / "project.yaml")
    brief.voice.provider = "arena"
    brief.approvals.script_approved = True

    script = parse_script_file(GTA6_LAUNCH_PROJ / "script" / "script.md")
    lexicon = PronunciationLexicon.load(GTA6_LAUNCH_PROJ / "script" / "pronunciations.yaml")

    takes_dir = tmp_path / "takes"
    takes_dir.mkdir(parents=True, exist_ok=True)

    def _make_wav(path: Path, frames: int = 2400) -> None:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(b"\x00\x00" * frames)

    # Section 0: two WAV parts -> concatenated; Sections 1-3: single WAV each
    _make_wav(takes_dir / "section_00_part01.wav", 1200)
    _make_wav(takes_dir / "section_00_part02.wav", 1200)
    for sec in script.sections[1:]:
        _make_wav(takes_dir / f"section_{sec.index:02d}.wav", 2400)

    takes = record_sections(script, brief, lexicon, takes_dir, enforce_approval_gates=True)
    assert len(takes) == len(script.sections)
    for tk in takes:
        assert tk.audio_path.exists()
        with wave.open(str(tk.audio_path), "rb") as wf:
            assert wf.getnframes() == 2400
