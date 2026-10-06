#!/usr/bin/env python3
"""Stash metadata provider for fc2cmadb.com.

Standard-library-only Stash script scraper. fc2cmadb.com is a Laravel +
Inertia.js application behind Cloudflare: page data is delivered as JSON inside
``<script data-page="app" type="application/json">`` and refreshed through
Inertia partial reloads. Article and writer pages are public; actress pages and
actress search require a login cookie (see README).

Protocol: Stash runs ``python fc2cmadb.py <operation>``, writes JSON to stdin
and reads the result JSON from stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SITE_BASE = "https://fc2cmadb.com"
ALLOWED_HOSTS = ("fc2cmadb.com", "www.fc2cmadb.com")
REQUEST_TIMEOUT_SECONDS = 40
INI_PATH = Path(__file__).with_name("fc2cmadb.ini")

VIDEO_PATH_RE = re.compile(r"/articles/(\d{5,7})")
ACTRESS_PATH_RE = re.compile(r"/actresses/(\d{1,7})")
FC2_ID_RE = re.compile(r"fc2[\s_-]*ppv[\s_-]*(\d{5,7})(?!\d)", re.IGNORECASE)
BARE_ID_RE = re.compile(r"(?<!\d)(\d{6,7})(?!\d)")
DATA_PAGE_RE = re.compile(
    r'<script[^>]*data-page="app"[^>]*type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)
DATA_PAGE_ALT_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*data-page="app"[^>]*>(.*?)</script>',
    re.DOTALL,
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)


class ScraperError(Exception):
    """Error surfaced to the user as ``fc2cmadb: <message>`` on stderr."""


def _read_ini_value(path, key):
    """Read a single key from an INI file, accepting ``[DEFAULT]`` or a bare key."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        parser = configparser.ConfigParser(interpolation=None)
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


def load_cookie():
    """Resolve the login cookie: ``FC2CMADB_COOKIE`` env var, then INI ``cookie``.

    The recommended value combines the long-lived ``remember_web_*`` cookie
    (which re-authenticates even after the session cookie expires), the
    ``fc2cmadb-session`` cookie and ``ageVerified=true``.
    """
    value = os.environ.get("FC2CMADB_COOKIE", "").strip()
    if value:
        return value
    ini_value = _read_ini_value(INI_PATH, "cookie")
    return ini_value.strip() if ini_value else ""


class HttpClient:
    """Minimal cookie-aware HTTP client for fc2cmadb.com.

    Cookies from the configured header are sent on every request; ``Set-Cookie``
    values from every response are absorbed so a session refreshed by the
    remember-me cookie is reused for the remaining requests of the same run.
    """

    def __init__(self, cookie=""):
        self.cookies = {}
        for part in (cookie or "").split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                name = name.strip()
                if name:
                    self.cookies[name] = value.strip()
        self.cookies.setdefault("ageVerified", "true")

    def _cookie_header(self):
        return "; ".join("{0}={1}".format(name, value) for name, value in self.cookies.items())

    def _absorb(self, headers):
        for value in headers.get_all("Set-Cookie") or []:
            pair = value.split(";", 1)[0].strip()
            if "=" in pair:
                name, cookie_value = pair.split("=", 1)
                name = name.strip()
                if name:
                    self.cookies[name] = cookie_value.strip()

    def get(self, url, headers=None):
        """GET a URL; returns ``(status, final_url, headers, body)``."""
        request_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html, application/xhtml+xml",
            "Cookie": self._cookie_header(),
        }
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT_SECONDS,
                context=ssl.create_default_context(),
            ) as response:
                body = response.read().decode("utf-8", "replace")
                self._absorb(response.headers)
                return response.status, response.geturl(), response.headers, body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            self._absorb(exc.headers)
            return exc.code, exc.geturl(), exc.headers, body
        except urllib.error.URLError as exc:
            raise ScraperError(
                "fc2cmadb.com unreachable ({0})".format(exc.reason)
            ) from exc
        except OSError as exc:
            raise ScraperError("fc2cmadb.com unreachable ({0})".format(exc)) from exc


def _client():
    return HttpClient(load_cookie())


def parse_data_page(html):
    """Decode the Inertia page object embedded in an fc2cmadb.com HTML response."""
    match = DATA_PAGE_RE.search(html or "") or DATA_PAGE_ALT_RE.search(html or "")
    if not match:
        raise ScraperError("fc2cmadb.com returned an unexpected page (no app data)")
    try:
        return json.loads(match.group(1))
    except ValueError as exc:
        raise ScraperError(
            "fc2cmadb.com returned an invalid app data block ({0})".format(exc)
        ) from exc


