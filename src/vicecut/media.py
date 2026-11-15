"""Official footage search, FFmpeg scene splitting, letterbox cropping, Start|Mid|End sheets & Playwright headline capture (<build> #5)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

    def upsert_source(self, rec: MediaSourceRecord) -> None:
        for i, existing in enumerate(self.sources):
            if existing.id == rec.id:
                self.sources[i] = rec
                return
        self.sources.append(rec)

    def upsert_shot(self, shot: ShotClip) -> None:
        for i, existing in enumerate(self.shots):
            if existing.id == shot.id:
                # Preserve human-curated drop flags/face_zones/tags if re-splitting
                if existing.dropped and not shot.dropped:
                    shot.dropped = existing.dropped
                    shot.drop_reason = existing.drop_reason
                if existing.face_zones and not shot.face_zones:
                    shot.face_zones = list(existing.face_zones)
                if existing.tags and not shot.tags:
                    shot.tags = list(existing.tags)
                self.shots[i] = shot
                return
        self.shots.append(shot)

    def upsert_headline(self, hl: HeadlineAsset) -> None:
        for i, existing in enumerate(self.headlines):
            if existing.id == hl.id:
                self.headlines[i] = hl
                return
        self.headlines.append(hl)

    def upsert_photo(self, photo: PhotoAsset) -> None:
        for i, existing in enumerate(self.photos):
            if existing.id == photo.id:
                self.photos[i] = photo
                return
        self.photos.append(photo)

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


# ---------------------------------------------------------------------------
# Declarative Media Acquisition Engine (Arena Agent -> GitHub Actions / Local)
# ---------------------------------------------------------------------------


@dataclass
class VideoFetchItem:
    """Single approved official video source to download and auto-slice into shots."""

    id: str
    title: str
    channel_or_outlet: str
    url: str
    fallback_url: str = ""
    page_url: str = ""
    date: str = ""
    license: str = "official-trailer-commentary"
    is_official: bool = True
    scene_threshold: float = 0.28
    min_clip_s: float = 1.6
    max_clip_s: float = 3.5
    drop_first_s: float = 2.5   # skips ESRB/PEGI rating card at start of official trailers
    drop_last_s: float = 4.0    # skips publisher logo/date endplate
    max_clips: int = 90         # cap evenly across long gameplay videos so splitting stays fast
    tags: list[str] = field(default_factory=list)


@dataclass
class HeadlineFetchItem:
    """Single article headline to screenshot with Playwright (auto-falling back to white card)."""

    id: str
    source_id: str
    outlet: str
    headline_text: str
    url: str
    date: str = ""
    selector: str = "h1"


@dataclass
class PhotoFetchItem:
    """Single official press-kit still/photo to download for Photo or Character cards."""

    id: str
    source_id: str
    label: str
    url: str
    page_url: str = ""
    date: str = ""
    license: str = "official-press-kit"
    is_mockup: bool = False


@dataclass
class MusicFetchItem:
    """Single non-copyrighted / CC0 / Public Domain background music track."""

    id: str
    title: str
    artist: str
    url: str
    fallback_url: str = ""
    page_url: str = ""
    license: str = "CC0 1.0 Universal (Public Domain)"
    trim_start_s: float = 2.0
    segment_duration_s: float = 105.0


@dataclass
class FetchPlan:
    """Declarative media/fetch_plan.yaml executed by `vicecut media-fetch` or GitHub Actions."""

    project: str
    videos: list[VideoFetchItem] = field(default_factory=list)
    headlines: list[HeadlineFetchItem] = field(default_factory=list)
    photos: list[PhotoFetchItem] = field(default_factory=list)
    music: list[MusicFetchItem] = field(default_factory=list)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "project": self.project,
            "videos": [asdict(v) for v in self.videos],
            "headlines": [asdict(h) for h in self.headlines],
            "photos": [asdict(p) for p in self.photos],
        }
        if self.music:
            payload["music"] = [asdict(m) for m in self.music]
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "FetchPlan":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            project=str(raw.get("project", "")),
            videos=[VideoFetchItem(**v) for v in raw.get("videos", [])],
            headlines=[HeadlineFetchItem(**h) for h in raw.get("headlines", [])],
            photos=[PhotoFetchItem(**p) for p in raw.get("photos", [])],
            music=[MusicFetchItem(**m) for m in raw.get("music", [])],
        )


def _assert_not_blocked(text_fields: list[str], blocked_sources: list[str] | None) -> None:
    blocked = [b.strip().lower() for b in (blocked_sources or []) if b.strip()]
    joined = " ".join(text_fields).lower()
    for term in blocked:
        if term in joined:
            raise ValueError(f"Refusing to fetch blocked source matching '{term}': {text_fields}")


def _is_direct_media_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https", "file"} and parsed.path.lower().endswith(
        (".mp4", ".mov", ".mkv", ".webm", ".m4v")
    )


def download_official_video(
    item: VideoFetchItem,
    raw_dir: Path,
    blocked_sources: list[str] | None = None,
    cookies_file: Path | None = None,
) -> tuple[Path, MediaSourceRecord]:
    """
    Download an approved official video (1080p+ preferred) via yt-dlp or direct press-kit URL.
    Strictly rejects any blocked/fan-made/concept source before touching the network.
    """
    _assert_not_blocked([item.title, item.channel_or_outlet, item.url, item.page_url], blocked_sources)
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_mp4 = raw_dir / f"{item.id}.mp4"

    def _fetch_one(target_url: str) -> None:
        local_candidate = Path(target_url)
        if local_candidate.exists() and local_candidate.is_file():
            if local_candidate.resolve() != out_mp4.resolve():
                shutil.copy2(local_candidate, out_mp4)
        elif _is_direct_media_url(target_url):
            req = urllib.request.Request(target_url, headers={"User-Agent": "ViceCut/1.0 (+official-press-fetch)"})
            with urllib.request.urlopen(req, timeout=180) as resp, out_mp4.open("wb") as dst:
                shutil.copyfileobj(resp, dst)
        else:
            cmd = [
                "yt-dlp",
                "--no-playlist",
                "--format",
                "bestvideo[height>=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height>=720]+bestaudio/best[ext=mp4]/best",
                "--merge-output-format",
                "mp4",
                "--output",
                str(out_mp4),
            ]
            if cookies_file and cookies_file.exists():
                cmd.extend(["--cookies", str(cookies_file)])
            cmd.append(target_url)
            subprocess.run(cmd, check=True)

    try:
        _fetch_one(item.url)
    except Exception:
        if item.fallback_url:
            _assert_not_blocked([item.fallback_url], blocked_sources)
            _fetch_one(item.fallback_url)
        else:
            raise

    rec = MediaSourceRecord(
        id=item.id,
        title=item.title,
        channel_or_outlet=item.channel_or_outlet,
        page_url=item.page_url or item.url,
        asset_url=item.url,
        date=item.date,
        asset_type="footage",
        local_path=str(out_mp4),
        license=item.license,
        is_official=item.is_official,
        is_mockup=False,
    )
    return out_mp4, rec


def probe_media_duration(video_path: Path) -> float:
    """Return duration in seconds using ffprobe."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(proc.stdout.strip())


