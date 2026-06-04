import re
import tempfile
import threading
import uuid
from functools import lru_cache
from pathlib import Path
from shutil import which
from typing import Any
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


BASE_DIR = Path(__file__).resolve().parent.parent
CLIENT_DIR = BASE_DIR / "client"
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "retrotube-downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_JOBS: dict[str, dict[str, Any]] = {}
DOWNLOAD_JOBS_LOCK = threading.Lock()

app = FastAPI(title="RetroTube")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class InspectRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    mode: str
    quality: int | None = None
    extension: str


def _game_over(message: str, status_code: int = 400) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "title": "GAME OVER",
            "message": message,
        },
    )


def _ydl_base_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "restrictfilenames": True,
    }

    ffmpeg_location = _ffmpeg_location()
    if ffmpeg_location:
        options["ffmpeg_location"] = ffmpeg_location

    return options


@lru_cache(maxsize=1)
def _ffmpeg_location() -> str | None:
    if which("ffmpeg"):
        return None

    try:
        import imageio_ffmpeg
    except ImportError:
        return None

    return imageio_ffmpeg.get_ffmpeg_exe()


def _has_ffmpeg() -> bool:
    return which("ffmpeg") is not None or _ffmpeg_location() is not None


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w .()-]+", "_", name, flags=re.ASCII).strip(" ._")
    return cleaned[:120] or "retrotube"


def _extract_info(url: str) -> dict[str, Any]:
    options = _ydl_base_options() | {"skip_download": True}
    try:
        with YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise _game_over(_error_message(str(exc)), 404) from exc
    except Exception as exc:
        raise _game_over(_error_message(str(exc)), 400) from exc


def _error_message(raw_error: str) -> str:
    lowered = raw_error.lower()
    if "unsupported url" in lowered or "invalid url" in lowered:
        return "LINK INVALID"
    if "private" in lowered or "sign in" in lowered or "login" in lowered:
        return "VIDEO BLOCKED"
    if "copyright" in lowered or "unavailable" in lowered or "not available" in lowered:
        return "VIDEO UNAVAILABLE"
    if "ffmpeg" in lowered:
        return "CONVERTER MISSING"
    if "format" in lowered:
        return "FORMAT NOT AVAILABLE"
    return "TRY ANOTHER LINK"


def _format_filesize(size: int | None) -> str | None:
    if not size or size <= 0:
        return None

    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1

    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    return f"{value:.1f} {units[unit_index]}"


def _filesize(format_info: dict[str, Any]) -> int | None:
    size = format_info.get("filesize") or format_info.get("filesize_approx")
    return size if isinstance(size, int) else None


def _best_audio_size(formats: list[dict[str, Any]]) -> int | None:
    audio_sizes = [
        _filesize(fmt)
        for fmt in formats
        if fmt.get("acodec") != "none" and fmt.get("vcodec") == "none"
    ]
    audio_sizes = [size for size in audio_sizes if size]
    return min(audio_sizes) if audio_sizes else None


def _estimate_mp3_size(duration: int | float | None) -> int | None:
    if not isinstance(duration, (int, float)):
        return None
    return int(duration * 192_000 / 8)


