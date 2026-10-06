"""Offline unit tests for the FC2CMADB Stash scraper.

The suite never opens a socket: ``HttpClient.get`` and
``urllib.request.urlopen`` are patched, and fixtures are read from
``tests/fixtures/fc2cmadb/``.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import email.message
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures" / "fc2cmadb"
SCRAPER_DIR = REPO_ROOT / "scrapers" / "fc2cmadb"
SCRAPER_PATH = SCRAPER_DIR / "fc2cmadb.py"
YML_PATH = SCRAPER_DIR / "fc2cmadb.yml"

_spec = importlib.util.spec_from_file_location("fc2cmadb", SCRAPER_PATH)
assert _spec is not None and _spec.loader is not None
fc2cmadb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fc2cmadb)

ARTICLE_URL = "https://fc2cmadb.com/articles/4961548"
ACTRESS_URL = "https://fc2cmadb.com/actresses/4755"
WRITER_URL = "https://fc2cmadb.com/writers/mumuken"
WRITER_FC2_URL = "https://adult.contents.fc2.com/users/mumuken/"
SEARCH_TITLE_URL = fc2cmadb._search_url("巨乳", "title")
SEARCH_ACTRESS_URL = fc2cmadb._search_url("えりか", "actress")


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def page_json(name):
    return fc2cmadb.parse_data_page(fixture(name))


class FakeHeaders:
    """Minimal stand-in for ``http.client.HTTPMessage``."""

    def __init__(self, set_cookies=None):
        self._set_cookies = list(set_cookies or [])

    def get_all(self, name):
        if name.lower() == "set-cookie":
            return list(self._set_cookies)
        return None


class FakeResponse:
    """Minimal stand-in for the object returned by ``urlopen``."""

    def __init__(self, body, url, status=200, set_cookies=None):
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self._url = url
        self.headers = FakeHeaders(set_cookies)

    def read(self):
        return self._body

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeHttp:
    """Sequential fake of ``HttpClient``: each ``get`` consumes the next call."""

    def __init__(self, calls):
        self._queue = list(calls)
        self.calls = []

    def get(self, url, headers=None):
        headers = dict(headers or {})
        self.calls.append({"url": url, "headers": headers})
        if not self._queue:
            raise AssertionError("unexpected request: {0}".format(url))
        expected = self._queue.pop(0)
        if expected["url"] != url:
            raise AssertionError(
                "expected {0!r}, got {1!r}".format(expected["url"], url)
            )
        inertia = expected.get("inertia")
        if inertia is not None:
            is_inertia = headers.get("X-Inertia") == "true"
            if is_inertia != inertia:
                raise AssertionError("X-Inertia mismatch for {0}".format(url))
        return (
            expected.get("status", 200),
            expected.get("final_url") or url,
            FakeHeaders(),
            expected["body"],
        )


def scene_calls(article_url=ARTICLE_URL):
    """The three requests a full scene fetch is expected to make."""
    return [
        {"url": article_url, "body": fixture("article-4961548.html"), "inertia": False},
        {
            "url": article_url,
            "body": fixture("article-4961548-actresses.json"),
            "inertia": True,
        },
        {"url": WRITER_URL, "body": fixture("writer-mumuken.html"), "inertia": False},
    ]


def article_from_fixture():
    return page_json("article-4961548.html")["props"]["article"]


def actresses_from_fixture():
    return json.loads(fixture("article-4961548-actresses.json"))["props"]["actresses"]


def writer_from_fixture():
    return page_json("writer-mumuken.html")["props"]["writer"]


class ExtractIdTests(unittest.TestCase):
    def test_fc2_ppv_prefixed_forms(self):
        self.assertEqual(fc2cmadb.extract_id("FC2-PPV-4985048.mp4"), "4985048")
        self.assertEqual(fc2cmadb.extract_id("fc2ppv4985048"), "4985048")
        self.assertEqual(fc2cmadb.extract_id("FC2 PPV 4971389 夜遊び"), "4971389")
        self.assertEqual(fc2cmadb.extract_id("fc2-ppv-4985048"), "4985048")

    def test_bare_id(self):
        self.assertEqual(fc2cmadb.extract_id("4986793"), "4986793")

    def test_unrelated_filename_has_no_id(self):
        self.assertIsNone(fc2cmadb.extract_id("Some.Movie.2024.1080p.mkv"))

    def test_empty_input(self):
        self.assertIsNone(fc2cmadb.extract_id(""))
        self.assertIsNone(fc2cmadb.extract_id(None))


class ExtractUrlTests(unittest.TestCase):
    def test_article_url(self):
        self.assertEqual(fc2cmadb.extract_id_from_url(ARTICLE_URL), "4961548")

    def test_article_url_with_query_and_fragment(self):
        self.assertEqual(
            fc2cmadb.extract_id_from_url(ARTICLE_URL + "?page=1#top"), "4961548"
        )

    def test_other_host_is_rejected(self):
        self.assertIsNone(
            fc2cmadb.extract_id_from_url("https://example.com/articles/4985048")
        )

    def test_actress_url_is_not_a_video(self):
        self.assertIsNone(fc2cmadb.extract_id_from_url(ACTRESS_URL))

    def test_actress_id(self):
        self.assertEqual(fc2cmadb.extract_actress_id(ACTRESS_URL), "4755")

    def test_actress_other_host_is_rejected(self):
        self.assertIsNone(
            fc2cmadb.extract_actress_id("https://example.com/actresses/4755")
        )

    def test_article_url_is_not_an_actress(self):
        self.assertIsNone(fc2cmadb.extract_actress_id(ARTICLE_URL))


class ParseDataPageTests(unittest.TestCase):
    def test_article_page(self):
        page = page_json("article-4961548.html")
        self.assertEqual(page["component"], "Articles/Show")
        article = page["props"]["article"]
        self.assertEqual(article["video_id"], 4961548)
        self.assertEqual(article["release_date"], "2026-08-14")
        self.assertEqual(article["duration"], "58:27")

    def test_actress_page(self):
        page = page_json("actress-4755.html")
        self.assertEqual(page["component"], "Actresses/Show")
        self.assertEqual(page["props"]["actress"]["name"], "清宮すず")

    def test_search_title_page(self):
        page = page_json("search-title-giant.html")
        self.assertEqual(page["component"], "Search/Title")
        self.assertEqual(page["deferredProps"], {"default": ["articles"]})

    def test_search_actress_page(self):
        page = page_json("search-actress-erika.html")
        self.assertEqual(page["component"], "Search/Actress")
        paginator = page["props"]["actresses"]
        self.assertEqual(paginator["total"], 80)
        self.assertEqual(len(paginator["data"]), 40)

    def test_error_pages(self):
        for name, status in (("error-404-article.html", 404), ("error-403-actress.html", 403)):
            page = page_json(name)
            self.assertEqual(page["component"], "Error")
            self.assertEqual(page["props"]["status"], status)

    def test_missing_data_page_raises(self):
        with self.assertRaises(fc2cmadb.ScraperError):
            fc2cmadb.parse_data_page("<html><body>nope</body></html>")


class HttpClientTests(unittest.TestCase):
    def test_cookie_header_includes_configured_cookies_and_age_verified(self):
        client = fc2cmadb.HttpClient("fc2cmadb-session=abc; remember_web_x=def")
        captured = []

        def fake_urlopen(request, timeout=None, context=None):
            captured.append(request)
            return FakeResponse("ok", "https://fc2cmadb.com/")

        with mock.patch.object(fc2cmadb.urllib.request, "urlopen", fake_urlopen):
            client.get("https://fc2cmadb.com/")
        header = captured[0].get_header("Cookie")
        self.assertIn("fc2cmadb-session=abc", header)
        self.assertIn("remember_web_x=def", header)
        self.assertIn("ageVerified=true", header)

    def test_age_verified_is_not_duplicated(self):
        client = fc2cmadb.HttpClient("ageVerified=true")
        self.assertEqual(client._cookie_header(), "ageVerified=true")

    def test_set_cookie_is_absorbed_for_later_requests(self):
        client = fc2cmadb.HttpClient("fc2cmadb-session=OLD")
        responses = [
            FakeResponse(
                "ok",
                "https://fc2cmadb.com/",
                set_cookies=["fc2cmadb-session=NEW; Path=/; HttpOnly"],
            ),
            FakeResponse("ok", "https://fc2cmadb.com/"),
        ]
        captured = []

        def fake_urlopen(request, timeout=None, context=None):
            captured.append(request)
            return responses.pop(0)

        with mock.patch.object(fc2cmadb.urllib.request, "urlopen", fake_urlopen):
            client.get("https://fc2cmadb.com/")
            client.get("https://fc2cmadb.com/")
        self.assertIn("fc2cmadb-session=NEW", captured[1].get_header("Cookie"))

    def test_http_error_returns_status_and_body(self):
        error_headers = email.message.Message()
        error_headers["Set-Cookie"] = "fc2cmadb-session=NEW"
        error = urllib.error.HTTPError(
            "https://fc2cmadb.com/actresses/6132",
            403,
            "Forbidden",
            error_headers,
            io.BytesIO(b"denied"),
        )
        client = fc2cmadb.HttpClient()
        with mock.patch.object(
            fc2cmadb.urllib.request, "urlopen", side_effect=error
        ):
            status, final_url, headers, body = client.get(
                "https://fc2cmadb.com/actresses/6132"
            )
        self.assertEqual(status, 403)
        self.assertEqual(final_url, "https://fc2cmadb.com/actresses/6132")
        self.assertEqual(body, "denied")
        self.assertEqual(client.cookies["fc2cmadb-session"], "NEW")


class FetchPageTests(unittest.TestCase):
    def test_ok_returns_final_url_and_page(self):
        http = FakeHttp([{"url": ARTICLE_URL, "body": fixture("article-4961548.html")}])
        final_url, page = fc2cmadb.fetch_page(http, ARTICLE_URL)
        self.assertEqual(final_url, ARTICLE_URL)
        self.assertEqual(page["component"], "Articles/Show")

    def test_error_component_maps_to_scraper_error(self):
        http = FakeHttp(
            [
                {
                    "url": ARTICLE_URL,
                    "body": fixture("error-404-article.html"),
                    "status": 404,
                }
            ]
        )
        with self.assertRaises(fc2cmadb.ScraperError) as ctx:
            fc2cmadb.fetch_page(http, ARTICLE_URL)
        self.assertIn("No query results", str(ctx.exception))

    def test_actress_403_retries_once_then_reports_login(self):
        http = FakeHttp(
            [
                {
                    "url": ACTRESS_URL,
                    "body": fixture("error-403-actress.html"),
                    "status": 403,
                },
                {
                    "url": ACTRESS_URL,
                    "body": fixture("error-403-actress.html"),
                    "status": 403,
                },
            ]
        )
        with self.assertRaises(fc2cmadb.ScraperError) as ctx:
            fc2cmadb.fetch_page(http, ACTRESS_URL)
        self.assertEqual(len(http.calls), 2)
        self.assertIn("login", str(ctx.exception))

    def test_403_retry_can_succeed(self):
        http = FakeHttp(
            [
                {
                    "url": ACTRESS_URL,
                    "body": fixture("error-403-actress.html"),
                    "status": 403,
                },
                {"url": ACTRESS_URL, "body": fixture("actress-4755.html")},
            ]
        )
        final_url, page = fc2cmadb.fetch_page(http, ACTRESS_URL)
        self.assertEqual(page["component"], "Actresses/Show")

    def test_html_without_app_data_reports_http_error(self):
        http = FakeHttp(
            [{"url": ARTICLE_URL, "body": "<html>oops</html>", "status": 500}]
        )
        with self.assertRaises(fc2cmadb.ScraperError) as ctx:
            fc2cmadb.fetch_page(http, ARTICLE_URL)
        self.assertIn("HTTP 500", str(ctx.exception))


class FetchPartialTests(unittest.TestCase):
    def test_partial_returns_props(self):
        http = FakeHttp(
            [
                {
                    "url": ARTICLE_URL,
                    "body": fixture("article-4961548-actresses.json"),
                    "inertia": True,
                }
            ]
        )
        props = fc2cmadb.fetch_partial(http, ARTICLE_URL, "Articles/Show", "v1", "actresses")
        self.assertEqual(props["actresses"][0]["name"], "清宮すず")
        sent = http.calls[0]["headers"]
        self.assertEqual(sent["X-Inertia-Partial-Component"], "Articles/Show")
        self.assertEqual(sent["X-Inertia-Partial-Data"], "actresses")
        self.assertEqual(sent["X-Inertia-Version"], "v1")

    def test_version_mismatch_refetches_once(self):
        http = FakeHttp(
            [
                {"url": ARTICLE_URL, "body": "", "status": 409, "inertia": True},
                {"url": ARTICLE_URL, "body": fixture("article-4961548.html"), "inertia": False},
                {
                    "url": ARTICLE_URL,
                    "body": fixture("article-4961548-actresses.json"),
                    "inertia": True,
                },
            ]
        )
        props = fc2cmadb.fetch_partial(http, ARTICLE_URL, "Articles/Show", "stale", "actresses")
        self.assertTrue(props["actresses"])
        self.assertEqual(len(http.calls), 3)
        self.assertEqual(
            http.calls[2]["headers"]["X-Inertia-Version"],
            page_json("article-4961548.html")["version"],
        )

    def test_http_error_maps_to_scraper_error(self):
        http = FakeHttp([{"url": ARTICLE_URL, "body": "", "status": 500, "inertia": True}])
        with self.assertRaises(fc2cmadb.ScraperError):
            fc2cmadb.fetch_partial(http, ARTICLE_URL, "Articles/Show", "v1", "actresses")

    def test_invalid_json_maps_to_scraper_error(self):
        http = FakeHttp(
            [{"url": ARTICLE_URL, "body": "<html>nope</html>", "inertia": True}]
        )
        with self.assertRaises(fc2cmadb.ScraperError):
            fc2cmadb.fetch_partial(http, ARTICLE_URL, "Articles/Show", "v1", "actresses")


class SceneMappingTests(unittest.TestCase):
    def setUp(self):
        self.scene = fc2cmadb._scene_from_article(
            article_from_fixture(), actresses_from_fixture(), writer_from_fixture()
        )

    def test_code_title_date_details(self):
        self.assertEqual(self.scene["code"], "FC2-PPV-4961548")
        self.assertTrue(self.scene["title"].startswith("スレンダーGカップ"))
        self.assertEqual(self.scene["date"], "2026-08-14")
        self.assertEqual(self.scene["details"], "Duration: 58:27")

    def test_urls_and_image(self):
        self.assertEqual(self.scene["urls"], [ARTICLE_URL])
        self.assertTrue(self.scene["image"].startswith("https://contents-thumbnail2.fc2.com/"))

    def test_studio(self):
        studio = self.scene["studio"]
        self.assertEqual(studio["name"], "ひらめき無無剣")
        self.assertEqual(studio["url"], WRITER_URL)
        self.assertEqual(studio["urls"], [WRITER_URL, WRITER_FC2_URL])
        self.assertTrue(studio["image"].startswith("https://storage69000.contents.fc2.com/"))
        self.assertEqual(studio["images"], [studio["image"]])

    def test_performers(self):
        performers = self.scene["performers"]
        self.assertEqual(len(performers), 1)
        performer = performers[0]
        self.assertEqual(performer["name"], "清宮すず")
        self.assertEqual(performer["url"], ACTRESS_URL)
        self.assertEqual(performer["urls"], [ACTRESS_URL])
        self.assertIn("松長千絵", performer["aliases"])
        self.assertNotIn("image", performer)

    def test_tags(self):
        self.assertIn({"name": "制服"}, self.scene["tags"])
        self.assertIn({"name": "無修正"}, self.scene["tags"])

    def test_placeholder_title_is_omitted(self):
        scene = fc2cmadb._scene_from_article(
            {"video_id": "123456", "title": "FC2-PPV-123456"}, [], None
        )
        self.assertNotIn("title", scene)

    def test_empty_title_is_omitted(self):
        scene = fc2cmadb._scene_from_article({"video_id": "123456", "title": " "}, [], None)
        self.assertNotIn("title", scene)

    def test_studio_without_writer_page_has_no_image(self):
        scene = fc2cmadb._scene_from_article(article_from_fixture(), [], None)
        self.assertNotIn("image", scene["studio"])
        self.assertEqual(scene["studio"]["urls"], [WRITER_URL])

    def test_minimal_article(self):
        scene = fc2cmadb._scene_from_article({"video_id": "123456"}, [], None)
        self.assertEqual(scene, {"code": "FC2-PPV-123456", "urls": ["https://fc2cmadb.com/articles/123456"]})

    def test_no_image_placeholder_is_skipped(self):
        scene = fc2cmadb._scene_from_article(
            {"video_id": "123456", "image_url": "/storage/images/article/no-image.jpg"}, [], None
        )
        self.assertNotIn("image", scene)


class HelperTests(unittest.TestCase):
    def test_aliases_split_and_join(self):
        self.assertEqual(fc2cmadb._aliases("a  b\tc"), "a, b, c")
        self.assertEqual(fc2cmadb._aliases(""), "")
        self.assertEqual(fc2cmadb._aliases(None), "")

    def test_real_image(self):
        self.assertEqual(fc2cmadb._real_image("/storage/images/actress/no-image.jpg"), "")
        self.assertEqual(fc2cmadb._real_image("no-image.jpg"), "")
        self.assertEqual(
            fc2cmadb._real_image("/storage/images/actress/1.jpg"),
            "https://fc2cmadb.com/storage/images/actress/1.jpg",
        )
        self.assertEqual(
            fc2cmadb._real_image("https://x.example/i.jpg"), "https://x.example/i.jpg"
        )
        self.assertEqual(fc2cmadb._real_image(""), "")


class SceneByUrlTests(unittest.TestCase):
    def test_full_scene_fetch(self):
        http = FakeHttp(scene_calls())
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            scene = fc2cmadb.scene_by_url({"url": ARTICLE_URL})
        self.assertEqual(scene["code"], "FC2-PPV-4961548")
        self.assertEqual(scene["studio"]["name"], "ひらめき無無剣")
        self.assertEqual(scene["performers"][0]["name"], "清宮すず")
        self.assertEqual(
            [call["url"] for call in http.calls],
            [ARTICLE_URL, ARTICLE_URL, WRITER_URL],
        )

    def test_invalid_url_raises(self):
        with self.assertRaises(fc2cmadb.ScraperError):
            fc2cmadb.scene_by_url({"url": "https://example.com/articles/4985048"})

    def test_404_article_maps_to_error(self):
        http = FakeHttp(
            [
                {
                    "url": ARTICLE_URL,
                    "body": fixture("error-404-article.html"),
                    "status": 404,
                }
            ]
        )
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            with self.assertRaises(fc2cmadb.ScraperError) as ctx:
                fc2cmadb.scene_by_url({"url": ARTICLE_URL})
        self.assertIn("No query results", str(ctx.exception))

    def test_writer_failure_still_returns_scene(self):
        calls = scene_calls()
        calls[2] = {"url": WRITER_URL, "body": "", "status": 500}
        http = FakeHttp(calls)
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            scene = fc2cmadb.scene_by_url({"url": ARTICLE_URL})
        self.assertEqual(scene["studio"]["name"], "ひらめき無無剣")
        self.assertNotIn("image", scene["studio"])


class SceneFragmentTests(unittest.TestCase):
    def test_unresolved_returns_none_without_fetch(self):
        with mock.patch.object(
            fc2cmadb, "_client", side_effect=AssertionError("no network")
        ):
            self.assertIsNone(
                fc2cmadb.scene_by_fragment({"title": "Some.Movie.2024.mkv"})
            )

    def test_files_path_resolves(self):
        http = FakeHttp(scene_calls())
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            scene = fc2cmadb.scene_by_fragment(
                {"files": [{"path": r"C:\vids\FC2-PPV-4961548.mp4"}]}
            )
        self.assertEqual(scene["code"], "FC2-PPV-4961548")

    def test_url_fragment_resolves(self):
        http = FakeHttp(scene_calls())
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            scene = fc2cmadb.scene_by_fragment({"urls": [ARTICLE_URL]})
        self.assertEqual(scene["code"], "FC2-PPV-4961548")

    def test_title_fragment_resolves(self):
        http = FakeHttp(scene_calls())
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            scene = fc2cmadb.scene_by_fragment({"title": "FC2 PPV 4961548 x"})
        self.assertEqual(scene["code"], "FC2-PPV-4961548")


class SceneNameTests(unittest.TestCase):
    def test_id_fast_path_needs_no_network(self):
        with mock.patch.object(
            fc2cmadb, "_client", side_effect=AssertionError("no network")
        ):
            results = fc2cmadb.scene_by_name({"name": "FC2-PPV-4961548 なの"})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], ARTICLE_URL)

    def test_empty_name_returns_empty(self):
        with mock.patch.object(
            fc2cmadb, "_client", side_effect=AssertionError("no network")
        ):
            self.assertEqual(fc2cmadb.scene_by_name({"name": " "}), [])

    def test_single_match_redirect(self):
        http = FakeHttp(
            [
                {
                    "url": SEARCH_TITLE_URL,
                    "body": fixture("article-4961548.html"),
                    "final_url": ARTICLE_URL,
                }
            ]
        )
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            results = fc2cmadb.scene_by_name({"name": "巨乳"})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], ARTICLE_URL)
        self.assertTrue(results[0]["title"])

    def test_multi_match_lists_candidates(self):
        http = FakeHttp(
            [
                {"url": SEARCH_TITLE_URL, "body": fixture("search-title-giant.html")},
                {
                    "url": SEARCH_TITLE_URL,
                    "body": fixture("search-title-giant-articles.json"),
                    "inertia": True,
                },
            ]
        )
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            results = fc2cmadb.scene_by_name({"name": "巨乳"})
        self.assertEqual(len(results), 30)
        self.assertTrue(all(r["url"].startswith("https://fc2cmadb.com/articles/") for r in results))
        self.assertTrue(all(r["title"] for r in results))

    def test_no_match_returns_empty(self):
        url = fc2cmadb._search_url("zzz", "title")
        page = (
            '<script data-page="app" type="application/json">'
            '{"component":"Search/Title","props":{"keyword":"zzz"},"version":"v1"}'
            "</script>"
        )
        partial = (
            '{"component":"Search/Title","props":{"articles":{"data":[]}},"version":"v1"}'
        )
        http = FakeHttp(
            [
                {"url": url, "body": page},
                {"url": url, "body": partial, "inertia": True},
            ]
        )
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            self.assertEqual(fc2cmadb.scene_by_name({"name": "zzz"}), [])


class PerformerUrlTests(unittest.TestCase):
    def test_full_performer(self):
        http = FakeHttp([{"url": ACTRESS_URL, "body": fixture("actress-4755.html")}])
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            performer = fc2cmadb.performer_by_url({"url": ACTRESS_URL})
        self.assertEqual(performer["name"], "清宮すず")
        self.assertEqual(performer["url"], ACTRESS_URL)
        self.assertEqual(performer["urls"], [ACTRESS_URL])
        self.assertIn("松長千絵", performer["aliases"])
        self.assertNotIn("image", performer)
        self.assertNotIn("details", performer)

    def test_invalid_url_raises(self):
        with self.assertRaises(fc2cmadb.ScraperError):
            fc2cmadb.performer_by_url({"url": "https://example.com/actresses/4755"})

    def test_403_without_cookie_reports_login(self):
        http = FakeHttp(
            [
                {
                    "url": ACTRESS_URL,
                    "body": fixture("error-403-actress.html"),
                    "status": 403,
                },
                {
                    "url": ACTRESS_URL,
                    "body": fixture("error-403-actress.html"),
                    "status": 403,
                },
            ]
        )
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            with self.assertRaises(fc2cmadb.ScraperError) as ctx:
                fc2cmadb.performer_by_url({"url": ACTRESS_URL})
        self.assertIn("login", str(ctx.exception))


class PerformerNameTests(unittest.TestCase):
    def test_multi_match_lists_candidates(self):
        http = FakeHttp(
            [{"url": SEARCH_ACTRESS_URL, "body": fixture("search-actress-erika.html")}]
        )
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            results = fc2cmadb.performer_by_name({"name": "えりか"})
        self.assertEqual(len(results), 40)
        self.assertEqual(results[0]["name"], "高橋ゆい")
        self.assertEqual(results[0]["url"], "https://fc2cmadb.com/actresses/3010")
        self.assertNotIn("image", results[0])

    def test_single_match_redirect(self):
        http = FakeHttp(
            [
                {
                    "url": SEARCH_ACTRESS_URL,
                    "body": fixture("actress-4755.html"),
                    "final_url": ACTRESS_URL,
                }
            ]
        )
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            results = fc2cmadb.performer_by_name({"name": "えりか"})
        self.assertEqual(results, [{"name": "清宮すず", "url": ACTRESS_URL}])

    def test_empty_name_returns_empty(self):
        with mock.patch.object(
            fc2cmadb, "_client", side_effect=AssertionError("no network")
        ):
            self.assertEqual(fc2cmadb.performer_by_name({"name": ""}), [])

    def test_403_without_cookie_reports_login(self):
        http = FakeHttp(
            [
                {
                    "url": SEARCH_ACTRESS_URL,
                    "body": fixture("error-403-actress.html"),
                    "status": 403,
                },
                {
                    "url": SEARCH_ACTRESS_URL,
                    "body": fixture("error-403-actress.html"),
                    "status": 403,
                },
            ]
        )
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            with self.assertRaises(fc2cmadb.ScraperError) as ctx:
                fc2cmadb.performer_by_name({"name": "えりか"})
        self.assertIn("login", str(ctx.exception))


class PerformerFragmentTests(unittest.TestCase):
    def test_url_present_resolves(self):
        http = FakeHttp([{"url": ACTRESS_URL, "body": fixture("actress-4755.html")}])
        with mock.patch.object(fc2cmadb, "_client", return_value=http):
            performer = fc2cmadb.performer_by_fragment({"urls": [ACTRESS_URL]})
        self.assertEqual(performer["name"], "清宮すず")

    def test_without_url_raises(self):
        with self.assertRaises(fc2cmadb.ScraperError) as ctx:
            fc2cmadb.performer_by_fragment({"name": "清宮すず"})
        self.assertIn("url", str(ctx.exception).lower())


class CookieConfigTests(unittest.TestCase):
    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"FC2CMADB_COOKIE": "a=1; b=2"}):
            self.assertEqual(fc2cmadb.load_cookie(), "a=1; b=2")

    def test_ini_file_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = Path(tmp) / "fc2cmadb.ini"
            ini.write_text("[fc2cmadb]\ncookie = x=y; z=w\n", encoding="utf-8")
            env = mock.patch.dict(os.environ, {}, clear=True)
            env.start()
            self.addCleanup(env.stop)
            with mock.patch.object(fc2cmadb, "INI_PATH", ini):
                self.assertEqual(fc2cmadb.load_cookie(), "x=y; z=w")

    def test_ini_percent_encoded_cookie_is_literal(self):
        """Cookie values contain %-escapes (e.g. %3D); configparser must not interpolate."""
        cookie = "fc2cmadb-session=abc%3D; remember_web_x=def%2F; ageVerified=true"
        with tempfile.TemporaryDirectory() as tmp:
            ini = Path(tmp) / "fc2cmadb.ini"
            ini.write_text(
                "[fc2cmadb]\ncookie = {0}\n".format(cookie), encoding="utf-8"
            )
            env = mock.patch.dict(os.environ, {}, clear=True)
            env.start()
            self.addCleanup(env.stop)
            with mock.patch.object(fc2cmadb, "INI_PATH", ini):
                self.assertEqual(fc2cmadb.load_cookie(), cookie)

    def test_missing_config_returns_empty(self):
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        with mock.patch.object(
            fc2cmadb, "INI_PATH", Path("does-not-exist.ini")
        ):
            self.assertEqual(fc2cmadb.load_cookie(), "")


class CliSubprocessTests(unittest.TestCase):
    """Exercise the real stdin/stdout protocol through a child process."""

    def _run(self, args, stdin_text):
        return subprocess.run(
            [sys.executable, str(SCRAPER_PATH)] + args,
            input=stdin_text,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )

    def test_scene_by_name_direct_id_exits_0(self):
        result = self._run(["scene-by-name"], '{"name": "FC2-PPV-4961548"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["url"], ARTICLE_URL)

    def test_japanese_output_is_unescaped_utf8(self):
        result = self._run(["scene-by-name"], '{"name": "FC2-PPV-4961548 なの(18)"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("なの(18)", result.stdout)
        self.assertNotIn("\\u306a", result.stdout)
        json.loads(result.stdout)

    def test_unknown_operation_exits_2(self):
        result = self._run(["bogus-operation"], "")
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr.strip())

    def test_performer_fragment_without_url_reports_no_result(self):
        result = self._run(["performer-by-fragment"], '{"name": "清宮すず"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(result.stdout))
        self.assertIn("url", result.stderr.lower())
        self.assertNotIn("Traceback", result.stderr)

    def test_scene_by_url_error_reports_no_result(self):
        result = self._run(
            ["scene-by-url"], '{"url": "https://example.com/articles/1234567"}'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(result.stdout))
        self.assertIn("not a recognized", result.stderr.lower())

    def test_unresolvable_fragment_reports_no_result(self):
        result = self._run(
            ["scene-by-fragment"],
            '{"files": [{"path": "C:\\\\vids\\\\Some.Movie.2024.mkv"}]}',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(result.stdout))


class MainErrorTests(unittest.TestCase):
    """Runtime errors must emit empty JSON and exit 0 (Stash reads stdout first)."""

    def _run_main(self, args, stdin_text, side_effect=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        patches = [
            mock.patch.object(sys, "stdin", io.StringIO(stdin_text)),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(sys, "stderr", stderr),
        ]
        if side_effect is not None:
            patches.append(mock.patch.object(fc2cmadb, "_client", side_effect=side_effect))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        code = fc2cmadb.main(["fc2cmadb.py"] + args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_scene_error_emits_null(self):
        code, out, err = self._run_main(
            ["scene-by-url"],
            '{"url": "https://fc2cmadb.com/articles/4961548"}',
            side_effect=fc2cmadb.ScraperError("boom"),
        )
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out))
        self.assertIn("boom", err)

    def test_list_error_emits_empty_list(self):
        code, out, err = self._run_main(
            ["scene-by-name"],
            '{"name": "えりか"}',
            side_effect=fc2cmadb.ScraperError("boom"),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])
        self.assertIn("boom", err)

    def test_unexpected_error_emits_null(self):
        code, out, err = self._run_main(
            ["scene-by-url"],
            '{"url": "https://fc2cmadb.com/articles/4961548"}',
            side_effect=RuntimeError("kaput"),
        )
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out))
        self.assertIn("unexpected error", err)

    def test_invalid_json_still_exits_1(self):
        code, out, err = self._run_main(["scene-by-url"], "{not json")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("invalid JSON", err)


try:
    import yaml
except ImportError:  # PyYAML is optional; textual checks below are primary
    yaml = None


class YmlConfigTests(unittest.TestCase):
    """Stdlib textual checks of the Stash scraper configuration."""

    @classmethod
    def setUpClass(cls):
        cls.text = YML_PATH.read_text(encoding="utf-8")
        cls.lines = cls.text.splitlines()

    def test_name_is_first_line(self):
        self.assertEqual(self.lines[0], "name: FC2CMADB")

    def test_all_operation_keys_present(self):
        for key in (
            "sceneByURL",
            "performerByURL",
            "performerByName",
            "sceneByFragment",
            "sceneByQueryFragment",
            "sceneByName",
            "performerByFragment",
        ):
            self.assertRegex(self.text, r"(?m)^" + key + r":$")

    def test_url_operations_are_lists(self):
        for key in ("sceneByURL", "performerByURL"):
            self.assertRegex(self.text, r"(?m)^" + key + r":\n  - action: script\n")

    def test_single_operations_are_mappings(self):
        for key in (
            "sceneByFragment",
            "sceneByQueryFragment",
            "sceneByName",
            "performerByName",
            "performerByFragment",
        ):
            self.assertRegex(self.text, r"(?m)^" + key + r":\n  action: script\n")

    def test_script_args_match_operations(self):
        for operation in (
            "scene-by-url",
            "scene-by-fragment",
            "scene-by-query-fragment",
            "scene-by-name",
            "performer-by-url",
            "performer-by-fragment",
            "performer-by-name",
        ):
            self.assertRegex(
                self.text,
                r"(?m)^ {4,6}- fc2cmadb\.py\n {4,6}- " + operation + r"$",
            )

    def test_url_filters(self):
        self.assertRegex(
            self.text,
            r"(?m)^sceneByURL:\n  - action: script\n    url:\n      - fc2cmadb\.com\n",
        )
        self.assertIn("      - fc2cmadb.com/actresses/", self.text)

    def test_every_url_key_is_a_list(self):
        """Stash unmarshals `url` into []string — a scalar breaks scraper load."""
        url_indices = [
            index
            for index, line in enumerate(self.lines)
            if line.strip().startswith("url:")
        ]
        self.assertGreaterEqual(len(url_indices), 2)
        for index in url_indices:
            line = self.lines[index]
            self.assertEqual(line.strip(), "url:", line)
            self.assertTrue(
                index + 1 < len(self.lines)
                and self.lines[index + 1].lstrip().startswith("- "),
                "url: at line {0} must be followed by a list item".format(index + 1),
            )

    def test_no_tags_or_driver_sections(self):
        self.assertNotRegex(self.text, r"(?m)^tags:")
        self.assertNotRegex(self.text, r"(?m)^driver:")


@unittest.skipUnless(yaml is not None, "PyYAML is not installed")
class YmlPyYamlTests(unittest.TestCase):
    def test_yaml_structure(self):
        assert yaml is not None
        data = yaml.safe_load(YML_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "FC2CMADB")
        self.assertIsInstance(data["sceneByURL"], list)
        self.assertIsInstance(data["performerByURL"], list)
        self.assertEqual(data["sceneByURL"][0]["action"], "script")
        self.assertEqual(data["sceneByURL"][0]["script"][-1], "scene-by-url")
        for entry in data["sceneByURL"] + data["performerByURL"]:
            self.assertIsInstance(entry["url"], list)
            self.assertTrue(all(isinstance(u, str) for u in entry["url"]))


class RepoShapeTests(unittest.TestCase):
    def test_scraper_files_exist(self):
        self.assertTrue(SCRAPER_PATH.is_file())
        self.assertTrue(YML_PATH.is_file())

    def test_fixtures_exist(self):
        for name in (
            "article-4961548.html",
            "article-4961548-actresses.json",
            "actress-4755.html",
            "writer-mumuken.html",
            "search-title-giant.html",
            "search-title-giant-articles.json",
            "search-actress-erika.html",
            "error-404-article.html",
            "error-403-actress.html",
        ):
            self.assertTrue((FIXTURES / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