def parse_ffmpeg_scene_timestamps(stderr_text: str) -> list[float]:
    """Parse `pts_time:12.345` markers emitted by FFmpeg `select='gt(scene,...)',showinfo`."""
    times: list[float] = []
    for match in re.finditer(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)", stderr_text):
        t = round(float(match.group(1)), 3)
        if not times or t - times[-1] >= 0.25:
            times.append(t)
    return times


def plan_scene_cuts(
    duration_s: float,
    detected_cuts_s: list[float],
    min_clip_s: float = 1.6,
    max_clip_s: float = 3.5,
    drop_first_s: float = 2.5,
    drop_last_s: float = 4.0,
) -> list[tuple[float, float]]:
    """
    Convert raw FFmpeg scene timestamps into clean non-overlapping B-roll segments:
    - Trims opening ESRB/PEGI rating boards (`drop_first_s`) and closing logo plates (`drop_last_s`)
    - Subdivides long continuous shots (> max_clip_s) into crisp ~2.5s unique clips
    - Drops ultra-short flash frames (< min_clip_s)
    """
    usable_start = max(0.0, drop_first_s)
    usable_end = max(usable_start, duration_s - max(0.0, drop_last_s))
    if usable_end - usable_start < min_clip_s:
        usable_start = 0.0
        usable_end = duration_s

    boundaries = [usable_start]
    for t in sorted(detected_cuts_s):
        if usable_start + 0.4 < t < usable_end - 0.4:
            boundaries.append(t)
    boundaries.append(usable_end)

    segments: list[tuple[float, float]] = []
    target_sub = min(max_clip_s, max(min_clip_s, 2.6))
    for a, b in zip(boundaries, boundaries[1:]):
        span = b - a
        if span < min_clip_s:
            continue
        # Trim 0.07s inside each scene boundary to avoid flash frames from previous/next cut!
        inner_a = a + 0.07
        inner_b = b - 0.07
        inner_span = inner_b - inner_a
        if inner_span < min_clip_s:
            continue
        if inner_span <= max_clip_s:
            segments.append((round(inner_a, 3), round(inner_b, 3)))
        else:
            n_chunks = max(2, int(round(inner_span / target_sub)))
            step = inner_span / n_chunks
            for k in range(n_chunks):
                ca = round(inner_a + k * step, 3)
                cb = round(inner_a + (k + 1) * step, 3)
                if cb - ca >= min_clip_s:
                    segments.append((ca, cb))
    return segments