def _login_hint(url):
    if "/actresses/" in url or "stype=actress" in url:
        return (
            "; actress pages and actress search require a login - set the cookie "
            "in fc2cmadb.ini or FC2CMADB_COOKIE (see README)"
        )
    return ""


def fetch_page(http, url, _retried=False):
    """Fetch a page and return ``(final_url, data_page)``.

    A 403 is retried once: with a ``remember_web_*`` cookie the server may
    re-authenticate on that response and issue a fresh session cookie.
    """
    status, final_url, headers, body = http.get(url)
    if status == 403 and not _retried:
        return fetch_page(http, url, _retried=True)
    try:
        page = parse_data_page(body)
    except ScraperError:
        if status >= 400:
            raise ScraperError(
                "fc2cmadb.com returned HTTP {0} for {1}{2}".format(
                    status, url, _login_hint(url)
                )
            )
        raise
    if page.get("component") == "Error":
        props = page.get("props") or {}
        code = props.get("status") or status
        message = props.get("message") or "request failed"
        raise ScraperError(
            "fc2cmadb.com returned {0}: {1}{2}".format(code, message, _login_hint(url))
        )
    if status >= 400:
        raise ScraperError(
            "fc2cmadb.com returned HTTP {0} for {1}{2}".format(
                status, url, _login_hint(url)
            )
        )
    return final_url, page


def fetch_partial(http, url, component, version, data_key, _retried=False):
    """Fetch an Inertia partial reload and return its props."""
    headers = {
        "X-Inertia": "true",
        "X-Inertia-Version": version or "",
        "X-Inertia-Partial-Component": component,
        "X-Inertia-Partial-Data": data_key,
        "X-Requested-With": "XMLHttpRequest",
    }
    status, final_url, resp_headers, body = http.get(url, headers)
    if status == 409 and not _retried:
        final_url, page = fetch_page(http, url)
        return fetch_partial(
            http,
            final_url,
            page.get("component") or component,
            page.get("version") or "",
            data_key,
            _retried=True,
        )
    if status >= 400:
        raise ScraperError(
            "fc2cmadb.com returned HTTP {0} for {1}{2}".format(
                status, url, _login_hint(url)
            )
        )
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ScraperError(
            "fc2cmadb.com returned an invalid partial response ({0})".format(exc)
        ) from exc
    if not isinstance(data, dict):
        raise ScraperError("fc2cmadb.com returned an unexpected partial response")
    return data.get("props") or {}


def extract_id_from_url(url):
    """Return the numeric FC2 id from an fc2cmadb.com article URL, else None."""
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname and parsed.hostname not in ALLOWED_HOSTS:
        return None
    match = VIDEO_PATH_RE.search(parsed.path or "")
    return match.group(1) if match else None


def extract_actress_id(url):
    """Return the numeric actress id from an fc2cmadb.com actress URL, else None."""
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname and parsed.hostname not in ALLOWED_HOSTS:
        return None
    match = ACTRESS_PATH_RE.search(parsed.path or "")
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


def article_url(video_id):
    return "{0}/articles/{1}".format(SITE_BASE, video_id)


def actress_url(actress_id):
    return "{0}/actresses/{1}".format(SITE_BASE, actress_id)


def _real_image(value):
    """Absolute URL for a real image, or "" for the site's no-image placeholder."""
    value = (value or "").strip()
    if not value or "no-image" in value:
        return ""
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return SITE_BASE + value
    return value


def _aliases(alias_name):
    parts = [part for part in re.split(r"\s+", (alias_name or "").strip()) if part]
    return ", ".join(parts)


def _fetch_article(http, url):
    """Return ``(article, actresses)`` for an article page URL."""
    final_url, page = fetch_page(http, url)
    if page.get("component") != "Articles/Show":
        raise ScraperError(
            "fc2cmadb.com did not return an article page for {0}".format(url)
        )
    article = (page.get("props") or {}).get("article") or {}
    if not article.get("video_id"):
        raise ScraperError("fc2cmadb.com article page is missing its data")
    if article.get("not_found"):
        raise ScraperError(
            "video {0} is marked as not found on fc2cmadb.com".format(
                article.get("video_id")
            )
        )
    props = fetch_partial(
        http, final_url, "Articles/Show", page.get("version") or "", "actresses"
    )
    return article, props.get("actresses") or []


def _fetch_writer(http, slug):
    """Return the writer (studio) object for a slug, or None."""
    slug = (slug or "").strip()
    if not slug:
        return None
    final_url, page = fetch_page(http, "{0}/writers/{1}".format(SITE_BASE, slug))
    if page.get("component") != "Writers/Show":
        return None
    return (page.get("props") or {}).get("writer")


