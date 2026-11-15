"""ViceCut CLI — end-to-end orchestration for faceless 1920x1080 30fps explainer production."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .align import (
    AlignmentResult,
    align_section_words,
    synthesize_synthetic_alignment,
    transcribe_with_faster_whisper,
)
from .audio import master_audio_with_ffmpeg
from .config import ProjectBrief, project_dirs
from .doctor import format_doctor_report, install_missing_python_tools, run_doctor
from .graphics import build_overlay_html, export_card_type_proofs, export_overlay_frames_playwright
from .intake import build_intake_message, scaffold_project
from .media import (
    FetchPlan,
    HeadlineAsset,
    MediaManifest,
    PhotoFetchItem,
    VideoFetchItem,
    capture_headline_with_playwright,
    download_official_photo,
    download_official_video,
    drop_shots,
    evaluate_shot_pool,
    execute_fetch_plan,
    search_official_candidates,
    split_video_into_scenes,
)
from .pronunciation import PronunciationLexicon
from .qc import generate_credits_markdown, measure_timeline_qc
from .render import (
    build_ffmpeg_composite_command,
    build_mobile_export_command,
    build_pre_render_storyboard_sheet,
    extract_preview_4fps_sheets,
)
from .research import ResearchLedger
from .script_checker import check_script
from .script_parser import parse_script_file
from .timeline import Timeline, compile_timeline
from .voice import estimate_tts_cost, record_sections


def _load_project(proj_arg: str) -> tuple[Path, dict[str, Path], ProjectBrief]:
    root = Path(proj_arg).resolve()
    brief_path = root / "project.yaml"
    if not brief_path.exists():
        raise FileNotFoundError(f"Not a ViceCut project directory (missing {brief_path})")
    return root, project_dirs(root), ProjectBrief.load(brief_path)


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = run_doctor(elevenlabs_key_file=args.key_file)
    print(format_doctor_report(checks))
    if args.install:
        installed = install_missing_python_tools(checks, dry_run=args.dry_run)
        if installed:
            print("\nInstalled:", ", ".join(installed))
        else:
            print("\nNo missing pip-installable packages to install.")
    return 0 if all(c.installed or not c.required for c in checks) else 1


def cmd_intake(_: argparse.Namespace) -> int:
    print(build_intake_message())
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    grad = tuple(args.gradient.split(",")) if args.gradient else None
    if grad is not None and len(grad) != 2:
        raise ValueError("--gradient must be two comma-separated hex colours, e.g. '#FF93DC,#FFB27C'")
    proj_root, brief = scaffold_project(
        projects_root=Path(args.projects_dir),
        topic=args.topic,
        viewer=args.viewer,
        length_preset=args.length,
        voice_provider=args.voice_provider,
        voice_id=args.voice_id,
        api_key_file=args.api_key_file,
        existing_footage=args.footage or [],
        blocked_sources=args.block or [],
        gradient_colors=grad,  # type: ignore[arg-type]
        key_art_path=Path(args.key_art) if args.key_art else None,
        preset_path=Path(args.preset) if args.preset else None,
        motif=args.motif or "",
        slug=args.slug,
    )
    print(f"Created ViceCut project: {proj_root}")
    print(f"  Topic:           {brief.topic}")
    print(f"  Length preset:   {brief.length_preset} ({brief.preset_spec['label']})")
    print(f"  Required facts:  >= {brief.required_fact_count()} confirmed facts")
    print(f"  Theme:           {brief.theme.name} ({brief.theme.gradient_start} -> {brief.theme.gradient_end} on {brief.theme.panel_bg})")
    print(f"  Voice provider:  {brief.voice.provider} ({brief.voice.voice_id})")
    return 0


def cmd_audit_research(args: argparse.Namespace) -> int:
    _, dirs, brief = _load_project(args.project)
    facts_path = dirs["research"] / "facts.yaml"
    ledger = ResearchLedger.load(facts_path)
    res = ledger.audit(brief)
    ledger.export_sources_yaml(dirs["research"] / "sources.yaml")
    print(f"Research Audit — {'PASS' if res.passed else 'FAIL'}")
    print(f"Usable confirmed facts: {res.usable_fact_count} / {res.required_fact_count} required")
    for w in res.warnings:
        print(f"  [WARN] {w}")
    for e in res.errors:
        print(f"  [FAIL] {e}")
    return 0 if res.passed else 1


def cmd_check(args: argparse.Namespace) -> int:
    _, dirs, brief = _load_project(args.project)
    script = parse_script_file(dirs["script"] / "script.md")
    facts_path = dirs["research"] / "facts.yaml"
    ledger = ResearchLedger.load(facts_path) if facts_path.exists() else None
    lexicon = PronunciationLexicon.load(dirs["script"] / "pronunciations.yaml")

    report = check_script(script, brief, ledger=ledger)
    report.save(dirs["script"] / "check_report.json")
    print(report.format_text())

    # Risky token pronunciation audit
    risky = lexicon.audit_risky_tokens(" ".join(ln.text for ln in script.all_lines))
    if risky:
        print(f"\nPronunciation check — add overrides if needed for: {', '.join(risky)}")

    # Paid TTS cost visibility before Gate 2
    cost = estimate_tts_cost(script, brief, lexicon)
    print(f"\nVoice estimate ({cost.provider}): {cost.total_chars:,} spoken characters (~{cost.estimated_credits:,} paid credits)")
    if cost.requires_approval and not brief.approvals.tts_cost_approved:
        print("  -> Paid voice requires explicit user approval (`vicecut approve <project> --gate tts-cost`).")
    if not brief.approvals.script_approved:
        print("  -> Show script, source list, and this report to user, then run `vicecut approve <project> --gate script`.")

    return 0 if report.passed else 1


def cmd_approve(args: argparse.Namespace) -> int:
    root, dirs, brief = _load_project(args.project)
    if args.gate == "script":
        brief.approvals.script_approved = True
        print("Gate 1 approved: script_approved = true")
    elif args.gate == "tts-cost":
        script = parse_script_file(dirs["script"] / "script.md")
        lex = PronunciationLexicon.load(dirs["script"] / "pronunciations.yaml")
        cost = estimate_tts_cost(script, brief, lex)
        brief.approvals.tts_estimated_chars = cost.total_chars
        brief.approvals.tts_cost_approved = True
        print(f"Gate 2 approved: tts_cost_approved = true ({cost.total_chars:,} chars)")
    elif args.gate == "storyboard":
        brief.approvals.storyboard_approved = True
        print("Gate 3 approved: storyboard_approved = true")
    elif args.gate == "qc-round":
        brief.approvals.qc_rounds_completed += 1
        print(f"Recorded look-and-fix QC round #{brief.approvals.qc_rounds_completed} (target >= 3)")
    brief.save(root / "project.yaml")
    return 0


def cmd_voice(args: argparse.Namespace) -> int:
    root, dirs, brief = _load_project(args.project)
    script = parse_script_file(dirs["script"] / "script.md")
    lex = PronunciationLexicon.load(dirs["script"] / "pronunciations.yaml")
    only = [int(x) for x in args.sections.split(",")] if args.sections else None
    takes = record_sections(
        script=script,
        brief=brief,
        lexicon=lex,
        takes_dir=dirs["voice_takes"],
        project_dir=root,
        only_sections=only,
        enforce_approval_gates=not args.force,
    )
    for t in takes:
        print(f"Recorded section {t.section_index:02d} ({t.section_title}): {t.char_count} chars -> {t.audio_path.name}")
    return 0


def cmd_align(args: argparse.Namespace) -> int:
    _, dirs, brief = _load_project(args.project)
    script = parse_script_file(dirs["script"] / "script.md")
    out_json = dirs["voice_aligned"] / "words.json"

    if args.synthetic:
        res = synthesize_synthetic_alignment(script, wpm=args.wpm)
        res.save(out_json)
        print(f"Generated synthetic 210-wpm alignment ({len(res.words)} words) -> {out_json}")
        return 0

    all_words = []
    all_mismatches = []
    redo_secs = []
    wid = 0
    time_offset = 0.0

    for sec in script.sections:
        wav_path = dirs["voice_takes"] / f"section_{sec.index:02d}.wav"
        if not wav_path.exists():
            raise FileNotFoundError(f"Missing section take: {wav_path}. Run `vicecut voice` first.")
        heard = transcribe_with_faster_whisper(
            wav_path,
            model_name=brief.voice.whisper_model,
            compute_type=brief.voice.whisper_compute_type,
        )
        words, mismatches, needs_redo = align_section_words(sec, heard, start_word_id=wid, time_offset_s=time_offset)
        all_words.extend(words)
        all_mismatches.extend(mismatches)
        if needs_redo:
            redo_secs.append(sec.index)
        wid += len(words)
        time_offset = (words[-1].end_s + 0.25) if words else time_offset

    res = AlignmentResult(words=all_words, mismatches=all_mismatches, sections_needing_redo=redo_secs)
    res.save(out_json)
    print(f"Aligned {len(all_words)} words -> {out_json}")
    for m in all_mismatches:
        print(f"  [{m.severity.upper()}] sec {m.section_index:02d}: '{m.script_phrase}' vs '{m.heard_phrase}' ({m.reason})")
    if redo_secs:
        print(f"\nRedo ONLY these sections: {','.join(str(s) for s in redo_secs)}")
        return 1
    return 0


def cmd_audio(args: argparse.Namespace) -> int:
    _, dirs, _ = _load_project(args.project)
    align_path = dirs["voice_aligned"] / "words.json"
    alignment = AlignmentResult.load(align_path)
    takes = sorted(dirs["voice_takes"].glob("section_*.wav"))
    if not takes:
        raise FileNotFoundError(f"No section takes found in {dirs['voice_takes']}")

    # Concatenate section takes into raw_full.wav
    concat_list = dirs["voice_master"] / "takes.txt"
    concat_list.write_text("\n".join(f"file '{p.resolve()}'" for p in takes) + "\n", encoding="utf-8")
    raw_full = dirs["voice_master"] / "raw_full.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(raw_full)],
        check=True,
        capture_output=True,
        text=True,
    )

    master_wav = dirs["voice_master"] / "voice_master.wav"
    remapped, stats = master_audio_with_ffmpeg(raw_full, master_wav, alignment.words)
    AlignmentResult(words=remapped, mismatches=alignment.mismatches, sections_needing_redo=[]).save(
        dirs["voice_aligned"] / "words_mastered.json"
    )
    (dirs["voice_master"] / "audio_stats.json").write_text(json.dumps(stats.__dict__, indent=2), encoding="utf-8")
    print(f"Mastered voice -> {master_wav}")
    print(f"  Pace: {stats.final_wpm} wpm (tempo {stats.tempo_factor}x) | Longest pause: {stats.longest_pause_s:.2f}s")
    print(f"  Loudness: {stats.integrated_lufs} LUFS | LRA: {stats.lra_lu} LU | True Peak: {stats.true_peak_db} dBTP")
    return 0


def cmd_media_search(args: argparse.Namespace) -> int:
    allowed: list[str] = []
    blocked: list[str] = []
    if args.project:
        _, _, brief = _load_project(args.project)
        allowed = brief.media.allowed_official_channels
        blocked = brief.media.blocked_sources
    results = search_official_candidates(args.query, allowed_channels=allowed, blocked_sources=blocked, max_results=args.limit)
    for idx, r in enumerate(results, start=1):
        badge = "[OFFICIAL]" if r["is_official_channel"] else "[CHECK]   "
        warn = f" !! {r['warning']}" if r["warning"] else ""
        print(f"{idx:02d}. {badge} {r['channel']} | {r['upload_date']} | {r['title']}{warn}")
        print(f"    {r['url']}")
    return 0


def cmd_media_budget(args: argparse.Namespace) -> int:
    _, dirs, brief = _load_project(args.project)
    manifest = MediaManifest.load(dirs["media"] / "manifest.yaml")
    budget = evaluate_shot_pool(manifest, runtime_s=brief.preset_spec["target_duration_s"])
    print(f"Shot Pool Budget — {'OK' if budget.ok else 'SHORT'}")
    print(f"  Target unique shots:   {budget.target_unique_shots} (~25/min for {budget.duration_s}s)")
    print(f"  Usable motion clips:   {budget.usable_motion_shots}")
    print(f"  Usable official stills:{budget.usable_still_shots} (max allowed <10%: {budget.max_allowed_stills})")
    print(f"  Shortfall:             {budget.shortfall_shots} shots")
    return 0 if budget.ok else 1


def cmd_media_download(args: argparse.Namespace) -> int:
    proj_dir, dirs, brief = _load_project(args.project)
    manifest_p = dirs["media"] / "manifest.yaml"
    manifest = MediaManifest.load(manifest_p) if manifest_p.exists() else MediaManifest(project=brief.slug)
    item = VideoFetchItem(
        id=args.id,
        title=args.title or args.id,
        channel_or_outlet=args.channel,
        url=args.url,
        page_url=args.page_url or args.url,
        date=args.date or brief.research_date,
        license=args.license,
    )
    cookies = Path(args.cookies) if args.cookies else None
    out_mp4, rec = download_official_video(
        item=item,
        raw_dir=dirs["media_raw"],
        blocked_sources=brief.media.blocked_sources,
        cookies_file=cookies,
    )
    manifest.upsert_source(rec)
    manifest.save(manifest_p)
    print(f"Downloaded official video -> {out_mp4} (registered as {rec.id} in {manifest_p})")
    return 0


def cmd_media_split(args: argparse.Namespace) -> int:
    proj_dir, dirs, brief = _load_project(args.project)
    manifest_p = dirs["media"] / "manifest.yaml"
    manifest = MediaManifest.load(manifest_p) if manifest_p.exists() else MediaManifest(project=brief.slug)

    targets: list[tuple[str, Path]] = []
    if args.source_id:
        src = manifest.source_by_id(args.source_id)
        cand = Path(src.local_path) if (src and src.local_path) else (dirs["media_raw"] / f"{args.source_id}.mp4")
        if not cand.is_absolute():
            cand = (proj_dir / cand).resolve()
        targets.append((args.source_id, cand))
    else:
        for mp4 in sorted(dirs["media_raw"].glob("*.mp4")):
            targets.append((mp4.stem, mp4))

    if not targets:
        print(f"No raw .mp4 files found in {dirs['media_raw']}.")
        return 1

    total_shots = 0
    for sid, vpath in targets:
        shots, sheet = split_video_into_scenes(
            video_path=vpath,
            source_id=sid,
            scenes_dir=dirs["media_scenes"],
            scene_threshold=args.threshold,
            min_clip_s=args.min_clip,
            max_clip_s=args.max_clip,
            drop_first_s=args.drop_first,
            drop_last_s=args.drop_last,
            project_root=proj_dir,
        )
        for sh in shots:
            manifest.upsert_shot(sh)
        total_shots += len(shots)
        print(f"Split {vpath.name} -> {len(shots)} unique scene clips (Start|Mid|End sheet: {sheet})")

    manifest.save(manifest_p)
    print(f"Updated {manifest_p} ({total_shots} scenes added/updated).")
    return 0


def cmd_media_photo(args: argparse.Namespace) -> int:
    proj_dir, dirs, brief = _load_project(args.project)
    manifest_p = dirs["media"] / "manifest.yaml"
    manifest = MediaManifest.load(manifest_p) if manifest_p.exists() else MediaManifest(project=brief.slug)
    item = PhotoFetchItem(
        id=args.id,
        source_id=args.source_id,
        label=args.label,
        url=args.url,
        page_url=args.page_url or args.url,
        date=args.date or brief.research_date,
        is_mockup=args.mockup,
    )
    asset, src = download_official_photo(
        item=item,
        photos_dir=dirs["media_photos"],
        blocked_sources=brief.media.blocked_sources,
        project_root=proj_dir,
    )
    manifest.upsert_photo(asset)
    manifest.upsert_source(src)
    manifest.save(manifest_p)
    print(f"Saved official photo [{asset.id}] ({asset.width_px}x{asset.height_px}) -> {asset.image_path}")
    return 0


def cmd_media_headline(args: argparse.Namespace) -> int:
    _, dirs, brief = _load_project(args.project)
    manifest_p = dirs["media"] / "manifest.yaml"
    manifest = MediaManifest.load(manifest_p) if manifest_p.exists() else MediaManifest(project=brief.slug)
    out_png = dirs["media_headlines"] / f"{args.id}.png"
    try:
        asset = capture_headline_with_playwright(
            url=args.url,
            selector=args.selector,
            output_png=out_png,
            outlet=args.outlet,
            headline_text=args.text,
            pub_date=args.date or brief.research_date,
            headline_id=args.id,
            source_id=args.source_id,
        )
    except Exception as exc:
        print(f"[WARN] Live headline screenshot failed ({exc}); saving clean white-card fallback.")
        asset = HeadlineAsset(
            id=args.id,
            source_id=args.source_id,
            outlet=args.outlet,
            headline_text=args.text,
            date=args.date or brief.research_date,
            use_text_fallback=True,
        )
    manifest.upsert_headline(asset)
    manifest.save(manifest_p)
    mode = "WHITE-CARD FALLBACK" if asset.use_text_fallback else f"SCREENSHOT ({asset.width_px}x{asset.height_px})"
    print(f"Registered headline [{asset.id}] ({mode}) in {manifest_p}")
    return 0


def cmd_media_fetch(args: argparse.Namespace) -> int:
    proj_dir, dirs, brief = _load_project(args.project)
    plan_p = Path(args.plan) if args.plan else (dirs["media"] / "fetch_plan.yaml")
    if not plan_p.exists():
        raise FileNotFoundError(f"Missing declarative fetch plan {plan_p}")
    plan = FetchPlan.load(plan_p)
    manifest_p = dirs["media"] / "manifest.yaml"
    manifest = MediaManifest.load(manifest_p) if manifest_p.exists() else MediaManifest(project=brief.slug)
    cookies = Path(args.cookies) if args.cookies else None

    summary = execute_fetch_plan(
        plan=plan,
        manifest=manifest,
        project_root=proj_dir,
        dirs=dirs,
        blocked_sources=brief.media.blocked_sources,
        cookies_file=cookies,
        skip_headlines=args.skip_headlines,
    )
    print(f"Media Fetch Complete -> {manifest_p}")
    print(f"  Videos downloaded:  {summary['videos_fetched']} -> {summary['shots_created']} scene clips")
    print(f"  Official photos:    {summary['photos_fetched']}")
    print(f"  Headlines captured: {summary['headlines_captured']} (white-card fallbacks: {summary['headlines_fallback']})")
    for cs in summary["contact_sheets"]:
        print(f"  Start|Mid|End sheet: {cs}")
    for err in summary["errors"]:
        print(f"  [WARN] {err}")
    return 0


def cmd_media_drop(args: argparse.Namespace) -> int:
    _, dirs, _ = _load_project(args.project)
    manifest_p = dirs["media"] / "manifest.yaml"
    manifest = MediaManifest.load(manifest_p)
    ids = [s.strip() for s in args.shots.split(",") if s.strip()]
    dropped = drop_shots(manifest, ids, reason=args.reason)
    manifest.save(manifest_p)
    print(f"Marked {len(dropped)} shots dropped ({args.reason}): {', '.join(dropped)}")
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    proj_dir, dirs, brief = _load_project(args.project)
    script = parse_script_file(dirs["script"] / "script.md")
    mastered_align = dirs["voice_aligned"] / "words_mastered.json"
    raw_align = dirs["voice_aligned"] / "words.json"
    align_path = mastered_align if mastered_align.exists() else raw_align
    if not align_path.exists():
        if args.auto_synthetic:
            alignment = synthesize_synthetic_alignment(script)
            alignment.save(raw_align)
        else:
            raise FileNotFoundError(f"Missing alignment file {align_path}. Run `vicecut align` first.")
    else:
        alignment = AlignmentResult.load(align_path)

    manifest = MediaManifest.load(dirs["media"] / "manifest.yaml")
    facts_path = dirs["research"] / "facts.yaml"
    ledger = ResearchLedger.load(facts_path) if facts_path.exists() else None

    tl = compile_timeline(script, alignment, manifest, brief, ledger=ledger)
    tl_path = dirs["timeline"] / "timeline.json"
    tl.save(tl_path)

    ov_html = build_overlay_html(tl, dirs["renders"] / "overlay_page.html", project_dir=proj_dir)
    sb_png = build_pre_render_storyboard_sheet(
        tl,
        dirs["timeline"] / "storyboard_sheet.png",
        thumbnails_dir=dirs["media_scenes"],
    )

    print(f"Compiled timeline -> {tl_path}")
    print(f"  Duration: {tl.duration_s:.1f}s ({tl.total_frames} frames @ {tl.fps}fps)")
    print(f"  Shots: {len(tl.shots)} | Cards: {len(tl.cards)} | Captions: {len(tl.captions)} | Pills: {len(tl.pills)}")
    print(f"Built transparent 1920x1080 overlay stage -> {ov_html}")
    print(f"Built pre-render storyboard audit sheet -> {sb_png}")
    for w in tl.warnings:
        print(f"  [WARN] {w}")
    return 0


def cmd_card_proofs(args: argparse.Namespace) -> int:
    proj_dir, dirs, _ = _load_project(args.project)
    tl = Timeline.load(dirs["timeline"] / "timeline.json")
    ov_html = dirs["renders"] / "overlay_page.html"
    if not ov_html.exists():
        build_overlay_html(tl, ov_html, project_dir=proj_dir)
    proofs = export_card_type_proofs(tl, ov_html, dirs["renders_proofs"])
    for ctype, p in proofs.items():
        print(f"Exported full-size 1920x1080 proof [{ctype}] -> {p}")
    return 0


def cmd_overlays(args: argparse.Namespace) -> int:
    proj_dir, dirs, _ = _load_project(args.project)
    tl = Timeline.load(dirs["timeline"] / "timeline.json")
    ov_html = dirs["renders"] / "overlay_page.html"
    if not ov_html.exists():
        build_overlay_html(tl, ov_html, project_dir=proj_dir)
    paths = export_overlay_frames_playwright(
        overlay_html=ov_html,
        frames_dir=dirs["renders_frames"],
        total_frames=tl.total_frames,
        timeline=tl,
        start_frame=args.start_frame,
        end_frame=args.end_frame,
    )
    print(f"Exported {len(paths)} transparent 1920x1080 overlay frames -> {dirs['renders_frames']}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    proj_dir, dirs, brief = _load_project(args.project)
    if not brief.approvals.storyboard_approved and not args.force:
        raise PermissionError(
            "Gate 3 blocked: inspect `timeline/storyboard_sheet.png` first and run "
            "`vicecut approve <project> --gate storyboard` before rendering."
        )
    tl = Timeline.load(dirs["timeline"] / "timeline.json")
    ov_html = dirs["renders"] / "overlay_page.html"
    master_wav = dirs["voice_master"] / "voice_master.wav"

    if args.mode == "mobile":
        master_mp4 = dirs["renders"] / "final_1080p.mp4"
        mobile_mp4 = dirs["renders"] / "mobile_under_25mb.mp4"
        cmd = build_mobile_export_command(master_mp4, mobile_mp4, tl.duration_s)
        if args.dry_run:
            print(" ".join(cmd))
            return 0
        subprocess.run(cmd, check=True)
        size_mb = mobile_mp4.stat().st_size / (1024 * 1024)
        print(f"Exported mobile copy -> {mobile_mp4} ({size_mb:.2f} MB; target < 25 MB)")
        return 0 if size_mb < 25.0 else 1

    is_preview = args.mode == "preview"
    out_mp4 = dirs["renders"] / ("preview_540p.mp4" if is_preview else "final_1080p.mp4")
    cmd = build_ffmpeg_composite_command(
        timeline=tl,
        overlay_frames_dir=dirs["renders_frames"],
        master_audio_wav=master_wav,
        output_mp4=out_mp4,
        preview=is_preview,
        music_path=Path(args.music) if args.music else None,
        project_dir=proj_dir,
    )
    if args.dry_run:
        print(" ".join(cmd))
        return 0

    if not args.skip_overlay_export:
        export_overlay_frames_playwright(ov_html, dirs["renders_frames"], tl.total_frames, timeline=tl)
    subprocess.run(cmd, check=True)
    print(f"Rendered -> {out_mp4}")

    if is_preview:
        sheets = extract_preview_4fps_sheets(out_mp4, tl.duration_s, dirs["renders"])
        for s in sheets:
            print(f"Extracted 4fps QC sheet -> {s}")
    return 0


def cmd_qc(args: argparse.Namespace) -> int:
    _, dirs, brief = _load_project(args.project)
    tl = Timeline.load(dirs["timeline"] / "timeline.json")
    align_p = dirs["voice_aligned"] / "words_mastered.json"
    if not align_p.exists():
        align_p = dirs["voice_aligned"] / "words.json"
    alignment = AlignmentResult.load(align_p) if align_p.exists() else None

    audio_stats = None
    stats_p = dirs["voice_master"] / "audio_stats.json"
    if stats_p.exists():
        from .audio import AudioMasterStats
        audio_stats = AudioMasterStats(**json.loads(stats_p.read_text(encoding="utf-8")))

    report = measure_timeline_qc(tl, brief, alignment=alignment, audio_stats=audio_stats)
    qc_md = dirs["renders"] / "qc_report.md"
    report.save(qc_md)

    facts_p = dirs["research"] / "facts.yaml"
    ledger = ResearchLedger.load(facts_p) if facts_p.exists() else None
    manifest_p = dirs["media"] / "manifest.yaml"
    manifest = MediaManifest.load(manifest_p) if manifest_p.exists() else None
    credits_md = generate_credits_markdown(brief, ledger, manifest, dirs["renders"] / "credits.md")

    print(report.to_markdown())
    print(f"Saved QC report -> {qc_md}")
    print(f"Saved credits   -> {credits_md}")
    return 0 if report.passed else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vicecut", description="ViceCut — Faceless 1920x1080 30fps Explainer Production Framework")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="Check local free toolchain & optionally install missing Python packages")
    d.add_argument("--install", action="store_true", help="Install missing pip packages & Chromium")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--key-file", default=None, help="Optional path to ElevenLabs key file to verify (never printed)")
    d.set_defaults(func=cmd_doctor)

    i = sub.add_parser("intake", help="Print the 6 required intake questions in one message")
    i.set_defaults(func=cmd_intake)

    init = sub.add_parser("init", help="Scaffold a new per-video project folder")
    init.add_argument("--topic", required=True, help="everything you need to know about ___ before you ___")
    init.add_argument("--viewer", required=True, help="Who the viewer is and what they are about to do")
    init.add_argument("--length", choices=["sample", "medium", "full"], default="sample")
    init.add_argument("--projects-dir", default="projects")
    init.add_argument("--preset", default="presets/gta6.yaml")
    init.add_argument("--voice-provider", choices=["kokoro", "elevenlabs", "mock"], default="kokoro")
    init.add_argument("--voice-id", default="af_heart")
    init.add_argument("--api-key-file", default=None)
    init.add_argument("--footage", action="append", help="Existing local footage path (repeatable)")
    init.add_argument("--block", action="append", help="Blocked outlet/domain/channel (repeatable)")
    init.add_argument("--gradient", default=None, help="Two hex colours separated by comma, e.g. '#FF93DC,#FFB27C'")
    init.add_argument("--key-art", default=None, help="Path to subject key art image for neighbouring-hue extraction")
    init.add_argument("--motif", default="")
    init.add_argument("--slug", default=None)
    init.set_defaults(func=cmd_init)

    ar = sub.add_parser("audit-research", help="Validate two-source fact ledger & export deduplicated sources.yaml")
    ar.add_argument("project")
    ar.set_defaults(func=cmd_audit_research)

    chk = sub.add_parser("check", help="Run automated script checker, pronunciation audit & TTS credit estimate")
    chk.add_argument("project")
    chk.set_defaults(func=cmd_check)

    app = sub.add_parser("approve", help="Record explicit human gate approval or QC round")
    app.add_argument("project")
    app.add_argument("--gate", choices=["script", "tts-cost", "storyboard", "qc-round"], required=True)
    app.set_defaults(func=cmd_approve)

    v = sub.add_parser("voice", help="Record section-by-section voice takes (Kokoro / ElevenLabs / Mock)")
    v.add_argument("project")
    v.add_argument("--sections", default=None, help="Comma-separated section indices to re-record, e.g. '1,3'")
    v.add_argument("--force", action="store_true", help="Bypass approval gates (dev/test only)")
    v.set_defaults(func=cmd_voice)

    al = sub.add_parser("align", help="Transcribe takes with faster-whisper & difflib-align words to script")
    al.add_argument("project")
    al.add_argument("--synthetic", action="store_true", help="Generate 210-wpm dry-run word timestamps")
    al.add_argument("--wpm", type=float, default=210.0)
    al.set_defaults(func=cmd_align)

    au = sub.add_parser("audio", help="Trim real silences (-35dB->0.30s), cap 1.12x tempo & 2-pass loudnorm (-18 LUFS)")
    au.add_argument("project")
    au.set_defaults(func=cmd_audio)

    ms = sub.add_parser("media-search", help="List yt-dlp official footage candidates before downloading")
    ms.add_argument("query")
    ms.add_argument("--project", default=None)
    ms.add_argument("--limit", type=int, default=8)
    ms.set_defaults(func=cmd_media_search)

    mb = sub.add_parser("media-budget", help="Check shot pool (~25/min) and official stills ratio (<10%)")
    mb.add_argument("project")
    mb.set_defaults(func=cmd_media_budget)

    md = sub.add_parser("media-download", help="Download an approved official video via yt-dlp or direct URL")
    md.add_argument("project")
    md.add_argument("--id", required=True, help="Source ID, e.g. src_trailer1")
    md.add_argument("--url", required=True, help="Official YouTube, press-kit, or direct .mp4 URL")
    md.add_argument("--channel", required=True, help="Official publisher/channel name, e.g. 'Rockstar Games'")
    md.add_argument("--title", default="")
    md.add_argument("--page-url", default="")
    md.add_argument("--date", default="")
    md.add_argument("--license", default="official-trailer-commentary")
    md.add_argument("--cookies", default=None, help="Optional yt-dlp cookies.txt file for age-gated trailers")
    md.set_defaults(func=cmd_media_download)

    msp = sub.add_parser("media-split", help="Auto-split official videos into clean 1.7-3.5s scenes + Start|Mid|End sheets")
    msp.add_argument("project")
    msp.add_argument("--source-id", default=None, help="Split one source ID (default: all raw .mp4 files)")
    msp.add_argument("--threshold", type=float, default=0.28, help="FFmpeg scene change threshold (default 0.28)")
    msp.add_argument("--min-clip", type=float, default=1.6)
    msp.add_argument("--max-clip", type=float, default=3.5)
    msp.add_argument("--drop-first", type=float, default=2.5, help="Skip opening rating card seconds")
    msp.add_argument("--drop-last", type=float, default=4.0, help="Skip closing publisher logo seconds")
    msp.set_defaults(func=cmd_media_split)

    mph = sub.add_parser("media-photo", help="Download an official press-kit photo and register in manifest.yaml")
    mph.add_argument("project")
    mph.add_argument("--id", required=True)
    mph.add_argument("--url", required=True)
    mph.add_argument("--label", required=True)
    mph.add_argument("--source-id", default="official_press")
    mph.add_argument("--page-url", default="")
    mph.add_argument("--date", default="")
    mph.add_argument("--mockup", action="store_true")
    mph.set_defaults(func=cmd_media_photo)

    mhl = sub.add_parser("media-headline", help="Capture article headline screenshot with automatic white-card fallback")
    mhl.add_argument("project")
    mhl.add_argument("--id", required=True)
    mhl.add_argument("--url", required=True)
    mhl.add_argument("--outlet", required=True)
    mhl.add_argument("--text", required=True, help="Exact headline text")
    mhl.add_argument("--selector", default="h1")
    mhl.add_argument("--source-id", default="article")
    mhl.add_argument("--date", default="")
    mhl.set_defaults(func=cmd_media_headline)

    mf = sub.add_parser("media-fetch", help="Execute declarative media/fetch_plan.yaml (download, split, photos, headlines)")
    mf.add_argument("project")
    mf.add_argument("--plan", default=None, help="Path to fetch_plan.yaml (default: <project>/media/fetch_plan.yaml)")
    mf.add_argument("--cookies", default=None, help="Optional yt-dlp cookies.txt file")
    mf.add_argument("--skip-headlines", action="store_true")
    mf.set_defaults(func=cmd_media_fetch)

    mdr = sub.add_parser("media-drop", help="Mark bad shots dropped after inspecting Start|Mid|End contact sheets")
    mdr.add_argument("project")
    mdr.add_argument("--shots", required=True, help="Comma-separated shot IDs to drop, e.g. src_t1_s001,src_t1_s019")
    mdr.add_argument("--reason", default="visual_qc_drop")
    mdr.set_defaults(func=cmd_media_drop)

    tl = sub.add_parser("timeline", help="Compile word-keyed timeline.json, overlay_page.html & storyboard_sheet.png")
    tl.add_argument("project")
    tl.add_argument("--auto-synthetic", action="store_true", help="Auto-generate synthetic alignment if missing")
    tl.set_defaults(func=cmd_timeline)

    cp = sub.add_parser("card-proofs", help="Export full-size 1920x1080 single-frame proof PNG of every card type")
    cp.add_argument("project")
    cp.set_defaults(func=cmd_card_proofs)

    ov = sub.add_parser("overlays", help="Export transparent 1920x1080 overlay PNG frames (supports chunked ranges)")
    ov.add_argument("project")
    ov.add_argument("--start-frame", type=int, default=0)
    ov.add_argument("--end-frame", type=int, default=None)
    ov.set_defaults(func=cmd_overlays)

    rn = sub.add_parser("render", help="Render 540p preview + 4fps QC sheets, 1080p master, or <25 MB mobile copy")
    rn.add_argument("project")
    rn.add_argument("--mode", choices=["preview", "final", "mobile"], default="preview")
    rn.add_argument("--music", default=None, help="Optional background music track (mixed at -34 dB)")
    rn.add_argument("--skip-overlay-export", action="store_true")
    rn.add_argument("--dry-run", action="store_true")
    rn.add_argument("--force", action="store_true")
    rn.set_defaults(func=cmd_render)

    qc = sub.add_parser("qc", help="Measure all 11 quantitative QC targets & write qc_report.md + credits.md")
    qc.add_argument("project")
    qc.set_defaults(func=cmd_qc)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