def split_video_into_scenes(
    video_path: Path,
    source_id: str,
    scenes_dir: Path,
    scene_threshold: float = 0.28,
    min_clip_s: float = 1.6,
    max_clip_s: float = 3.5,
    drop_first_s: float = 2.5,
    drop_last_s: float = 4.0,
    max_clips: int = 90,
    tags: list[str] | None = None,
    project_root: Path | None = None,
) -> tuple[list[ShotClip], Path | None]:
    """
    Split an official trailer/gameplay clip into individual muted 30fps scene clips,
    auto-detect letterbox crop, extract Start|Mid|End thumbnails, score luminance,
    and build a numbered Start|Mid|End contact sheet PNG for visual QC!
    """
    scenes_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir = scenes_dir / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    duration_s = probe_media_duration(video_path)
    scan_limit_s = min(duration_s, 300.0) if duration_s > 360.0 else duration_s
    crop_filter = detect_letterbox_crop(video_path)

    # Detect real visual scene changes via FFmpeg (cap scan to first 5 min on 20+ min gameplay videos for speed!)
    det_cmd = ["ffmpeg", "-hide_banner"]
    if scan_limit_s < duration_s:
        det_cmd += ["-t", f"{scan_limit_s:.1f}"]
    det_cmd += [
        "-i", str(video_path),
        "-vf", f"select='gt(scene,{scene_threshold:.2f})',showinfo",
        "-f", "null", "-",
    ]
    det = subprocess.run(det_cmd, capture_output=True, text=True, check=False)
    raw_cuts = parse_ffmpeg_scene_timestamps(det.stderr)
    segments = plan_scene_cuts(
        duration_s=scan_limit_s,
        detected_cuts_s=raw_cuts,
        min_clip_s=min_clip_s,
        max_clip_s=max_clip_s,
        drop_first_s=drop_first_s,
        drop_last_s=drop_last_s,
    )
    if max_clips > 0 and len(segments) > max_clips:
        # Evenly sample across the full video duration so long gameplay tours stay fast & varied!
        step = len(segments) / max_clips
        segments = [segments[int(i * step)] for i in range(max_clips)]

    shots: list[ShotClip] = []
    triplets: list[tuple[str, Path, Path, Path, str]] = []

    for idx, (t0, t1) in enumerate(segments, start=1):
        shot_id = f"{source_id}_s{idx:03d}"
        clip_path = scenes_dir / f"{shot_id}.mp4"
        vf_parts = []
        if crop_filter:
            vf_parts.append(crop_filter)
        vf_parts.extend([
            f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase",
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}",
            "fps=30",
            "setsar=1",
        ])

        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{t0:.3f}", "-to", f"{t1:.3f}",
                "-i", str(video_path),
                "-vf", ",".join(vf_parts),
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p",
                str(clip_path),
            ],
            check=True,
        )

        # Extract Start / Mid / End frames for visual QC & luminance scoring
        seg_dur = max(0.2, t1 - t0)
        p_start = thumbs_dir / f"{shot_id}_start.png"
        p_mid = thumbs_dir / f"{shot_id}_mid.png"
        p_end = thumbs_dir / f"{shot_id}_end.png"
        for offset, out_p in ((0.08, p_start), (seg_dur * 0.5, p_mid), (max(0.08, seg_dur - 0.12), p_end)):
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{offset:.3f}", "-i", str(clip_path),
                    "-frames:v", "1", "-vf", "scale=320:180",
                    str(out_p),
                ],
                check=False,
            )

        luma = compute_image_luminance(p_mid) if p_mid.exists() else 0.5
        rel_path = (
            str(clip_path.relative_to(project_root))
            if (project_root and clip_path.is_relative_to(project_root))
            else str(clip_path)
        )
        shot = ShotClip(
            id=shot_id,
            source_id=source_id,
            path=rel_path,
            duration_s=round(seg_dur, 3),
            is_still=False,
            luma_mean=luma,
            tags=list(tags or []),
            crop_filter="",  # already cleanly cropped into scene clip!
        )
        shots.append(shot)
        triplets.append((shot_id, p_start, p_mid, p_end, f"{t0:.1f}s-{t1:.1f}s | {'BRIGHT' if shot.is_bright else 'DARK'} ({luma:.2f})"))

    sheet_path: Path | None = None
    if triplets:
        sheet_path = build_scene_contact_sheet(triplets, scenes_dir / f"contact_sheet_{source_id}.png")
    return shots, sheet_path


