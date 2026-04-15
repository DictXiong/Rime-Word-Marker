import json
from unittest import TestCase, mock

from app.ai import (
    AIAnnotationWorker,
    AIConfig,
    AIResponseTruncatedError,
    AIResponseValidationError,
    MAX_AI_MAX_TOKENS,
    OpenAICompatClient,
)


class OpenAICompatClientTestCase(TestCase):
    def test_annotate_batch_repairs_bad_model_output(self) -> None:
        client = OpenAICompatClient(
            AIConfig(endpoint="http://example.test/v1", model="demo-model")
        )
        items = [{"id": 1, "phrase": "程序员"}, {"id": 2, "phrase": "喝"}]

        with mock.patch.object(
            client,
            "_request_completion",
            side_effect=[
                '{"items":[{"id":1,"score":0.9}]}',
                '{"items":[{"id":1,"score":0.9},{"id":2,"score":0.1}]}',
            ],
        ) as request_completion, mock.patch("builtins.print") as print_mock:
            predictions = client.annotate_batch([], items)

        self.assertEqual(
            predictions,
            [{"id": 1, "score": 0.9}, {"id": 2, "score": 0.1}],
        )
        self.assertEqual(request_completion.call_count, 2)
        self.assertIn("invalid response", print_mock.call_args.args[0])

    def test_annotate_batch_splits_when_repair_fails(self) -> None:
        client = OpenAICompatClient(
            AIConfig(endpoint="http://example.test/v1", model="demo-model")
        )
        items = [{"id": 1, "phrase": "程序员"}, {"id": 2, "phrase": "喝"}]

        with mock.patch.object(
            client,
            "_request_completion",
            side_effect=[
                '{"items":[{"id":1,"score":0.9}]}',
                '{"items":[{"id":1,"score":0.9}]}',
                '{"items":[{"id":1,"score":0.9}]}',
                '{"items":[{"id":2,"score":0.1}]}',
            ],
        ), mock.patch("builtins.print") as print_mock:
            predictions = client.annotate_batch([], items)

        self.assertEqual(
            predictions,
            [{"id": 1, "score": 0.9}, {"id": 2, "score": 0.1}],
        )
        printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("splitting batch", printed)

    def test_annotate_batch_increases_max_tokens_after_truncation(self) -> None:
        client = OpenAICompatClient(
            AIConfig(endpoint="http://example.test/v1", model="demo-model", max_tokens=4096)
        )
        seen_max_tokens = []

        def fake_send(http_request):
            seen_max_tokens.append(json.loads(http_request.data.decode("utf-8"))["max_tokens"])
            if len(seen_max_tokens) == 1:
                return {
                    "choices": [
                        {
                            "message": {"content": '{"items":[{"id":1,"score"'},
                            "finish_reason": "length",
                        }
                    ]
                }
            return {
                "choices": [
                    {"message": {"content": '{"items":[{"id":1,"score":0.9}]}'}}
                ]
            }

        with mock.patch.object(
            client,
            "_send_request_with_retries",
            side_effect=fake_send,
        ), mock.patch("builtins.print"):
            predictions = client.annotate_batch([], [{"id": 1, "phrase": "程序员"}])

        self.assertEqual(predictions, [{"id": 1, "score": 0.9}])
        self.assertEqual(seen_max_tokens, [4096, 8192])

    def test_annotate_batch_splits_when_truncated_at_max_tokens(self) -> None:
        client = OpenAICompatClient(
            AIConfig(
                endpoint="http://example.test/v1",
                model="demo-model",
                max_tokens=MAX_AI_MAX_TOKENS,
            )
        )
        items = [{"id": 1, "phrase": "程序员"}, {"id": 2, "phrase": "喝"}]

        with mock.patch.object(
            client,
            "_request_completion",
            side_effect=[
                AIResponseTruncatedError("truncated"),
                '{"items":[{"id":1,"score":0.9}]}',
                '{"items":[{"id":2,"score":0.1}]}',
            ],
        ) as request_completion, mock.patch("builtins.print") as print_mock:
            predictions = client.annotate_batch([], items)

        self.assertEqual(
            predictions,
            [{"id": 1, "score": 0.9}, {"id": 2, "score": 0.1}],
        )
        self.assertEqual(request_completion.call_count, 3)
        printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("splitting batch after max_tokens truncation", printed)

    def test_annotate_batch_raises_for_unrecoverable_single_item_output(self) -> None:
        client = OpenAICompatClient(
            AIConfig(endpoint="http://example.test/v1", model="demo-model")
        )

        with mock.patch.object(
            client,
            "_request_completion",
            side_effect=['{"items":[]}', '{"items":[]}'],
        ), mock.patch("builtins.print"):
            with self.assertRaises(AIResponseValidationError):
                client.annotate_batch([], [{"id": 1, "phrase": "程序员"}])

    def test_annotate_batch_rejects_duplicate_ids(self) -> None:
        client = OpenAICompatClient(
            AIConfig(endpoint="http://example.test/v1", model="demo-model")
        )
        duplicate_output = (
            '{"items":[{"id":1,"score":0.9},{"id":1,"score":0.8},{"id":2,"score":0.1}]}'
        )

        with mock.patch.object(
            client,
            "_request_completion",
            side_effect=[duplicate_output, duplicate_output, duplicate_output, duplicate_output],
        ), mock.patch("builtins.print"):
            with self.assertRaisesRegex(AIResponseValidationError, "重复"):
                client.annotate_batch(
                    [],
                    [{"id": 1, "phrase": "程序员"}, {"id": 2, "phrase": "喝"}],
                )

    def test_verbose_request_logging_excludes_api_key(self) -> None:
        client = OpenAICompatClient(
            AIConfig(
                endpoint="http://example.test/v1",
                api_key="sk-secret",
                model="demo-model",
                verbose=True,
            )
        )

        with mock.patch.object(
            client,
            "_send_request_with_retries",
            return_value={
                "choices": [
                    {"message": {"content": '{"items":[{"id":1,"score":0.9}]}'}}
                ]
            },
        ), mock.patch("builtins.print") as print_mock:
            predictions = client.annotate_batch([], [{"id": 1, "phrase": "程序员"}])

        self.assertEqual(predictions, [{"id": 1, "score": 0.9}])
        printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("AI request", printed)
        self.assertIn("AI response", printed)
        self.assertIn("程序员", printed)
        self.assertNotIn("sk-secret", printed)

    def test_request_completion_reports_max_token_truncation(self) -> None:
        client = OpenAICompatClient(
            AIConfig(endpoint="http://example.test/v1", model="demo-model")
        )

        with mock.patch.object(
            client,
            "_send_request_with_retries",
            return_value={
                "choices": [
                    {
                        "message": {"content": '{"items":[{"id":1,"score"'},
                        "finish_reason": "length",
                    }
                ]
            },
        ), mock.patch("builtins.print") as print_mock:
            with self.assertRaisesRegex(RuntimeError, "max_tokens 截断"):
                client._request_completion([{"role": "user", "content": "demo"}])
        self.assertIn("max_tokens 截断", print_mock.call_args.args[0])


