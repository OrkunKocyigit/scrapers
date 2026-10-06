#!/usr/bin/env python3
"""Stash metadata provider for fc2ppv-db.com.

Standard-library-only Stash script scraper. Every page fetch goes through a
local FlareSolverr instance because fc2ppv-db.com sits behind Cloudflare and an
age verification wall.

Protocol: Stash runs ``python fc2ppv-db.py <operation>``, writes JSON to stdin
and reads the result JSON from stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

SITE_BASE = "https://fc2ppv-db.com"
ALLOWED_HOSTS = ("fc2ppv-db.com", "www.fc2ppv-db.com")
DEFAULT_FLARESOLVERR_URL = "http://localhost:8191/v1"
INI_PATH = Path(__file__).with_name("fc2ppv-db.ini")

VIDEO_PATH_RE = re.compile(r"/videos/(\d{5,7})")
FC2_ID_RE = re.compile(r"fc2[\s_-]*ppv[\s_-]*(\d{5,7})(?!\d)", re.IGNORECASE)
BARE_ID_RE = re.compile(r"(?<!\d)(\d{6,7})(?!\d)")
ACTRESS_UUID_RE = re.compile(
    r"/(?:en|ja|zh)/actresses/"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"(?:[/?#]|$)"
)
SEARCH_VIDEO_HREF_RE = re.compile(r"/(?:en|ja|zh)/videos/(\d+)(?:$|[/?#])")
JP_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
META_SELLER_RE = re.compile(r"販売者:\s*(.+?)(?:\s*/|$)")
META_DATE_RE = re.compile(r"公開日:\s*(\d{4})年(\d{1,2})月(\d{1,2})日")
META_DURATION_RE = re.compile(r"再生時間:\s*(\d{1,2}:\d{2}(?::\d{2})?)")


class ScraperError(Exception):
    """Error surfaced to the user as ``fc2ppv-db: <message>`` on stderr."""


def load_flaresolverr_url():
    """Resolve the FlareSolverr endpoint: env var, then INI, then default."""
    env_url = os.environ.get("FLARESOLVERR_URL", "").strip()
    if env_url:
        return env_url
    ini_url = _read_ini_value(INI_PATH, "flaresolverr_url")
    if ini_url and ini_url.strip():
        return ini_url.strip()
    return DEFAULT_FLARESOLVERR_URL


def _read_ini_value(path, key):
    """Read a single key from an INI file, accepting ``[DEFAULT]`` or a bare key."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error:
        parser = configparser.ConfigParser()
        try:
            parser.read_string("[DEFAULT]\n" + text)
        except configparser.Error:
            return None
    defaults = parser.defaults()
    if key in defaults:
        return defaults[key]
    for section in parser.sections():
        if key in parser[section]:
            return parser[section][key]
    return None