def download_official_photo(
    item: PhotoFetchItem,
    photos_dir: Path,
    blocked_sources: list[str] | None = None,
    project_root: Path | None = None,
) -> tuple[PhotoAsset, MediaSourceRecord]:
    """Download an official press-kit photo, verify with Pillow, and return PhotoAsset + source record."""
    _assert_not_blocked([item.label, item.url, item.page_url], blocked_sources)
    photos_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(urlparse(item.url).path).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        ext = ".png"
    out_img = photos_dir / f"{item.id}{ext}"

    local_candidate = Path(item.url)
    if local_candidate.exists() and local_candidate.is_file():
        shutil.copy2(local_candidate, out_img)
    else:
        req = urllib.request.Request(item.url, headers={"User-Agent": "ViceCut/1.0 (+official-press-fetch)"})
        with urllib.request.urlopen(req, timeout=60) as resp, out_img.open("wb") as dst:
            shutil.copyfileobj(resp, dst)

    with Image.open(out_img) as im:
        w, h = im.size
        if ext == ".webp":
            png_out = photos_dir / f"{item.id}.png"
            im.convert("RGB").save(png_out)
            out_img.unlink(missing_ok=True)
            out_img = png_out

    rel_path = (
        str(out_img.relative_to(project_root))
        if (project_root and out_img.is_relative_to(project_root))
        else str(out_img)
    )
    asset = PhotoAsset(
        id=item.id,
        source_id=item.source_id,
        label=item.label,
        image_path=rel_path,
        width_px=w,
        height_px=h,
        license=item.license,
        is_mockup=item.is_mockup,
    )
    src = MediaSourceRecord(
        id=f"src_photo_{item.id}",
        title=item.label,
        channel_or_outlet=item.source_id,
        page_url=item.page_url or item.url,
        asset_url=item.url,
        date=item.date,
        asset_type="photo",
        local_path=rel_path,
        license=item.license,
        is_official=not item.is_mockup,
        is_mockup=item.is_mockup,
    )
    return asset, src


def drop_shots(manifest: MediaManifest, shot_ids: list[str], reason: str = "visual_qc_drop") -> list[str]:
    """Mark shots dropped after inspecting Start|Mid|End contact sheets (logos, ESRB plates, spoilers)."""
    dropped_now: list[str] = []
    wanted = {s.strip() for s in shot_ids if s.strip()}
    for shot in manifest.shots:
        if shot.id in wanted:
            shot.dropped = True
            shot.drop_reason = reason
            dropped_now.append(shot.id)
    return dropped_now


