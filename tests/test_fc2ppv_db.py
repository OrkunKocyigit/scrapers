"""Offline unit tests for the FC2PPV-DB Stash scraper.

The suite never opens a socket: ``urllib.request.urlopen`` and the
module-level ``fetch_html`` are patched, and fixtures are read from
``tests/fixtures/``.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures"
SCRAPER_DIR = REPO_ROOT / "scrapers" / "fc2ppv-db"
SCRAPER_PATH = SCRAPER_DIR / "fc2ppv-db.py"
YML_PATH = SCRAPER_DIR / "fc2ppv-db.yml"

_spec = importlib.util.spec_from_file_location("fc2ppv_db", SCRAPER_PATH)
assert _spec is not None and _spec.loader is not None
fc2ppv_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fc2ppv_db)

VIDEO_URL = "https://fc2ppv-db.com/en/videos/4985048"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    """Minimal stand-in for the object returned by ``urlopen``."""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def ok_envelope(html, final_url=VIDEO_URL, status=200):
    return json.dumps(
        {
            "status": "ok",
            "solution": {"url": final_url, "status": status, "response": html},
        }
    ).encode("utf-8")


class FetchHtmlTests(unittest.TestCase):
    """FlareSolverr request contract, error mapping and SSRF guard."""

    def setUp(self):
        self.calls = []
        env = mock.patch.dict(os.environ, {"FLARESOLVERR_SESSION": "none"})
        env.start()
        self.addCleanup(env.stop)

    def fake_urlopen(self, request, timeout=None):
        self.calls.append((request, timeout))
        return FakeResponse(ok_envelope("<html><title>Fine</title></html>"))

    def test_request_contract(self):
        with mock.patch.dict(
            os.environ, {"FLARESOLVERR_URL": "http://solver.local:8191/v1"}
        ):
            with mock.patch.object(
                fc2ppv_db.urllib.request, "urlopen", self.fake_urlopen
            ):
                html = fc2ppv_db.fetch_html(VIDEO_URL)
        self.assertIn("Fine", html)
        self.assertEqual(len(self.calls), 1)
        request, timeout = self.calls[0]
        self.assertEqual(request.full_url, "http://solver.local:8191/v1")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(timeout, 130)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["cmd"], "request.get")
        self.assertEqual(body["url"], VIDEO_URL)
        self.assertEqual(body["maxTimeout"], 60000)
        self.assertEqual(body["cookies"], [{"name": "age-verified", "value": "true"}])
        self.assertNotIn("session", body)
        self.assertNotIn("session_ttl_minutes", body)

    def test_solver_status_error_maps_to_scraper_error(self):
        payload = json.dumps(
            {"status": "error", "message": "Challenge not solved"}
        ).encode("utf-8")
        with mock.patch.object(
            fc2ppv_db.urllib.request,
            "urlopen",
            lambda request, timeout=None: FakeResponse(payload),
        ):
            with self.assertRaises(fc2ppv_db.ScraperError) as ctx:
                fc2ppv_db.fetch_html(VIDEO_URL)
        self.assertIn("Challenge not solved", str(ctx.exception))

    def test_cloudflare_1005_maps_to_scraper_error(self):
        payload = json.dumps(
            {
                "status": "ok",
                "solution": {
                    "url": VIDEO_URL,
                    "status": 403,
                    "response": "<html>Error 1005: ASN blocked</html>",
                },
            }
        ).encode("utf-8")
        with mock.patch.object(
            fc2ppv_db.urllib.request,
            "urlopen",
            lambda request, timeout=None: FakeResponse(payload),
        ):
            with self.assertRaises(fc2ppv_db.ScraperError) as ctx:
                fc2ppv_db.fetch_html(VIDEO_URL)
        self.assertIn("Cloudflare", str(ctx.exception))

    def test_connection_refused_maps_to_scraper_error(self):
        def boom(request, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

        with mock.patch.object(fc2ppv_db.urllib.request, "urlopen", boom):
            with self.assertRaises(fc2ppv_db.ScraperError) as ctx:
                fc2ppv_db.fetch_html(VIDEO_URL)
        self.assertIn("unreachable", str(ctx.exception))

    def test_age_gate_response_maps_to_scraper_error(self):
        payload = ok_envelope(
            fixture("age-verify.html"),
            final_url="https://fc2ppv-db.com/en/age-verify?redirect=%2Fen",
        )
        with mock.patch.object(
            fc2ppv_db.urllib.request,
            "urlopen",
            lambda request, timeout=None: FakeResponse(payload),
        ):
            with self.assertRaises(fc2ppv_db.ScraperError) as ctx:
                fc2ppv_db.fetch_html(VIDEO_URL)
        self.assertIn("age", str(ctx.exception).lower())

    def test_non_200_maps_to_scraper_error(self):
        payload = ok_envelope("<html>nope</html>", status=503)
        with mock.patch.object(
            fc2ppv_db.urllib.request,
            "urlopen",
            lambda request, timeout=None: FakeResponse(payload),
        ):
            with self.assertRaises(fc2ppv_db.ScraperError) as ctx:
                fc2ppv_db.fetch_html(VIDEO_URL)
        self.assertIn("503", str(ctx.exception))

    def test_foreign_host_never_reaches_urlopen(self):
        with mock.patch.object(fc2ppv_db.urllib.request, "urlopen", self.fake_urlopen):
            with self.assertRaises(fc2ppv_db.ScraperError):
                fc2ppv_db.fetch_html("https://evil.example.com/en/videos/1234567")
        self.assertEqual(self.calls, [])

    def test_not_found_page_maps_to_scraper_error(self):
        payload = ok_envelope(
            fixture("not-found.html"),
            final_url="https://fc2ppv-db.com/en/videos/635598",
        )
        with mock.patch.object(
            fc2ppv_db.urllib.request,
            "urlopen",
            lambda request, timeout=None: FakeResponse(payload),
        ):
            with self.assertRaises(fc2ppv_db.ScraperError) as ctx:
                fc2ppv_db.fetch_html("https://fc2ppv-db.com/en/videos/635598")
        self.assertIn("404", str(ctx.exception))


class SessionTests(unittest.TestCase):
    """FlareSolverr session reuse: create-or-reuse, then retry once on failure."""

    MISSING_INI = Path("does-not-exist.ini")

    def setUp(self):
        self.calls = []

    def _fetch(self, fake_post):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(fc2ppv_db, "INI_PATH", self.MISSING_INI):
                with mock.patch.object(fc2ppv_db, "_solver_post", fake_post):
                    return fc2ppv_db.fetch_html(VIDEO_URL)

    def _ok_html(self):
        return json.loads(
            ok_envelope("<html><title>Fine</title></html>").decode("utf-8")
        )

    def _created(self, payload):
        return {
            "status": "ok",
            "message": "Session created successfully.",
            "session": payload.get("session"),
        }

    def test_create_then_get_with_session(self):
        def fake_post(solver_url, payload):
            self.calls.append(payload)
            if payload["cmd"] == "sessions.create":
                return self._created(payload)
            return self._ok_html()

        html = self._fetch(fake_post)
        self.assertIn("Fine", html)
        self.assertEqual(
            [call["cmd"] for call in self.calls], ["sessions.create", "request.get"]
        )
        self.assertEqual(self.calls[0]["session"], "fc2ppv-db")
        get = self.calls[1]
        self.assertEqual(get["session"], "fc2ppv-db")
        self.assertEqual(get["session_ttl_minutes"], 30)
        self.assertEqual(get["cookies"], [{"name": "age-verified", "value": "true"}])

    def test_existing_session_is_reused(self):
        def fake_post(solver_url, payload):
            self.calls.append(payload)
            if payload["cmd"] == "sessions.create":
                return {
                    "status": "ok",
                    "message": "Session already exists.",
                    "session": payload.get("session"),
                }
            return self._ok_html()

        html = self._fetch(fake_post)
        self.assertIn("Fine", html)
        self.assertEqual(
            [call["cmd"] for call in self.calls], ["sessions.create", "request.get"]
        )

    def test_create_failure_falls_back_to_stateless(self):
        def fake_post(solver_url, payload):
            self.calls.append(payload)
            if payload["cmd"] == "sessions.create":
                raise fc2ppv_db.ScraperError("FlareSolverr error: no sessions")
            return self._ok_html()

        html = self._fetch(fake_post)
        self.assertIn("Fine", html)
        self.assertEqual(
            [call["cmd"] for call in self.calls], ["sessions.create", "request.get"]
        )
        self.assertNotIn("session", self.calls[1])
        self.assertNotIn("session_ttl_minutes", self.calls[1])

    def test_get_failure_recreates_session_and_retries(self):
        get_calls = []

        def fake_post(solver_url, payload):
            self.calls.append(payload)
            if payload["cmd"] == "sessions.create":
                return self._created(payload)
            if payload["cmd"] == "sessions.destroy":
                return {"status": "ok", "message": "The session has been removed."}
            get_calls.append(payload)
            if len(get_calls) == 1:
                raise fc2ppv_db.ScraperError("FlareSolverr error: chrome not reachable")
            return self._ok_html()

        html = self._fetch(fake_post)
        self.assertIn("Fine", html)
        self.assertEqual(
            [call["cmd"] for call in self.calls],
            [
                "sessions.create",
                "request.get",
                "sessions.destroy",
                "sessions.create",
                "request.get",
            ],
        )

    def test_retry_exhausted_raises(self):
        def fake_post(solver_url, payload):
            self.calls.append(payload)
            if payload["cmd"] == "sessions.create":
                return self._created(payload)
            if payload["cmd"] == "sessions.destroy":
                return {"status": "ok", "message": "The session has been removed."}
            raise fc2ppv_db.ScraperError("FlareSolverr error: still broken")

        with self.assertRaises(fc2ppv_db.ScraperError) as ctx:
            self._fetch(fake_post)
        self.assertIn("still broken", str(ctx.exception))
        self.assertEqual(
            [call["cmd"] for call in self.calls],
            [
                "sessions.create",
                "request.get",
                "sessions.destroy",
                "sessions.create",
                "request.get",
            ],
        )

    def test_sessions_disabled_via_env(self):
        def fake_post(solver_url, payload):
            self.calls.append(payload)
            return self._ok_html()

        with mock.patch.dict(
            os.environ, {"FLARESOLVERR_SESSION": "none"}, clear=True
        ):
            with mock.patch.object(fc2ppv_db, "INI_PATH", self.MISSING_INI):
                with mock.patch.object(fc2ppv_db, "_solver_post", fake_post):
                    html = fc2ppv_db.fetch_html(VIDEO_URL)
        self.assertIn("Fine", html)
        self.assertEqual([call["cmd"] for call in self.calls], ["request.get"])
        self.assertNotIn("session", self.calls[0])


class AgeGateTests(unittest.TestCase):
    def test_age_gate_fixture_detected(self):
        self.assertTrue(
            fc2ppv_db.is_age_gate(
                fixture("age-verify.html"),
                "https://fc2ppv-db.com/en/age-verify?redirect=%2Fen",
            )
        )

    def test_video_fixture_is_not_gate(self):
        self.assertFalse(fc2ppv_db.is_age_gate(fixture("video-4985048.html"), VIDEO_URL))

    def test_final_url_marks_gate(self):
        self.assertTrue(
            fc2ppv_db.is_age_gate("<html></html>", "https://fc2ppv-db.com/en/age-verify")
        )


class NotFoundTests(unittest.TestCase):
    def test_not_found_fixture_detected(self):
        self.assertTrue(fc2ppv_db.is_not_found(fixture("not-found.html")))

    def test_video_pages_are_not_flagged(self):
        for name in (
            "video-4985048.html",
            "video-4548515.html",
            "video-4986793.html",
        ):
            self.assertFalse(fc2ppv_db.is_not_found(fixture(name)), name)


class ConfigTests(unittest.TestCase):
    def _write_ini(self, tmp, text):
        path = Path(tmp) / "fc2ppv-db.ini"
        path.write_text(text, encoding="utf-8")
        return path

    def test_env_var_wins_over_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = self._write_ini(
                tmp, "[DEFAULT]\nflaresolverr_url = http://from-ini:8191/v1\n"
            )
            with mock.patch.object(fc2ppv_db, "INI_PATH", ini):
                with mock.patch.dict(
                    os.environ, {"FLARESOLVERR_URL": "http://from-env:8191/v1"}
                ):
                    self.assertEqual(
                        fc2ppv_db.load_flaresolverr_url(), "http://from-env:8191/v1"
                    )

    def test_ini_default_section_beats_builtin_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = self._write_ini(
                tmp, "[DEFAULT]\nflaresolverr_url = http://from-ini:8191/v1\n"
            )
            with mock.patch.object(fc2ppv_db, "INI_PATH", ini):
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(
                        fc2ppv_db.load_flaresolverr_url(), "http://from-ini:8191/v1"
                    )

    def test_bare_key_ini_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = self._write_ini(tmp, "flaresolverr_url = http://bare:8191/v1\n")
            with mock.patch.object(fc2ppv_db, "INI_PATH", ini):
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(
                        fc2ppv_db.load_flaresolverr_url(), "http://bare:8191/v1"
                    )

    def test_default_when_no_ini_and_no_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.ini"
            with mock.patch.object(fc2ppv_db, "INI_PATH", missing):
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(
                        fc2ppv_db.load_flaresolverr_url(),
                        "http://localhost:8191/v1",
                    )

    def test_unparsable_ini_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = self._write_ini(tmp, "[[[[[\n")
            with mock.patch.object(fc2ppv_db, "INI_PATH", ini):
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(
                        fc2ppv_db.load_flaresolverr_url(),
                        "http://localhost:8191/v1",
                    )

    def test_session_name_env_wins_over_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = self._write_ini(tmp, "session_name = from-ini\n")
            with mock.patch.object(fc2ppv_db, "INI_PATH", ini):
                with mock.patch.dict(
                    os.environ, {"FLARESOLVERR_SESSION": "from-env"}, clear=True
                ):
                    self.assertEqual(fc2ppv_db.load_session_name(), "from-env")

    def test_session_name_from_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = self._write_ini(tmp, "session_name = from-ini\n")
            with mock.patch.object(fc2ppv_db, "INI_PATH", ini):
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(fc2ppv_db.load_session_name(), "from-ini")

    def test_session_name_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.ini"
            with mock.patch.object(fc2ppv_db, "INI_PATH", missing):
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(fc2ppv_db.load_session_name(), "fc2ppv-db")

    def test_session_name_none_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.ini"
            with mock.patch.object(fc2ppv_db, "INI_PATH", missing):
                with mock.patch.dict(
                    os.environ, {"FLARESOLVERR_SESSION": "None"}, clear=True
                ):
                    self.assertEqual(fc2ppv_db.load_session_name(), "")

    def test_session_ttl_default_and_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.ini"
            with mock.patch.object(fc2ppv_db, "INI_PATH", missing):
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(fc2ppv_db.load_session_ttl_minutes(), 30)
                with mock.patch.dict(
                    os.environ, {"FLARESOLVERR_SESSION_TTL_MINUTES": "7"}, clear=True
                ):
                    self.assertEqual(fc2ppv_db.load_session_ttl_minutes(), 7)
                with mock.patch.dict(
                    os.environ,
                    {"FLARESOLVERR_SESSION_TTL_MINUTES": "junk"},
                    clear=True,
                ):
                    self.assertEqual(fc2ppv_db.load_session_ttl_minutes(), 30)
                with mock.patch.dict(
                    os.environ, {"FLARESOLVERR_SESSION_TTL_MINUTES": "0"}, clear=True
                ):
                    self.assertEqual(fc2ppv_db.load_session_ttl_minutes(), 30)

    def test_session_ttl_from_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = self._write_ini(tmp, "session_ttl_minutes = 12\n")
            with mock.patch.object(fc2ppv_db, "INI_PATH", ini):
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(fc2ppv_db.load_session_ttl_minutes(), 12)


class ParseVideoPageTests(unittest.TestCase):
    """scene-by-url DOM mapping, grounded in tests/fixtures/video-4985048.html."""

    @classmethod
    def setUpClass(cls):
        cls.html = fixture("video-4985048.html")
        cls.scene = fc2ppv_db.parse_video_page(cls.html, VIDEO_URL)

    def test_code(self):
        self.assertEqual(self.scene["code"], "FC2-PPV-4985048")

    def test_date(self):
        self.assertEqual(self.scene["date"], "2026-09-30")

    def test_title_strips_code_prefix(self):
        self.assertIn("なの(18)", self.scene["title"])
        self.assertFalse(self.scene["title"].startswith("FC2-PPV-4985048"))

    def test_studio(self):
        self.assertEqual(self.scene["studio"]["name"], "大人仮面Z")
        self.assertIn("/sellers/", self.scene["studio"]["url"])
        self.assertEqual(
            self.scene["studio"]["urls"], [self.scene["studio"]["url"]]
        )
        self.assertTrue(
            self.scene["studio"]["image"].endswith("/sellers/otonakamenz.webp")
        )
        self.assertEqual(
            self.scene["studio"]["images"], [self.scene["studio"]["image"]]
        )

    def test_performers(self):
        performer = self.scene["performers"][0]
        self.assertEqual(performer["name"], "なの(18)")
        self.assertIn("/actresses/", performer["url"])

    def test_image_is_thumbnail(self):
        self.assertTrue(self.scene["image"].endswith("/thumbnails/49/4985048.webp"))

    def test_urls_contains_video_url(self):
        self.assertTrue(
            any(url.endswith("/videos/4985048") for url in self.scene["urls"])
        )

    def test_details_contains_duration(self):
        self.assertIn("1:25:35", self.scene["details"])

    def test_tags(self):
        names = [tag["name"] for tag in self.scene["tags"]]
        self.assertEqual(names[0], "素人")
        self.assertIn("中出し", names)
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 10)


class SceneByUrlTests(unittest.TestCase):
    def test_missing_url_raises(self):
        with self.assertRaises(fc2ppv_db.ScraperError):
            fc2ppv_db.scene_by_url({})

    def test_foreign_url_raises(self):
        with self.assertRaises(fc2ppv_db.ScraperError):
            fc2ppv_db.scene_by_url({"url": "https://example.com/en/videos/1234567"})

    def test_scene_by_url_uses_fetch_html(self):
        html = fixture("video-4985048.html")
        with mock.patch.object(
            fc2ppv_db, "fetch_html", return_value=html
        ) as fetch:
            scene = fc2ppv_db.scene_by_url({"url": VIDEO_URL})
        fetch.assert_called_once_with(VIDEO_URL)
        self.assertEqual(scene["code"], "FC2-PPV-4985048")
        self.assertEqual(scene["date"], "2026-09-30")


class ExtractIdFromUrlTests(unittest.TestCase):
    def test_video_url(self):
        self.assertEqual(
            fc2ppv_db.extract_id_from_url(VIDEO_URL), "4985048"
        )

    def test_foreign_host_returns_none(self):
        self.assertIsNone(
            fc2ppv_db.extract_id_from_url("https://example.com/en/videos/1234567")
        )


class ExtractIdTests(unittest.TestCase):
    def test_prefixed_forms(self):
        cases = {
            "FC2-PPV-4985048.mp4": "4985048",
            "FC2PPV-4985048": "4985048",
            "fc2ppv4985048": "4985048",
            "FC2_PPV_4985048.mkv": "4985048",
            "fc2 ppv 4971389": "4971389",
            "FC2-PPV-4985048-1080.mp4": "4985048",
        }
        for text, expected in cases.items():
            self.assertEqual(fc2ppv_db.extract_id(text), expected, text)

    def test_bare_token(self):
        self.assertEqual(fc2ppv_db.extract_id("4985048.mp4"), "4985048")
        self.assertEqual(fc2ppv_db.extract_id(r"C:\vids\4971389.mp4"), "4971389")

    def test_resolution_never_matches(self):
        self.assertIsNone(fc2ppv_db.extract_id("1080p.mkv"))
        self.assertIsNone(fc2ppv_db.extract_id("movie-1080.mp4"))

    def test_prefixed_form_wins_over_earlier_bare_token(self):
        self.assertEqual(
            fc2ppv_db.extract_id("123456 FC2-PPV-4985048"), "4985048"
        )

    def test_nothing_to_extract(self):
        self.assertIsNone(fc2ppv_db.extract_id("no id in this title"))
        self.assertIsNone(fc2ppv_db.extract_id(""))
        self.assertIsNone(fc2ppv_db.extract_id(None))


class VideoEdgeFixtureTests(unittest.TestCase):
    """Fixtures with missing fields must omit keys rather than invent data."""

    def test_4971389_has_no_performers(self):
        scene = fc2ppv_db.parse_video_page(
            fixture("video-4971389.html"), "https://fc2ppv-db.com/en/videos/4971389"
        )
        self.assertEqual(scene["code"], "FC2-PPV-4971389")
        self.assertEqual(scene["date"], "2026-09-08")
        self.assertIn("studio", scene)
        self.assertTrue(
            scene["studio"]["image"].endswith("/sellers/hamideru.webp")
        )
        self.assertIn("image", scene)
        self.assertNotIn("performers", scene)

    def test_4548515_missing_date_and_image_but_performer_has_face(self):
        scene = fc2ppv_db.parse_video_page(
            fixture("video-4548515.html"), "https://fc2ppv-db.com/en/videos/4548515"
        )
        self.assertEqual(scene["code"], "FC2-PPV-4548515")
        self.assertNotIn("date", scene)
        self.assertNotIn("image", scene)
        self.assertNotIn("studio", scene)
        performer = scene["performers"][0]
        self.assertEqual(performer["name"], "田中なな実")
        self.assertIn("/actresses/", performer["url"])
        self.assertIn("faces/actress_", performer["image"])
        self.assertEqual(performer["images"], [performer["image"]])


class TagTests(unittest.TestCase):
    """Tag pills (/videos?tags=) map to ScrapedTag entries in page order."""

    def test_4986793_all_tags_in_order(self):
        scene = fc2ppv_db.parse_video_page(
            fixture("video-4986793.html"),
            "https://fc2ppv-db.com/en/videos/4986793",
        )
        self.assertEqual(
            [tag["name"] for tag in scene["tags"]],
            [
                "痙攣",
                "中出し",
                "フェラ",
                "長身",
                "新人",
                "色白",
                "モデル",
                "インフルエンサー",
                "騎乗位",
                "バック",
                "クンニ",
                "初撮り",
                "スレンダー",
                "神回",
            ],
        )

    def test_4548515_has_no_tags(self):
        scene = fc2ppv_db.parse_video_page(
            fixture("video-4548515.html"),
            "https://fc2ppv-db.com/en/videos/4548515",
        )
        self.assertNotIn("tags", scene)


class DescriptionTests(unittest.TestCase):
    """The site's description block is appended below the duration in details."""

    def test_4856210_duration_then_description(self):
        scene = fc2ppv_db.parse_video_page(
            fixture("video-4856210.html"),
            "https://fc2ppv-db.com/en/videos/4856210",
        )
        self.assertEqual(scene["code"], "FC2-PPV-4856210")
        details = scene["details"]
        self.assertTrue(details.startswith("Duration: 59:36\n\n"))
        self.assertIn(
            "★過去二作はこちら★\n"
            "女〇アナに内定の大型新人!!モデル並みのスタイルと男をコロがす最強のあざと可愛い逸材♡"
            "たった一度の過ち映像を期間限定大公開!!",
            details,
        )

    def test_multiline_description_preserved(self):
        scene = fc2ppv_db.parse_video_page(fixture("video-4985048.html"), VIDEO_URL)
        details = scene["details"]
        self.assertTrue(details.startswith("Duration: 1:25:35\n\n"))
        self.assertIn("今回お会いしたのは、なのちゃん(18)です。", details)
        self.assertIn("\n", details.split("\n\n", 1)[1])

    def test_4548515_has_no_details(self):
        scene = fc2ppv_db.parse_video_page(
            fixture("video-4548515.html"),
            "https://fc2ppv-db.com/en/videos/4548515",
        )
        self.assertNotIn("details", scene)


