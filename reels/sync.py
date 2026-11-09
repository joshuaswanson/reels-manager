import threading
import time

from . import store
from .instagram import Instagram, InstagramError, summarize


class StreamedCollection:
    def __init__(self, collection_id: str, name: str):
        self.id = collection_id
        self.name = name
        self.members: list[str] = []

    def pks(self) -> list[str]:
        return list(dict.fromkeys(self.members))

    def to_client(self) -> dict:
        return {"id": self.id, "name": self.name, "pks": self.pks()}


class SyncJob:
    def __init__(self):
        self._lock = threading.Lock()
        self.status = {"running": False, "message": "", "error": None, "finishedAt": None}
        self.fetched: list[dict] = []
        self.by_pk: dict[str, dict] = {}
        self.collections: list[StreamedCollection] = []

    def start(self, client: Instagram) -> bool:
        with self._lock:
            if self.status["running"]:
                return False
            self.status = {"running": True, "message": "Connecting to Instagram", "error": None, "finishedAt": None}
            self.fetched = []
            self.by_pk = {}
            self.collections = []
        threading.Thread(target=self._run, args=(client,), daemon=True).start()
        return True

    def snapshot(self, since: int | None) -> dict:
        snapshot = {**self.status, "total": len(self.fetched)}
        if since is not None:
            snapshot["media"] = [store.to_client(m) for m in self.fetched[since:]]
            snapshot["collections"] = [c.to_client() for c in list(self.collections)]
        return snapshot

    def _report(self, message: str) -> None:
        self.status = {**self.status, "message": message}

    def _keep(self, raw: dict) -> str:
        summary = summarize(raw)
        if summary["pk"] not in self.by_pk:
            self.by_pk[summary["pk"]] = summary
            self.fetched.append(summary)
        return summary["pk"]

    def _run(self, client: Instagram) -> None:
        try:
            by_id: dict[str, StreamedCollection] = {}
            known_names = store.collection_names()

            for item in client.saved_media():
                pk = self._keep(item)
                for raw_id in item.get("saved_collection_ids") or []:
                    cid = str(raw_id)
                    if cid not in by_id:
                        by_id[cid] = StreamedCollection(cid, known_names.get(cid, f"Collection {len(by_id) + 1}"))
                        self.collections.append(by_id[cid])
                    by_id[cid].members.append(pk)
                self._report(f"Read {len(self.fetched)} saved posts")

            store.replace_library(
                self.fetched,
                [{"id": c.id, "name": c.name} for c in self.collections],
                {c.id: c.pks() for c in self.collections},
            )
            store.save_session(client.cookies, (store.load_session() or {}).get("username", ""))
            self.status = {
                "running": False,
                "message": f"Synced {len(self.fetched)} saved posts and {len(self.collections)} collections",
                "error": None,
                "finishedAt": int(time.time()),
            }
        except InstagramError as exc:
            self.status = {"running": False, "message": "", "error": str(exc), "finishedAt": int(time.time())}
        except Exception as exc:
            self.status = {
                "running": False,
                "message": "",
                "error": f"Sync failed with {type(exc).__name__}: {exc}",
                "finishedAt": int(time.time()),
            }