class AIAnnotationWorkerTestCase(TestCase):
    def test_run_once_prints_batch_summary(self) -> None:
        service = mock.Mock()
        service.get_ai_overview.return_value = {
            "enabled": True,
            "worker_status": "idle",
            "training": {"sufficient": True},
        }
        service.get_ai_batch_candidates.return_value = {
            "items": [{"id": 1, "phrase": "程序员"}, {"id": 2, "phrase": "喝"}],
            "next_scan_id": 2,
        }
        service.sample_ai_training_examples.return_value = [{"phrase": "示例", "label": "accepted"}]
        service.apply_ai_annotations.return_value = 2
        worker = AIAnnotationWorker(
            service,
            AIConfig(endpoint="http://example.test/v1", model="demo-model", candidate_mode="random"),
        )

        with mock.patch.object(
            worker.client,
            "annotate_batch",
            return_value=[{"id": 1, "score": 0.9}, {"id": 2, "score": 0.1}],
        ), mock.patch("builtins.print") as print_mock:
            result = worker._run_once()

        self.assertTrue(result)
        service.get_ai_batch_candidates.assert_called_once_with(
            limit=24,
            prompt_version="4.0",
            selection_mode="random",
        )
        print_mock.assert_called_once()
        printed_message = print_mock.call_args.args[0]
        self.assertIn("input=2", printed_message)
        self.assertIn("accepted=1", printed_message)
        self.assertIn("pending=0", printed_message)
        self.assertIn("rejected=1", printed_message)

    def test_run_once_retries_extreme_all_accepted_batch_once(self) -> None:
        service = mock.Mock()
        service.get_ai_overview.return_value = {
            "enabled": True,
            "worker_status": "idle",
            "training": {"sufficient": True},
        }
        service.get_ai_batch_candidates.return_value = {
            "items": [{"id": 1, "phrase": "程序员"}, {"id": 2, "phrase": "喝"}],
            "next_scan_id": 2,
        }
        service.sample_ai_training_examples.return_value = [{"phrase": "示例", "label": "accepted"}]
        service.apply_ai_annotations.return_value = 2
        worker = AIAnnotationWorker(
            service,
            AIConfig(
                endpoint="http://example.test/v1",
                model="demo-model",
                retry_extreme_batches=True,
            ),
        )

        with mock.patch.object(
            worker.client,
            "annotate_batch",
            side_effect=[
                [{"id": 1, "score": 0.9}, {"id": 2, "score": 0.91}],
                [{"id": 1, "score": 0.9}, {"id": 2, "score": 0.1}],
            ],
        ) as annotate_batch, mock.patch("builtins.print"):
            result = worker._run_once()

        self.assertTrue(result)
        self.assertEqual(annotate_batch.call_count, 2)
        service.apply_ai_annotations.assert_called_once_with(
            service.get_ai_batch_candidates.return_value["items"],
            [{"id": 1, "score": 0.9}, {"id": 2, "score": 0.1}],
            model_name="demo-model",
            prompt_version="4.0",
            next_scan_id=2,
        )

    def test_run_once_does_not_retry_extreme_batch_by_default(self) -> None:
        service = mock.Mock()
        service.get_ai_overview.return_value = {
            "enabled": True,
            "worker_status": "idle",
            "training": {"sufficient": True},
        }
        service.get_ai_batch_candidates.return_value = {
            "items": [{"id": 1, "phrase": "程序员"}, {"id": 2, "phrase": "词库"}],
            "next_scan_id": 2,
        }
        service.sample_ai_training_examples.return_value = [{"phrase": "示例", "label": "accepted"}]
        service.apply_ai_annotations.return_value = 2
        worker = AIAnnotationWorker(
            service,
            AIConfig(endpoint="http://example.test/v1", model="demo-model"),
        )
        predictions = [{"id": 1, "score": 0.9}, {"id": 2, "score": 0.91}]

        with mock.patch.object(
            worker.client,
            "annotate_batch",
            return_value=predictions,
        ) as annotate_batch, mock.patch("builtins.print"):
            result = worker._run_once()

        self.assertTrue(result)
        self.assertEqual(annotate_batch.call_count, 1)
        service.apply_ai_annotations.assert_called_once_with(
            service.get_ai_batch_candidates.return_value["items"],
            predictions,
            model_name="demo-model",
            prompt_version="4.0",
            next_scan_id=2,
        )

    def test_worker_loop_prints_background_errors(self) -> None:
        service = mock.Mock()
        worker = AIAnnotationWorker(
            service,
            AIConfig(endpoint="http://example.test/v1", model="demo-model"),
        )

        with mock.patch.object(
            worker,
            "_run_once",
            side_effect=[RuntimeError("boom"), KeyboardInterrupt()],
        ), mock.patch.object(
            worker._wake_event,
            "wait",
            return_value=None,
        ), mock.patch("builtins.print") as print_mock:
            with self.assertRaises(KeyboardInterrupt):
                worker._run()

        printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("[AI] worker error RuntimeError: boom", printed)
        service.update_ai_runtime_state.assert_called_once_with("error", last_error="boom")
