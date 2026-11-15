"""Pre-render storyboard sheet, FFmpeg hard-cut & frame-exact blur compositor, 540p preview + 4fps QC sheets, 1080p master & <25 MB mobile export (<build> #7)."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw

from .config import MOBILE_MAX_MB, PREVIEW_HEIGHT, PREVIEW_WIDTH, VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH
from .timeline import BlurInterval, ShotEvent, Timeline


def build_pre_render_storyboard_sheet(
    timeline: Timeline,
    output_png: Path,
    thumbnails_dir: Path | None = None,
    thumb_size: tuple[int, int] = (320, 180),
) -> Path:
    """
    Draw EVERY shot in film order with its overlaid caption & card text printed directly
    on the tile BEFORE any video render so mismatched captions/shots are caught immediately (<build> #7).
    """
    tw, th = thumb_size
    cols = 4
    pad = 14
    header_h = 54
    caption_bar_h = 58
    cell_w = tw + pad
    cell_h = th + caption_bar_h + pad
    rows = max(1, (len(timeline.shots) + cols - 1) // cols)

    canvas = Image.new("RGB", (cols * cell_w + pad, header_h + rows * cell_h + pad), (24, 15, 29))
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (pad, 16),
        f"PRE-RENDER STORYBOARD AUDIT -- {timeline.project_slug} ({len(timeline.shots)} shots, {timeline.duration_s:.1f}s) -- CHECK EVERY CAPTION AGAINST ITS PICTURE!",
        fill=(255, 178, 124),
    )

    for idx, shot in enumerate(timeline.shots):
        r, c = divmod(idx, cols)
        x = pad + c * cell_w
        y = header_h + r * cell_h

        # Draw shot thumbnail if present (supports split_video_into_scenes thumbs/ layout), else luminance-coded placeholder
        thumb = None
        if thumbnails_dir:
            for cand in (
                thumbnails_dir / "thumbs" / f"{shot.shot_id}_mid.png",
                thumbnails_dir / f"{shot.shot_id}_mid.png",
                thumbnails_dir / f"{shot.shot_id}.png",
            ):
                if cand.exists():
                    thumb = cand
                    break
        if thumb is not None:
            im = Image.open(thumb).convert("RGB").resize((tw, th), Image.Resampling.LANCZOS)
            canvas.paste(im, (x, y))
        else:
            bg_val = int(42 + shot.luma_mean * 110)
            tint = (min(255, bg_val + 25), bg_val, min(255, bg_val + 35))
            draw.rectangle([x, y, x + tw, y + th], fill=tint)
            kind = "STILL (3% PUSH)" if shot.is_still else ("BRIGHT" if shot.is_bright else "DARK")
            draw.text((x + 12, y + 12), f"{shot.shot_id} [{kind}]", fill=(255, 241, 222))
            if shot.face_zones:
                draw.text((x + 12, y + 32), f"FACES: {','.join(shot.face_zones)}", fill=(255, 147, 220))

        # Active cards & captions overlapping this shot
        shot_cards = [
            f"[{cd.card_type.upper()}] {cd.payload.get('label') or cd.payload.get('text') or cd.payload.get('figure') or cd.payload.get('name') or cd.payload.get('outlet') or ''}"
            for cd in timeline.cards
            if not (cd.end_s <= shot.start_s or cd.start_s >= shot.end_s)
        ]
        shot_caps = [
            " ".join(w.text for w in cp.words)
            for cp in timeline.captions
            if not (cp.end_s <= shot.start_s or cp.start_s >= shot.end_s)
        ]

        # Print caption band directly across bottom of thumbnail (like in the film!)
        if shot_caps:
            draw.rectangle([x, y + th - 36, x + tw, y + th], fill=(30, 19, 34))
            draw.text((x + 8, y + th - 26), f"CAP: {' / '.join(shot_caps)[:38]}", fill=(255, 147, 220))

        # Border & metadata bar underneath
        border_col = (255, 120, 205) if shot_cards else (160, 130, 175)
        draw.rectangle([x, y, x + tw, y + th], outline=border_col, width=2)
        meta1 = f"#{idx+1:02d} {shot.start_s:.1f}s-{shot.end_s:.1f}s ({shot.duration_s:.2f}s)"
        meta2 = textwrap.shorten(" | ".join(shot_cards) if shot_cards else "bare footage", width=44, placeholder="...")
        draw.text((x + 4, y + th + 6), meta1, fill=(255, 241, 222))
        draw.text((x + 4, y + th + 24), meta2, fill=(255, 186, 144) if shot_cards else (175, 165, 185))

    output_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_png)
    return output_png


def _build_blur_darken_chain(intervals: list[BlurInterval], width: int, height: int, is_preview: bool) -> str:
    """
    Build exact frame-gated FFmpeg blur (26px at 1080p, 13px at 540p) + darken overlay
    switched ON and OFF on the EXACT frame each card starts and ends (zero flicker!).
    """
    if not intervals:
        return "[vcuts]null[vbg]"

    sigma = 13 if is_preview else 26
    enable_terms = [f"between(n,{b.start_frame},{max(b.start_frame, b.end_frame - 1)})" for b in intervals if b.blur_px > 0]
    chain_parts: list[str] = []
    cur = "vcuts"
    if enable_terms:
        expr = "+".join(enable_terms)
        chain_parts.append(f"[{cur}]gblur=sigma={sigma}:enable='{expr}'[vblur]")
        cur = "vblur"

    # Per-alpha darken drawbox filters (exact darkening: 42% photo, 50% headline, 55% quote, 60% chapter)
    by_alpha: dict[float, list[BlurInterval]] = {}
    for b in intervals:
        if b.darken_alpha > 0.0:
            by_alpha.setdefault(round(b.darken_alpha, 2), []).append(b)

    for idx, (alpha, group) in enumerate(sorted(by_alpha.items())):
        expr = "+".join(f"between(n,{b.start_frame},{max(b.start_frame, b.end_frame - 1)})" for b in group)
        nxt = f"vdk{idx}"
        chain_parts.append(
            f"[{cur}]drawbox=x=0:y=0:w={width}:h={height}:color=black@{alpha:.2f}:t=fill:enable='{expr}'[{nxt}]"
        )
        cur = nxt

    chain_parts.append(f"[{cur}]null[vbg]")
    return ";".join(chain_parts)


def render_background_cuts_track(
    timeline: Timeline,
    output_bg_mp4: Path,
    preview: bool = False,
    project_dir: Path | None = None,
) -> Path:
    """
    Pre-concatenate all hard-cut background shots sequentially (1 decoder at a time = <100 MB RAM)
    so 100+ shot videos never OOM FFmpeg on 7 GB cloud runners!
    """
    w = PREVIEW_WIDTH if preview else VIDEO_WIDTH
    h = PREVIEW_HEIGHT if preview else VIDEO_HEIGHT
    fps = timeline.fps

    seg_dir = output_bg_mp4.parent / f".bg_segs_{'prev' if preview else 'final'}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    seg_paths: list[Path] = []

    # Collect valid real footage paths so if unique clips exhaust on long cloud renders
    # (e.g. without YouTube cookies), we NEVER render minutes of blank black screens —
    # we pull from earliest-used clips (max temporal distance) with a subtle 1.08x push-in!
    valid_real_shots: list[tuple[str, str]] = []
    for sh in timeline.shots:
        sp = sh.path
        if project_dir is not None and sp and not Path(sp).is_absolute():
            sp = str((project_dir / sp).resolve())
        if sp and Path(sp).exists() and not sh.is_still:
            valid_real_shots.append((sp, sh.crop_filter))
    recycle_idx = 0

    for idx, shot in enumerate(timeline.shots):
        dur = max(1 / fps, shot.duration_s)
        n_frames = max(1, shot.end_frame - shot.start_frame)
        seg_out = seg_dir / f"seg_{idx:04d}.mp4"
        shot_input_path = shot.path
        crop_f = shot.crop_filter
        recycled = False
        if project_dir is not None and shot_input_path and not Path(shot_input_path).is_absolute():
            shot_input_path = str((project_dir / shot_input_path).resolve())

        if (not shot_input_path or not Path(shot_input_path).exists()) and valid_real_shots:
            shot_input_path, crop_f = valid_real_shots[recycle_idx % len(valid_real_shots)]
            recycle_idx += 1
            recycled = True

        if not shot_input_path or not Path(shot_input_path).exists():
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c=#120B17:s={w}x{h}:r={fps}:d={dur:.4f}",
                "-frames:v", str(n_frames),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p",
                str(seg_out),
            ]
        elif shot.is_still and not recycled:
            zoom_step = 0.03 / (2.5 * fps)
            vf = (
                f"scale={w*2}:{h*2}:force_original_aspect_ratio=increase,"
                f"crop={w*2}:{h*2},"
                f"zoompan=z='min(1.03,1+{zoom_step:.6f}*on)':d={n_frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps},"
                f"setsar=1,format=yuv420p"
            )
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-loop", "1", "-t", f"{dur:.4f}", "-i", shot_input_path,
                "-vf", vf, "-frames:v", str(n_frames),
                "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p",
                str(seg_out),
            ]
        else:
            crop_prefix = f"{crop_f}," if crop_f else ""
            if recycled:
                sw, sh_px = int(w * 1.08), int(h * 1.08)
                vf = (
                    f"{crop_prefix}scale={sw}:{sh_px}:force_original_aspect_ratio=increase,"
                    f"crop={w}:{h},fps={fps},setsar=1,format=yuv420p"
                )
            else:
                vf = (
                    f"{crop_prefix}scale={w}:{h}:force_original_aspect_ratio=increase,"
                    f"crop={w}:{h},fps={fps},setsar=1,format=yuv420p"
                )
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-stream_loop", "-1", "-i", shot_input_path,
                "-vf", vf, "-frames:v", str(n_frames),
                "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p",
                str(seg_out),
            ]
        subprocess.run(cmd, check=True)
        seg_paths.append(seg_out)

    list_txt = seg_dir / "concat.txt"
    list_txt.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in seg_paths) + "\n",
        encoding="utf-8",
    )
    output_bg_mp4.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_txt),
            "-c", "copy", str(output_bg_mp4),
        ],
        check=True,
    )
    for p in seg_paths:
        p.unlink(missing_ok=True)
    list_txt.unlink(missing_ok=True)
    return output_bg_mp4


def build_ffmpeg_composite_command(
    timeline: Timeline,
    overlay_frames_dir: Path,
    master_audio_wav: Path,
    output_mp4: Path,
    preview: bool = False,
    music_path: Path | None = None,
    project_dir: Path | None = None,
    preconcat_bg_mp4: Path | None = None,
) -> list[str]:
    """
    Compile the complete FFmpeg hard-cut + frame-exact blur + transparent overlay command.
    Strictly hard cuts only (no dissolves!), zero SFX on cuts/text, optional barely-audible music.
    """
    w = PREVIEW_WIDTH if preview else VIDEO_WIDTH
    h = PREVIEW_HEIGHT if preview else VIDEO_HEIGHT
    fps = timeline.fps

    inputs: list[str] = []
    filter_parts: list[str] = []
    concat_labels: list[str] = []

    if preconcat_bg_mp4 is not None:
        inputs += ["-i", str(preconcat_bg_mp4)]
        filter_parts.append(f"[0:v]fps={fps},setsar=1,format=yuv420p[vcuts]")
        ov_idx = 1
    else:
        for idx, shot in enumerate(timeline.shots):
            dur = max(1 / fps, shot.duration_s)
            n_frames = max(1, shot.end_frame - shot.start_frame)
            shot_input_path = shot.path
            if project_dir is not None and shot_input_path and not Path(shot_input_path).is_absolute():
                shot_input_path = str((project_dir / shot_input_path).resolve())
            if shot.is_still:
                inputs += ["-loop", "1", "-t", f"{dur:.4f}", "-i", shot_input_path]
                # Official still: 3% push-in over 2.5s (1.0 -> 1.03)
                zoom_step = 0.03 / (2.5 * fps)
                vf = (
                    f"[{idx}:v]scale={w*2}:{h*2}:force_original_aspect_ratio=increase,"
                    f"crop={w*2}:{h*2},"
                    f"zoompan=z='min(1.03,1+{zoom_step:.6f}*on)':d={n_frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps},"
                    f"setsar=1,format=yuv420p[s{idx}]"
                )
            else:
                inputs += ["-i", shot_input_path]
                crop_prefix = f"{shot.crop_filter}," if shot.crop_filter else ""
                vf = (
                    f"[{idx}:v]trim=start=0:end={dur:.4f},setpts=PTS-STARTPTS,"
                    f"{crop_prefix}scale={w}:{h}:force_original_aspect_ratio=increase,"
                    f"crop={w}:{h},fps={fps},setsar=1,format=yuv420p[s{idx}]"
                )
            filter_parts.append(vf)
            concat_labels.append(f"[s{idx}]")
        ov_idx = len(timeline.shots)

    # Overlay PNG sequence input
    inputs += ["-framerate", str(fps), "-i", str(overlay_frames_dir / "frame_%06d.png")]

    # Mastered voice audio input
    aud_idx = ov_idx + 1
    inputs += ["-i", str(master_audio_wav)]

    # Optional barely-audible music bed (-34 dB)
    mus_idx = None
    if music_path is not None and music_path.exists():
        mus_idx = aud_idx + 1
        inputs += ["-stream_loop", "-1", "-i", str(music_path)]

    # 1. Hard-cut concat (zero dissolves!)
    if preconcat_bg_mp4 is None:
        filter_parts.append(f"{''.join(concat_labels)}concat=n={len(concat_labels)}:v=1:a=0[vcuts]")

    # 2. Exact frame-gated 26px blur + card darkening
    filter_parts.append(_build_blur_darken_chain(timeline.blur_intervals, w, h, is_preview=preview))

    # 3. Scale transparent Playwright overlay if preview & composite over blurred footage
    if preview:
        filter_parts.append(f"[{ov_idx}:v]scale={w}:{h}:flags=lanczos[vov]")
        filter_parts.append("[vbg][vov]overlay=0:0:shortest=1:format=auto[vout]")
    else:
        filter_parts.append(f"[vbg][{ov_idx}:v]overlay=0:0:shortest=1:format=auto[vout]")

    # 4. Audio mix (voice mastered at -18 LUFS; optional subtle bed at -34 dB)
    if mus_idx is not None:
        filter_parts.append(f"[{mus_idx}:a]volume=-34dB[mlow];[{aud_idx}:a][mlow]amix=inputs=2:duration=first:normalize=0[aout]")
        audio_map = "[aout]"
    else:
        audio_map = f"{aud_idx}:a"

    crf = "23" if preview else "18"
    preset = "veryfast" if preview else "medium"
    cmd = [
        "ffmpeg", "-y", "-hide_banner",
        *inputs,
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]", "-map", audio_map,
        "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_mp4),
    ]
    return cmd


def compute_mobile_video_bitrate_kbps(duration_s: float, max_mb: float = MOBILE_MAX_MB, audio_kbps: int = 128) -> int:
    """Compute target H.264 bitrate guaranteed to keep the mobile copy strictly under 25 MB."""
    # Leave 1.5 MB headroom for MP4 muxing overhead & bitrate variance
    budget_bits = max(1.0, (max_mb - 1.5)) * 8.0 * 1024.0 * 1024.0
    total_kbps = (budget_bits / max(duration_s, 1.0)) / 1000.0
    return max(350, min(6000, int(total_kbps - audio_kbps)))


def build_mobile_export_command(master_1080p: Path, mobile_mp4: Path, duration_s: float) -> list[str]:
    """Generate FFmpeg command for a crisp phone delivery copy strictly under 25 MB (<build> #7)."""
    v_kbps = compute_mobile_video_bitrate_kbps(duration_s, max_mb=MOBILE_MAX_MB - 1.0)
    return [
        "ffmpeg", "-y", "-hide_banner", "-i", str(master_1080p),
        "-vf", "scale=1280:720:flags=lanczos",
        "-c:v", "libx264", "-preset", "medium",
        "-b:v", f"{v_kbps}k", "-maxrate", f"{int(v_kbps * 1.15)}k", "-bufsize", f"{v_kbps * 2}k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(mobile_mp4),
    ]


def extract_preview_4fps_sheets(
    preview_mp4: Path,
    duration_s: float,
    output_dir: Path,
) -> list[Path]:
    """
    Pull 4 frames/second from the first 60s and two 30s windows later in the preview,
    assembling contact sheets for visual look-and-fix QC rounds (<build> #7).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    windows: list[tuple[str, float, float]] = [("win1_first_60s", 0.0, min(60.0, duration_s))]
    if duration_s > 90.0:
        w2_start = round(duration_s * 0.42, 1)
        w3_start = round(max(w2_start + 35.0, duration_s - 35.0), 1)
        windows.append(("win2_mid_30s", w2_start, min(30.0, duration_s - w2_start)))
        if w3_start < duration_s - 5.0:
            windows.append(("win3_late_30s", w3_start, min(30.0, duration_s - w3_start)))

    sheets: list[Path] = []
    for label, start_s, span_s in windows:
        out_png = output_dir / f"qc_4fps_{label}.png"
        n_frames = max(4, int(span_s * 4))
        cols = 8
        rows = (n_frames + cols - 1) // cols
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner",
                "-ss", f"{start_s:.2f}", "-t", f"{span_s:.2f}",
                "-i", str(preview_mp4),
                "-vf", f"fps=4,scale=240:135,tile={cols}x{rows}",
                "-frames:v", "1", str(out_png),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        sheets.append(out_png)
    return sheets
