import json
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from urllib.parse import unquote

import httpx

API_BASE = "https://www.instagram.com/api/v1"
WEB_APP_ID = "936619743392459"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
# Minimum gap between any two API requests, so large syncs and bulk downloads do not trip Instagram's rate limits.
MIN_REQUEST_INTERVAL_SECONDS = 1.0
# From Instagram's web Relay operations, inspected 2026-11-05.
WEB_MUTATIONS = {
    "create": ("PolarisAPICreateCollectionMutation", "27305499509043563", "xig_media_collection_create", "collection"),
    "add": ("PolarisAPIEditCollectionAddMediaMutation", "27261476466806134", "xig_media_collection_add_media", "collection"),
    "remove": ("PolarisAPIEditCollectionRemoveMediaMutation", "27067931769494869", "xig_media_collection_remove_media", "collection"),
    "unsave": ("PolarisAPIUnsavePostMutation", "27203919972607144", "xig_media_unsave", "media"),
}


class InstagramError(Exception):
    pass


class SessionExpired(InstagramError):
    pass


class CdnError(Exception):
    pass


def parse_cookie(raw: str) -> dict[str, str]:
    raw = raw.strip().removeprefix("Cookie:").strip()
    if "=" not in raw:
        return {"sessionid": raw}
    cookies = {}
    for part in raw.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies[name] = value.strip('"')
    return cookies


