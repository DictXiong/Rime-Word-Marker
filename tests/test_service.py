from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from app.service import WordService


class WordServiceTestCase(TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.service = WordService(self.db_path)

    def test_import_deduplicates_and_applies_defaults(self) -> None:
        sample = "\n".join(
            [
                "---",
                "name: test",
                "...",
                "你好\tni hao\t20",
                "OpenAI助手\t\t",
                "你好\tni hao\t99",
            ]
        )

        with mock.patch("app.service.transliterate_phrase", return_value="openai zhu shou"):
            result = self.service.import_text(sample)

        self.assertEqual(result["parsed"], 3)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["skipped"], 1)

        page = self.service.list_entries(page=1, page_size=10)
        self.assertEqual(page["total"], 2)
        phrases = {item["phrase"]: item for item in page["items"]}
        self.assertEqual(phrases["你好"]["weight"], 20)
        self.assertEqual(phrases["OpenAI助手"]["pinyin"], "openai zhu shou")
        self.assertEqual(phrases["OpenAI助手"]["weight"], 1)
        self.assertEqual(result["accepted_existing"], 0)
        self.assertEqual(result["rejected_existing"], 0)

    def test_import_skips_everything_between_yaml_markers(self) -> None:
        sample = "\n".join(
            [
                "---",
                "name: test",
                "not_a_header_line",
                "foo\tbar\t99",
                "...",
                "真正词条\tzhēn zhèng cí tiáo\t3",
            ]
        )

        result = self.service.import_text(sample)

        self.assertEqual(result["parsed"], 1)
        page = self.service.list_entries(page=1, page_size=10)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["phrase"], "真正词条")

    def test_import_reports_existing_accepted_and_rejected_counts(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ", "bǐng"]):
            self.service.import_text("甲\n乙\n丙")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }
        self.service.update_status(entries["甲"]["id"], "accepted")
        self.service.update_status(entries["乙"]["id"], "rejected")

        with mock.patch("app.service.transliterate_phrase", return_value="dīng"):
            result = self.service.import_text("甲\n乙\n丁")

        self.assertEqual(result["parsed"], 3)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["accepted_existing"], 1)
        self.assertEqual(result["rejected_existing"], 1)

    def test_import_reports_existing_statuses_across_large_batches(self) -> None:
        raw_text = "\n".join(f"词{i}\tpīn\t1" for i in range(905))
        self.service.import_text(raw_text)

        with self.service._managed_connection() as connection:
            rows = connection.execute(
                "SELECT id, phrase FROM entries WHERE phrase IN (?, ?)",
                ("词0", "词904"),
            ).fetchall()
        entries = {row["phrase"]: row for row in rows}

        self.service.update_status(entries["词0"]["id"], "accepted")
        self.service.update_status(entries["词904"]["id"], "rejected")
        result = self.service.import_text(raw_text)

        self.assertEqual(result["parsed"], 905)
        self.assertEqual(result["skipped"], 905)
        self.assertEqual(result["accepted_existing"], 1)
        self.assertEqual(result["rejected_existing"], 1)

    def test_update_entry_can_edit_all_fields_and_regenerate_pinyin(self) -> None:
        with mock.patch("app.service.transliterate_phrase", return_value="cè shì"):
            self.service.import_text("测试")

        entry = self.service.get_next_pending()

        with mock.patch("app.service.transliterate_phrase", return_value="xīn cí"):
            updated = self.service.update_entry(
                entry["id"],
                {
                    "phrase": "新词",
                    "pinyin": "",
                    "weight": "7",
                    "status": "accepted",
                    "imported_at": "2026-04-12T12:30:00",
                    "labeled_at": "2026-04-12T13:45:00",
                },
            )

        self.assertEqual(updated["phrase"], "新词")
        self.assertEqual(updated["pinyin"], "xīn cí")
        self.assertEqual(updated["weight"], 7)
        self.assertEqual(updated["status"], "accepted")
        self.assertTrue(updated["imported_at"].startswith("2026-04-12T12:30:00"))
        self.assertTrue(updated["labeled_at"].startswith("2026-04-12T13:45:00"))

    def test_update_entry_rejects_duplicate_phrase(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["yī", "èr"]):
            self.service.import_text("一\n二")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }

        with self.assertRaisesRegex(ValueError, "重复词条"):
            self.service.update_entry(entries["二"]["id"], {"phrase": "一"})

    def test_batch_update_entries_can_change_multiple_fields(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ"]):
            self.service.import_text("甲\n乙")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }

        result = self.service.batch_update_entries(
            [entries["甲"]["id"], entries["乙"]["id"]],
            {
                "status": "rejected",
                "weight": "8",
                "imported_at": "2026-04-12T15:00:00",
            },
            clear_labeled_at=True,
        )

        self.assertEqual(result["updated_count"], 2)
        updated_entries = {item["phrase"]: item for item in result["entries"]}
        self.assertEqual(updated_entries["甲"]["status"], "rejected")
        self.assertEqual(updated_entries["乙"]["weight"], 8)
        self.assertTrue(updated_entries["甲"]["imported_at"].startswith("2026-04-12T15:00:00"))
        self.assertIsNone(updated_entries["乙"]["labeled_at"])

    def test_review_state_persists_across_service_instances(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ", "bǐng"]):
            self.service.import_text("甲\n乙\n丙")

        with mock.patch("app.service.random.randint", return_value=1):
            first = self.service.advance_review()
        self.assertEqual(first["current_entry"]["phrase"], "甲")

        self.service.update_status(first["current_entry"]["id"], "accepted")
        with mock.patch("app.service.random.randint", return_value=1):
            second = self.service.advance_review()
        self.assertEqual(second["current_entry"]["phrase"], "乙")

        another_service = WordService(self.db_path)
        restored = another_service.get_review_state()
        self.assertEqual(restored["current_entry"]["phrase"], "乙")
        self.assertTrue(restored["can_go_back"])

        previous = another_service.move_review_back()
        self.assertEqual(previous["current_entry"]["phrase"], "甲")

    def test_review_sessions_are_isolated(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ"]):
            self.service.import_text("甲\n乙")

        with mock.patch("app.service.random.randint", return_value=1):
            first_a = self.service.advance_review(session_key="session-a")
        self.assertEqual(first_a["current_entry"]["phrase"], "甲")

        self.service.update_status(first_a["current_entry"]["id"], "accepted")
        with mock.patch("app.service.random.randint", return_value=1):
            second_a = self.service.advance_review(session_key="session-a")
        self.assertEqual(second_a["current_entry"]["phrase"], "乙")

        with mock.patch("app.service.random.randint", return_value=1):
            first_b = self.service.advance_review(session_key="session-b")
        self.assertEqual(first_b["current_entry"]["phrase"], "乙")
        self.assertFalse(first_b["can_go_back"])

    def test_review_state_can_be_exhausted_and_still_go_back(self) -> None:
        with mock.patch("app.service.transliterate_phrase", return_value="dān"):
            self.service.import_text("单")

        with mock.patch("app.service.random.randint", return_value=1):
            first = self.service.advance_review()
        self.assertEqual(first["current_entry"]["phrase"], "单")
        self.service.update_status(first["current_entry"]["id"], "accepted")

        with mock.patch("app.service.random.randint", return_value=1):
            exhausted = self.service.advance_review()
        self.assertIsNone(exhausted["current_entry"])
        self.assertTrue(exhausted["can_go_back"])

        restored = self.service.move_review_back()
        self.assertEqual(restored["current_entry"]["phrase"], "单")

    def test_label_and_advance_updates_status_and_returns_next_entry(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ"]):
            self.service.import_text("甲\n乙")

        with mock.patch("app.service.random.randint", return_value=1):
            first = self.service.advance_review()
        with mock.patch("app.service.random.randint", return_value=1):
            labeled = self.service.label_and_advance(first["current_entry"]["id"], "accepted")

        self.assertEqual(labeled["updated_entry"]["status"], "accepted")
        self.assertEqual(labeled["current_entry"]["phrase"], "乙")
        self.assertTrue(labeled["can_go_back"])

    def test_preview_review_entries_returns_upcoming_items(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ", "bǐng"]):
            self.service.import_text("甲\n乙\n丙")

        with mock.patch("app.service.random.randint", return_value=1):
            first = self.service.advance_review()
        self.assertEqual(first["current_entry"]["phrase"], "甲")

        with mock.patch("app.service.random.randint", return_value=1):
            preview = self.service.preview_review_entries(count=2)
        self.assertEqual([item["phrase"] for item in preview["entries"]], ["乙", "丙"])

    def test_review_mode_is_persisted_and_random_mode_advances(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ"]):
            self.service.import_text("甲\n乙")

        switched = self.service.set_review_mode("random")
        self.assertEqual(switched["mode"], "random")

        with mock.patch("app.service.random.randint", return_value=2):
            next_state = self.service.advance_review()

        self.assertEqual(next_state["mode"], "random")
        self.assertEqual(next_state["current_entry"]["phrase"], "乙")

        another_service = WordService(self.db_path)
        restored = another_service.get_review_state()
        self.assertEqual(restored["mode"], "random")

    def test_review_can_prefer_ai_labeled_pending_entries(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ", "bǐng"]):
            self.service.import_text("甲\n乙\n丙")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }
        self.service.apply_ai_annotations(
            [entries["乙"]],
            [{"id": entries["乙"]["id"], "score": 0.91}],
            model_name="demo-model",
            prompt_version="demo-v1",
            next_scan_id=entries["乙"]["id"],
        )

        with mock.patch("app.service.random.randint", return_value=1):
            preferred = self.service.advance_review(prefer_ai=True)

        self.assertEqual(preferred["current_entry"]["phrase"], "乙")

    def test_review_without_ai_preference_prioritizes_unlabeled_entries(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ", "bǐng"]):
            self.service.import_text("甲\n乙\n丙")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }
        self.service.apply_ai_annotations(
            [entries["乙"]],
            [{"id": entries["乙"]["id"], "score": 0.91}],
            model_name="demo-model",
            prompt_version="demo-v1",
            next_scan_id=entries["乙"]["id"],
        )

        with mock.patch("app.service.random.randint", return_value=entries["乙"]["id"]):
            preferred = self.service.advance_review(prefer_ai=False)

        self.assertEqual(preferred["current_entry"]["phrase"], "丙")

    def test_review_ai_preference_prioritizes_ai_pending_before_ai_decisions(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ", "bǐng"]):
            self.service.import_text("甲\n乙\n丙")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }
        ordered_entries = [entries["甲"], entries["乙"], entries["丙"]]
        self.service.apply_ai_annotations(
            ordered_entries,
            [
                {"id": entries["甲"]["id"], "score": 0.91},
                {"id": entries["乙"]["id"], "score": 0.5},
                {"id": entries["丙"]["id"], "score": 0.1},
            ],
            model_name="demo-model",
            prompt_version="demo-v1",
            next_scan_id=entries["丙"]["id"],
        )

        with mock.patch("app.service.random.randint", return_value=entries["甲"]["id"]):
            preferred = self.service.advance_review(prefer_ai=True)

        self.assertEqual(preferred["current_entry"]["phrase"], "乙")

    def test_update_status_tracks_labeled_time(self) -> None:
        with mock.patch("app.service.transliterate_phrase", return_value="ce shi"):
            self.service.import_text("测试")

        entry = self.service.get_next_pending()
        updated = self.service.update_status(entry["id"], "accepted")
        self.assertEqual(updated["status"], "accepted")
        self.assertIsNotNone(updated["labeled_at"])

        reverted = self.service.update_status(entry["id"], "pending")
        self.assertEqual(reverted["status"], "pending")
        self.assertIsNotNone(reverted["labeled_at"])

    def test_export_dictionary_respects_status_and_weight(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["a", "b", "c"]):
            self.service.import_text("甲\n乙\n丙")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }
        self.service.update_status(entries["甲"]["id"], "accepted")
        self.service.update_status(entries["乙"]["id"], "rejected")

        exported = self.service.export_dictionary(
            statuses=["accepted", "rejected"],
            include_weight=True,
            dictionary_name="demo",
        )

        self.assertIn("name: demo", exported)
        self.assertIn("甲\ta\t1", exported)
        self.assertIn("乙\tb\t1", exported)
        self.assertNotIn("丙\tc\t1", exported)
        stats = self.service.get_stats()
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["rejected"], 1)

    def test_count_export_entries_matches_selected_statuses(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["a", "b", "c"]):
            self.service.import_text("甲\n乙\n丙")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }
        self.service.update_status(entries["甲"]["id"], "accepted")
        self.service.update_status(entries["乙"]["id"], "rejected")

        self.assertEqual(self.service.count_export_entries(["accepted"]), 1)
        self.assertEqual(self.service.count_export_entries(["accepted", "rejected"]), 2)

    def test_export_dictionary_sanitizes_dictionary_name(self) -> None:
        with mock.patch("app.service.transliterate_phrase", return_value="a"):
            self.service.import_text("甲")

        exported = self.service.export_dictionary(
            statuses=["pending"],
            dictionary_name='bad"\r\nname: injected',
        )

        self.assertIn("name: bad_name_injected\n", exported)
        self.assertNotIn("\r", exported)
        self.assertNotIn('bad"', exported)

    def test_export_can_include_ai_assist_for_pending_entries(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["a", "b", "c"]):
            self.service.import_text("甲\n乙\n丙")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }
        self.service.update_status(entries["甲"]["id"], "accepted")
        self.service.apply_ai_annotations(
            [entries["乙"], entries["丙"]],
            [
                {"id": entries["乙"]["id"], "score": 0.91},
                {"id": entries["丙"]["id"], "score": 0.18},
            ],
            model_name="demo-model",
            prompt_version="demo-v1",
            next_scan_id=entries["丙"]["id"],
        )

        self.assertEqual(self.service.count_export_entries(["accepted"]), 1)
        self.assertEqual(
            self.service.count_export_entries(["accepted"], include_ai_assist=True),
            2,
        )
        self.assertEqual(
            self.service.count_export_entries(["rejected"], include_ai_assist=True),
            1,
        )

        exported = self.service.export_dictionary(
            statuses=["accepted", "rejected"],
            include_weight=False,
            include_ai_assist=True,
            dictionary_name="ai-demo",
        )
        self.assertIn("甲\ta", exported)
        self.assertIn("乙\tb", exported)
        self.assertIn("丙\tc", exported)

    def test_list_entries_can_filter_by_ai_status(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["a", "b", "c"]):
            self.service.import_text("甲\n乙\n丙")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }
        self.service.apply_ai_annotations(
            [entries["甲"], entries["乙"]],
            [
                {"id": entries["甲"]["id"], "score": 0.91},
                {"id": entries["乙"]["id"], "score": 0.18},
            ],
            model_name="demo-model",
            prompt_version="demo-v1",
            next_scan_id=entries["乙"]["id"],
        )

        accepted_page = self.service.list_entries(page=1, page_size=10, ai_status="accepted")
        rejected_page = self.service.list_entries(page=1, page_size=10, ai_status="rejected")
        none_page = self.service.list_entries(page=1, page_size=10, ai_status="none")

        self.assertEqual([item["phrase"] for item in accepted_page["items"]], ["甲"])
        self.assertEqual([item["phrase"] for item in rejected_page["items"]], ["乙"])
        self.assertEqual([item["phrase"] for item in none_page["items"]], ["丙"])

    def test_update_entry_clears_ai_annotation_when_phrase_changes(self) -> None:
        with mock.patch("app.service.transliterate_phrase", return_value="cè shì"):
            self.service.import_text("测试")

        entry = self.service.get_next_pending()
        self.service.apply_ai_annotations(
            [entry],
            [{"id": entry["id"], "score": 0.94}],
            model_name="demo-model",
            prompt_version="demo-v1",
            next_scan_id=entry["id"],
        )

        with mock.patch("app.service.transliterate_phrase", return_value="xīn cí"):
            updated = self.service.update_entry(entry["id"], {"phrase": "新词"})

        self.assertIsNone(updated["ai_label"])
        self.assertIsNone(updated["ai_score"])
        self.assertIsNone(updated["ai_labeled_at"])
        self.assertIsNone(updated["ai_model"])
        self.assertIsNone(updated["ai_prompt_version"])

    def test_set_ai_enabled_requires_sufficient_human_labels(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ"]):
            self.service.import_text("甲\n乙")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }
        self.service.update_status(entries["甲"]["id"], "accepted")
        self.service.update_status(entries["乙"]["id"], "rejected")

        with mock.patch("app.service.AI_MIN_TRAINING_TOTAL", 10), mock.patch(
            "app.service.AI_MIN_CLASS_COUNT", 5
        ):
            overview, message = self.service.set_ai_enabled(True, configured=True)

        self.assertFalse(overview["enabled"])
        self.assertIn("人工标注数据量不足", message)

    def test_set_ai_enabled_succeeds_when_threshold_is_met(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ"]):
            self.service.import_text("甲\n乙")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }
        self.service.update_status(entries["甲"]["id"], "accepted")
        self.service.update_status(entries["乙"]["id"], "rejected")

        with mock.patch("app.service.AI_MIN_TRAINING_TOTAL", 2), mock.patch(
            "app.service.AI_MIN_CLASS_COUNT", 1
        ):
            overview, message = self.service.set_ai_enabled(
                True,
                configured=True,
                model_name="demo-model",
                prompt_version="demo-v1",
            )

        self.assertTrue(overview["enabled"])
        self.assertEqual(overview["model"], "demo-model")
        self.assertEqual(overview["prompt_version"], "demo-v1")
        self.assertEqual(message, "")

    def test_ai_training_examples_are_stable_and_prioritize_disagreements(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ", "bǐng", "dīng"]):
            self.service.import_text("甲\n乙\n丙\n丁")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }
        self.service.apply_ai_annotations(
            [entries["甲"], entries["乙"]],
            [
                {"id": entries["甲"]["id"], "score": 0.1},
                {"id": entries["乙"]["id"], "score": 0.9},
            ],
            model_name="demo-model",
            prompt_version="demo-v1",
            next_scan_id=entries["乙"]["id"],
        )
        self.service.update_status(entries["甲"]["id"], "accepted")
        self.service.update_status(entries["乙"]["id"], "rejected")
        self.service.update_status(entries["丙"]["id"], "accepted")
        self.service.update_status(entries["丁"]["id"], "rejected")

        first = self.service.sample_ai_training_examples(per_class=1)
        second = self.service.sample_ai_training_examples(per_class=1)

        self.assertEqual(first, second)
        hard_examples = {
            item["phrase"]: item
            for item in first
            if item.get("source") == "human_ai_disagreement"
        }
        self.assertEqual(hard_examples["甲"]["label"], "accepted")
        self.assertEqual(hard_examples["甲"]["previous_ai_label"], "rejected")
        self.assertEqual(hard_examples["乙"]["label"], "rejected")
        self.assertEqual(hard_examples["乙"]["previous_ai_label"], "accepted")

    def test_ai_training_examples_are_cached_and_invalidated_by_human_labels(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ", "bǐng"]):
            self.service.import_text("甲\n乙\n丙")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }
        self.service.update_status(entries["甲"]["id"], "accepted")
        self.service.update_status(entries["乙"]["id"], "rejected")

        with mock.patch.object(
            self.service,
            "_build_ai_training_examples",
            wraps=self.service._build_ai_training_examples,
        ) as build_examples:
            first = self.service.sample_ai_training_examples(per_class=10)
            first[0]["phrase"] = "污染缓存"
            second = self.service.sample_ai_training_examples(per_class=10)

            self.assertEqual(build_examples.call_count, 1)
            self.assertNotIn("污染缓存", {item["phrase"] for item in second})

            self.service.update_status(entries["丙"]["id"], "accepted")
            third = self.service.sample_ai_training_examples(per_class=10)

            self.assertEqual(build_examples.call_count, 2)
            self.assertIn("丙", {item["phrase"] for item in third})

    def test_ai_batch_reprocesses_pending_entries_from_old_prompt_version(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ"]):
            self.service.import_text("甲\n乙")

        entries = {
            item["phrase"]: item
            for item in self.service.list_entries(page=1, page_size=10)["items"]
        }
        self.service.apply_ai_annotations(
            [entries["甲"], entries["乙"]],
            [
                {"id": entries["甲"]["id"], "score": 0.91},
                {"id": entries["乙"]["id"], "score": 0.18},
            ],
            model_name="demo-model",
            prompt_version="old-rules",
            next_scan_id=entries["乙"]["id"],
        )

        old_batch = self.service.get_ai_batch_candidates(
            limit=10,
            prompt_version="old-rules",
        )
        new_batch = self.service.get_ai_batch_candidates(
            limit=10,
            prompt_version="new-rules",
        )
        overview = self.service.get_ai_overview(prompt_version="new-rules")

        self.assertEqual(old_batch["items"], [])
        self.assertEqual([item["phrase"] for item in new_batch["items"]], ["甲", "乙"])
        self.assertEqual(overview["queue"]["unlabeled"], 2)
        self.assertEqual(overview["queue"]["outdated"], 2)

        self.service.update_status(entries["甲"]["id"], "accepted")
        pending_only_batch = self.service.get_ai_batch_candidates(
            limit=10,
            prompt_version="new-rules",
        )

        self.assertEqual([item["phrase"] for item in pending_only_batch["items"]], ["乙"])

    def test_ai_batch_can_select_random_candidates(self) -> None:
        with mock.patch("app.service.transliterate_phrase", side_effect=["jiǎ", "yǐ", "bǐng"]):
            self.service.import_text("甲\n乙\n丙")

        with mock.patch("app.service.random.randint", return_value=2):
            batch = self.service.get_ai_batch_candidates(
                limit=2,
                prompt_version="rules-v1",
                selection_mode="random",
            )

        self.assertEqual([item["phrase"] for item in batch["items"]], ["乙", "丙"])
