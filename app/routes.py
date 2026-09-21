from __future__ import annotations

import json
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.claude_academy import fetch_claude_academy_webinars
from app.config import ALLOWED_CHANNEL_FILTERS, ALLOWED_LANGUAGES, ALLOWED_MODELS, BASE_DIR, JOBS_DIR, library
from app.content import build_editorial_material
from app.discovery import enrich_search_videos, extract_channel_videos, search_video_matches, video_record
from app.library import LibraryError
from app.models import Batch, Job, batch_lock, batches, jobs, jobs_lock, persist_batch_state, public_batch, public_job
from app.pipeline import batch_from_state, start_batch_worker, start_job_worker
from app.templating import templates
from app.utils import default_output_dir, destination_dir_for, form_checkbox_enabled, topic_output_dir
from app.youtube import is_youtube_host, media_id, parse_urls, search_extract_info

router = APIRouter()

SEARCH_METADATA_CANDIDATE_CAP = 200
SEARCH_METADATA_TIME_BUDGET_SECONDS = 25


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    static_version = int((BASE_DIR / "app" / "static" / "styles.css").stat().st_mtime)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "default_output_dir": default_output_dir(), "static_version": static_version},
    )


@router.get("/api/projects")
def list_projects() -> JSONResponse:
    return JSONResponse({"projects": library.list_projects()})


@router.get("/api/projects/{project_id}")
def get_project(project_id: str) -> JSONResponse:
    try:
        project = library.get_project(project_id)
        transcripts = library.project_transcripts(project_id)
    except LibraryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse({"project": project, "transcripts": transcripts})


@router.post("/api/projects")
def create_project(
    name: str = Form(...),
    aliases: str = Form(""),
    output_dir: str = Form(""),
    manufacturer_text: str = Form(""),
) -> JSONResponse:
    project_dir = topic_output_dir(output_dir, name)
    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        project = library.create_project(
            name=name,
            aliases=aliases,
            output_dir=str(project_dir),
            manufacturer_text=manufacturer_text,
        )
    except (LibraryError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"project": project})


@router.post("/api/projects/{project_id}/scan")
def scan_project_transcripts(
    project_id: str,
    transcriptions_dir: str = Form(""),
    filename_filter: str = Form(""),
) -> JSONResponse:
    try:
        project = library.get_project(project_id)
        directory = transcriptions_dir.strip() or project["output_dir"]
        candidates = library.scan_directory(directory, filename_filter.strip())
    except LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"project": project, "directory": directory, "count": len(candidates), "candidates": candidates})


@router.post("/api/projects/{project_id}/import")
def import_project_transcripts(
    project_id: str,
    transcriptions_dir: str = Form(""),
    filename_filter: str = Form(""),
    paths_json: str = Form("[]"),
) -> JSONResponse:
    try:
        selected_paths = json.loads(paths_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="The selected files are not valid.") from exc
    if not isinstance(selected_paths, list) or not all(isinstance(path, str) for path in selected_paths):
        raise HTTPException(status_code=400, detail="The selected files are not valid.")
    try:
        project = library.get_project(project_id)
        result = library.import_paths(
            project_id=project_id,
            directory=transcriptions_dir.strip() or project["output_dir"],
            selected_paths=selected_paths,
            filename_filter=filename_filter.strip(),
        )
    except LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@router.post("/api/open-folder")
def open_folder(output_dir: str = Form("")) -> JSONResponse:
    destination_dir = destination_dir_for(output_dir.strip() or default_output_dir())
    destination_dir.mkdir(parents=True, exist_ok=True)

    if sys.platform == "darwin":
        command = ["open", str(destination_dir)]
    elif sys.platform.startswith("linux"):
        command = ["xdg-open", str(destination_dir)]
    else:
        raise HTTPException(status_code=400, detail="Opening folders from the app is only supported on macOS and Linux.")

    try:
        subprocess.Popen(command)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not open the folder: {exc}") from exc

    return JSONResponse({"path": str(destination_dir)})