class SceneFragmentTests(unittest.TestCase):
    def test_resolves_from_file_path(self):
        html = fixture("video-4971389.html")
        with mock.patch.object(fc2ppv_db, "fetch_html", return_value=html) as fetch:
            scene = fc2ppv_db.scene_by_fragment(
                {"files": [{"path": r"C:\vids\FC2-PPV-4971389.mp4"}]}
            )
        fetch.assert_called_once_with("https://fc2ppv-db.com/en/videos/4971389")
        self.assertEqual(scene["code"], "FC2-PPV-4971389")

    def test_resolves_from_urls_first(self):
        html = fixture("video-4971389.html")
        with mock.patch.object(fc2ppv_db, "fetch_html", return_value=html) as fetch:
            scene = fc2ppv_db.scene_by_fragment(
                {"urls": ["https://fc2ppv-db.com/en/videos/4971389"]}
            )
        fetch.assert_called_once_with("https://fc2ppv-db.com/en/videos/4971389")
        self.assertEqual(scene["code"], "FC2-PPV-4971389")

    def test_query_fragment_resolves_from_title(self):
        html = fixture("video-4971389.html")
        dispatch = fc2ppv_db.OPERATIONS["scene-by-query-fragment"]
        with mock.patch.object(fc2ppv_db, "fetch_html", return_value=html) as fetch:
            scene = dispatch({"title": "FC2 PPV 4971389 夜遊び"})
        fetch.assert_called_once_with("https://fc2ppv-db.com/en/videos/4971389")
        self.assertEqual(scene["code"], "FC2-PPV-4971389")

    def test_no_id_returns_empty_dict_without_fetch(self):
        guard = mock.patch.object(
            fc2ppv_db,
            "fetch_html",
            side_effect=AssertionError("fetch_html must not be called"),
        )
        with guard:
            self.assertEqual(
                fc2ppv_db.scene_by_fragment({"title": "nothing resolvable"}), {}
            )


