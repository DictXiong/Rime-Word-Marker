from __future__ import annotations

import tempfile
import threading
import urllib.error
import urllib.request
from functools import partial
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import SkipTest, TestCase

import main
from app.service import WordService


class AccessTokenAuthTestCase(TestCase):
    def setUp(self) -> None:
        self._previous_service = main.SERVICE
        self._previous_access_token = main.ACCESS_TOKEN
        self._previous_allowed_hosts = main.ALLOWED_HOSTS
        self._previous_max_request_body_bytes = main.MAX_REQUEST_BODY_BYTES
        self._tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tempdir.name) / "words.db"
        main.SERVICE = WordService(db_path)
        main.ACCESS_TOKEN = "secret-token"
        main.ALLOWED_HOSTS = []
        main.MAX_REQUEST_BODY_BYTES = main.DEFAULT_MAX_REQUEST_BODY_BYTES

        handler = partial(main.AppHandler, directory=str(main.STATIC_DIR))
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except PermissionError as exc:
            self._restore_globals()
            self._tempdir.cleanup()
            raise SkipTest("当前沙箱不允许创建本地测试 socket。") from exc
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self._restore_globals()
        self._tempdir.cleanup()

    def _restore_globals(self) -> None:
        main.SERVICE = self._previous_service
        main.ACCESS_TOKEN = self._previous_access_token
        main.ALLOWED_HOSTS = self._previous_allowed_hosts
        main.MAX_REQUEST_BODY_BYTES = self._previous_max_request_body_bytes

    def test_home_and_stats_are_public(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/") as response:
            self.assertEqual(response.status, 200)

        with urllib.request.urlopen(f"{self.base_url}/api/stats") as response:
            self.assertEqual(response.status, 200)

    def test_protected_api_requires_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.base_url}/api/entries")

        self.assertEqual(raised.exception.code, 401)

    def test_query_token_redirects_to_clean_url_and_sets_cookie(self) -> None:
        cookie_jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

        response = opener.open(f"{self.base_url}/manage?token=secret-token")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.url, f"{self.base_url}/manage")
        self.assertTrue(any(cookie.name == main.ACCESS_TOKEN_COOKIE_NAME for cookie in cookie_jar))

        with opener.open(f"{self.base_url}/api/entries") as api_response:
            self.assertEqual(api_response.status, 200)
