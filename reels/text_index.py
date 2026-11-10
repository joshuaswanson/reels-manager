import multiprocessing
import tempfile
import threading
from pathlib import Path

from . import store
from .instagram import CdnError, fetch_cdn


class ReadError(Exception):
    pass


def _serve(connection) -> None:
    import objc

    from . import ocr

    while (path := connection.recv()) is not None:
        try:
            with objc.autorelease_pool():
                connection.send(ocr.read_text(Path(path)))
        except Exception as exc:
            connection.send(ReadError(str(exc)))


class Reader:
    """Reads text in a child process, because Vision holds about 2 GB until its process exits."""

    def __init__(self):
        context = multiprocessing.get_context("spawn")
        self._connection, remote = context.Pipe()
        self._process = context.Process(target=_serve, args=(remote,), daemon=True)
        self._process.start()

    def read(self, path: Path) -> str:
        self._connection.send(str(path))
        result = self._connection.recv()
        if isinstance(result, ReadError):
            raise result
        return result

    def close(self) -> None:
        try:
            self._connection.send(None)
        except OSError:
            pass
        self._process.join(timeout=5)
        if self._process.is_alive():
            self._process.kill()


class TextIndexJob:
    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.done = 0
        self.total = 0
        self.read: list[dict] = []
        self._unreachable: set[str] = set()

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self.running = True
            self.done = 0
            self.total = 0
            self.read = []
        threading.Thread(target=self._run, daemon=True).start()

    def snapshot(self, since: int | None) -> dict:
        snapshot = {"running": self.running, "done": self.done, "total": self.total, "cursor": len(self.read)}
        if since is not None:
            snapshot["read"] = self.read[since:]
        return snapshot

    def _run(self) -> None:
        try:
            rows = [row for row in store.media_without_screen_text() if row["pk"] not in self._unreachable]
            self.total = len(rows)
            if not rows:
                return
            reader = Reader()
            try:
                with tempfile.TemporaryDirectory() as scratch:
                    for row in rows:
                        self._read(row, Path(scratch), reader)
                        self.done += 1
            finally:
                reader.close()
        except (EOFError, OSError):
            pass
        finally:
            self.running = False

    def _read(self, row, scratch: Path, reader: Reader) -> None:
        pk = row["pk"]
        texts = []
        try:
            for index, slide in enumerate(store.slides(row)):
                url = slide["video_url"] or slide["image_url"]
                if not url:
                    continue
                cached = store.file_path(pk, index, bool(slide["video_url"]))
                if cached.exists():
                    texts.append(reader.read(cached))
                    continue
                temporary = scratch / cached.name
                try:
                    fetch_cdn(url, temporary)
                    texts.append(reader.read(temporary))
                finally:
                    temporary.unlink(missing_ok=True)
        except CdnError:
            # Expired links work again after a sync, which starts a new run.
            self._unreachable.add(pk)
            return
        except ReadError:
            pass
        text = "\n".join(text for text in texts if text)
        store.save_screen_text(pk, text)
        self.read.append({"pk": pk, "text": text})