@router.post("/api/batches/resume")
def resume_batch(state_path: str = Form(...)) -> JSONResponse:
    try:
        batch = batch_from_state(state_path.strip())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with batch_lock:
        existing = batches.get(batch.id)
        if existing and existing.status in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="That batch is already running in this application.")
        batches[batch.id] = batch
    persist_batch_state(batch)
    if not start_batch_worker(batch.id):
        raise HTTPException(status_code=409, detail="That batch is already running in this application.")
    return JSONResponse(public_batch(batch))


@router.post("/api/editorial-material")
def create_editorial_material(
    product_name: str = Form(""),
    manufacturer_text: str = Form(""),
    transcriptions_dir: str = Form(""),
    max_words: str = Form("3500"),
    project_id: str = Form(""),
) -> JSONResponse:
    parsed_max_words = int(max_words) if max_words.strip().isdigit() else 3500
    try:
        transcript_files = None
        if project_id.strip():
            project = library.get_project(project_id.strip())
            product_name = product_name.strip() or project["name"]
            manufacturer_text = manufacturer_text.strip() or project["manufacturer_text"]
            transcriptions_dir = transcriptions_dir.strip() or project["output_dir"]
            transcript_files = library.project_paths(project_id.strip())
        output_path = build_editorial_material(
            product_name.strip(),
            manufacturer_text.strip(),
            transcriptions_dir.strip() or default_output_dir(),
            max(500, min(parsed_max_words, 20000)),
            transcript_files=transcript_files,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"path": str(output_path)})


@router.post("/api/catalogs/claude-academy")
def import_claude_academy_webinars() -> JSONResponse:
    try:
        webinars = fetch_claude_academy_webinars()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not load the Claude Academy webinar catalog: {exc}") from exc
    providers = Counter(webinar["platform"] for webinar in webinars)
    return JSONResponse(
        {
            "count": len(webinars),
            "providers": dict(providers),
            "webinars": webinars,
            "links": [webinar["url"] for webinar in webinars],
        }
    )