class SearchResultsTests(unittest.TestCase):
    def test_parse_fixture(self):
        results = fc2ppv_db.parse_search_results(fixture("search-results.html"))
        urls = [result["url"] for result in results]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertIn("https://fc2ppv-db.com/en/videos/4548515", urls)
        for result in results:
            self.assertRegex(result["url"], r"^https://fc2ppv-db\.com/en/videos/\d+$")
            self.assertTrue(result["title"])

    def test_dedupe_keeps_page_order(self):
        html = (
            '<a title="A" href="/en/videos/111111"></a>'
            '<a title="B" href="/en/videos/222222"></a>'
            '<a href="/ja/videos/111111"></a>'
        )
        results = fc2ppv_db.parse_search_results(html)
        self.assertEqual(
            [result["url"] for result in results],
            [
                "https://fc2ppv-db.com/en/videos/111111",
                "https://fc2ppv-db.com/en/videos/222222",
            ],
        )
        self.assertEqual(results[0]["title"], "A")


class SceneNameTests(unittest.TestCase):
    def test_direct_id_skips_network(self):
        guard = mock.patch.object(
            fc2ppv_db,
            "fetch_html",
            side_effect=AssertionError("fetch_html must not be called"),
        )
        with guard:
            results = fc2ppv_db.scene_by_name({"name": "FC2-PPV-4985048"})
        self.assertEqual(
            results,
            [
                {
                    "title": "FC2-PPV-4985048",
                    "url": "https://fc2ppv-db.com/en/videos/4985048",
                }
            ],
        )

    def test_search_path_queries_site(self):
        html = fixture("search-results.html")
        with mock.patch.object(fc2ppv_db, "fetch_html", return_value=html) as fetch:
            results = fc2ppv_db.scene_by_name({"name": "田中なな実"})
        fetch.assert_called_once_with(
            "https://fc2ppv-db.com/en/search?q="
            + urllib.parse.quote_plus("田中なな実")
        )
        self.assertIn(
            "https://fc2ppv-db.com/en/videos/4548515",
            [result["url"] for result in results],
        )

    def test_empty_name_returns_empty_list(self):
        self.assertEqual(fc2ppv_db.scene_by_name({"name": "   "}), [])