def fetch_and_mix_music_bed(
    items: list[MusicFetchItem],
    music_dir: Path,
    manifest: MediaManifest,
    project_root: Path,
) -> tuple[Path | None, list[str]]:
    """
    Download non-copyrighted CC0 / Public Domain background tracks, register them in
    media/manifest.yaml (so credits.md lists every track), normalize each to -18 LUFS,
    and crossfade them smoothly (2.5s acrossfade) into a single stereo soundtrack_bed.wav.
    """
    if not items:
        return None, []

    music_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[tuple[MusicFetchItem, Path]] = []
    errors: list[str] = []

    for item in items:
        out_mp3 = music_dir / f"{item.id}.mp3"
        ok = False
        urls = [u for u in (item.url, item.fallback_url) if u]
        last_exc: Exception | None = None
        for target in urls:
            try:
                local_cand = Path(target)
                if local_cand.exists() and local_cand.is_file():
                    if local_cand.resolve() != out_mp3.resolve():
                        shutil.copy2(local_cand, out_mp3)
                else:
                    req = urllib.request.Request(
                        target,
                        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ViceCut/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=120) as resp, out_mp3.open("wb") as dst:
                        shutil.copyfileobj(resp, dst)
                if out_mp3.exists() and out_mp3.stat().st_size > 10_000:
                    ok = True
                    break
            except Exception as exc:
                last_exc = exc
        if ok:
            downloaded.append((item, out_mp3))
            try:
                rel_p = str(out_mp3.resolve().relative_to(project_root.resolve()))
            except ValueError:
                rel_p = str(out_mp3)
            manifest.upsert_source(
                MediaSourceRecord(
                    id=item.id,
                    title=f"{item.title} — {item.artist}",
                    channel_or_outlet=item.artist,
                    page_url=item.page_url or item.url,
                    asset_url=item.url,
                    date="2017",
                    asset_type="music",
                    local_path=rel_p,
                    license=item.license,
                    is_official=True,
                )
            )
        else:
            errors.append(f"music {item.id}: {last_exc}")

    if not downloaded:
        return None, errors

    # Normalize & trim each track to consistent -18 LUFS stereo 48kHz WAV segments
    norm_segs: list[Path] = []
    for idx, (item, mp3_p) in enumerate(downloaded):
        seg_wav = music_dir / f".norm_{idx:02d}_{item.id}.wav"
        trim_filter = (
            f"atrim=start={max(0.0, item.trim_start_s):.2f}:duration={max(15.0, item.segment_duration_s):.2f},"
            "asetpts=PTS-STARTPTS,"
            "loudnorm=I=-18.0:LRA=7.0:TP=-2.0"
        )
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(mp3_p),
                    "-af", trim_filter,
                    "-ar", "48000", "-ac", "2",
                    str(seg_wav),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            norm_segs.append(seg_wav)
        except Exception as exc:
            errors.append(f"music normalize {item.id}: {exc}")
            seg_wav.unlink(missing_ok=True)

    if not norm_segs:
        return None, errors

    bed_wav = music_dir / "soundtrack_bed.wav"
    if len(norm_segs) == 1:
        shutil.copy2(norm_segs[0], bed_wav)
    else:
        ff_inputs: list[str] = []
        for seg in norm_segs:
            ff_inputs += ["-i", str(seg)]
        parts: list[str] = []
        cur = "0:a"
        for i in range(1, len(norm_segs)):
            nxt_lbl = f"xf{i}" if i < len(norm_segs) - 1 else "aout"
            parts.append(f"[{cur}][{i}:a]acrossfade=d=2.5:c1=tri:c2=tri[{nxt_lbl}]")
            cur = nxt_lbl
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                *ff_inputs,
                "-filter_complex", ";".join(parts),
                "-map", "[aout]",
                "-ar", "48000", "-ac", "2",
                str(bed_wav),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    for seg in norm_segs:
        seg.unlink(missing_ok=True)
    return bed_wav, errors


