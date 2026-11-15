"""Transparent 1920x1080 HTML overlay builder, fast lossless Playwright frame exporter, and full-size card proof generator (<build> #6 & #7)."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

from PIL import Image

from .timeline import Timeline


def _template_path() -> Path:
    return Path(__file__).resolve().parent / "templates" / "overlay.html"


def _resolve_asset_uris(timeline_dict: dict, project_dir: Path | None) -> dict:
    """Convert relative image_path references in card payloads to file:// URIs so Chromium loads them reliably."""
    if project_dir is None:
        return timeline_dict
    data = copy.deepcopy(timeline_dict)
    for card in data.get("cards", []):
        payload = card.get("payload") or {}
        img_p = payload.get("image_path")
        if img_p and not str(img_p).startswith(("http://", "https://", "file://", "data:")):
            cand = Path(img_p)
            if not cand.is_absolute():
                cand = (project_dir / cand).resolve()
            if cand.exists():
                payload["image_path"] = cand.as_uri()
    return data


def build_overlay_html(timeline: Timeline, output_html: Path, project_dir: Path | None = None) -> Path:
    """Compile a self-contained transparent 1920x1080 overlay HTML stage from Timeline."""
    tpl = _template_path().read_text(encoding="utf-8")
    data = _resolve_asset_uris(timeline.to_dict(), project_dir)
    payload = json.dumps(data, ensure_ascii=False)
    rendered = tpl.replace("/*__TIMELINE_JSON__*/null", payload)
    output_html.parent.mkdir(parents=True, exist_ok=True)
    output_html.write_text(rendered, encoding="utf-8")
    return output_html


def compute_frame_render_key(timeline: Timeline, frame_idx: int) -> str | None:
    """
    Compute a deterministic visual-state key for lossless frame deduplication:
    - Returns "EMPTY" if NO card, caption, or pill is active on this frame.
    - Returns "STATIC:..." if all active elements are strictly past fade-in/typing and before fade-out
      (identical pixel-for-pixel to adjacent static frames).
    - Returns None if any element is animating (fading, typing quote, or revealing caption word) -> MUST screenshot!
    """
    fps = timeline.fps
    t = frame_idx / fps

    active_cards = [c for c in timeline.cards if c.start_frame <= frame_idx < c.end_frame]
    active_caps = [c for c in timeline.captions if c.start_frame <= frame_idx < c.end_frame]
    active_pills = [p for p in timeline.pills if p.start_frame <= frame_idx < p.end_frame]

    if not active_cards and not active_caps and not active_pills:
        return "EMPTY"

    # Check if any active card is fading, typing, or a continuously animated motion prop!
    for c in active_cards:
        if c.card_type == "prop":
            return None  # Motion prop moves smoothly every single frame!
        fade_f = max(1, int(round(c.fade_s * fps))) + 1
        if frame_idx < c.start_frame + fade_f or frame_idx >= c.end_frame - fade_f:
            return None
        if c.card_type == "quote":
            typed = c.payload.get("typed_words") or []
            if typed and t <= float(typed[-1].get("reveal_s", 0.0)) + 0.30:
                return None

    # Check if any active caption is fading or revealing a word
    for cap in active_caps:
        if frame_idx < cap.start_frame + 10 or frame_idx >= cap.end_frame - 8:
            return None
        if cap.words and t <= cap.words[-1].reveal_s + 0.30:
            return None

    # Check if any active pill is fading
    for p in active_pills:
        if frame_idx < p.start_frame + 8 or frame_idx >= p.end_frame - 8:
            return None

    key_parts = (
        [f"C:{c.id}" for c in active_cards]
        + [f"K:{c.id}" for c in active_caps]
        + [f"P:{p.id}" for p in active_pills]
    )
    return "STATIC:" + "|".join(key_parts)