class PerformerSearchTests(unittest.TestCase):
    """performer-by-name reads the actress search results page."""

    def test_parse_fixture(self):
        results = fc2ppv_db.parse_performer_search_results(
            fixture("actress-search.html")
        )
        urls = [result["url"] for result in results]
        self.assertGreaterEqual(len(results), 20)
        self.assertEqual(len(urls), len(set(urls)))
        first = results[0]
        self.assertEqual(first["name"], "えりか")
        self.assertEqual(
            first["url"],
            "https://fc2ppv-db.com/en/actresses/6ea8c948-da15-4f16-b445-43fb05b3cc69",
        )
        self.assertTrue(
            first["image"].endswith(
                "/faces/actress_6ea8c948-da15-4f16-b445-43fb05b3cc69.jpg"
            )
        )
        self.assertEqual(first["images"], [first["image"]])
        without_image = next(
            result
            for result in results
            if "63bc576f-fda5-11f0-ad20-aad14526765e" in result["url"]
        )
        self.assertNotIn("image", without_image)

    def test_search_path_queries_site(self):
        html = fixture("actress-search.html")
        with mock.patch.object(fc2ppv_db, "fetch_html", return_value=html) as fetch:
            results = fc2ppv_db.performer_by_name({"name": "えりか"})
        fetch.assert_called_once_with(
            "https://fc2ppv-db.com/en/actresses?view=all&q="
            + urllib.parse.quote_plus("えりか")
            + "&page=1"
        )
        self.assertTrue(results)

    def test_empty_name_returns_empty_list(self):
        guard = mock.patch.object(
            fc2ppv_db,
            "fetch_html",
            side_effect=AssertionError("fetch_html must not be called"),
        )
        with guard:
            self.assertEqual(fc2ppv_db.performer_by_name({"name": "  "}), [])