def fetch_html(url):
    """Fetch an fc2ppv-db.com page through FlareSolverr and return its HTML."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ALLOWED_HOSTS:
        raise ScraperError(
            "refusing to fetch {0!r}: only http(s) URLs on {1} are allowed".format(
                url, " or ".join(ALLOWED_HOSTS)
            )
        )

    solver_url = load_flaresolverr_url()
    body = json.dumps(
        {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": 60000,
            "cookies": [{"name": "age-verified", "value": "true"}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        solver_url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=130) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise ScraperError(
            "FlareSolverr at {0} returned HTTP {1}".format(solver_url, exc.code)
        ) from exc
    except urllib.error.URLError as exc:
        raise ScraperError(
            "FlareSolverr unreachable at {0} ({1})".format(solver_url, exc.reason)
        ) from exc
    except OSError as exc:
        raise ScraperError(
            "FlareSolverr unreachable at {0} ({1})".format(solver_url, exc)
        ) from exc
    except ValueError as exc:
        raise ScraperError(
            "FlareSolverr at {0} returned an invalid JSON response ({1})".format(
                solver_url, exc
            )
        ) from exc

    if not isinstance(payload, dict):
        raise ScraperError("FlareSolverr returned an unexpected response shape")

    if payload.get("status") != "ok":
        message = payload.get("message") or "unknown error"
        raise ScraperError("FlareSolverr error: {0}".format(message))

    solution = payload.get("solution")
    if not isinstance(solution, dict):
        raise ScraperError("FlareSolverr returned no solution for {0}".format(url))

    html = solution.get("response") or ""
    status = solution.get("status")
    if status == 403 and ("1005" in html or "1005" in str(payload.get("message") or "")):
        raise ScraperError(
            "Cloudflare blocked the request (ASN block, error 1005); "
            "try another network or FlareSolverr exit node"
        )
    if status != 200:
        raise ScraperError(
            "fc2ppv-db.com returned HTTP {0} for {1}".format(status, url)
        )

    final_url = solution.get("url") or url
    if is_age_gate(html, final_url):
        raise ScraperError(
            "fc2ppv-db.com returned the age verification page; "
            "the age-verified cookie was rejected"
        )
    return html


def is_age_gate(html, final_url):
    """True when the response is the site's age verification wall."""
    match = re.search(
        r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL
    )
    if match and "age verification" in match.group(1).lower():
        return True
    return "age-verify" in (final_url or "").lower()


def extract_id_from_url(url):
    """Return the numeric FC2 id from an fc2ppv-db.com video URL, else None."""
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname and parsed.hostname not in ALLOWED_HOSTS:
        return None
    match = VIDEO_PATH_RE.search(parsed.path or "")
    return match.group(1) if match else None


def extract_id(text):
    """Best-effort FC2 id from a filename, title or fragment string.

    Prefers the FC2-PPV prefixed form (``FC2-PPV-4985048.mp4``, ``fc2ppv4985048``)
    over a bare 6-7 digit token so resolution numbers like ``1080`` never win.
    """
    if not text:
        return None
    match = FC2_ID_RE.search(text)
    if match:
        return match.group(1)
    match = BARE_ID_RE.search(text)
    if match:
        return match.group(1)
    return None


def extract_actress_uuid(url):
    """Return the actress uuid from an fc2ppv-db.com actress URL, else None."""
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname and parsed.hostname not in ALLOWED_HOSTS:
        return None
    match = ACTRESS_UUID_RE.search(parsed.path or "")
    return match.group(1) if match else None


