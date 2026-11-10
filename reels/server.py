import re
import threading
from contextlib import contextmanager
import tempfile
import zipfile
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import browser_cookies, store
from .instagram import CdnError, Instagram, InstagramError, SessionExpired, fetch_cdn, parse_cookie, summarize
from .sync import SyncJob
from .text_index import TextIndexJob

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Reels manager")
sync_job = SyncJob()
text_index_job = TextIndexJob()
_client: Instagram | None = None
_edit_lock = threading.Lock()


def client() -> Instagram:
    global _client
    if _client is None:
        session = store.load_session()
        if not session:
            raise SessionExpired("Connect your Instagram account first.")
        _client = Instagram(session["cookies"])
    return _client


@app.exception_handler(SessionExpired)
def session_expired(_: Request, exc: SessionExpired):
    return JSONResponse({"error": str(exc), "signedOut": True}, status_code=401)


@app.exception_handler(InstagramError)
def instagram_error(_: Request, exc: InstagramError):
    return JSONResponse({"error": str(exc)}, status_code=502)


class SessionBody(BaseModel):
    cookie: str


class PendingEditsBody(BaseModel):
    count: int


pending_edits = {"count": 0}


@app.post("/api/pending-edits")
def report_pending_edits(body: PendingEditsBody):
    pending_edits["count"] = max(0, body.count)
    return {"ok": True}


class PksBody(BaseModel):
    pks: list[str]


class MoveBody(PksBody):
    to_id: str


class RenameBody(BaseModel):
    name: str


class CreateCollectionBody(BaseModel):
    name: str
    pks: list[str] = []


@app.get("/api/state")
def state():
    session = store.load_session()
    return {"signedIn": session is not None, "username": session["username"] if session else None}


def _connect(cookies: dict[str, str]) -> dict:
    global _client
    candidate = Instagram(cookies)
    candidate.verify()
    username = candidate.username()
    store.save_session(candidate.cookies, username)
    _client = candidate
    return {"signedIn": True, "username": username}


@app.post("/api/session")
def sign_in(body: SessionBody):
    return _connect(parse_cookie(body.cookie))


@app.post("/api/session/browser")
def sign_in_from_browser():
    try:
        cookies, browser = browser_cookies.instagram_cookies()
    except browser_cookies.BrowserCookieError as exc:
        raise HTTPException(400, str(exc))
    return {**_connect(cookies), "browser": browser}


@app.delete("/api/session")
def sign_out():
    global _client
    store.clear_session()
    _client = None
    return {"signedIn": False}


@app.get("/api/library")
def library():
    return store.library()


@app.post("/api/sync")
def start_sync():
    with _edit_lock:
        sync_job.start(client())
    return sync_job.snapshot(None)


@app.get("/api/sync")
def sync_status(since: int | None = None):
    return sync_job.snapshot(since)


@app.post("/api/text-index")
def start_text_index():
    if not sync_job.status["running"]:
        text_index_job.start()
    return text_index_job.snapshot(None)


@app.get("/api/text-index")
def text_index_status(since: int | None = None):
    return text_index_job.snapshot(since)


def _require_collection(collection_id: str):
    row = store.collection(collection_id)
    if row is None:
        raise HTTPException(404, "That collection is not in the library. Sync and try again.")
    return row


@app.post("/api/collections")
def create_collection(body: CreateCollectionBody):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Give the collection a name.")
    with _editing(body.pks, allow_empty=True) as pks:
        created = client().create_collection(name, store.media_ids(pks))
        store.add_collection(created["id"], created["name"])
        store.add_membership(created["id"], pks)
    return created


