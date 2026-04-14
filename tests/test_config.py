from unittest import TestCase

from main import _load_ai_config


class ConfigTestCase(TestCase):
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