def _quality_options(formats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    can_merge = _has_ffmpeg()
    audio_size = _best_audio_size(formats) if can_merge else None
    heights = sorted(
        {
            fmt.get("height")
            for fmt in formats
            if fmt.get("vcodec") != "none"
            and (can_merge or fmt.get("acodec") != "none")
            and isinstance(fmt.get("height"), int)
        }
    )
    options = []
    for height in heights:
        matching = [
            fmt
            for fmt in formats
            if fmt.get("height") == height and fmt.get("vcodec") != "none"
        ]
        progressive_sizes = [
            _filesize(fmt)
            for fmt in matching
            if fmt.get("acodec") != "none"
        ]
        video_only_sizes = [
            _filesize(fmt)
            for fmt in matching
            if fmt.get("acodec") == "none"
        ]
        progressive_sizes = [size for size in progressive_sizes if size]
        video_only_sizes = [size for size in video_only_sizes if size]
        size = min(progressive_sizes) if progressive_sizes else None
        if size is None and video_only_sizes and audio_size:
            size = min(video_only_sizes) + audio_size

        label = f"{height}p"
        short_label = f"{label} MP4"
        size_label = _format_filesize(size)
        options.append(
            {
                "label": short_label if not size_label else f"{short_label} - {size_label}",
                "shortLabel": short_label,
                "quality": height,
                "extension": "mp4",
                "size": size,
            }
        )
    return options


def _audio_options(formats: list[dict[str, Any]], duration: int | float | None) -> list[dict[str, Any]]:
    seen = {
        fmt.get("ext")
        for fmt in formats
        if fmt.get("acodec") != "none" and fmt.get("vcodec") == "none" and fmt.get("ext")
    }
    preferred = ["mp3", "m4a", "webm", "opus"]
    options = []
    for ext in preferred:
        if ext != "mp3" and ext not in seen:
            continue

        if ext == "mp3":
            size = _estimate_mp3_size(duration)
        else:
            sizes = [
                _filesize(fmt)
                for fmt in formats
                if fmt.get("ext") == ext and fmt.get("acodec") != "none" and fmt.get("vcodec") == "none"
            ]
            sizes = [size for size in sizes if size]
            size = min(sizes) if sizes else None

        label = ext.upper()
        size_label = _format_filesize(size)
        options.append(
            {
                "label": label if not size_label else f"{label} - {size_label}",
                "shortLabel": label,
                "extension": ext,
                "size": size,
            }
        )
    return options


def _video_selector(quality: int) -> str:
    return (
        f"b[height<={quality}][ext=mp4]/"
        f"b[height<={quality}]/"
        f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height<={quality}]+ba/"
        "best"
    )


