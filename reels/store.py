import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / "Library" / "Application Support" / "Reels Manager"
DATA_DIR = Path(os.environ.get("REELS_DATA_DIR", DEFAULT_DATA_DIR))
THUMBS_DIR = DATA_DIR / "thumbs"
FILES_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "library.db"
SESSION_PATH = DATA_DIR / "session.json"

MEDIA_COLUMNS = (
    "pk", "id", "code", "is_reel", "username", "full_name", "caption", "taken_at",
    "thumb_url", "image_url", "video_url", "duration", "play_count", "like_count", "slides",
)

SCHEMA = f"""
create table if not exists media (
    {", ".join(MEDIA_COLUMNS)}, saved_rank integer,
    primary key (pk)
);
create table if not exists collections (id text primary key, name text not null, rank integer);
create table if not exists membership (
    collection_id text not null, media_pk text not null, rank integer,
    primary key (collection_id, media_pk)
);
create table if not exists meta (key text primary key, value text);
create table if not exists collection_names (id text primary key, name text not null);
create table if not exists screen_text (pk text primary key, text text not null);
"""

for directory in (DATA_DIR, THUMBS_DIR, FILES_DIR):
    directory.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


with connect() as _conn:
    _conn.executescript(SCHEMA)
    existing_columns = {row["name"] for row in _conn.execute("pragma table_info(media)")}
    for column in MEDIA_COLUMNS:
        if column not in existing_columns:
            _conn.execute(f"alter table media add column {column}")


def slides(row) -> list[dict]:
    if row["slides"]:
        return json.loads(row["slides"])
    return [{"thumb_url": row["thumb_url"], "image_url": row["image_url"], "video_url": row["video_url"]}]


def file_path(pk: str, slide: int, is_video: bool) -> Path:
    stem = pk if slide == 0 else f"{pk}_{slide}"
    return FILES_DIR / f"{stem}.{'mp4' if is_video else 'jpg'}"


def load_session() -> dict | None:
    if not SESSION_PATH.exists():
        return None
    return json.loads(SESSION_PATH.read_text())


def save_session(cookies: dict[str, str], username: str) -> None:
    SESSION_PATH.write_text(json.dumps({"cookies": cookies, "username": username}))
    SESSION_PATH.chmod(0o600)


def clear_session() -> None:
    SESSION_PATH.unlink(missing_ok=True)


def replace_library(media: list[dict], collections: list[dict], membership: dict[str, list[str]]) -> None:
    placeholders = ", ".join("?" for _ in MEDIA_COLUMNS)
    with connect() as conn:
        conn.execute("delete from media")
        conn.execute("delete from collections")
        conn.execute("delete from membership")
        conn.executemany(
            f"insert into media ({', '.join(MEDIA_COLUMNS)}, saved_rank) values ({placeholders}, ?)",
            [[m[c] for c in MEDIA_COLUMNS] + [rank] for rank, m in enumerate(media)],
        )
        conn.executemany(
            "insert into collections (id, name, rank) values (?, ?, ?)",
            [(c["id"], c["name"], rank) for rank, c in enumerate(collections)],
        )
        conn.executemany(
            "insert or ignore into membership (collection_id, media_pk, rank) values (?, ?, ?)",
            [(cid, pk, rank) for cid, pks in membership.items() for rank, pk in enumerate(pks)],
        )
        conn.execute("insert or replace into meta (key, value) values ('last_sync', ?)", (str(int(time.time())),))


def to_client(row) -> dict:
    return {
        "pk": row["pk"],
        "code": row["code"],
        "isReel": bool(row["is_reel"]),
        "isVideo": bool(row["video_url"]),
        "slides": [{"isVideo": bool(slide["video_url"])} for slide in slides(row)],
        "username": row["username"],
        "fullName": row["full_name"],
        "caption": row["caption"],
        "takenAt": row["taken_at"],
        "duration": row["duration"],
        "playCount": row["play_count"],
        "likeCount": row["like_count"],
    }


def library() -> dict:
    with connect() as conn:
        texts = {row["pk"]: row["text"] for row in conn.execute("select pk, text from screen_text")}
        media = [
            {**to_client(row), "screenText": texts.get(row["pk"], "")}
            for row in conn.execute("select * from media order by saved_rank")
        ]
        collections = [
            {
                "id": row["id"],
                "name": row["name"],
                "pks": [
                    r["media_pk"]
                    for r in conn.execute(
                        "select media_pk from membership where collection_id = ? order by rank", (row["id"],)
                    )
                ],
            }
            for row in conn.execute("select * from collections order by rank")
        ]
        last_sync = conn.execute("select value from meta where key = 'last_sync'").fetchone()
    return {"media": media, "collections": collections, "lastSync": int(last_sync[0]) if last_sync else None}


def media(pk: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("select * from media where pk = ?", (pk,)).fetchone()


def media_ids(pks: list[str]) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            f"select pk, id from media where pk in ({', '.join('?' for _ in pks)})", pks
        ).fetchall()
    ids = {row["pk"]: row["id"] for row in rows}
    return [ids[pk] for pk in pks if pk in ids]


def update_urls(pk: str, fresh: dict) -> None:
    with connect() as conn:
        conn.execute(
            "update media set thumb_url = ?, image_url = ?, video_url = ?, slides = ? where pk = ?",
            (fresh["thumb_url"], fresh["image_url"], fresh["video_url"], fresh["slides"], pk),
        )


def collection(collection_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("select * from collections where id = ?", (collection_id,)).fetchone()


def collection_names() -> dict[str, str]:
    with connect() as conn:
        return {row["id"]: row["name"] for row in conn.execute("select id, name from collection_names")}


def set_collection_name(collection_id: str, name: str) -> None:
    with connect() as conn:
        conn.execute("insert or replace into collection_names (id, name) values (?, ?)", (collection_id, name))
        conn.execute("update collections set name = ? where id = ?", (name, collection_id))


def add_collection(collection_id: str, name: str) -> None:
    set_collection_name(collection_id, name)
    with connect() as conn:
        conn.execute(
            "insert or replace into collections (id, name, rank) "
            "values (?, ?, (select coalesce(min(rank), 0) - 1 from collections))",
            (collection_id, name),
        )


def add_membership(collection_id: str, pks: list[str]) -> None:
    with connect() as conn:
        top = conn.execute(
            "select coalesce(min(rank), 0) from membership where collection_id = ?", (collection_id,)
        ).fetchone()[0]
        conn.executemany(
            "insert or ignore into membership (collection_id, media_pk, rank) values (?, ?, ?)",
            [(collection_id, pk, top - len(pks) + i) for i, pk in enumerate(pks)],
        )


def remove_membership(collection_id: str, pks: list[str]) -> None:
    with connect() as conn:
        conn.executemany(
            "delete from membership where collection_id = ? and media_pk = ?",
            [(collection_id, pk) for pk in pks],
        )


def unsave_media(pk: str) -> None:
    with connect() as conn:
        conn.execute("delete from screen_text where pk = ?", (pk,))
        conn.execute("delete from membership where media_pk = ?", (pk,))
        conn.execute("delete from media where pk = ?", (pk,))


def media_without_screen_text() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "select * from media where pk not in (select pk from screen_text) order by saved_rank"
        ).fetchall()


def save_screen_text(pk: str, text: str) -> None:
    with connect() as conn:
        conn.execute("insert or replace into screen_text (pk, text) values (?, ?)", (pk, text))