def execute_fetch_plan(
    plan: FetchPlan,
    manifest: MediaManifest,
    project_root: Path,
    dirs: dict[str, Path],
    blocked_sources: list[str] | None = None,
    cookies_file: Path | None = None,
    skip_headlines: bool = False,
) -> dict[str, Any]:
    """
    Execute an entire declarative media/fetch_plan.yaml end-to-end:
    1. Download official videos -> auto-split into scenes -> build Start|Mid|End sheets
    2. Download official press-kit photos
    3. Download & crossfade non-copyrighted CC0/PD music tracks into media/music/soundtrack_bed.wav
    4. Capture article headlines via Playwright (auto-falling back to white text cards on paywall/small crop)
    5. Save updated media/manifest.yaml
    """
    summary: dict[str, Any] = {
        "videos_fetched": 0,
        "shots_created": 0,
        "contact_sheets": [],
        "photos_fetched": 0,
        "music_tracks_fetched": 0,
        "music_bed": "",
        "headlines_captured": 0,
        "headlines_fallback": 0,
        "errors": [],
    }

    for v in plan.videos:
        try:
            raw_path, rec = download_official_video(
                item=v,
                raw_dir=dirs["media_raw"],
                blocked_sources=blocked_sources,
                cookies_file=cookies_file,
            )
            manifest.upsert_source(rec)
            summary["videos_fetched"] += 1

            new_shots, sheet = split_video_into_scenes(
                video_path=raw_path,
                source_id=v.id,
                scenes_dir=dirs["media_scenes"],
                scene_threshold=v.scene_threshold,
                min_clip_s=v.min_clip_s,
                max_clip_s=v.max_clip_s,
                drop_first_s=v.drop_first_s,
                drop_last_s=v.drop_last_s,
                max_clips=v.max_clips,
                tags=v.tags,
                project_root=project_root,
            )
            for sh in new_shots:
                manifest.upsert_shot(sh)
            summary["shots_created"] += len(new_shots)
            if sheet:
                summary["contact_sheets"].append(str(sheet))
        except ValueError:
            # Hard policy rejection (blocked source / fan-made / watermarked) — always fail loudly!
            raise
        except Exception as exc:
            summary["errors"].append(f"video {v.id}: {exc}")

    if plan.videos and summary["videos_fetched"] == 0:
        raise RuntimeError(
            "All planned videos failed to download: " + "; ".join(summary["errors"])
        )

    # If real scene clips were just generated on disk, prune stale dry-run placeholder shots
    # whose files don't exist so FFmpeg never hits a missing path during cloud renders!
    if summary["shots_created"] > 0:
        kept_shots: list[ShotClip] = []
        pruned = 0
        for sh in manifest.shots:
            cand = Path(sh.path)
            if not cand.is_absolute():
                cand = (project_root / cand).resolve()
            if sh.path and cand.exists():
                kept_shots.append(sh)
            else:
                pruned += 1
        manifest.shots = kept_shots
        summary["placeholder_shots_pruned"] = pruned

    for ph in plan.photos:
        asset, src = download_official_photo(
            item=ph,
            photos_dir=dirs["media_photos"],
            blocked_sources=blocked_sources,
            project_root=project_root,
        )
        manifest.upsert_photo(asset)
        manifest.upsert_source(src)
        summary["photos_fetched"] += 1

    if plan.music:
        bed_wav, music_errs = fetch_and_mix_music_bed(
            items=plan.music,
            music_dir=dirs["media"] / "music",
            manifest=manifest,
            project_root=project_root,
        )
        if bed_wav is not None:
            summary["music_bed"] = str(bed_wav)
            summary["music_tracks_fetched"] = len(plan.music) - len(music_errs)
        summary["errors"].extend(music_errs)

    if not skip_headlines:
        for hl in plan.headlines:
            _assert_not_blocked([hl.outlet, hl.headline_text, hl.url], blocked_sources)
            out_png = dirs["media_headlines"] / f"{hl.id}.png"
            try:
                asset = capture_headline_with_playwright(
                    url=hl.url,
                    selector=hl.selector,
                    output_png=out_png,
                    outlet=hl.outlet,
                    headline_text=hl.headline_text,
                    pub_date=hl.date,
                    headline_id=hl.id,
                    source_id=hl.source_id,
                )
            except Exception as exc:
                # Per <gotchas>: paywall/bot-block -> crisp white card fallback instead of broken frame!
                asset = HeadlineAsset(
                    id=hl.id,
                    source_id=hl.source_id,
                    outlet=hl.outlet,
                    headline_text=hl.headline_text,
                    date=hl.date,
                    use_text_fallback=True,
                )
                summary["errors"].append(f"Headline {hl.id} screenshot failed ({exc}); using white-card fallback.")
            if asset.use_text_fallback:
                summary["headlines_fallback"] += 1
            else:
                summary["headlines_captured"] += 1
            manifest.upsert_headline(asset)

    manifest.save(dirs["media"] / "manifest.yaml")
    return summary