def _download_file(request: DownloadRequest, progress_hook: Any | None = None) -> tuple[Path, str]:
    info = _extract_info(str(request.url))
    title = _safe_filename(info.get("title") or "retrotube")
    workdir = Path(tempfile.mkdtemp(prefix="retrotube-", dir=DOWNLOAD_DIR))
    output_template = str(workdir / f"{title}.%(ext)s")

    if request.mode == "video":
        if request.quality is None:
            raise _game_over("FORMAT NOT AVAILABLE", 400)
        options = _ydl_base_options() | {
            "format": _video_selector(request.quality),
            "merge_output_format": "mp4",
            "outtmpl": output_template,
        }
        filename = f"{title}-{request.quality}p.mp4"
    elif request.mode == "audio":
        if request.extension == "mp3":
            options = _ydl_base_options() | {
                "format": "bestaudio/best",
                "outtmpl": output_template,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        else:
            options = _ydl_base_options() | {
                "format": f"bestaudio[ext={request.extension}]/bestaudio/best",
                "outtmpl": output_template,
            }
        filename = f"{title}.{request.extension}"
    else:
        raise _game_over("FORMAT NOT AVAILABLE", 400)

    if progress_hook:
        options["progress_hooks"] = [progress_hook]

    try:
        with YoutubeDL(options) as ydl:
            ydl.download([str(request.url)])
    except Exception as exc:
        raise _game_over(_error_message(str(exc)), 500) from exc

    files = [path for path in workdir.iterdir() if path.is_file()]
    if not files:
        raise _game_over("DOWNLOAD FAILED", 500)

    downloaded = max(files, key=lambda path: path.stat().st_mtime)
    return downloaded, filename


def _cleanup(path: Path) -> None:
    parent = path.parent
    if parent.parent == DOWNLOAD_DIR:
        for child in parent.iterdir():
            child.unlink(missing_ok=True)
        parent.rmdir()


def _set_job(job_id: str, **updates: Any) -> None:
    with DOWNLOAD_JOBS_LOCK:
        if job_id in DOWNLOAD_JOBS:
            DOWNLOAD_JOBS[job_id].update(updates)


def _get_job(job_id: str) -> dict[str, Any]:
    with DOWNLOAD_JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
        if not job:
            raise _game_over("DOWNLOAD EXPIRED", 404)
        return dict(job)


def _cleanup_job(job_id: str) -> None:
    with DOWNLOAD_JOBS_LOCK:
        job = DOWNLOAD_JOBS.pop(job_id, None)
    if job and job.get("filePath"):
        _cleanup(Path(job["filePath"]))


def _job_progress_hook(job_id: str):
    def hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes") or 0
            percent = int(downloaded * 100 / total) if total else None
            _set_job(
                job_id,
                status="running",
                stage="DOWNLOADING",
                progress=percent,
                downloaded=downloaded,
                total=total,
            )
        elif status == "finished":
            _set_job(job_id, status="running", stage="CONVERTING", progress=100)

    return hook


def _run_download_job(job_id: str, request: DownloadRequest) -> None:
    try:
        _set_job(job_id, status="running", stage="PREPARING", progress=0)
        path, filename = _download_file(request, _job_progress_hook(job_id))
        _set_job(
            job_id,
            status="ready",
            stage="READY",
            progress=100,
            filePath=str(path),
            filename=filename,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        _set_job(
            job_id,
            status="error",
            stage="GAME OVER",
            error=detail.get("message", "TRY ANOTHER LINK"),
            progress=0,
        )
    except Exception as exc:
        _set_job(
            job_id,
            status="error",
            stage="GAME OVER",
            error=_error_message(str(exc)),
            progress=0,
        )


@app.post("/api/inspect")
def inspect_video(request: InspectRequest) -> dict[str, Any]:
    info = _extract_info(str(request.url))
    formats = info.get("formats") or []
    video_options = _quality_options(formats)
    audio_options = _audio_options(formats, info.get("duration"))

    if not video_options and not audio_options:
        raise _game_over("FORMAT NOT AVAILABLE", 404)

    return {
        "status": "YOU WIN",
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel") or "Unknown",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "videoOptions": video_options,
        "audioOptions": audio_options,
    }


@app.post("/api/download/start")
def start_download(request: DownloadRequest) -> dict[str, str]:
    job_id = uuid.uuid4().hex
    with DOWNLOAD_JOBS_LOCK:
        DOWNLOAD_JOBS[job_id] = {
            "status": "queued",
            "stage": "QUEUED",
            "progress": 0,
            "filename": None,
            "filePath": None,
            "error": None,
        }

    thread = threading.Thread(target=_run_download_job, args=(job_id, request), daemon=True)
    thread.start()
    return {"jobId": job_id}


@app.get("/api/download/progress/{job_id}")
def download_progress(job_id: str) -> dict[str, Any]:
    job = _get_job(job_id)
    return {
        "jobId": job_id,
        "status": job.get("status"),
        "stage": job.get("stage"),
        "progress": job.get("progress"),
        "downloaded": job.get("downloaded"),
        "total": job.get("total"),
        "filename": job.get("filename"),
        "error": job.get("error"),
    }


@app.get("/api/download/file/{job_id}")
def download_file(job_id: str, background_tasks: BackgroundTasks) -> FileResponse:
    job = _get_job(job_id)
    if job.get("status") != "ready" or not job.get("filePath"):
        raise _game_over("DOWNLOAD NOT READY", 409)

    path = Path(job["filePath"])
    filename = job.get("filename") or path.name
    background_tasks.add_task(_cleanup_job, job_id)
    quoted = quote(filename)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
        background=background_tasks,
    )


@app.post("/api/download")
def download_media(request: DownloadRequest, background_tasks: BackgroundTasks) -> FileResponse:
    path, filename = _download_file(request)
    background_tasks.add_task(_cleanup, path)
    quoted = quote(filename)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
        background=background_tasks,
    )


app.mount("/", StaticFiles(directory=CLIENT_DIR, html=True), name="client")