@router.post("/api/jobs")
def create_job(
    url: str = Form(...),
    language: str = Form("auto"),
    model_size: str = Form("tiny"),
    prefer_captions: str = Form(""),
    output_dir: str = Form(""),
    topic_name: str = Form(""),
    save_srt: str = Form(""),
) -> JSONResponse:
    if not media_id(url):
        raise HTTPException(status_code=400, detail="Enter a valid YouTube URL or a public Claude Academy Goldcast recording URL.")

    job_id = uuid.uuid4().hex[:12]
    selected_model = model_size if model_size in ALLOWED_MODELS else "tiny"
    selected_output_dir = output_dir.strip() or default_output_dir()
    if topic_name.strip():
        selected_output_dir = str(topic_output_dir(selected_output_dir, topic_name))
    try:
        destination_dir_for(selected_output_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Could not create the output folder: {exc}") from exc

    job = Job(
        id=job_id,
        url=url.strip(),
        language=language.strip() if language.strip() in ALLOWED_LANGUAGES else "auto",
        model_size=selected_model,
        prefer_captions=form_checkbox_enabled(prefer_captions),
        output_dir=selected_output_dir,
        save_srt=save_srt == "on",
    )
    with jobs_lock:
        jobs[job_id] = job

    start_job_worker(job_id)
    return JSONResponse(public_job(job))


@router.post("/api/channel/extract")
def extract_channel(
    channel_url: str = Form(...),
    filter_type: str = Form("all"),
    limit: int = Form(10),
) -> JSONResponse:
    if not is_youtube_host(urlparse(channel_url.strip()).hostname):
        raise HTTPException(status_code=400, detail="Enter a valid YouTube URL.")
    if filter_type not in ALLOWED_CHANNEL_FILTERS:
        raise HTTPException(status_code=400, detail="The requested filter is not valid.")
    if limit <= 0 and filter_type != "all":
        raise HTTPException(status_code=400, detail="The limit must be greater than zero.")
    videos = extract_channel_videos(channel_url.strip(), filter_type, limit)
    return JSONResponse({"count": len(videos), "videos": videos, "links": [video["url"] for video in videos]})


@router.post("/api/search")
def search_videos(
    query: str = Form(...),
    limit: int = Form(10),
    sort: str = Form("relevance"),
    video_type: str = Form("all"),
    duration_filter: str = Form("any"),
    upload_filter: str = Form("any"),
    feature: str = Form("all"),
) -> JSONResponse:
    if not query.strip():
        raise HTTPException(status_code=400, detail="Enter a video search query.")
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=400, detail="The limit must be between 1 and 200.")
    if sort not in {"relevance", "most_viewed", "newest"}:
        raise HTTPException(status_code=400, detail="The requested sort order is not valid.")
    if video_type not in {"all", "videos", "shorts"}:
        raise HTTPException(status_code=400, detail="The requested video type is not valid.")
    if duration_filter not in {"any", "short", "medium", "long"}:
        raise HTTPException(status_code=400, detail="The requested duration is not valid.")
    if upload_filter not in {"any", "today", "week", "month", "year"}:
        raise HTTPException(status_code=400, detail="The requested date filter is not valid.")
    if feature not in {"all", "live", "hd", "4k", "subtitles"}:
        raise HTTPException(status_code=400, detail="The requested feature is not valid.")

    advanced_filter = video_type != "all" or duration_filter != "any" or upload_filter != "any" or feature != "all"
    fetch_limit = min(max(limit * (5 if advanced_filter else 1), 20), 300)
    prefix = "ytsearchdate" if sort == "newest" else "ytsearch"
    search_term = f"{prefix}{fetch_limit}:{query.strip()}"
    info = search_extract_info(search_term, fetch_limit)

    entries = [entry for entry in (info.get("entries", []) if info else []) if entry and entry.get("id")]
    received_count = len(entries)
    seen_ids = set()
    videos = []
    for entry in entries:
        video = video_record(entry, len(videos) + 1)
        if not video["id"] or video["id"] in seen_ids:
            continue
        seen_ids.add(video["id"])
        videos.append(video)

    metadata = {
        "attempted": 0,
        "completed": 0,
        "verified": 0,
        "failed": 0,
        "pending": 0,
        "skipped": 0,
        "time_budget_seconds": None,
    }
    needs_metadata = sort in {"most_viewed", "newest"} or advanced_filter
    if needs_metadata:
        candidate_limit = min(len(videos), max(limit * 2, 20), SEARCH_METADATA_CANDIDATE_CAP)
        videos, metadata = enrich_search_videos(
            videos,
            candidate_limit=candidate_limit,
            time_budget_seconds=SEARCH_METADATA_TIME_BUDGET_SECONDS,
            return_stats=True,
        )
        videos = videos[:candidate_limit]
    evaluated_count = len(videos)
    videos = [
        video
        for video in videos
        if (not needs_metadata or video.get("metadata_available"))
        and search_video_matches(video, video_type, duration_filter, upload_filter, feature)
    ]
    matched_count = len(videos)
    if sort == "most_viewed":
        videos.sort(key=lambda item: item.get("view_count") or -1, reverse=True)
    elif sort == "newest":
        videos.sort(key=lambda item: item.get("upload_date") or "", reverse=True)
    videos = videos[:limit]
    videos = [{**video, "position": index + 1} for index, video in enumerate(videos)]
    return JSONResponse(
        {
            "count": len(videos),
            "requested": limit,
            "received": received_count,
            "evaluated": evaluated_count,
            "matched": matched_count,
            "scanned": evaluated_count,
            "metadata": metadata,
            "videos": videos,
            "links": [video["url"] for video in videos],
        }
    )