def _studio_from(article, writer_page):
    writer = article.get("writer") or {}
    slug = (writer.get("slug") or "").strip()
    name = (writer.get("name") or "").strip()
    if not name and not slug:
        return None
    studio = {"name": name}
    urls = []
    if slug:
        url = "{0}/writers/{1}".format(SITE_BASE, slug)
        studio["url"] = url
        urls.append(url)
    image = ""
    if writer_page:
        writer_url = (writer_page.get("writer_url") or "").strip()
        if writer_url and writer_url not in urls:
            urls.append(writer_url)
        image = _real_image(writer_page.get("image_url"))
    if urls:
        studio["urls"] = urls
    if image:
        studio["image"] = image
        studio["images"] = [image]
    return studio


def _performers_from(actresses):
    performers = []
    for actress in actresses or []:
        if not isinstance(actress, dict):
            continue
        name = (actress.get("name") or "").strip()
        if not name:
            continue
        performer = {"name": name}
        actress_id = actress.get("id")
        if actress_id:
            url = actress_url(actress_id)
            performer["url"] = url
            performer["urls"] = [url]
        aliases = _aliases(actress.get("alias_name"))
        if aliases:
            performer["aliases"] = aliases
        image = _real_image(actress.get("image_url"))
        if image:
            performer["image"] = image
            performer["images"] = [image]
        performers.append(performer)
    return performers


def _scene_from_article(article, actresses, writer_page=None):
    video_id = str(article.get("video_id") or "").strip()
    code = "FC2-PPV-{0}".format(video_id)
    scene: dict = {"code": code}
    title = (article.get("title") or "").strip()
    if title and title.upper() != code.upper() and title != video_id:
        scene["title"] = title
    date = (article.get("release_date") or "").strip()
    if date:
        scene["date"] = date
    duration = (article.get("duration") or "").strip()
    if duration:
        scene["details"] = "Duration: {0}".format(duration)
    scene["urls"] = [article_url(video_id)]
    image = _real_image(article.get("image_url"))
    if image:
        scene["image"] = image
    studio = _studio_from(article, writer_page)
    if studio:
        scene["studio"] = studio
    performers = _performers_from(actresses)
    if performers:
        scene["performers"] = performers
    tags = [
        {"name": tag.get("name")}
        for tag in article.get("tags") or []
        if isinstance(tag, dict) and tag.get("name")
    ]
    if tags:
        scene["tags"] = tags
    return scene


def _fetch_scene(video_id):
    http = _client()
    article, actresses = _fetch_article(http, article_url(video_id))
    writer_page = None
    slug = (article.get("writer") or {}).get("slug")
    if slug:
        try:
            writer_page = _fetch_writer(http, slug)
        except ScraperError:
            writer_page = None
    return _scene_from_article(article, actresses, writer_page)


def scene_by_url(payload):
    """Stash ``scene-by-url``: stdin ``{"url": ...}`` -> one ScrapedScene."""
    url = (payload or {}).get("url") or ""
    video_id = extract_id_from_url(url)
    if not video_id:
        raise ScraperError(
            "scene-by-url: {0!r} is not a recognized fc2cmadb.com article URL".format(url)
        )
    return _fetch_scene(video_id)


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


def scene_by_fragment(payload):
    """Stash ``scene-by-fragment`` / ``scene-by-query-fragment``.

    Unresolvable fragments return no result (``None``) instead of an empty
    object: Stash opens the scrape dialog for an empty object, but reports
    "no results" for a null one.
    """
    video_id = _resolve_video_id(payload or {})
    if not video_id:
        return None
    return _fetch_scene(video_id)


def _search_url(keyword, stype):
    query = urllib.parse.urlencode({"keyword": keyword, "stype": stype})
    return "{0}/search?{1}".format(SITE_BASE, query)


def scene_by_name(payload):
    """Stash ``scene-by-name``: candidates from the site search, or a fast path."""
    name = ((payload or {}).get("name") or "").strip()
    if not name:
        return []
    video_id = extract_id(name)
    if video_id:
        # The name already carries an FC2 id: one candidate, no network fetch.
        return [{"title": name, "url": article_url(video_id)}]
    http = _client()
    final_url, page = fetch_page(http, _search_url(name, "title"))
    component = page.get("component")
    if component == "Articles/Show":
        article = (page.get("props") or {}).get("article") or {}
        video_id = str(article.get("video_id") or "").strip()
        if not video_id:
            return []
        title = (article.get("title") or "").strip() or "FC2-PPV-{0}".format(video_id)
        return [{"title": title, "url": article_url(video_id)}]
    if component != "Search/Title":
        raise ScraperError("fc2cmadb.com search returned an unexpected page")
    props = fetch_partial(
        http, final_url, "Search/Title", page.get("version") or "", "articles"
    )
    paginator = props.get("articles") or {}
    items = paginator.get("data") if isinstance(paginator, dict) else paginator
    results = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("video_id") or "").strip()
        if not video_id:
            continue
        title = (item.get("title") or "").strip() or "FC2-PPV-{0}".format(video_id)
        results.append({"title": title, "url": article_url(video_id)})
    return results