class _VideoPageParser(HTMLParser):
    """Collect the DOM pieces that :func:`parse_video_page` needs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.h1 = ""
        self.title = ""
        self.canonical = None
        self.meta_description = None
        self.paragraphs = []
        self.description = ""
        self.anchors = []
        self.images = []
        self._h1_parts = []
        self._title_parts = []
        self._in_h1 = False
        self._in_title = False
        self._in_p = False
        self._p_parts = []
        self._p_class = ""
        self._anchor = None
        self._anchor_parts = []
        self._anchor_images = []
        self._anchor_strong_texts = []
        self._span_stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "h1":
            self._in_h1 = True
            self._h1_parts = []
        elif tag == "title":
            self._in_title = True
            self._title_parts = []
        elif tag == "link":
            if attrs.get("rel") == "canonical" and attrs.get("href"):
                self.canonical = attrs["href"]
        elif tag == "meta":
            if attrs.get("name") == "description" and attrs.get("content") is not None:
                self.meta_description = attrs["content"]
        elif tag == "p":
            self._in_p = True
            self._p_parts = []
            self._p_class = attrs.get("class") or ""
        elif tag == "a":
            self._anchor = attrs.get("href") or ""
            self._anchor_parts = []
            self._anchor_images = []
            self._anchor_strong_texts = []
            self._span_stack = []
        elif tag == "span":
            if self._anchor is not None:
                self._span_stack.append(
                    {"class": attrs.get("class") or "", "parts": []}
                )
        elif tag == "img":
            image = {
                "src": attrs.get("src") or "",
                "alt": attrs.get("alt") or "",
                "fetchpriority": attrs.get("fetchpriority") or "",
            }
            self.images.append(image)
            if self._anchor is not None:
                self._anchor_images.append(dict(image))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._in_h1:
            self._h1_parts.append(data)
        if self._in_title:
            self._title_parts.append(data)
        if self._in_p:
            self._p_parts.append(data)
        if self._anchor is not None:
            self._anchor_parts.append(data)
            for span in self._span_stack:
                span["parts"].append(data)

    def handle_endtag(self, tag):
        if tag == "h1" and self._in_h1:
            self._in_h1 = False
            self.h1 = "".join(self._h1_parts).strip()
        elif tag == "title" and self._in_title:
            self._in_title = False
            self.title = "".join(self._title_parts).strip()
        elif tag == "p" and self._in_p:
            self._in_p = False
            text = "".join(self._p_parts).strip()
            self.paragraphs.append(text)
            if "whitespace-pre-wrap" in self._p_class:
                self.description = text
            self._p_parts = []
            self._p_class = ""
        elif tag == "span":
            if self._span_stack:
                span = self._span_stack.pop()
                if "font-medium" in span["class"]:
                    text = "".join(span["parts"]).strip()
                    if text:
                        self._anchor_strong_texts.append(text)
        elif tag == "a" and self._anchor is not None:
            self.anchors.append(
                {
                    "href": self._anchor,
                    "text": "".join(self._anchor_parts).strip(),
                    "strong_texts": self._anchor_strong_texts,
                    "images": self._anchor_images,
                }
            )
            self._anchor = None
            self._span_stack = []


def _absolutize(href):
    if href.startswith(("http://", "https://")):
        return href
    return SITE_BASE + ("" if href.startswith("/") else "/") + href


def _anchor_name(anchor):
    """Name for a seller/actress anchor: font-medium span, link text, img alt."""
    if anchor["strong_texts"]:
        return anchor["strong_texts"][0]
    if anchor["text"]:
        return anchor["text"]
    return next(
        (img["alt"].strip() for img in anchor["images"] if img["alt"].strip()), ""
    )


def _convert_jp_date(text):
    match = JP_DATE_RE.search(text or "")
    if not match:
        return None
    return "{0}-{1:02d}-{2:02d}".format(
        match.group(1), int(match.group(2)), int(match.group(3))
    )


def parse_video_page(html, page_url):
    """Build a Stash ScrapedScene dict from an fc2ppv-db.com video page."""
    video_id = extract_id_from_url(page_url)
    if not video_id:
        return {}
    parser = _VideoPageParser()
    parser.feed(html or "")
    parser.close()

    code = "FC2-PPV-{0}".format(video_id)
    scene: dict = {"code": code}

    prefix = re.compile(r"^FC2-PPV-{0}\s*".format(video_id), re.IGNORECASE)
    title = prefix.sub("", parser.h1, count=1).strip()
    scene["title"] = title or code

    date = None
    for index, paragraph in enumerate(parser.paragraphs):
        if paragraph.lower() == "release date" and index + 1 < len(parser.paragraphs):
            date = _convert_jp_date(parser.paragraphs[index + 1])
            break
    if not date:
        match = META_DATE_RE.search(parser.meta_description or "")
        if match:
            date = "{0}-{1:02d}-{2:02d}".format(
                match.group(1), int(match.group(2)), int(match.group(3))
            )
    if date:
        scene["date"] = date

    studio = None
    for anchor in parser.anchors:
        if "/sellers/" in anchor["href"]:
            name = _anchor_name(anchor)
            if name:
                url = _absolutize(anchor["href"])
                studio = {"name": name, "url": url, "urls": [url]}
                seller_image = next(
                    (
                        img["src"]
                        for img in anchor["images"]
                        if "/sellers/" in img["src"]
                    ),
                    "",
                )
                if seller_image:
                    studio["image"] = seller_image
                    studio["images"] = [seller_image]
                break
    if studio is None and parser.meta_description:
        match = META_SELLER_RE.search(parser.meta_description)
        if match:
            studio = {"name": match.group(1).strip()}
    if studio:
        scene["studio"] = studio

    image = None
    for img in parser.images:
        if img["fetchpriority"].lower() == "high" and img["src"]:
            image = img["src"]
            break
    if not image:
        for img in parser.images:
            src = img["src"]
            if "/thumbnails/" in src and re.search(
                r"/{0}\.webp(?:\?|$)".format(video_id), src
            ):
                image = src
                break
    if image:
        scene["image"] = image

    if parser.canonical:
        scene["urls"] = [parser.canonical.strip()]
    else:
        scene["urls"] = [page_url]

    detail_parts = []
    if parser.meta_description:
        match = META_DURATION_RE.search(parser.meta_description)
        if match:
            detail_parts.append("Duration: {0}".format(match.group(1)))
    description = parser.description.strip()
    if description:
        detail_parts.append(description)
    if detail_parts:
        scene["details"] = "\n\n".join(detail_parts)

    performers = []
    for anchor in parser.anchors:
        if "/actresses/" not in anchor["href"]:
            continue
        name = _anchor_name(anchor)
        if not name:
            continue
        performer: dict = {"name": name}
        url = _absolutize(anchor["href"])
        performer["url"] = url
        performer["urls"] = [url]
        face = next(
            (img["src"] for img in anchor["images"] if "/faces/actress_" in img["src"]),
            "",
        )
        if face:
            performer["image"] = face
            performer["images"] = [face]
        performers.append(performer)
    if performers:
        scene["performers"] = performers

    return scene


def scene_by_url(payload):
    """Stash ``scene-by-url``: stdin ``{"url": ...}`` -> one ScrapedScene."""
    url = (payload or {}).get("url") or ""
    video_id = extract_id_from_url(url)
    if not video_id:
        raise ScraperError(
            "scene-by-url: {0!r} is not a recognized fc2ppv-db.com video URL".format(url)
        )
    html = fetch_html(url)
    return parse_video_page(html, url)


def _resolve_video_id(payload):
    """Resolve an FC2 id from a scene fragment: urls, files[].path, title/name."""
    for url in payload.get("urls") or []:
        video_id = extract_id_from_url(url)
        if video_id:
            return video_id
    for entry in payload.get("files") or []:
        if isinstance(entry, dict):
            video_id = extract_id(entry.get("path") or "")
            if video_id:
                return video_id
    for key in ("title", "name"):
        video_id = extract_id(payload.get(key) or "")
        if video_id:
            return video_id
    return None


def _fetch_scene(video_id):
    url = "{0}/en/videos/{1}".format(SITE_BASE, video_id)
    html = fetch_html(url)
    return parse_video_page(html, url)


def scene_by_fragment(payload):
    """Stash ``scene-by-fragment`` / ``scene-by-query-fragment``.

    Unresolvable fragments return an empty object instead of an error.
    """
    video_id = _resolve_video_id(payload or {})
    if not video_id:
        return {}
    return _fetch_scene(video_id)


class _SearchResultsParser(HTMLParser):
    """Collect anchors from a search results page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = dict(attrs)
            self.anchors.append(
                {"href": attrs.get("href") or "", "title": attrs.get("title") or ""}
            )

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def parse_search_results(html):
    """Map a ``/en/search?q=`` page to Stash sceneByName candidates."""
    parser = _SearchResultsParser()
    parser.feed(html or "")
    parser.close()
    results = []
    seen = set()
    for anchor in parser.anchors:
        match = SEARCH_VIDEO_HREF_RE.search(anchor["href"])
        if not match:
            continue
        video_id = match.group(1)
        if video_id in seen:
            continue
        seen.add(video_id)
        title = anchor["title"].strip() or "FC2-PPV-{0}".format(video_id)
        results.append(
            {"title": title, "url": "{0}/en/videos/{1}".format(SITE_BASE, video_id)}
        )
    return results