@router.post("/api/batches")
async def create_batch(
    links_text: str = Form(""),
    links_file: Optional[UploadFile] = File(None),
    language: str = Form("auto"),
    model_size: str = Form("tiny"),
    prefer_captions: str = Form(""),
    output_dir: str = Form(""),
    save_srt: str = Form(""),
    topic_name: str = Form(""),
    start_number: str = Form(""),
    skip_existing: str = Form(""),
    generate_knowledge_base: str = Form(""),
    generate_consolidated_summary: str = Form(""),
    summary_max_words: str = Form("3500"),
    project_id: str = Form(""),
) -> JSONResponse:
    file_text = ""
    if links_file and links_file.filename:
        file_text = (await links_file.read()).decode("utf-8", errors="ignore")
    urls = parse_urls(f"{links_text}\n{file_text}")
    if not urls:
        raise HTTPException(status_code=400, detail="No supported YouTube or Claude Academy Goldcast links were found.")

    selected_model = model_size if model_size in ALLOWED_MODELS else "tiny"
    parsed_start_number = int(start_number) if start_number.strip().isdigit() else None
    parsed_summary_max_words = int(summary_max_words) if summary_max_words.strip().isdigit() else 3500
    selected_project_id = project_id.strip() or None
    selected_project = None
    if selected_project_id:
        try:
            selected_project = library.get_project(selected_project_id)
        except LibraryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    selected_output_dir = output_dir.strip() or default_output_dir()
    if selected_project:
        selected_output_dir = selected_project["output_dir"]
    elif topic_name.strip():
        selected_output_dir = str(topic_output_dir(selected_output_dir, topic_name))
    try:
        destination_dir_for(selected_output_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Could not create the output folder: {exc}") from exc
    batch_id = uuid.uuid4().hex[:12]
    batch = Batch(
        id=batch_id,
        urls=urls,
        language=language.strip() if language.strip() in ALLOWED_LANGUAGES else "auto",
        model_size=selected_model,
        prefer_captions=form_checkbox_enabled(prefer_captions),
        output_dir=selected_output_dir,
        save_srt=save_srt == "on",
        project_id=selected_project_id,
        start_number=parsed_start_number,
        skip_existing=form_checkbox_enabled(skip_existing),
        generate_knowledge_base=form_checkbox_enabled(generate_knowledge_base),
        generate_consolidated_summary=form_checkbox_enabled(generate_consolidated_summary),
        summary_max_words=max(500, min(parsed_summary_max_words, 20000)),
        total=len(urls),
    )
    batch.state_path = str(destination_dir_for(batch.output_dir) / f"batch-state-{batch_id}.json")
    with batch_lock:
        batches[batch_id] = batch

    persist_batch_state(batch)

    start_batch_worker(batch_id)
    return JSONResponse(public_batch(batch))


@router.get("/api/batches/{batch_id}")
def get_batch(batch_id: str) -> JSONResponse:
    with batch_lock:
        batch = batches.get(batch_id)
        if not batch:
            batch_file = JOBS_DIR / "batches" / f"{batch_id}.json"
            if batch_file.exists():
                return JSONResponse(json.loads(batch_file.read_text(encoding="utf-8")))
            raise HTTPException(status_code=404, detail="Batch not found.")
        data = public_batch(batch)
    return JSONResponse(data)


@router.post("/api/batches/{batch_id}/cancel")
def cancel_batch(batch_id: str) -> JSONResponse:
    with batch_lock:
        batch = batches.get(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found.")
        if batch.status not in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="This batch is not running.")
        batch.status = "cancelled"
        batch.message = "Cancelling after the current video finishes"
    persist_batch_state(batch)
    return JSONResponse(public_batch(batch))


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            job_file = JOBS_DIR / job_id / "job.json"
            if job_file.exists():
                return JSONResponse(json.loads(job_file.read_text(encoding="utf-8")))
            raise HTTPException(status_code=404, detail="Job not found.")
        data = public_job(job)
    return JSONResponse(data)


@router.get("/api/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str) -> FileResponse:
    if kind not in {"txt", "srt"}:
        raise HTTPException(status_code=404, detail="Formato no disponible.")

    with jobs_lock:
        job = jobs.get(job_id)

    if job:
        selected_path = job.transcript_path if kind == "txt" else job.srt_path
        if not selected_path:
            raise HTTPException(status_code=404, detail="Archivo no disponible.")
        path = Path(selected_path)
        filename = f"{job.output_basename or 'youtube-transcript'}.{kind}"
    else:
        candidates = sorted((JOBS_DIR / job_id).glob(f"*.{kind}"))
        path = candidates[0] if candidates else JOBS_DIR / job_id / f"transcript.{kind}"
        filename = path.name

    if not path.exists():
        raise HTTPException(status_code=404, detail="Archivo no disponible.")
    return FileResponse(path, filename=filename)
