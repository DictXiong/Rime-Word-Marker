import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase
from urllib.parse import urlparse

from main import (
    ThreadingHTTPServerV6,
    _access_token_matches,
    _format_bind_url,
    _host_allowed,
    _is_public_path,
    _load_access_token,
    _load_ai_config,
    _load_allowed_hosts,
    _load_hosts,
    _load_max_request_body_bytes,
    _normalize_host_header,
    _normalize_hosts,
    _query_access_token,
    _server_address,
    _server_class,
    _strip_access_token_query,
)


class ConfigTestCase(TestCase):
    def test_normalize_hosts_accepts_arrays_commas_and_ipv6_brackets(self) -> None:
        self.assertEqual(
            _normalize_hosts(["127.0.0.1, 0.0.0.0", "[::1]", "127.0.0.1"]),
            ["127.0.0.1", "0.0.0.0", "::1"],
        )

    def test_load_hosts_uses_config_and_cli_override(self) -> None:
        self.assertEqual(_load_hosts({"host": ["127.0.0.1", "::1"]}, None), ["127.0.0.1", "::1"])
        self.assertEqual(
            _load_hosts({"host": "127.0.0.1"}, ["0.0.0.0", "::"]),
            ["0.0.0.0", "::"],
        )
        self.assertEqual(
            _load_hosts({"hosts": "127.0.0.1,::1", "host": "0.0.0.0"}, None),
            ["127.0.0.1", "::1"],
        )

    def test_allowed_hosts_normalize_host_headers(self) -> None:
        allowed_hosts = _load_allowed_hosts({"allowed_hosts": ["example.test", "[::1]"]})

        self.assertEqual(allowed_hosts, ["example.test", "::1"])
        self.assertEqual(_normalize_host_header("Example.Test:443"), "example.test")
        self.assertEqual(_normalize_host_header("[::1]:8000"), "::1")
        self.assertTrue(_host_allowed("example.test", allowed_hosts))
        self.assertFalse(_host_allowed("evil.test", allowed_hosts))
        self.assertTrue(_host_allowed("anything.test", ["*"]))

    def test_max_request_body_limit_can_be_configured(self) -> None:
        self.assertEqual(_load_max_request_body_bytes({}), 512 * 1024 * 1024)
        self.assertEqual(_load_max_request_body_bytes({"max_request_body_mb": 32}), 32 * 1024 * 1024)
        self.assertEqual(_load_max_request_body_bytes({"max_request_body_bytes": 2_000_000}), 2_000_000)

    def test_access_token_helpers(self) -> None:
        self.assertEqual(_load_access_token({}), "")
        self.assertEqual(_load_access_token({"access_token": "  secret-token  "}), "secret-token")
        self.assertTrue(_access_token_matches("secret-token", "secret-token"))
        self.assertFalse(_access_token_matches("wrong", "secret-token"))
        self.assertEqual(_query_access_token("page=2&token=secret-token"), "secret-token")
        self.assertEqual(_query_access_token("access_token=secret-token"), "secret-token")

    def test_access_token_file_takes_precedence_and_strips_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            token_path = Path(tempdir) / "access-token"
            token_path.write_text("  file-token\n", encoding="utf-8")

            self.assertEqual(
                _load_access_token(
                    {"access_token": "inline-token", "access_token_file": "access-token"},
                    Path(tempdir),
                ),
                "file-token",
            )

    def test_access_token_file_must_be_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with self.assertRaises(ValueError):
                _load_access_token({"access_token_file": "missing-token"}, Path(tempdir))

    def test_access_token_file_must_not_be_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            token_path = Path(tempdir) / "access-token"
            token_path.write_text("\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                _load_access_token({"access_token_file": "access-token"}, Path(tempdir))

    def test_strip_access_token_query_preserves_other_params(self) -> None:
        parsed = urlparse("/manage?page=2&token=secret&status=accepted")

        self.assertEqual(_strip_access_token_query(parsed), "/manage?page=2&status=accepted")

    def test_public_paths_are_limited(self) -> None:
        self.assertTrue(_is_public_path("/"))
        self.assertTrue(_is_public_path("/index.html"))
        self.assertTrue(_is_public_path("/js/app.js"))
        self.assertTrue(_is_public_path("/api/export"))
        self.assertTrue(_is_public_path("/api/stats"))
        self.assertFalse(_is_public_path("/api/export/count"))
        self.assertFalse(_is_public_path("/manage"))
        self.assertFalse(_is_public_path("/js/../manage.html"))
        self.assertFalse(_is_public_path("/js/%2e%2e/manage.html"))
        self.assertFalse(_is_public_path("/api/entries"))

    def test_bind_helpers_support_ipv4_and_ipv6(self) -> None:
        self.assertEqual(_server_class("127.0.0.1"), ThreadingHTTPServer)
        self.assertEqual(_server_address("127.0.0.1", 8000), ("127.0.0.1", 8000))
        self.assertEqual(_format_bind_url("127.0.0.1", 8000), "http://127.0.0.1:8000")

        self.assertEqual(_server_class("::1"), ThreadingHTTPServerV6)
        self.assertEqual(_server_address("::1", 8000), ("::1", 8000, 0, 0))
        self.assertEqual(_format_bind_url("::1", 8000), "http://[::1]:8000")

    def test_ai_max_tokens_adapts_to_batch_size_when_unset(self) -> None:
        config = {
            "ai": {
                "endpoint": "http://example.test/v1",
                "model": "demo-model",
                "batch_size": 128,
            }
        }

        ai_config = _load_ai_config(config)

        self.assertEqual(ai_config.batch_size, 128)
        self.assertEqual(ai_config.max_tokens, 24576)

    def test_ai_max_tokens_respects_explicit_value(self) -> None:
        config = {
            "ai": {
                "endpoint": "http://example.test/v1",
                "model": "demo-model",
                "batch_size": 128,
                "max_tokens": 8192,
            }
        }

        ai_config = _load_ai_config(config)

        self.assertEqual(ai_config.batch_size, 128)
        self.assertEqual(ai_config.max_tokens, 8192)

    def test_ai_verbose_is_only_controlled_by_top_level_verbose(self) -> None:
        config = {
            "ai": {
                "endpoint": "http://example.test/v1",
                "model": "demo-model",
                "verbose": True,
            }
        }

        disabled = _load_ai_config(config, verbose=False)
        enabled = _load_ai_config(config, verbose=True)

        self.assertFalse(disabled.verbose)
        self.assertTrue(enabled.verbose)

    def test_ai_retry_extreme_batches_defaults_to_disabled(self) -> None:
        ai_config = _load_ai_config(
            {
                "ai": {
                    "endpoint": "http://example.test/v1",
                    "model": "demo-model",
                }
            }
        )

        self.assertFalse(ai_config.retry_extreme_batches)

    def test_ai_retry_extreme_batches_can_be_enabled(self) -> None:
        ai_config = _load_ai_config(
            {
                "ai": {
                    "endpoint": "http://example.test/v1",
                    "model": "demo-model",
                    "retry_extreme_batches": True,
                }
            }
        )

        self.assertTrue(ai_config.retry_extreme_batches)