class Instagram:
    def __init__(self, cookies: dict[str, str]):
        if not cookies.get("sessionid"):
            raise InstagramError("The cookie has no sessionid. Copy the full Cookie header from a logged-in browser.")
        self.cookies = dict(cookies)
        self._throttle = threading.Lock()
        self._last_request = 0.0
        self._web_tokens: dict | None = None
        self._web_nonce = ""
        self.http = httpx.Client(
            base_url=API_BASE,
            timeout=30,
            headers={
                "User-Agent": USER_AGENT,
                "X-IG-App-ID": WEB_APP_ID,
                "X-ASBD-ID": "359341",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Accept-Language": "en-US,en;q=0.9",
                "X-IG-Max-Touch-Points": "0",
                "Referer": "https://www.instagram.com/",
                "Origin": "https://www.instagram.com",
                "Accept": "*/*",
            },
        )

    @property
    def user_id(self) -> str:
        return self.cookies.get("ds_user_id") or unquote(self.cookies["sessionid"]).split(":")[0]

    def _send(self, method: str, url: str, headers: dict | None = None, **kwargs) -> httpx.Response:
        headers = {
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
            "X-CSRFToken": self.cookies.get("csrftoken", ""),
            **(headers or {}),
        }
        with self._throttle:
            wait = self._last_request + MIN_REQUEST_INTERVAL_SECONDS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.http.request(method, url, headers=headers, **kwargs)
            finally:
                self._last_request = time.monotonic()
        for cookie in response.cookies.jar:
            if cookie.value and cookie.value != '""':
                self.cookies[cookie.name] = cookie.value
        if response.status_code == 429:
            raise InstagramError("Instagram is rate limiting requests. Wait a few minutes and try again.")
        if response.is_redirect:
            raise SessionExpired("Instagram redirected to the login page. Reconnect your account.")
        return response

    def _request(self, method: str, path: str, **kwargs) -> dict:
        response = self._send(method, path, **kwargs)
        try:
            payload = response.json()
        except ValueError:
            raise InstagramError(f"Instagram answered {path} with HTTP {response.status_code} and no JSON body.")

        message = payload.get("message") or ""
        if message == "login_required" or response.status_code == 401:
            raise SessionExpired("Your Instagram session expired. Paste a fresh cookie.")
        if message == "checkpoint_required" or "challenge" in payload:
            raise InstagramError("Instagram wants to confirm it is you. Open instagram.com, finish the check, then try again.")
        if payload.get("status") != "ok" or response.status_code >= 400:
            raise InstagramError(f"Instagram rejected {path}: {message or f'HTTP {response.status_code}'}")
        return payload

    def _paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        params = dict(params or {})
        while True:
            page = self._request("GET", path, params=params)
            yield page
            next_max_id = page.get("next_max_id")
            if not page.get("more_available") or not next_max_id:
                return
            params["max_id"] = next_max_id

    def username(self) -> str:
        try:
            return self._request("GET", f"/users/{self.user_id}/info/")["user"]["username"]
        except (InstagramError, KeyError):
            return ""

    def verify(self) -> None:
        self._request("GET", "/feed/saved/posts/")

    def saved_media(self) -> Iterator[dict]:
        for page in self._paginate("/feed/saved/posts/"):
            for item in page.get("items", []):
                yield item["media"]

    def media_info(self, pk: str) -> dict:
        return self._request("GET", f"/media/{pk}/info/")["items"][0]

    def comments(self, pk: str, cursor: str | None = None) -> dict:
        params = {"can_support_threading": "true", "permalinks_enabled": "false"}
        if cursor:
            params["min_id"] = cursor
        page = self._request("GET", f"/media/{pk}/comments/", params=params)
        return {
            "count": page.get("comment_count") or 0,
            "comments": [_comment(c) for c in page.get("comments", [])],
            "cursor": page.get("next_min_id") if page.get("has_more_headload_comments") else None,
        }

    def replies(self, pk: str, comment_pk: str, cursor: str | None = None) -> dict:
        page = self._request("GET", f"/media/{pk}/comments/{comment_pk}/child_comments/", params={"max_id": cursor} if cursor else None)
        return {
            "comments": [_comment(c) for c in page.get("child_comments", [])],
            "cursor": page.get("next_max_child_cursor") if page.get("has_more_tail_child_comments") else None,
        }

    def _load_web_tokens(self) -> dict:
        response = self._send("GET", "https://www.instagram.com/")
        nonce = re.search(r'nonce="([^"]+)"', response.text)
        self._web_nonce = nonce[1] if nonce else ""
        tokens = {}
        for module, field in (("DTSGInitialData", "fb_dtsg"), ("LSD", "lsd")):
            match = re.search(r'"' + module + r'",\[\],(\{.*?\}),\d+\]', response.text)
            if match:
                tokens[field] = json.loads(match[1]).get("token")
        if response.status_code != 200 or not tokens.get("fb_dtsg") or not tokens.get("lsd"):
            raise SessionExpired("Could not load Instagram's web session. Reconnect your account.")
        user = re.search(r'"CurrentUserInitialData",\[\],(\{.*?\}),\d+\]', response.text)
        if not user:
            raise SessionExpired("Instagram's web account context is missing. Reconnect your account.")
        tokens["av"] = json.loads(user[1])["NON_FACEBOOK_USER_ID"]
        tokens["jazoest"] = "2" + str(sum(map(ord, tokens["fb_dtsg"])))
        site = re.search(r'"SiteData",\[\],(\{.*?\}),\d+\]', response.text)
        if site:
            data = json.loads(site[1])
            for form, key in (("__hs", "haste_session"), ("__rev", "client_revision"), ("__hsi", "hsi"), ("__spin_r", "__spin_r"), ("__spin_b", "__spin_b"), ("__spin_t", "__spin_t")):
                if key in data:
                    tokens[form] = str(data[key])
        self._web_tokens = tokens
        return tokens

    def _web_mutation(self, action: str, inputs: dict) -> dict:
        name, doc_id, result_field, object_field = WEB_MUTATIONS[action]
        tokens = self._web_tokens or self._load_web_tokens()
        response = self._send("POST", "https://www.instagram.com/api/graphql", headers={
            "X-FB-Entry-Nonce": self._web_nonce, "X-FB-Friendly-Name": name, "X-FB-LSD": tokens["lsd"], "X-IG-D": "www",
        }, data={
            **tokens, "__req": "1", "__crn": "comet.igweb.PolarisFeedRoute", "__d": "www", "__user": "0", "__a": "1", "__comet_req": "7",
            "fb_api_caller_class": "RelayModern", "fb_api_req_friendly_name": name,
            "doc_id": doc_id, "variables": json.dumps({"input": {
                **inputs, "actor_id": tokens["av"], "client_mutation_id": uuid.uuid4().hex,
            }}),
            "server_timestamps": "true",
        })
        try:
            payload = json.loads(response.text.removeprefix("for (;;);"))
        except ValueError:
            raise InstagramError(f"Instagram answered {name} with HTTP {response.status_code} and no JSON body.")
        if response.status_code in (401, 403):
            self._web_tokens = None
            raise SessionExpired("Instagram rejected the web session. Reconnect your account.")
        if payload.get("error"):
            raise InstagramError(f"Instagram rejected {name}: {payload.get('errorSummary', 'Request failed')} (code {payload['error']})")
        errors = payload.get("errors") or []
        if errors or response.status_code >= 400:
            message = errors[0].get("message", "Mutation failed") if errors else f"HTTP {response.status_code}"
            raise InstagramError(f"Instagram rejected {name}: {message}")
        result = (payload.get("data") or {}).get(result_field)
        if not isinstance(result, dict) or not (result.get(object_field) or {}).get("id"):
            raise InstagramError(f"Instagram did not confirm {name}. Refresh before retrying.")
        return result

    def unsave_media(self, pk: str, collection_id: str | None = None) -> None:
        if collection_id is not None:
            self.edit_collection(collection_id, remove=[pk])
        else:
            self._web_mutation("unsave", {"media_id": pk.split("_")[0]})

    def edit_collection(self, collection_id: str, add: Sequence[str] = (), remove: Sequence[str] = ()) -> None:
        for action, ids in (("add", add), ("remove", remove)):
            for media_id in ids:
                self._web_mutation(action, {
                    "collection_id": collection_id, "media_id": media_id.split("_")[0],
                })

    def create_collection(self, name: str, media_ids: list[str]) -> dict:
        result = self._web_mutation("create", {
            "name": name,
            "added_media_ids": list(dict.fromkeys(media_id.split("_")[0] for media_id in media_ids)),
        })
        collection = result["collection"]
        return {"id": str(collection["id"]), "name": collection.get("name") or name}


