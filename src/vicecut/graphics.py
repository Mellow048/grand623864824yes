"""Transparent 1920x1080 HTML overlay builder, Playwright frame exporter, and full-size card proof generator (<build> #6 & #7)."""

from __future__ import annotations

import json
from pathlib import Path

from .timeline import Timeline


def _template_path() -> Path:
    return Path(__file__).resolve().parent / "templates" / "overlay.html"


def build_overlay_html(timeline: Timeline, output_html: Path) -> Path:
    """Compile a self-contained transparent 1920x1080 overlay HTML stage from Timeline."""
    tpl = _template_path().read_text(encoding="utf-8")
    payload = json.dumps(timeline.to_dict(), ensure_ascii=False)
    rendered = tpl.replace("/*__TIMELINE_JSON__*/null", payload)
    output_html.parent.mkdir(parents=True, exist_ok=True)
    output_html.write_text(rendered, encoding="utf-8")
    return output_html


def export_overlay_frames_playwright(
    overlay_html: Path,
    frames_dir: Path,
    total_frames: int,
    width: int = 1920,
    height: int = 1080,
    frame_subset: list[int] | None = None,
) -> list[Path]:
    """
    Export transparent RGBA PNG frames via Playwright Chromium (`omit_background=True`).
    Note per <gotchas>: 9,000 frames (~5 min @ 30fps) takes ~20 minutes — render a
    960x540 preview or specific card-proof frames first!
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required for frame export. Install via `pip install playwright && playwright install chromium`."
        ) from exc

    frames_dir.mkdir(parents=True, exist_ok=True)
    targets = frame_subset if frame_subset is not None else list(range(total_frames))
    exported: list[Path] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
        page.goto(overlay_html.resolve().as_uri(), wait_until="load")
        page.evaluate("() => document.fonts ? document.fonts.ready : Promise.resolve()")

        for fidx in targets:
            page.evaluate("(f) => window.renderFrame(f)", fidx)
            out_png = frames_dir / f"frame_{fidx:06d}.png"
            page.screenshot(path=str(out_png), omit_background=True)
            exported.append(out_png)
        browser.close()

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
            # Sample right at midpoint (full opacity, well past 0.25s fade-in)
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
