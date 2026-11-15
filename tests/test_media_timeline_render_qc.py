"""Tests for media budget, contact sheets, word-keyed timeline guards, overlay HTML, FFmpeg builder & QC."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from vicecut.align import synthesize_synthetic_alignment
from vicecut.config import ProjectBrief
from vicecut.graphics import build_overlay_html
from vicecut.media import (
    MediaManifest,
    ShotClip,
    build_scene_contact_sheet,
    compute_image_luminance,
    evaluate_shot_pool,
)
from vicecut.qc import generate_credits_markdown, measure_timeline_qc
from vicecut.render import (
    build_ffmpeg_composite_command,
    build_mobile_export_command,
    build_pre_render_storyboard_sheet,
)
from vicecut.research import ResearchLedger
from vicecut.script_parser import parse_script_text
from vicecut.timeline import compile_timeline


def test_media_budget_luminance_and_contact_sheet(tmp_path: Path) -> None:
    bright_p = tmp_path / "bright.png"
    dark_p = tmp_path / "dark.png"
    Image.new("RGB", (64, 36), (230, 220, 210)).save(bright_p)
    Image.new("RGB", (64, 36), (20, 15, 28)).save(dark_p)

    assert compute_image_luminance(bright_p) > 0.7
    assert compute_image_luminance(dark_p) < 0.2

    sheet_p = tmp_path / "sheet.png"
    build_scene_contact_sheet(
        scene_triplets=[
            ("shot_01", bright_p, bright_p, dark_p, "BRIGHT"),
            ("shot_02", dark_p, dark_p, bright_p, "DARK"),
        ],
        output_png=sheet_p,
    )
    assert sheet_p.exists()
    with Image.open(sheet_p) as im:
        assert im.width > 400 and im.height > 150

    # Pool with 20 motion clips + 10 stills for 1.0 min -> stills capped at <10% (max 2 stills)
    shots = [ShotClip(id=f"m{i}", source_id="s1", path="", duration_s=2.5, is_still=False) for i in range(20)]
    shots += [ShotClip(id=f"st{i}", source_id="s1", path="", duration_s=2.8, is_still=True) for i in range(10)]
    manifest = MediaManifest(project="t", shots=shots)
    budget = evaluate_shot_pool(manifest, runtime_s=60.0)
    assert budget.max_allowed_stills == 2
    assert budget.ok is False  # 20 + 2 = 22 < 25 required


def test_timeline_invariants_and_face_safe_flip(tmp_path: Path) -> None:
    sample_dir = Path(__file__).resolve().parent.parent / "examples" / "gta6-sample"
    brief = ProjectBrief.load(sample_dir / "project.yaml")
    ledger = ResearchLedger.load(sample_dir / "research" / "facts.yaml")
    manifest = MediaManifest.load(sample_dir / "media" / "manifest.yaml")
    script = parse_script_text((sample_dir / "script" / "script.md").read_text(encoding="utf-8"))
    alignment = synthesize_synthetic_alignment(script, wpm=210.0)

    # Mark every shot in the pool as having a face on the RIGHT to verify automatic flip to LEFT!
    for sc in manifest.shots:
        sc.face_zones = ["right"]

    tl = compile_timeline(script, alignment, manifest, brief, ledger=ledger)

    # 1. Cards strictly in chronological order; first proof card near t~6.9s!
    assert tl.cards == sorted(tl.cards, key=lambda c: c.start_frame)
    assert tl.cards[0].card_type == "headline"
    assert tl.cards[0].start_s < 10.0

    # 2. Zero repeated shots & zero missing shots
    shot_ids = [s.shot_id for s in tl.shots]
    assert len(shot_ids) == len(set(shot_ids))
    assert not any(sid.startswith("MISSING_") for sid in shot_ids)

    # 3. Character card LUCIA over right-faced shot_11 auto-flipped to LEFT!
    lucia_cards = [c for c in tl.cards if c.card_type == "character" and c.payload.get("name") == "LUCIA"]
    assert len(lucia_cards) == 1
    assert lucia_cards[0].payload["side"] == "left"

    # 4. Explicit [CAPTION:WAIT FOR PROOF] preserved verbatim!
    cap_texts = [" ".join(w.text for w in cp.words) for cp in tl.captions]
    assert "WAIT FOR PROOF" in cap_texts

    # 5. Overlay HTML, Storyboard PNG, FFmpeg commands, QC report & credits all build cleanly!
    ov_html = build_overlay_html(tl, tmp_path / "overlay.html")
    html_text = ov_html.read_text(encoding="utf-8")
    assert "window.renderFrame" in html_text
    assert "--vc-panel-bg" in html_text

    sb_png = build_pre_render_storyboard_sheet(tl, tmp_path / "storyboard.png")
    assert sb_png.exists()

    cmd_preview = build_ffmpeg_composite_command(
        timeline=tl,
        overlay_frames_dir=tmp_path / "frames",
        master_audio_wav=tmp_path / "voice.wav",
        output_mp4=tmp_path / "preview.mp4",
        preview=True,
    )
    assert "gblur=sigma=13" in " ".join(cmd_preview)

    cmd_final = build_ffmpeg_composite_command(
        timeline=tl,
        overlay_frames_dir=tmp_path / "frames",
        master_audio_wav=tmp_path / "voice.wav",
        output_mp4=tmp_path / "final.mp4",
        preview=False,
    )
    assert "gblur=sigma=26" in " ".join(cmd_final)

    cmd_mob = build_mobile_export_command(tmp_path / "final.mp4", tmp_path / "mobile.mp4", tl.duration_s)
    assert "-b:v" in cmd_mob

    qc = measure_timeline_qc(tl, brief, alignment=alignment)
    by_name = {m.name: m for m in qc.metrics}
    assert by_name["Picture changes / min"].passed
    assert by_name["First-60s picture changes"].passed
    assert by_name["Median shot duration"].passed
    assert by_name["Shots over 6s"].passed
    assert by_name["Unique shots (zero repeats)"].passed
    assert by_name["Card on-screen coverage"].passed
    assert by_name["Longest bare footage streak"].passed
    assert by_name["Official stills share"].passed
    assert by_name["End voice/caption sync drift"].passed
    # Honest shortfalls clearly listed when audio/QC rounds aren't completed yet
    assert len(qc.shortfalls) == 2

    credits_out = tmp_path / "credits.md"
    generate_credits_markdown(brief, ledger, manifest, credits_out)
    credits_md = credits_out.read_text(encoding="utf-8")
    assert "Rockstar Games" in credits_md
    assert "F01" in credits_md