def scene_by_name(payload):
    """Stash ``scene-by-name``: candidates from the site search, or a fast path."""
    name = ((payload or {}).get("name") or "").strip()
    if not name:
        return []
    video_id = extract_id(name)
    if video_id:
        # The name already carries an FC2 id: one candidate, no network fetch.
        return [
            {
                "title": name,
                "url": "{0}/en/videos/{1}".format(SITE_BASE, video_id),
            }
        ]
    query = urllib.parse.quote_plus(name)
    html = fetch_html("{0}/en/search?q={1}".format(SITE_BASE, query))
    return parse_search_results(html)


class _PerformerSearchParser(HTMLParser):
    """Collect actress search result cards (href + h3 name + face image)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self._anchor = None
        self._h3_parts = []
        self._in_h3 = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self._anchor = {
                "href": attrs.get("href") or "",
                "name": "",
                "images": [],
            }
        elif tag == "h3" and self._anchor is not None:
            self._in_h3 = True
            self._h3_parts = []
        elif tag == "img" and self._anchor is not None:
            self._anchor["images"].append(attrs.get("src") or "")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._in_h3:
            self._h3_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "h3" and self._in_h3:
            self._in_h3 = False
            if self._anchor is not None:
                self._anchor["name"] = "".join(self._h3_parts).strip()
        elif tag == "a" and self._anchor is not None:
            self.anchors.append(self._anchor)
            self._anchor = None


def parse_performer_search_results(html):
    """Map an actress search page to Stash performerByName candidates."""
    parser = _PerformerSearchParser()
    parser.feed(html or "")
    parser.close()
    results = []
    seen = set()
    for anchor in parser.anchors:
        actress_uuid = extract_actress_uuid(anchor["href"])
        if not actress_uuid or actress_uuid in seen or not anchor["name"]:
            continue
        seen.add(actress_uuid)
        result = {
            "name": anchor["name"],
            "url": "{0}/en/actresses/{1}".format(SITE_BASE, actress_uuid),
        }
        image = next(
            (
                src.split("?", 1)[0]
                for src in anchor["images"]
                if "/faces/actress_" in src
            ),
            "",
        )
        if image:
            result["image"] = image
            result["images"] = [image]
        results.append(result)
    return results


def performer_by_name(payload):
    """Stash ``performer-by-name``: candidates from the actress search page."""
    name = ((payload or {}).get("name") or "").strip()
    if not name:
        return []
    query = urllib.parse.quote_plus(name)
    html = fetch_html(
        "{0}/en/actresses?view=all&q={1}&page=1".format(SITE_BASE, query)
    )
    return parse_performer_search_results(html)


class _ActressPageParser(HTMLParser):
    """Collect the DOM pieces that :func:`parse_actress_page` needs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.h1 = ""
        self.title = ""
        self.canonical = None
        self.ld_json_blocks = []
        self.images = []
        self._h1_parts = []
        self._title_parts = []
        self._in_h1 = False
        self._in_title = False
        self._in_ld_json = False
        self._ld_json_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "h1":
            self._in_h1 = True
            self._h1_parts = []
        elif tag == "title":
            self._in_title = True
            self._title_parts = []
        elif tag == "link":
            if attrs.get("rel") == "canonical" and attrs.get("href"):
                self.canonical = attrs["href"]
        elif tag == "script":
            if (attrs.get("type") or "").lower() == "application/ld+json":
                self._in_ld_json = True
                self._ld_json_parts = []
        elif tag == "img":
            self.images.append(
                {"src": attrs.get("src") or "", "alt": attrs.get("alt") or ""}
            )

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._in_h1:
            self._h1_parts.append(data)
        if self._in_title:
            self._title_parts.append(data)
        if self._in_ld_json:
            self._ld_json_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "h1" and self._in_h1:
            self._in_h1 = False
            self.h1 = "".join(self._h1_parts).strip()
        elif tag == "title" and self._in_title:
            self._in_title = False
            self.title = "".join(self._title_parts).strip()
        elif tag == "script" and self._in_ld_json:
            self._in_ld_json = False
            self.ld_json_blocks.append("".join(self._ld_json_parts))


