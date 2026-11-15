"""Official footage search, FFmpeg scene splitting, letterbox cropping, Start|Mid|End sheets & Playwright headline capture (<build> #5)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image, ImageDraw

from .config import VIDEO_HEIGHT, VIDEO_WIDTH


@dataclass
class MediaSourceRecord:
    """Provenance record for a raw video source, photo, or headline page."""

    id: str
    title: str
    channel_or_outlet: str
    page_url: str
    asset_url: str
    date: str
    asset_type: str  # "footage" | "still" | "photo" | "headline"
    local_path: str = ""
    license: str = "official-press-or-commentary-quote"
    is_official: bool = True
    is_mockup: bool = False
    age_gated: bool = False
    notes: str = ""


@dataclass
class ShotClip:
    """Single usable scene clip or official still in the edit pool."""

    id: str
    source_id: str
    path: str
    duration_s: float
    is_still: bool = False
    luma_mean: float = 0.5          # 0..1 average luminance for bright/dark alternation
    tags: list[str] = field(default_factory=list)  # semantic keywords for word pinning
    face_zones: list[str] = field(default_factory=list)  # "left" | "center" | "right"
    has_burned_text: bool = False
    dropped: bool = False
    drop_reason: str = ""           # e.g. "logo", "rating_board", "presents_plate", "burned_date", "graphic"
    crop_filter: str = ""           # e.g. "crop=1920:804:0:138"

    @property
    def is_bright(self) -> bool:
        return self.luma_mean >= 0.45


@dataclass
class HeadlineAsset:
    id: str
    source_id: str
    outlet: str
    headline_text: str
    date: str
    image_path: str = ""
    width_px: int = 0
    height_px: int = 0
    use_text_fallback: bool = False  # True if crop failed or width < 1/3 frame (<640px)
    is_mockup: bool = False


@dataclass
class PhotoAsset:
    id: str
    source_id: str
    label: str
    image_path: str
    width_px: int = 1280
    height_px: int = 720
    license: str = "official-press-kit"
    is_mockup: bool = False


@dataclass
class MediaManifest:
    project: str
    sources: list[MediaSourceRecord] = field(default_factory=list)
    shots: list[ShotClip] = field(default_factory=list)
    headlines: list[HeadlineAsset] = field(default_factory=list)
    photos: list[PhotoAsset] = field(default_factory=list)

    def usable_shots(self) -> list[ShotClip]:
        return [s for s in self.shots if not s.dropped]

    def shot_by_id(self, shot_id: str) -> ShotClip | None:
        return next((s for s in self.shots if s.id == shot_id), None)

    def photo_by_id(self, photo_id: str) -> PhotoAsset | None:
        return next((p for p in self.photos if p.id == photo_id), None)

    def headline_by_id(self, headline_id: str) -> HeadlineAsset | None:
        return next((h for h in self.headlines if h.id == headline_id), None)

    def source_by_id(self, source_id: str) -> MediaSourceRecord | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "project": self.project,
                    "sources": [asdict(s) for s in self.sources],
                    "shots": [asdict(s) for s in self.shots],
                    "headlines": [asdict(h) for h in self.headlines],
                    "photos": [asdict(p) for p in self.photos],
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "MediaManifest":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            project=str(raw.get("project", "")),
            sources=[MediaSourceRecord(**s) for s in raw.get("sources", [])],
            shots=[ShotClip(**s) for s in raw.get("shots", [])],
            headlines=[HeadlineAsset(**h) for h in raw.get("headlines", [])],
            photos=[PhotoAsset(**p) for p in raw.get("photos", [])],
        )


@dataclass
class ShotPoolBudget:
    duration_s: float
    target_unique_shots: int
    usable_motion_shots: int
    usable_still_shots: int
    max_allowed_stills: int
    shortfall_shots: int
    still_ratio_if_used: float
    ok: bool


def evaluate_shot_pool(manifest: MediaManifest, runtime_s: float) -> ShotPoolBudget:
    """
    Verify shot inventory against <build> #5:
    - Need ~25 distinct shots per minute (never show any shot twice!)
    - Official stills (2.8s each w/ 3% push-in) must stay strictly under 10% of runtime.
    """
    usable = manifest.usable_shots()
    motion = [s for s in usable if not s.is_still]
    stills = [s for s in usable if s.is_still]
    target_shots = max(1, round((runtime_s / 60.0) * 25.0))
    max_still_seconds = runtime_s * 0.095  # keep strictly < 10%
    max_stills = int(max_still_seconds // 2.8)
    usable_stills_capped = min(len(stills), max_stills)
    total_available = len(motion) + usable_stills_capped
    shortfall = max(0, target_shots - total_available)
    still_ratio = (usable_stills_capped * 2.8) / max(runtime_s, 1.0)
    return ShotPoolBudget(
        duration_s=round(runtime_s, 1),
        target_unique_shots=target_shots,
        usable_motion_shots=len(motion),
        usable_still_shots=len(stills),
        max_allowed_stills=max_stills,
        shortfall_shots=shortfall,
        still_ratio_if_used=round(still_ratio, 4),
        ok=(shortfall == 0 and still_ratio < 0.10),
    )


def search_official_candidates(
    query: str,
    allowed_channels: list[str] | None = None,
    blocked_sources: list[str] | None = None,
    max_results: int = 8,
) -> list[dict[str, Any]]:
    """
    List yt-dlp search results FIRST so the operator can verify official channel,
    upload date, and age-gate status before downloading (never watermarked re-uploads).
    """
    proc = subprocess.run(
        ["yt-dlp", "--dump-json", "--flat-playlist", f"ytsearch{max_results}:{query}"],
        capture_output=True,
        text=True,
        check=True,
    )
    allowed_l = [c.lower() for c in (allowed_channels or [])]
    blocked_l = [b.lower() for b in (blocked_sources or [])]
    results: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        info = json.loads(line)
        channel = str(info.get("channel") or info.get("uploader") or "")
        url = str(info.get("webpage_url") or info.get("url") or "")
        if any(b in channel.lower() or b in url.lower() for b in blocked_l):
            continue
        official_match = any(a in channel.lower() for a in allowed_l) if allowed_l else False
        results.append(
            {
                "title": info.get("title", ""),
                "channel": channel,
                "url": url,
                "duration_s": info.get("duration"),
                "upload_date": info.get("upload_date", ""),
                "age_limit": info.get("age_limit", 0),
                "is_official_channel": official_match,
                "warning": "AGE-GATED (requires login — prefer open trailer/site)" if (info.get("age_limit") or 0) >= 18 else "",
            }
        )
    # Sort official publisher uploads first
    results.sort(key=lambda r: (not r["is_official_channel"], r["age_limit"] >= 18))
    return results


def detect_letterbox_crop(video_path: Path) -> str:
    """Detect black letterbox bars via FFmpeg cropdetect and return clean crop=W:H:X:Y string (or '')."""
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(video_path),
            "-vf", "cropdetect=limit=24:round=2:reset=0",
            "-frames:v", "120", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    crops = re.findall(r"crop=(\d+:\d+:\d+:\d+)", proc.stderr)
    if not crops:
        return ""
    # Pick modal crop rectangle
    modal = max(set(crops), key=crops.count)
    w, h, x, y = (int(v) for v in modal.split(":"))
    if x == 0 and y == 0:
        return ""
    return f"crop={w}:{h}:{x}:{y}"


def compute_image_luminance(image_path: Path) -> float:
    """Compute perceptual relative luminance (0..1) of a thumbnail for bright/dark alternation."""
    img = Image.open(image_path).convert("RGB")
    img.thumbnail((96, 54))
    arr = np.asarray(img, dtype=np.float32) / 255.0
    luma = 0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]
    return round(float(luma.mean()), 3)


def build_scene_contact_sheet(
    scene_triplets: list[tuple[str, Path, Path, Path, str]],
    output_png: Path,
    thumb_size: tuple[int, int] = (240, 135),
) -> Path:
    """
    Build a numbered Start | Mid | End visual inspection sheet (<build> #5 + <gotchas>).
    Showing first, middle, and last frame catches missed cuts inside a scene, logos,
    ESRB boards, 'presents' plates, burned-in dates, and awkward cut frames!
    Each tuple: (shot_id, first_frame_png, mid_frame_png, last_frame_png, label_text)
    """
    tw, th = thumb_size
    pad = 12
    label_h = 26
    row_w = 3 * tw + 4 * pad
    row_h = th + label_h + pad
    cols = 2
    rows = max(1, (len(scene_triplets) + cols - 1) // cols)

    sheet = Image.new("RGB", (cols * row_w + pad, rows * row_h + pad), (24, 16, 28))
    draw = ImageDraw.Draw(sheet)

    for idx, (shot_id, p_start, p_mid, p_end, meta) in enumerate(scene_triplets):
        r, c = divmod(idx, cols)
        ox = pad + c * row_w
        oy = pad + r * row_h

        draw.rectangle([ox - 2, oy - 2, ox + row_w - pad + 2, oy + row_h - 6], outline=(247, 211, 234), width=1)
        draw.text((ox + 6, oy + 4), f"#{idx+1:02d} {shot_id}  {meta}   [FIRST | MID | LAST]", fill=(255, 241, 222))

        for k, frame_p in enumerate((p_start, p_mid, p_end)):
            fx = ox + pad + k * (tw + pad // 2)
            fy = oy + label_h
            if frame_p.exists():
                im = Image.open(frame_p).convert("RGB").resize((tw, th), Image.Resampling.LANCZOS)
                sheet.paste(im, (fx, fy))
            else:
                draw.rectangle([fx, fy, fx + tw, fy + th], fill=(50, 35, 58))
                draw.text((fx + 10, fy + th // 2 - 6), "missing", fill=(255, 147, 220))

    output_png.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_png)
    return output_png


def check_headline_needs_fallback(image_width_px: int, frame_width: int = VIDEO_WIDTH) -> bool:
    """
    Per <gotchas>: 'A screenshot of a short headline comes out as a small dark strip.
    If a headline card is under a third of the frame wide, set it as text on white instead.'
    """
    return image_width_px < (frame_width / 3.0)


def capture_headline_with_playwright(
    url: str,
    selector: str,
    output_png: Path,
    outlet: str,
    headline_text: str,
    pub_date: str,
    headline_id: str,
    source_id: str,
) -> HeadlineAsset:
    """Screenshot an article's headline element on its real page background; auto-flag fallback if <1/3 frame wide."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Playwright not installed. Run `pip install playwright && playwright install chromium`.") from exc

    output_png.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        loc = page.locator(selector).first
        box = loc.bounding_box()
        if not box or box["width"] < 200 or box["height"] < 30:
            browser.close()
            return HeadlineAsset(
                id=headline_id,
                source_id=source_id,
                outlet=outlet,
                headline_text=headline_text,
                date=pub_date,
                use_text_fallback=True,
            )
        loc.screenshot(path=str(output_png))
        browser.close()

    with Image.open(output_png) as im:
        w, h = im.size
    use_fallback = check_headline_needs_fallback(w)
    return HeadlineAsset(
        id=headline_id,
        source_id=source_id,
        outlet=outlet,
        headline_text=headline_text,
        date=pub_date,
        image_path=str(output_png),
        width_px=w,
        height_px=h,
        use_text_fallback=use_fallback,
    )
