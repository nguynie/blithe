from dataclasses import dataclass
import json
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import requests

logger = logging.getLogger(__name__)


@dataclass
class FacebookPost:
    post_id: str
    url: str
    creation_time: Optional[int] = None
    message: Optional[str] = None


class FacebookScraper:
    def __init__(self, cookies: Optional[Dict[str, str]] = None):
        self.session = requests.Session()
        self.last_auth_error: Optional[str] = None
        self.consecutive_auth_failures: int = 0
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        if cookies:
            self.session.cookies.update(cookies)

    @staticmethod
    def clean_url(raw_url: str) -> str:
        """
        Clean URL by stripping Facebook tracking parameters while preserving
        critical parameters like story_fbid, id, fbid, etc.
        """
        if not raw_url:
            return ""
        unescaped = raw_url.replace(r"\/", "/")
        parsed = urlparse(unescaped)
        tracking_prefixes = (
            "fbclid",
            "__cft",
            "__tn",
            "ref",
            "notif",
            "mibextid",
            "paipv",
            "idorvanity",
            "rdid",
            "extid",
            "checkpoint",
        )
        cleaned_params = []
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            if not any(k.startswith(p) for p in tracking_prefixes):
                cleaned_params.append((k, v))
        new_query = urlencode(cleaned_params)
        return urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, "", new_query, "")
        )

    def _extract_page_identifier(self, page_url: str) -> Optional[str]:
        """Extract numeric page ID or username slug from page URL."""
        parsed = urlparse(page_url)
        params = dict(parse_qsl(parsed.query))
        if "id" in params and params["id"].isdigit():
            return params["id"]
        path_parts = [p for p in parsed.path.split("/") if p]
        if path_parts:
            return path_parts[0]
        return None

    def scrape_page(self, page_url: str) -> List[FacebookPost]:
        """
        Fetch a Facebook page and extract recent posts without launching a browser.
        Returns a list of FacebookPost sorted with newest first.
        """
        clean_page_base = page_url.rstrip("/")
        page_id = self._extract_page_identifier(page_url)
        try:
            resp = self.session.get(page_url, timeout=15, allow_redirects=True)
            if resp.status_code in (401, 403, 429):
                self.last_auth_error = f"HTTP {resp.status_code}"
                self.consecutive_auth_failures += 1
                logger.warning(
                    f"HTTP {resp.status_code} (auth/rate-limit) when accessing {page_url}"
                )
                return []
            if resp.status_code != 200:
                logger.warning(
                    f"HTTP {resp.status_code} when accessing {page_url}"
                )
                return []

            final_url_lower = str(resp.url).lower()
            if any(
                p in final_url_lower
                for p in ("checkpoint", "login/?next", "login.php", "login_attempt")
            ):
                self.last_auth_error = f"Redirected to auth/checkpoint: {resp.url}"
                self.consecutive_auth_failures += 1
                logger.warning(
                    f"Facebook auth challenge detected for {page_url}: redirected to {resp.url}"
                )
                return []

            text = resp.text
            if (
                "checkpoint_title" in text
                or 'name="approvals_code"' in text
                or 'id="checkpointbox"' in text
            ):
                self.last_auth_error = "Checkpoint challenge detected in response body"
                self.consecutive_auth_failures += 1
                logger.warning(
                    f"Facebook checkpoint challenge in body for {page_url}"
                )
                return []
        except Exception as e:
            logger.error(f"Error fetching {page_url}: {e}")
            return []

        posts_by_id: Dict[str, FacebookPost] = {}
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", text, re.DOTALL)

        # 1. Primary: Parse timeline feed units from Relay Modern / Comet scripts
        for s in scripts:
            if "timeline_list_feed_units" in s or "timeline_feed_units" in s:
                try:
                    data = json.loads(s)
                    self._parse_timeline_feed(
                        data, posts_by_id, clean_page_base, page_id
                    )
                except Exception:
                    pass

        # 2. Secondary fallback: Look for top-level Story objects in scripts
        if not posts_by_id:
            for s in scripts:
                if "Story" in s and "post_id" in s:
                    try:
                        data = json.loads(s)
                        self._parse_story_fallback(
                            data, posts_by_id, clean_page_base, page_id
                        )
                    except Exception:
                        pass

        # 3. Tertiary fallback: Regex search for page-specific permalinks
        if not posts_by_id:
            self._parse_regex_fallback(
                text, posts_by_id, clean_page_base, page_id
            )

        results = list(posts_by_id.values())
        results.sort(
            key=lambda x: (x.creation_time or 0),
            reverse=True,
        )
        if results:
            self.consecutive_auth_failures = 0
            self.last_auth_error = None
        return results

    def _parse_timeline_feed(
        self,
        data: dict,
        posts_by_id: Dict[str, FacebookPost],
        clean_page_base: str,
        page_id: Optional[str],
    ) -> None:
        def walk(obj):
            if isinstance(obj, dict):
                for feed_key in ("timeline_list_feed_units", "timeline_feed_units"):
                    if feed_key in obj and isinstance(obj[feed_key], dict):
                        edges = obj[feed_key].get("edges", [])
                        for edge in edges:
                            if not isinstance(edge, dict):
                                continue
                            node = edge.get("node")
                            if not node or not isinstance(node, dict):
                                continue
                            pid = node.get("post_id")
                            if not pid:
                                continue
                            pid = str(pid)

                            ctime = node.get("creation_time")
                            if ctime:
                                try:
                                    ctime = int(ctime)
                                except (ValueError, TypeError):
                                    ctime = None

                            raw_perm = node.get("permalink_url") or node.get("url")
                            if not raw_perm:
                                ufi_story = (
                                    node.get("comet_sections", {})
                                    .get("feedback", {})
                                    .get("story", {})
                                    .get("story_ufi_container", {})
                                    .get("story", {})
                                )
                                raw_perm = ufi_story.get("permalink_url") or ufi_story.get("url")

                            purl = self.clean_url(raw_perm) if raw_perm else ""
                            # If bare permalink.php or missing, build complete permalink
                            if not purl or purl.endswith("/permalink.php"):
                                if page_id and page_id.isdigit():
                                    purl = f"https://www.facebook.com/permalink.php?story_fbid={pid}&id={page_id}"
                                else:
                                    purl = f"{clean_page_base}/posts/{pid}"

                            # Message text extraction
                            msg = None
                            content_story = (
                                node.get("comet_sections", {})
                                .get("content", {})
                                .get("story", {})
                            )
                            if (
                                isinstance(content_story, dict)
                                and content_story.get("message")
                                and isinstance(content_story["message"], dict)
                            ):
                                msg = content_story["message"].get("text")
                            elif (
                                node.get("message")
                                and isinstance(node["message"], dict)
                            ):
                                msg = node["message"].get("text")

                            # Handle attached/shared story context
                            att = content_story.get("attached_story") or node.get("attached_story")
                            if att and isinstance(att, dict):
                                att_msg = (
                                    att.get("message", {}).get("text")
                                    if isinstance(att.get("message"), dict)
                                    else None
                                )
                                att_actors = att.get("actors", [])
                                actor_name = (
                                    att_actors[0].get("name")
                                    if att_actors and isinstance(att_actors[0], dict)
                                    else ""
                                )
                                if att_msg:
                                    prefix = (
                                        f"[Chia sẻ từ {actor_name}]:\n"
                                        if actor_name
                                        else "[Chia sẻ]:\n"
                                    )
                                    if msg:
                                        msg = f"{msg}\n\n{prefix}{att_msg}"
                                    else:
                                        msg = f"{prefix}{att_msg}"

                            if pid not in posts_by_id:
                                posts_by_id[pid] = FacebookPost(
                                    post_id=pid,
                                    url=purl,
                                    creation_time=ctime,
                                    message=msg,
                                )
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(data)

    def _parse_story_fallback(
        self,
        data: dict,
        posts_by_id: Dict[str, FacebookPost],
        clean_page_base: str,
        page_id: Optional[str],
    ) -> None:
        def walk(obj, in_attached=False, in_highlight=False):
            if isinstance(obj, dict):
                is_att = (
                    in_attached
                    or "attached_story" in obj
                    or obj.get("__typename") == "HighlightPostUnit"
                )
                is_hi = (
                    in_highlight
                    or "highlight_units_section" in obj
                    or obj.get("__typename") == "HighlightPostUnit"
                )
                if (
                    not is_att
                    and not is_hi
                    and obj.get("__typename") == "Story"
                    and obj.get("post_id")
                ):
                    pid = str(obj["post_id"])
                    ctime = obj.get("creation_time")
                    try:
                        ctime = int(ctime) if ctime else None
                    except (ValueError, TypeError):
                        ctime = None
                    raw_perm = obj.get("permalink_url") or obj.get("url")
                    purl = self.clean_url(raw_perm) if raw_perm else ""
                    if not purl or purl.endswith("/permalink.php"):
                        if page_id and page_id.isdigit():
                            purl = f"https://www.facebook.com/permalink.php?story_fbid={pid}&id={page_id}"
                        else:
                            purl = f"{clean_page_base}/posts/{pid}"

                    msg = None
                    if isinstance(obj.get("message"), dict):
                        msg = obj["message"].get("text")

                    if pid not in posts_by_id and len(pid) >= 4:
                        posts_by_id[pid] = FacebookPost(
                            post_id=pid,
                            url=purl,
                            creation_time=ctime,
                            message=msg,
                        )
                for k, v in obj.items():
                    walk(
                        v,
                        in_attached=(is_att or k == "attached_story"),
                        in_highlight=(is_hi or k == "highlight_units_section"),
                    )
            elif isinstance(obj, list):
                for item in obj:
                    walk(item, in_attached, in_highlight)

        walk(data)

    def _parse_regex_fallback(
        self,
        text: str,
        posts_by_id: Dict[str, FacebookPost],
        clean_page_base: str,
        page_id: Optional[str],
    ) -> None:
        found_permalinks = re.findall(r'\"permalink_url\":\"([^\"]+)\"', text)
        for raw_link in found_permalinks:
            clean_link = self.clean_url(raw_link)
            if not clean_link:
                continue

            # Verify link belongs to this page
            if page_id and page_id not in clean_link:
                continue

            match = re.search(r"/(?:posts|videos|reel)/([^/?]+)", clean_link)
            link_id = match.group(1) if match else None
            if not link_id:
                # Check permalink.php?story_fbid=
                match_perm = re.search(r"story_fbid=([^&]+)", clean_link)
                link_id = match_perm.group(1) if match_perm else None

            if link_id and link_id not in posts_by_id and len(link_id) >= 4:
                posts_by_id[link_id] = FacebookPost(
                    post_id=link_id,
                    url=clean_link,
                    creation_time=None,
                    message=None,
                )