def parse_actress_page(html, page_url):
    """Build a Stash ScrapedPerformer dict from an fc2ppv-db.com actress page.

    The site has no bio, birthdate or aliases, so only name/url/image are
    emitted; the image keys are omitted entirely when no face image exists.
    """
    parser = _ActressPageParser()
    parser.feed(html or "")
    parser.close()

    person = None
    for block in parser.ld_json_blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Person":
            person = data
            break

    name = ""
    if person and isinstance(person.get("name"), str):
        name = person["name"].strip()
    if not name:
        name = parser.h1
    if not name:
        name = parser.title

    result: dict = {"name": name}
    if page_url:
        result["url"] = page_url
        result["urls"] = [page_url]

    image = ""
    if person:
        person_image = person.get("image")
        if isinstance(person_image, str) and person_image.strip():
            image = person_image.strip()
        elif isinstance(person_image, list):
            image = next(
                (
                    item.strip()
                    for item in person_image
                    if isinstance(item, str) and item.strip()
                ),
                "",
            )
    if not image:
        actress_uuid = extract_actress_uuid(page_url)
        for img in parser.images:
            src = img["src"]
            if "/faces/actress_" in src and (not actress_uuid or actress_uuid in src):
                image = src.split("?", 1)[0]
                break
    if image:
        result["image"] = image
        result["images"] = [image]
    return result