@app.post("/api/collections/{collection_id}/rename")
def rename_collection(collection_id: str, body: RenameBody):
    _require_collection(collection_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Give the collection a name.")
    store.set_collection_name(collection_id, name)
    return {"id": collection_id, "name": name}


@contextmanager
def _editing(pks: list[str], *, allow_empty: bool = False):
    with _edit_lock:
        if sync_job.status["running"]:
            raise HTTPException(409, "Wait for the sync to finish before editing saved posts.")
        unique = list(dict.fromkeys(pks))
        if not unique and not allow_empty:
            raise HTTPException(400, "Select at least one post.")
        if any(store.media(pk) is None for pk in unique):
            raise HTTPException(404, "A selected post is not in the library. Refresh and try again.")
        yield unique


def _add(collection_id: str, pks: list[str]):
    for pk in pks:
        client().edit_collection(collection_id, add=store.media_ids([pk]))
        store.add_membership(collection_id, [pk])


def _remove(collection_id: str, pks: list[str]):
    for pk in pks:
        client().unsave_media(pk, collection_id)
        store.remove_membership(collection_id, [pk])


@app.post("/api/collections/{collection_id}/add")
def add_to_collection(collection_id: str, body: PksBody):
    with _editing(body.pks) as pks:
        _require_collection(collection_id)
        _add(collection_id, pks)
    return {"ok": True}


@app.post("/api/collections/{collection_id}/remove")
def remove_from_collection(collection_id: str, body: PksBody):
    with _editing(body.pks) as pks:
        _require_collection(collection_id)
        _remove(collection_id, pks)
    return {"ok": True}


@app.post("/api/collections/{collection_id}/move")
def move_to_collection(collection_id: str, body: MoveBody):
    with _editing(body.pks) as pks:
        _require_collection(collection_id)
        _require_collection(body.to_id)
        if collection_id != body.to_id:
            # Finish every destination save before removing any source membership.
            _add(body.to_id, pks)
            _remove(collection_id, pks)
    return {"ok": True}


@app.post("/api/media/unsave")
def unsave_media(body: PksBody):
    with _editing(body.pks) as pks:
        for pk in pks:
            client().unsave_media(pk)
            store.unsave_media(pk)
    return {"ok": True}


def _media_or_404(pk: str):
    row = store.media(pk) or sync_job.by_pk.get(pk)
    if row is None:
        raise HTTPException(404, "That post is not in the library.")
    return row


@app.get("/api/media/{pk}/comments")
def comments(pk: str, cursor: str | None = None):
    _media_or_404(pk)
    return client().comments(pk, cursor)


@app.get("/api/media/{pk}/comments/{comment_pk}/replies")
def replies(pk: str, comment_pk: str, cursor: str | None = None):
    _media_or_404(pk)
    return client().replies(pk, comment_pk, cursor)


_download_locks: dict[str, threading.Lock] = {}
_download_locks_guard = threading.Lock()


def _download_lock(key: str) -> threading.Lock:
    with _download_locks_guard:
        return _download_locks.setdefault(key, threading.Lock())


def _slide_or_404(row, slide: int) -> dict:
    slides = store.slides(row)
    if not 0 <= slide < len(slides):
        raise HTTPException(404, "That post has no item at this position.")
    return slides[slide]


def _cached_file(pk: str, slide: int = 0, refresh_expired: bool = True) -> tuple[Path, str]:
    row = _media_or_404(pk)
    dest = store.file_path(pk, slide, bool(_slide_or_404(row, slide)["video_url"]))
    extension = dest.suffix.removeprefix(".")
    if dest.exists():
        return dest, extension
    with _download_lock(f"{pk}:{slide}"):
        if not dest.exists():
            _download(pk, row, slide, dest, refresh_expired)
    return dest, extension


def _download(pk: str, row, slide: int, dest: Path, refresh_expired: bool) -> None:
    item = _slide_or_404(row, slide)
    try:
        fetch_cdn(item["video_url"] or item["image_url"], dest)
    except CdnError:
        if not refresh_expired:
            raise
        fresh = summarize(client().media_info(pk))
        store.update_urls(pk, fresh)
        fresh_item = _slide_or_404(fresh, slide)
        try:
            fetch_cdn(fresh_item["video_url"] or fresh_item["image_url"], dest)
        except CdnError as exc:
            raise HTTPException(502, f"Instagram's CDN refused the file. {exc}")


def _download_name(row, extension: str, position: int | None = None) -> str:
    parts = [row["username"], row["code"] or row["pk"]]
    if position is not None:
        parts.append(str(position))
    stem = "_".join(part for part in parts if part)
    return f"{re.sub(r'[^A-Za-z0-9._-]', '_', stem)}.{extension}"


def _post_files(pk: str) -> list[tuple[Path, str]]:
    row = _media_or_404(pk)
    count = len(store.slides(row))
    files = []
    for slide in range(count):
        path, extension = _cached_file(pk, slide)
        files.append((path, _download_name(row, extension, slide + 1 if count > 1 else None)))
    return files


def _zip_response(entries: list[tuple[Path, str]], name: str) -> FileResponse:
    archive = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    archive.close()
    archive_path = Path(archive.name)
    try:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as zf:
            for path, arcname in entries:
                zf.write(path, arcname)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip() or "reels-manager"
    return FileResponse(
        archive_path,
        filename=f"{safe_name}.zip",
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@app.get("/media/{pk}/thumb")
def thumbnail(pk: str, slide: int = 0):
    dest = store.THUMBS_DIR / store.file_path(pk, slide, False).name
    if not dest.exists():
        thumb_url = _slide_or_404(_media_or_404(pk), slide)["thumb_url"]
        if not thumb_url:
            raise HTTPException(404)
        try:
            fetch_cdn(thumb_url, dest)
        except CdnError:
            raise HTTPException(404, "Thumbnail link expired. Sync to refresh it.")
    return FileResponse(dest, media_type="image/jpeg", headers={"Cache-Control": "max-age=31536000, immutable"})


@app.get("/media/{pk}/file")
def media_file(pk: str, slide: int = 0):
    path, extension = _cached_file(pk, slide)
    return FileResponse(path, media_type="video/mp4" if extension == "mp4" else "image/jpeg")


def _prefetch(pk: str, slide: int) -> None:
    try:
        _cached_file(pk, slide, refresh_expired=False)
    except (HTTPException, CdnError):
        pass


@app.post("/media/{pk}/prefetch")
def prefetch(pk: str, background: BackgroundTasks, slide: int = 0):
    background.add_task(_prefetch, pk, slide)
    return {"ok": True}


@app.get("/media/{pk}/download")
def download(pk: str):
    files = _post_files(pk)
    if len(files) == 1:
        path, filename = files[0]
        return FileResponse(path, filename=filename)
    return _zip_response(files, Path(_download_name(_media_or_404(pk), "zip")).stem)


@app.post("/api/download.zip")
def download_zip(pks: str = Form(...), name: str = Form("reels-manager")):
    entries = [entry for pk in pks.split(",") if pk for entry in _post_files(pk)]
    return _zip_response(entries, name)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