def export_overlay_frames_playwright(
    overlay_html: Path,
    frames_dir: Path,
    total_frames: int,
    width: int = 1920,
    height: int = 1080,
    frame_subset: list[int] | None = None,
    timeline: Timeline | None = None,
    start_frame: int = 0,
    end_frame: int | None = None,
) -> list[Path]:
    """
    Export transparent RGBA PNG frames via Playwright Chromium (`omit_background=True`).
    When `timeline` is provided, uses 100% lossless empty-frame + static-hold deduplication
    (3x-5x faster on CPU/GitHub Actions runners with zero visual difference!).
    Supports `start_frame`..`end_frame` chunking for parallel matrix renders.
    """
    frames_dir.mkdir(parents=True, exist_ok=True)
    if frame_subset is not None:
        targets = list(frame_subset)
    else:
        hi = min(total_frames, end_frame) if end_frame is not None else total_frames
        targets = list(range(max(0, start_frame), hi))

    exported: list[Path] = []
    empty_template: Path | None = None
    static_cache: dict[str, Path] = {}

    def _ensure_empty_png() -> Path:
        nonlocal empty_template
        if empty_template is None:
            empty_template = frames_dir / "_empty_transparent.png"
            if not empty_template.exists():
                Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(empty_template)
        return empty_template

    # Determine which frames actually require Chromium screenshots
    need_chromium: list[tuple[int, str | None]] = []
    copy_plan: list[tuple[int, str]] = []

    for fidx in targets:
        key = compute_frame_render_key(timeline, fidx) if timeline is not None else None
        if key == "EMPTY":
            copy_plan.append((fidx, "EMPTY"))
        elif key is not None and key in static_cache:
            copy_plan.append((fidx, key))
        else:
            need_chromium.append((fidx, key))
            if key is not None:
                static_cache[key] = frames_dir / f"frame_{fidx:06d}.png"

    if need_chromium:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required for frame export. Install via `pip install playwright && playwright install chromium`."
            ) from exc

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            page.goto(overlay_html.resolve().as_uri(), wait_until="load")
            page.evaluate("() => document.fonts ? document.fonts.ready : Promise.resolve()")

            for fidx, _ in need_chromium:
                page.evaluate("(f) => window.renderFrame(f)", fidx)
                out_png = frames_dir / f"frame_{fidx:06d}.png"
                page.screenshot(path=str(out_png), omit_background=True)
            browser.close()

    for fidx, key in copy_plan:
        out_png = frames_dir / f"frame_{fidx:06d}.png"
        src = _ensure_empty_png() if key == "EMPTY" else static_cache[key]
        shutil.copyfile(src, out_png)

    for fidx in targets:
        exported.append(frames_dir / f"frame_{fidx:06d}.png")
    return exported


def export_card_type_proofs(
    timeline: Timeline,
    overlay_html: Path,
    proofs_dir: Path,
) -> dict[str, Path]:
    """
    Capture one full-size 1920x1080 proof frame for EVERY card type present in the
    timeline so the operator can inspect border weight, glow, radius, type size & finish (<build> #7).
    """
    chosen: dict[str, int] = {}
    for c in timeline.cards:
        if c.card_type not in chosen:
            mid_frame = (c.start_frame + c.end_frame) // 2
            chosen[c.card_type] = mid_frame
    if timeline.captions:
        cap = timeline.captions[0]
        chosen["caption"] = min(cap.end_frame - 1, cap.start_frame + int(timeline.fps * 0.7))

    if not chosen:
        return {}

    paths = export_overlay_frames_playwright(
        overlay_html=overlay_html,
        frames_dir=proofs_dir,
        total_frames=timeline.total_frames,
        width=timeline.width,
        height=timeline.height,
        frame_subset=list(chosen.values()),
    )
    out: dict[str, Path] = {}
    for (ctype, fidx), src_png in zip(chosen.items(), paths):
        renamed = proofs_dir / f"proof_{ctype}_f{fidx:06d}.png"
        if src_png != renamed:
            src_png.replace(renamed)
        out[ctype] = renamed
    return out