def _comment(comment: dict) -> dict:
    return {
        "pk": str(comment["pk"]),
        "username": (comment.get("user") or {}).get("username", ""),
        "text": comment.get("text", ""),
        "createdAt": comment.get("created_at"),
        "likeCount": comment.get("comment_like_count") or 0,
        "replyCount": comment.get("child_comment_count") or 0,
    }


def _closest_image(media: dict, width: int) -> str | None:
    candidates = (media.get("image_versions2") or {}).get("candidates") or []
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(c.get("width", 0) - width))["url"]


def _slide(item: dict) -> dict:
    videos = item.get("video_versions") or []
    best_video = max(videos, key=lambda v: v.get("width", 0) * v.get("height", 0)) if videos else None
    return {
        "thumb_url": _closest_image(item, 540),
        "image_url": _closest_image(item, 1440),
        "video_url": best_video["url"] if best_video else None,
    }


def summarize(media: dict) -> dict:
    items = media.get("carousel_media") or [media]
    slides = [_slide(item) for item in items]
    first = items[0]
    user = media.get("user") or {}
    return {
        "pk": str(media["pk"]),
        "id": str(media["id"]),
        "code": media.get("code") or "",
        "is_reel": media.get("product_type") == "clips",
        "username": user.get("username", ""),
        "full_name": user.get("full_name", ""),
        "caption": (media.get("caption") or {}).get("text", ""),
        "taken_at": media.get("taken_at"),
        "thumb_url": slides[0]["thumb_url"],
        "image_url": slides[0]["image_url"],
        "video_url": slides[0]["video_url"],
        "slides": json.dumps(slides),
        "duration": first.get("video_duration") or media.get("video_duration"),
        "play_count": media.get("play_count") or media.get("ig_play_count") or media.get("view_count"),
        "like_count": media.get("like_count"),
    }


def fetch_cdn(url: str, dest: Path) -> None:
    partial = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.part")
    try:
        with httpx.stream("GET", url, headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True) as response:
            if response.status_code != 200:
                raise CdnError(f"CDN answered HTTP {response.status_code}")
            with partial.open("wb") as f:
                for chunk in response.iter_bytes(65536):
                    f.write(chunk)
        os.replace(partial, dest)
    except httpx.HTTPError as exc:
        raise CdnError(str(exc)) from exc
    finally:
        partial.unlink(missing_ok=True)
