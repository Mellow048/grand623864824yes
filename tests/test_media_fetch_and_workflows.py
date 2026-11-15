"""Tests for declarative media acquisition, scene-cut planning, lossless overlay dedup & GitHub Actions workflows."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from PIL import Image

from vicecut.align import synthesize_synthetic_alignment
from vicecut.cli import main as cli_main
from vicecut.config import ProjectBrief
from vicecut.graphics import build_overlay_html, compute_frame_render_key
from vicecut.media import (
    FetchPlan,
    MediaManifest,
    PhotoFetchItem,
    ShotClip,
    VideoFetchItem,
    download_official_photo,
    download_official_video,
    drop_shots,
    parse_ffmpeg_scene_timestamps,
    plan_scene_cuts,
)
from vicecut.research import ResearchLedger
from vicecut.script_parser import parse_script_text
from vicecut.timeline import compile_timeline


def test_fetch_plan_roundtrip_and_sample_plan(tmp_path: Path) -> None:
    sample_plan = Path(__file__).resolve().parent.parent / "examples" / "gta6-sample" / "media" / "fetch_plan.yaml"
    plan = FetchPlan.load(sample_plan)
    assert plan.project == "gta6-before-you-buy-sample"
    assert len(plan.videos) == 2
    assert len(plan.headlines) == 3

    saved = tmp_path / "fetch_plan.yaml"
    plan.save(saved)
    reloaded = FetchPlan.load(saved)
    assert reloaded.videos[0].url == plan.videos[0].url
    assert reloaded.headlines[0].outlet == "ROCKSTAR GAMES"


def test_scene_timestamp_parser_and_cut_planner() -> None:
    stderr = """
    [Parsed_showinfo_1 @ 0x1] n:   0 pts:  12000 pts_time:4.200 ...
    [Parsed_showinfo_1 @ 0x1] n:   1 pts:  12300 pts_time:4.310 ...
    [Parsed_showinfo_1 @ 0x1] n:   2 pts:  38000 pts_time:12.800 ...
    [Parsed_showinfo_1 @ 0x1] n:   3 pts:  65000 pts_time:24.000 ...
    """
    ts = parse_ffmpeg_scene_timestamps(stderr)
    # 4.310 is within 0.25s of 4.200 -> deduplicated cleanly!
    assert ts == [4.2, 12.8, 24.0]

    # 30s trailer: drop first 2.5s (ESRB) and last 4.0s (logo endplate) -> usable [2.5, 26.0]
    segs = plan_scene_cuts(
        duration_s=30.0,
        detected_cuts_s=ts,
        min_clip_s=1.6,
        max_clip_s=3.5,
        drop_first_s=2.5,
        drop_last_s=4.0,
    )
    assert len(segs) >= 6
    # All segments respect opening/closing drop margins & stay within [min_clip_s, max_clip_s]
    for a, b in segs:
        assert a >= 2.5 and b <= 26.0
        assert 1.6 <= (b - a) <= 3.55
    # Strictly non-overlapping
    for (_, b1), (a2, _) in zip(segs, segs[1:]):
        assert a2 >= b1


def test_download_official_video_blocks_fan_made_and_copies_local(tmp_path: Path) -> None:
    bad_item = VideoFetchItem(
        id="bad_fan",
        title="GTA 6 Fan Made Concept Trailer 4K",
        channel_or_outlet="Random Reuploads",
        url="https://example.com/fake.mp4",
    )
    with pytest.raises(ValueError, match="blocked source"):
        download_official_video(bad_item, tmp_path / "raw", blocked_sources=["fan made", "concept trailer"])

    # Valid local file download
    fake_mp4 = tmp_path / "official_press_clip.mp4"
    fake_mp4.write_bytes(b"FAKE_MP4_BYTES")
    ok_item = VideoFetchItem(
        id="src_official",
        title="GTA 6 Official Trailer",
        channel_or_outlet="Rockstar Games",
        url=str(fake_mp4),
        date="2025-05-06",
    )
    out_mp4, rec = download_official_video(ok_item, tmp_path / "raw", blocked_sources=["fan made"])
    assert out_mp4.exists() and out_mp4.read_bytes() == b"FAKE_MP4_BYTES"
    assert rec.id == "src_official" and rec.is_official is True


def test_download_photo_drop_shots_and_upsert_preserves_qc_curations(tmp_path: Path) -> None:
    src_img = tmp_path / "lucia_official.png"
    Image.new("RGB", (900, 1200), (180, 90, 150)).save(src_img)

    item = PhotoFetchItem(
        id="ph_lucia",
        source_id="src_presskit",
        label="LUCIA CAMINOS",
        url=str(src_img),
        date="2025-05-06",
    )
    asset, src = download_official_photo(item, tmp_path / "proj" / "media" / "photos", project_root=tmp_path / "proj")
    assert asset.width_px == 900 and asset.height_px == 1200
    assert asset.image_path == "media/photos/ph_lucia.png"
    assert src.asset_type == "photo"

    # Dropping shots & verifying upsert_shot preserves dropped=True & face_zones on re-split!
    manifest = MediaManifest(
        project="demo",
        shots=[
            ShotClip(id="s01", source_id="t1", path="s01.mp4", duration_s=2.4, face_zones=["left"]),
            ShotClip(id="s02", source_id="t1", path="s02.mp4", duration_s=2.5),
        ],
    )
    dropped = drop_shots(manifest, ["s01"], reason="esrb_plate")
    assert dropped == ["s01"]
    assert manifest.shot_by_id("s01").dropped is True

    # Re-upserting s01 from a fresh media-split MUST keep dropped=True & face_zones=["left"]!
    manifest.upsert_shot(ShotClip(id="s01", source_id="t1", path="s01.mp4", duration_s=2.4))
    assert manifest.shot_by_id("s01").dropped is True
    assert manifest.shot_by_id("s01").drop_reason == "esrb_plate"
    assert manifest.shot_by_id("s01").face_zones == ["left"]


def test_cli_media_drop_and_photo(tmp_path: Path) -> None:
    src = Path(__file__).resolve().parent.parent / "examples" / "gta6-sample"
    dst = tmp_path / "sample_media_cli"
    shutil.copytree(src, dst)

    local_img = tmp_path / "vice_art.png"
    Image.new("RGB", (1920, 1080), (40, 20, 60)).save(local_img)

    assert cli_main([
        "media-photo", str(dst),
        "--id", "ph_new_still",
        "--url", str(local_img),
        "--label", "LEONIDA COAST",
    ]) == 0
    assert cli_main(["media-drop", str(dst), "--shots", "shot_01,shot_02", "--reason", "logo_plate"]) == 0

    m = MediaManifest.load(dst / "media" / "manifest.yaml")
    assert m.photo_by_id("ph_new_still") is not None
    assert m.shot_by_id("shot_01").dropped is True
    assert m.shot_by_id("shot_02").dropped is True


def test_lossless_overlay_frame_dedup_and_uri_resolution(tmp_path: Path) -> None:
    sample_dir = Path(__file__).resolve().parent.parent / "examples" / "gta6-sample"
    brief = ProjectBrief.load(sample_dir / "project.yaml")
    ledger = ResearchLedger.load(sample_dir / "research" / "facts.yaml")
    manifest = MediaManifest.load(sample_dir / "media" / "manifest.yaml")
    script = parse_script_text((sample_dir / "script" / "script.md").read_text(encoding="utf-8"))
    alignment = synthesize_synthetic_alignment(script, wpm=210.0)

    # Create a real local photo inside tmp_path so relative URI resolution converts to file://
    photo_file = tmp_path / "media" / "photos" / "vice.png"
    photo_file.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), (255, 147, 220)).save(photo_file)
    manifest.photo_by_id("ph_vice_city").image_path = "media/photos/vice.png"

    tl = compile_timeline(script, alignment, manifest, brief, ledger=ledger)
    ov_html = build_overlay_html(tl, tmp_path / "renders" / "overlay.html", project_dir=tmp_path)
    assert photo_file.resolve().as_uri() in ov_html.read_text(encoding="utf-8")

    # Verify lossless dedup classifies EMPTY frames, animating frames (None), and STATIC holds!
    keys = [compute_frame_render_key(tl, f) for f in range(tl.total_frames)]
    empty_count = sum(1 for k in keys if k == "EMPTY")
    static_count = sum(1 for k in keys if k and k.startswith("STATIC:"))
    anim_count = sum(1 for k in keys if k is None)
    assert empty_count > 100
    assert static_count > 100
    assert anim_count > 100
    # Over 40% of frames avoid redundant Chromium screenshots with zero visual loss!
    assert (empty_count + static_count) / tl.total_frames > 0.40


def test_execute_fetch_plan_prunes_stale_placeholders_when_real_clips_exist(
    tmp_path: Path, monkeypatch
) -> None:
    from vicecut.config import project_dirs
    from vicecut.media import execute_fetch_plan

    proj = tmp_path / "proj"
    dirs = project_dirs(proj)
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    # Start with a stale dry-run placeholder shot pointing to a non-existent file
    manifest = MediaManifest(
        project="proj",
        shots=[ShotClip(id="placeholder_01", source_id="old", path="media/scenes/missing.mp4", duration_s=2.5)],
    )
    fake_raw = tmp_path / "trailer.mp4"
    fake_raw.write_bytes(b"RAW")
    real_clip = dirs["media_scenes"] / "src_t1_s001.mp4"
    real_clip.write_bytes(b"CLIP")

    plan = FetchPlan(
        project="proj",
        videos=[VideoFetchItem(id="src_t1", title="Official", channel_or_outlet="Rockstar Games", url=str(fake_raw))],
    )

    def _fake_split(**kwargs):
        return (
            [ShotClip(id="src_t1_s001", source_id="src_t1", path="media/scenes/src_t1_s001.mp4", duration_s=2.4)],
            dirs["media_scenes"] / "contact_sheet_src_t1.png",
        )

    monkeypatch.setattr("vicecut.media.split_video_into_scenes", _fake_split)
    summary = execute_fetch_plan(plan, manifest, proj, dirs, skip_headlines=True)
    assert summary["shots_created"] == 1
    assert summary["placeholder_shots_pruned"] == 1
    assert [s.id for s in manifest.shots] == ["src_t1_s001"]


def test_github_actions_workflows_valid_yaml_and_structure() -> None:
    wf_dir = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    expected = ["ci.yml", "vicecut-preview.yml", "vicecut-final.yml"]
    for name in expected:
        p = wf_dir / name
        assert p.exists(), f"Missing workflow {name}"
        parsed = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert "jobs" in parsed and len(parsed["jobs"]) >= 1
        # PyYAML parses top-level `on:` as boolean True or string "on"
        triggers = parsed.get(True) or parsed.get("on")
        assert triggers is not None

    # Verify preview & final workflows expose 1-click workflow_dispatch & wipe runtime secrets
    for name in ("vicecut-preview.yml", "vicecut-final.yml"):
        raw = (wf_dir / name).read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw)
        triggers = parsed.get(True) or parsed.get("on")
        assert "workflow_dispatch" in triggers
        assert "project_dir" in triggers["workflow_dispatch"]["inputs"]
        assert "rm -rf .secrets" in raw
        assert "actions/upload-artifact@v4" in raw