class PerformerTests(unittest.TestCase):
    def _fetch_performer(self, fixture_name, url):
        html = fixture(fixture_name)
        with mock.patch.object(fc2ppv_db, "fetch_html", return_value=html) as fetch:
            performer = fc2ppv_db.performer_by_url({"url": url})
        fetch.assert_called_once_with(url)
        return performer

    def test_actress_with_image(self):
        url = "https://fc2ppv-db.com/en/actresses/e23e5998-dd03-4fac-8f8b-1990e458bc40"
        performer = self._fetch_performer("actress-with-image.html", url)
        self.assertEqual(performer["name"], "田中なな実")
        self.assertEqual(performer["url"], url)
        self.assertEqual(performer["urls"], [url])
        self.assertIn(
            "actress_e23e5998-dd03-4fac-8f8b-1990e458bc40.jpg", performer["image"]
        )
        self.assertEqual(performer["images"], [performer["image"]])
        for absent in ("aliases", "details", "birthdate"):
            self.assertNotIn(absent, performer)

    def test_actress_without_image_omits_image_keys(self):
        url = "https://fc2ppv-db.com/en/actresses/f7f49e91-d756-4d7e-9a00-b85ca9cc112e"
        performer = self._fetch_performer("actress-no-image.html", url)
        self.assertEqual(performer["name"], "なの(18)")
        self.assertNotIn("image", performer)
        self.assertNotIn("images", performer)

    def test_second_actress_with_image(self):
        url = "https://fc2ppv-db.com/en/actresses/76c3bcb7-34bb-4bc9-8df7-c89b3570337e"
        performer = self._fetch_performer("actress-example2.html", url)
        self.assertEqual(performer["name"], "姫崎あむ")
        self.assertIn(
            "actress_76c3bcb7-34bb-4bc9-8df7-c89b3570337e.jpg", performer["image"]
        )

    def test_foreign_actress_url_raises(self):
        with self.assertRaises(fc2ppv_db.ScraperError):
            fc2ppv_db.performer_by_url({"url": "https://example.com/en/actresses/e23e5998-dd03-4fac-8f8b-1990e458bc40"})

    def test_fragment_with_url_delegates(self):
        url = "https://fc2ppv-db.com/en/actresses/e23e5998-dd03-4fac-8f8b-1990e458bc40"
        with mock.patch.object(
            fc2ppv_db, "performer_by_url", return_value={"name": "x"}
        ) as delegate:
            result = fc2ppv_db.performer_by_fragment({"url": url})
        delegate.assert_called_once_with({"url": url})
        self.assertEqual(result, {"name": "x"})

    def test_fragment_with_urls_list_delegates(self):
        url = "https://fc2ppv-db.com/en/actresses/e23e5998-dd03-4fac-8f8b-1990e458bc40"
        with mock.patch.object(
            fc2ppv_db, "performer_by_url", return_value={"name": "x"}
        ) as delegate:
            result = fc2ppv_db.performer_by_fragment({"urls": [url]})
        delegate.assert_called_once_with({"url": url})
        self.assertEqual(result, {"name": "x"})

    def test_fragment_without_url_raises(self):
        with self.assertRaises(fc2ppv_db.ScraperError) as ctx:
            fc2ppv_db.performer_by_fragment({"name": "なの(18)"})
        self.assertIn("url", str(ctx.exception).lower())
        self.assertIn("name search", str(ctx.exception))


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
        result = self._run(["scene-by-name"], '{"name": "FC2-PPV-4985048"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsInstance(payload, list)
        self.assertEqual(
            payload[0]["url"], "https://fc2ppv-db.com/en/videos/4985048"
        )

    def test_japanese_output_is_unescaped_utf8(self):
        result = self._run(
            ["scene-by-name"], '{"name": "FC2-PPV-4985048 なの(18)"}'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("なの(18)", result.stdout)
        self.assertNotIn("\\u306a", result.stdout)
        json.loads(result.stdout)

    def test_unknown_operation_exits_2(self):
        result = self._run(["bogus-operation"], "")
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr.strip())

    def test_performer_fragment_without_url_exits_1(self):
        result = self._run(["performer-by-fragment"], '{"name": "なの(18)"}')
        self.assertEqual(result.returncode, 1)
        self.assertIn("url", result.stderr.lower())
        self.assertNotIn("Traceback", result.stderr)


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
        self.assertEqual(self.lines[0], "name: FC2PPV-DB")

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
            self.assertRegex(
                self.text, r"(?m)^" + key + r":\n  - action: script\n"
            )

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
        pairs = {
            "scene-by-url": "scene-by-url",
            "scene-by-fragment": "scene-by-fragment",
            "scene-by-query-fragment": "scene-by-query-fragment",
            "scene-by-name": "scene-by-name",
            "performer-by-url": "performer-by-url",
            "performer-by-fragment": "performer-by-fragment",
            "performer-by-name": "performer-by-name",
        }
        for operation in pairs.values():
            self.assertRegex(
                self.text,
                r"(?m)^ {4,6}- fc2ppv-db\.py\n {4,6}- " + operation + r"$",
            )

    def test_url_filters(self):
        self.assertRegex(
            self.text,
            r"(?m)^sceneByURL:\n  - action: script\n    url:\n      - fc2ppv-db\.com\n",
        )
        for locale in ("en", "ja", "zh"):
            self.assertIn(
                "      - fc2ppv-db.com/{0}/actresses/".format(locale), self.text
            )

    def test_every_url_key_is_a_list(self):
        """Stash unmarshals `url` into []string — a scalar breaks scraper load."""
        url_indices = [
            index
            for index, line in enumerate(self.lines)
            if line.strip().startswith("url:")
        ]
        self.assertGreaterEqual(len(url_indices), 4)
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
        self.assertEqual(data["name"], "FC2PPV-DB")
        self.assertIsInstance(data["sceneByURL"], list)
        self.assertIsInstance(data["performerByURL"], list)
        self.assertEqual(data["sceneByURL"][0]["action"], "script")
        self.assertEqual(data["sceneByURL"][0]["script"][-1], "scene-by-url")
        for entry in data["sceneByURL"] + data["performerByURL"]:
            self.assertIsInstance(entry["url"], list)
            self.assertTrue(all(isinstance(u, str) for u in entry["url"]))
        for entry in data["performerByURL"]:
            self.assertEqual(entry["action"], "script")
            self.assertEqual(entry["script"][-1], "performer-by-url")
        for key in (
            "sceneByFragment",
            "sceneByQueryFragment",
            "sceneByName",
            "performerByFragment",
        ):
            self.assertIsInstance(data[key], dict)
            self.assertEqual(data[key]["action"], "script")


class RepoShapeTests(unittest.TestCase):
    """The repository must look like a valid Stash scraper source index."""

    def test_scraper_files_live_in_scrapers_dir(self):
        self.assertTrue((SCRAPER_DIR / "fc2ppv-db.py").is_file())
        self.assertTrue((SCRAPER_DIR / "fc2ppv-db.yml").is_file())

    def test_no_scraper_files_at_repo_root(self):
        self.assertFalse((REPO_ROOT / "fc2ppv-db.py").exists())
        self.assertFalse((REPO_ROOT / "fc2ppv-db.yml").exists())

    def test_build_site_template(self):
        build_site = (REPO_ROOT / "build_site.sh").read_text(encoding="utf-8")
        self.assertIn("index.yml", build_site)

    def test_build_site_has_lf_line_endings(self):
        data = (REPO_ROOT / "build_site.sh").read_bytes()
        self.assertNotIn(b"\r\n", data)

    def test_deploy_workflow_template(self):
        deploy = (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("build_site.sh", deploy)

    def test_licence_present(self):
        licence = (REPO_ROOT / "LICENCE").read_text(encoding="utf-8")
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", licence)

    def test_gitattributes_pins_sh_to_lf(self):
        attrs = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.sh text eol=lf", attrs)

    def test_readme_documents_install(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("index.yml", readme)
        self.assertIn("FLARESOLVERR_URL", readme)

    def test_tests_are_not_packaged(self):
        self.assertTrue((REPO_ROOT / "tests").is_dir())
        packaged = list((REPO_ROOT / "scrapers").glob("**/tests"))
        self.assertEqual(packaged, [])


if __name__ == "__main__":
    unittest.main()