def _performer_stub(actress):
    name = (actress.get("name") or "").strip()
    if not name:
        return None
    result = {"name": name}
    actress_id = actress.get("id")
    if actress_id:
        result["url"] = actress_url(actress_id)
    image = _real_image(actress.get("image_url"))
    if image:
        result["image"] = image
        result["images"] = [image]
    return result


def performer_by_url(payload):
    """Stash ``performer-by-url``: stdin ``{"url": ...}`` -> one ScrapedPerformer."""
    url = (payload or {}).get("url") or ""
    actress_id = extract_actress_id(url)
    if not actress_id:
        raise ScraperError(
            "performer-by-url: {0!r} is not a recognized fc2cmadb.com actress URL".format(
                url
            )
        )
    http = _client()
    final_url, page = fetch_page(http, actress_url(actress_id))
    if page.get("component") != "Actresses/Show":
        raise ScraperError(
            "fc2cmadb.com did not return an actress page for {0}".format(url)
        )
    actress = (page.get("props") or {}).get("actress") or {}
    name = (actress.get("name") or "").strip()
    if not name:
        raise ScraperError("fc2cmadb.com actress page is missing its data")
    canonical = actress_url(actress.get("id") or actress_id)
    performer = {"name": name, "url": canonical, "urls": [canonical]}
    aliases = _aliases(actress.get("alias_name"))
    if aliases:
        performer["aliases"] = aliases
    details = (actress.get("description") or "").strip()
    if details:
        performer["details"] = details
    image = _real_image(actress.get("image_url"))
    if image:
        performer["image"] = image
        performer["images"] = [image]
    return performer


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
            "performer-by-fragment requires a url: fc2cmadb.com performer "
            "fragments cannot be resolved without one"
        )
    return performer_by_url({"url": url})


def performer_by_name(payload):
    """Stash ``performer-by-name``: candidates from the actress search page."""
    name = ((payload or {}).get("name") or "").strip()
    if not name:
        return []
    http = _client()
    final_url, page = fetch_page(http, _search_url(name, "actress"))
    component = page.get("component")
    if component == "Actresses/Show":
        actress = (page.get("props") or {}).get("actress") or {}
        stub = _performer_stub(actress)
        return [stub] if stub else []
    if component != "Search/Actress":
        raise ScraperError("fc2cmadb.com actress search returned an unexpected page")
    paginator = (page.get("props") or {}).get("actresses") or {}
    items = paginator.get("data") if isinstance(paginator, dict) else paginator
    results = []
    for actress in items or []:
        if not isinstance(actress, dict):
            continue
        stub = _performer_stub(actress)
        if stub:
            results.append(stub)
    return results


OPERATIONS = {
    "scene-by-url": scene_by_url,
    "scene-by-fragment": scene_by_fragment,
    "scene-by-query-fragment": scene_by_fragment,
    "scene-by-name": scene_by_name,
    "performer-by-url": performer_by_url,
    "performer-by-fragment": performer_by_fragment,
    "performer-by-name": performer_by_name,
}

LIST_OPERATIONS = ("scene-by-name", "performer-by-name")


def _empty_result(operation):
    """Result emitted when an operation fails at runtime.

    Stash decodes the script's stdout before it looks at the exit code, so
    exiting non-zero without JSON surfaces as "could not unmarshal json from
    script output: EOF" in the UI. Emitting an empty result instead makes Stash
    report "no results", while the real reason stays visible in Stash's logs
    through stderr (``[Scrape / fc2cmadb] ...``).
    """
    return [] if operation in LIST_OPERATIONS else None


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
            "usage: fc2cmadb.py <{0}>".format("|".join(sorted(OPERATIONS))),
            file=sys.stderr,
        )
        return 2

    raw_input = sys.stdin.read()
    try:
        payload = json.loads(raw_input) if raw_input.strip() else {}
    except json.JSONDecodeError as exc:
        print("fc2cmadb: invalid JSON on stdin: {0}".format(exc), file=sys.stderr)
        return 1

    try:
        result = OPERATIONS[argv[1]](payload)
    except ScraperError as exc:
        print("fc2cmadb: {0}".format(exc), file=sys.stderr)
        result = _empty_result(argv[1])
    except Exception as exc:  # no traceback for CLI users
        print("fc2cmadb: unexpected error: {0}".format(exc), file=sys.stderr)
        result = _empty_result(argv[1])

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
