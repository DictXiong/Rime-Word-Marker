from http.server import ThreadingHTTPServer
from unittest import TestCase

from main import (
    ThreadingHTTPServerV6,
    _format_bind_url,
    _load_ai_config,
    _load_hosts,
    _normalize_hosts,
    _server_address,
    _server_class,
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