def performer_by_url(payload):
    """Stash ``performer-by-url``: stdin ``{"url": ...}`` -> one ScrapedPerformer."""
    url = (payload or {}).get("url") or ""
    actress_uuid = extract_actress_uuid(url)
    if not actress_uuid:
        raise ScraperError(
            "performer-by-url: {0!r} is not a recognized fc2ppv-db.com actress URL".format(
                url
            )
        )
    html = fetch_html(url)
    return parse_actress_page(html, url)


def performer_by_fragment(payload):
    """Stash ``performer-by-fragment``: only works when a URL is present."""
    payload = payload or {}
    url = payload.get("url")
    if not url:
        urls = payload.get("urls") or []
        if urls:
            url = urls[0]
    if not url:
        raise ScraperError(
            "performer-by-fragment requires a url: fc2ppv-db.com has no "
            "performer name search"
        )
    return performer_by_url({"url": url})


OPERATIONS = {
    "scene-by-url": scene_by_url,
    "scene-by-fragment": scene_by_fragment,
    "scene-by-query-fragment": scene_by_fragment,
    "scene-by-name": scene_by_name,
    "performer-by-url": performer_by_url,
    "performer-by-fragment": performer_by_fragment,
    "performer-by-name": performer_by_name,
}


def main(argv=None):
    argv = sys.argv if argv is None else argv
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    if len(argv) < 2 or argv[1] not in OPERATIONS:
        print(
            "usage: fc2ppv-db.py <{0}>".format("|".join(sorted(OPERATIONS))),
            file=sys.stderr,
        )
        return 2

    raw_input = sys.stdin.read()
    try:
        payload = json.loads(raw_input) if raw_input.strip() else {}
    except json.JSONDecodeError as exc:
        print("fc2ppv-db: invalid JSON on stdin: {0}".format(exc), file=sys.stderr)
        return 1

    try:
        result = OPERATIONS[argv[1]](payload)
    except ScraperError as exc:
        print("fc2ppv-db: {0}".format(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # no traceback for CLI users
        print("fc2ppv-db: unexpected error: {0}".format(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
