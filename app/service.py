from __future__ import annotations

import hashlib
import json
import math
import random
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from app.ai import MAX_AI_BATCH_SIZE, score_to_label
from app.constants import ACCEPTED, DEFAULT_EXPORT_STATUSES, PENDING, REJECTED, VALID_STATUSES
from app.pinyin_utils import (
    export_code_for_mixed_phrase,
    normalize_mixed_export_scheme,
    phrase_has_ascii_letters,
    transliterate_phrase,
)

YAML_HEADER_PATTERN = re.compile(r"^[A-Za-z_][\w-]*\s*:")
USERDB_METADATA_PATTERN = re.compile(r"(?:^|\s)[cdt]=")
USERDB_WEIGHT_PATTERN = re.compile(r"(?:^|\s)c=(-?\d+)(?:\s|$)")
PINYIN_TONE_MARK_PATTERN = re.compile(r"[āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜńňǹḿ]")
EXPORT_DICTIONARY_NAME_UNSAFE_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")
DEFAULT_DICTIONARY_NAME = "rime_word_marker_export"
DEFAULT_REVIEW_SESSION = "default"
IMPORT_IGNORED_CHARACTERS = "\u200c"
DERIVATIVE_SPLIT_PATTERN = re.compile(r"[\t\r\n]+")
DERIVATIVE_BULK_SPACE_FIELD_PATTERN = re.compile(r"[ ]+")
DERIVATIVE_BULK_MODES = {"merge", "overwrite"}
DERIVATIVE_BULK_MAX_LINES = 100_000
DERIVATIVE_BULK_MAX_DERIVATIVES_PER_PHRASE = 200
DERIVATIVE_BULK_MAX_ITEM_LENGTH = 200
REVIEW_HISTORY_LIMIT = 500
REVIEW_MODE_SEQUENTIAL = "sequential"
REVIEW_MODE_RANDOM = "random"
VALID_REVIEW_MODES = {REVIEW_MODE_SEQUENTIAL, REVIEW_MODE_RANDOM}
SQLITE_IN_MAX_VARIABLES = 900
AI_MIN_TRAINING_TOTAL = 2000
AI_MIN_CLASS_COUNT = 300
AI_HARD_EXAMPLES_PER_CLASS = 128
AI_SETTING_ENABLED = "ai_enabled"
AI_SETTING_WORKER_STATUS = "ai_worker_status"
AI_SETTING_LAST_SCAN_ID = "ai_last_scan_id"
AI_SETTING_LAST_ERROR = "ai_last_error"
AI_SETTING_LAST_RUN_AT = "ai_last_run_at"
AI_SETTING_PROGRESS_SAMPLES = "ai_progress_samples"
AI_SETTING_REPROCESS_OUTDATED = "ai_reprocess_outdated"
AI_PROGRESS_SAMPLE_WINDOW_SECONDS = 600
AI_WORKER_DISABLED = "disabled"
AI_WORKER_IDLE = "idle"
AI_WORKER_RUNNING = "running"
AI_WORKER_ERROR = "error"
VALID_AI_WORKER_STATUSES = {
    AI_WORKER_DISABLED,
    AI_WORKER_IDLE,
    AI_WORKER_RUNNING,
    AI_WORKER_ERROR,
}


class WordService:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._ai_training_examples_cache: dict[int, list[dict[str, Any]]] = {}
        self._ai_training_examples_cache_version = 0
        self._ai_training_examples_cache_lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _managed_connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        with self._managed_connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phrase TEXT NOT NULL UNIQUE,
                    pinyin TEXT NOT NULL,
                    pinyin_locked INTEGER NOT NULL DEFAULT 0,
                    derivatives TEXT NOT NULL DEFAULT '[]',
                    weight INTEGER NOT NULL DEFAULT 1,
                    weight_defined INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    imported_at TEXT NOT NULL,
                    labeled_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_entries_status_id
                    ON entries (status, id);

                CREATE INDEX IF NOT EXISTS idx_entries_imported_at
                    ON entries (imported_at DESC);

                CREATE INDEX IF NOT EXISTS idx_entries_labeled_at
                    ON entries (labeled_at DESC);

                CREATE TABLE IF NOT EXISTS review_state (
                    session_key TEXT PRIMARY KEY,
                    history_json TEXT NOT NULL DEFAULT '[]',
                    pointer INTEGER NOT NULL DEFAULT -1,
                    mode TEXT NOT NULL DEFAULT 'sequential',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            entry_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(entries)").fetchall()
            }
            if "weight_defined" not in entry_columns:
                connection.execute(
                    "ALTER TABLE entries ADD COLUMN weight_defined INTEGER NOT NULL DEFAULT 0"
                )
                entry_columns.add("weight_defined")
            if "pinyin_locked" not in entry_columns:
                connection.execute(
                    "ALTER TABLE entries ADD COLUMN pinyin_locked INTEGER NOT NULL DEFAULT 0"
                )
                entry_columns.add("pinyin_locked")
            if "derivatives" not in entry_columns:
                connection.execute(
                    "ALTER TABLE entries ADD COLUMN derivatives TEXT NOT NULL DEFAULT '[]'"
                )
                entry_columns.add("derivatives")
            for column_name, column_type in (
                ("ai_label", "TEXT"),
                ("ai_score", "REAL"),
                ("ai_labeled_at", "TEXT"),
                ("ai_model", "TEXT"),
                ("ai_prompt_version", "TEXT"),
            ):
                if column_name not in entry_columns:
                    connection.execute(f"ALTER TABLE entries ADD COLUMN {column_name} {column_type}")

            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(review_state)").fetchall()
            }
            if "mode" not in columns:
                connection.execute(
                    "ALTER TABLE review_state ADD COLUMN mode TEXT NOT NULL DEFAULT 'sequential'"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_entries_ai_queue
                    ON entries (status, ai_label, id)
                """
            )
            self._ensure_setting(connection, AI_SETTING_ENABLED, "0")
            self._ensure_setting(connection, AI_SETTING_WORKER_STATUS, AI_WORKER_DISABLED)
            self._ensure_setting(connection, AI_SETTING_LAST_SCAN_ID, "0")
            self._ensure_setting(connection, AI_SETTING_LAST_ERROR, "")
            self._ensure_setting(connection, AI_SETTING_LAST_RUN_AT, "")
            self._ensure_setting(connection, AI_SETTING_PROGRESS_SAMPLES, "[]")
            self._ensure_setting(connection, AI_SETTING_REPROCESS_OUTDATED, "0")
            connection.commit()

    def import_text(
        self,
        raw_text: str,
        overwrite_pinyin: bool = False,
        overwrite_weight: bool = True,
        mark_accepted: bool = False,
        ignore_pinyin: bool = False,
        skip_new_entries: bool = False,
        backup_before_import: bool = False,
    ) -> dict[str, Any]:
        if ignore_pinyin and overwrite_pinyin:
            raise ValueError("“忽略拼音”和“覆盖拼音”不能同时启用。")

        raw_text = self._clean_import_text(raw_text)
        backup_path = str(self.backup_database()) if backup_before_import else None

        inserted = 0
        skipped = 0
        skipped_new = 0
        updated = 0
        updated_pinyin = 0
        updated_weight = 0
        accepted_marked = 0
        parsed = 0
        accepted_existing = 0
        rejected_existing = 0
        imported_at = self._now()
        in_yaml_header = False
        accepted_marked_phrases: set[str] = set()
        parsed_entries: list[tuple[int, str, str, int, bool, bool]] = []

        for line_number, raw_line in enumerate(raw_text.splitlines(), start=1):
            stripped = raw_line.strip("\ufeff").strip()
            if stripped == "---":
                in_yaml_header = True
                continue
            if in_yaml_header:
                if stripped == "...":
                    in_yaml_header = False
                    continue
                if stripped.startswith("#") or YAML_HEADER_PATTERN.match(stripped):
                    continue
                userdb_line = self._parse_userdb_import_line(raw_line)
                if userdb_line is None:
                    continue
                in_yaml_header = False
                parsed_line = userdb_line
            else:
                parsed_line = self._parse_import_line(raw_line)

            if parsed_line is None:
                continue

            parsed += 1
            phrase, pinyin, weight, has_pinyin, has_weight = parsed_line
            parsed_entries.append((line_number, phrase, pinyin, weight, has_pinyin, has_weight))

        with self._managed_connection() as connection:
            cursor = connection.cursor()
            known_entries = self._fetch_entry_import_summaries(
                connection,
                [phrase for _, phrase, _, _, _, _ in parsed_entries],
            )

            for line_number, phrase, pinyin, weight, has_pinyin, has_weight in parsed_entries:
                if phrase not in known_entries:
                    if skip_new_entries:
                        skipped_new += 1
                        continue
                    if ignore_pinyin:
                        pinyin = transliterate_phrase(phrase)
                        has_pinyin = True
                    next_status = ACCEPTED if mark_accepted else PENDING
                    next_labeled_at = imported_at if mark_accepted else None
                    try:
                        cursor.execute(
                            """
                            INSERT INTO entries (
                                phrase, pinyin, weight, weight_defined, status, imported_at, labeled_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                phrase,
                                pinyin,
                                weight,
                                1 if has_weight else 0,
                                next_status,
                                imported_at,
                                next_labeled_at,
                            ),
                        )
                    except RuntimeError:
                        raise
                    except Exception as exc:  # pragma: no cover - defensive API path
                        raise ValueError(f"第 {line_number} 行导入失败：{exc}") from exc
                    inserted += 1
                    known_entries[phrase] = {
                        "status": next_status,
                        "weight": weight,
                        "weight_defined": has_weight,
                    }
                    if mark_accepted and phrase not in accepted_marked_phrases:
                        accepted_marked += 1
                        accepted_marked_phrases.add(phrase)
                else:
                    skipped += 1
                    current = known_entries.get(phrase, {})
                    status = current.get("status")
                    if status == ACCEPTED:
                        accepted_existing += 1
                    elif status == REJECTED:
                        rejected_existing += 1

                    update_fields: list[str] = []
                    update_values: list[Any] = []
                    if overwrite_pinyin and has_pinyin:
                        update_fields.append("pinyin = ?")
                        update_values.append(pinyin)
                    current_weight = int(current.get("weight") or 1)
                    current_weight_defined = bool(current.get("weight_defined"))
                    should_update_weight = (
                        overwrite_weight
                        and has_weight
                        and (not current_weight_defined or weight > current_weight)
                    )
                    if should_update_weight:
                        update_fields.append("weight = ?")
                        update_values.append(weight)
                        update_fields.append("weight_defined = 1")
                    if mark_accepted:
                        update_fields.append("status = ?")
                        update_values.append(ACCEPTED)
                        update_fields.append("labeled_at = ?")
                        update_values.append(imported_at)

                    if update_fields:
                        try:
                            cursor.execute(
                                f"""
                                UPDATE entries
                                SET {", ".join(update_fields)}
                                WHERE phrase = ?
                                """,
                                [*update_values, phrase],
                            )
                        except RuntimeError:
                            raise
                        except Exception as exc:  # pragma: no cover - defensive API path
                            raise ValueError(f"第 {line_number} 行更新失败：{exc}") from exc
                        updated += 1
                        if overwrite_pinyin and has_pinyin:
                            updated_pinyin += 1
                        if should_update_weight:
                            updated_weight += 1
                            current["weight"] = weight
                            current["weight_defined"] = True
                        if mark_accepted:
                            current["status"] = ACCEPTED
                            if phrase not in accepted_marked_phrases:
                                accepted_marked += 1
                                accepted_marked_phrases.add(phrase)

            connection.commit()

        if accepted_marked:
            self._invalidate_ai_training_examples_cache()

        return {
            "parsed": parsed,
            "inserted": inserted,
            "skipped": skipped,
            "skipped_new": skipped_new,
            "updated": updated,
            "updated_pinyin": updated_pinyin,
            "updated_weight": updated_weight,
            "accepted_marked": accepted_marked,
            "accepted_existing": accepted_existing,
            "rejected_existing": rejected_existing,
            "imported_at": imported_at,
            "backup_path": backup_path,
        }

    @staticmethod
    def _clean_import_text(raw_text: str) -> str:
        cleaned = str(raw_text or "")
        for char in IMPORT_IGNORED_CHARACTERS:
            cleaned = cleaned.replace(char, "")
        return cleaned

    def backup_database(self) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup_path = Path(f"{self.db_path}.backup_{timestamp}")
        suffix_index = 1
        while backup_path.exists():
            backup_path = Path(f"{self.db_path}.backup_{timestamp}_{suffix_index}")
            suffix_index += 1

        with self._managed_connection() as source:
            source.execute("PRAGMA wal_checkpoint(FULL)")
            target = sqlite3.connect(backup_path)
            try:
                source.backup(target)
            finally:
                target.close()

        return backup_path

    def recompute_toneless_pinyin(self) -> dict[str, int]:
        scanned = 0
        matched = 0
        updated = 0
        updates: list[tuple[str, int]] = []

        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT id, phrase, pinyin, pinyin_locked FROM entries"
            ).fetchall()
            for row in rows:
                scanned += 1
                if row["pinyin_locked"]:
                    continue
                current_pinyin = row["pinyin"] or ""
                if self._pinyin_has_tone(current_pinyin):
                    continue
                matched += 1
                next_pinyin = transliterate_phrase(row["phrase"])
                if next_pinyin and next_pinyin != current_pinyin:
                    updates.append((next_pinyin, int(row["id"])))

            if updates:
                connection.executemany(
                    "UPDATE entries SET pinyin = ? WHERE id = ?",
                    updates,
                )
                updated = len(updates)
            connection.commit()

        return {
            "scanned": scanned,
            "matched": matched,
            "updated": updated,
        }

    def cap_rejected_weights(self, cap: int = 10) -> dict[str, int]:
        if cap < 1:
            raise ValueError("词频上限必须大于 0。")

        with self._managed_connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM entries
                WHERE status = ? AND weight > ?
                """,
                (REJECTED, cap),
            ).fetchone()
            updated = int(row["total"]) if row else 0
            if updated:
                connection.execute(
                    """
                    UPDATE entries
                    SET weight = ?, weight_defined = 1
                    WHERE status = ? AND weight > ?
                    """,
                    (cap, REJECTED, cap),
                )
            connection.commit()

        return {
            "cap": cap,
            "updated": updated,
        }

    @staticmethod
    def _pinyin_has_tone(pinyin: str) -> bool:
        return bool(PINYIN_TONE_MARK_PATTERN.search(pinyin or ""))

    def _parse_import_line(self, raw_line: str) -> tuple[str, str, int, bool, bool] | None:
        line = raw_line.strip("\ufeff").rstrip("\n\r")
        stripped = line.strip()
        if not stripped:
            return None

        if stripped.startswith("#") or stripped in {"---", "..."}:
            return None

        if "\t" not in stripped and YAML_HEADER_PATTERN.match(stripped):
            return None

        parts = [part.strip() for part in line.split("\t")]
        userdb_line = self._parse_userdb_import_parts(parts)
        if userdb_line is not None:
            return userdb_line
        if self._has_userdb_metadata(parts):
            return None

        phrase = parts[0].strip()
        if not phrase:
            return None

        has_pinyin = len(parts) > 1 and bool(parts[1])
        has_weight = len(parts) > 2 and bool(parts[2]) and self._is_valid_weight(parts[2])
        pinyin = parts[1] if has_pinyin else transliterate_phrase(phrase)
        weight = self._parse_weight(parts[2]) if len(parts) > 2 else 1
        return phrase, pinyin, weight, has_pinyin, has_weight

    def _parse_userdb_import_line(
        self,
        raw_line: str,
    ) -> tuple[str, str, int, bool, bool] | None:
        line = raw_line.strip("\ufeff").rstrip("\n\r")
        return self._parse_userdb_import_parts([part.strip() for part in line.split("\t")])

    @classmethod
    def _parse_userdb_import_parts(
        cls,
        parts: list[str],
    ) -> tuple[str, str, int, bool, bool] | None:
        if len(parts) < 3:
            return None
        pinyin = parts[0].strip()
        phrase = parts[1].strip()
        metadata = " ".join(part for part in parts[2:] if part).strip()
        if not pinyin or not phrase or not metadata:
            return None
        match = USERDB_WEIGHT_PATTERN.search(metadata)
        if match is None:
            return None
        return phrase, pinyin, int(match.group(1)), True, True

    @staticmethod
    def _has_userdb_metadata(parts: list[str]) -> bool:
        if len(parts) < 3:
            return False
        metadata = " ".join(part for part in parts[2:] if part).strip()
        return bool(USERDB_METADATA_PATTERN.search(metadata))

    @staticmethod
    def _parse_weight(raw_weight: str) -> int:
        try:
            return int(raw_weight.strip())
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _is_valid_weight(raw_weight: str) -> bool:
        try:
            int(raw_weight.strip())
        except (TypeError, ValueError):
            return False
        return True

    def export_dictionary(
        self,
        statuses: list[str] | None = None,
        include_weight: bool = False,
        include_ai_assist: bool = False,
        export_mode: str = "main",
        include_mixed: bool = False,
        mixed_scheme: str = "full_pinyin",
        omit_yaml_header: bool = False,
        dictionary_name: str = DEFAULT_DICTIONARY_NAME,
    ) -> str:
        return "".join(
            self.iter_export_dictionary_lines(
                statuses=statuses,
                include_weight=include_weight,
                include_ai_assist=include_ai_assist,
                export_mode=export_mode,
                include_mixed=include_mixed,
                mixed_scheme=mixed_scheme,
                omit_yaml_header=omit_yaml_header,
                dictionary_name=dictionary_name,
            )
        )

    def iter_export_dictionary_lines(
        self,
        statuses: list[str] | None = None,
        include_weight: bool = False,
        include_ai_assist: bool = False,
        export_mode: str = "main",
        include_mixed: bool = False,
        mixed_scheme: str = "full_pinyin",
        omit_yaml_header: bool = False,
        dictionary_name: str = DEFAULT_DICTIONARY_NAME,
    ):
        dictionary_name = self.normalize_export_dictionary_name(dictionary_name)
        statuses = self._normalize_statuses(statuses or DEFAULT_EXPORT_STATUSES)
        if include_mixed and export_mode == "main":
            export_mode = "mixed"
        export_mode = self._normalize_export_mode(export_mode)
        if export_mode == "opencc":
            yield from self._iter_opencc_export_lines(statuses, include_ai_assist)
            return

        include_mixed = export_mode == "mixed"
        mixed_scheme = normalize_mixed_export_scheme(mixed_scheme) if include_mixed else "full_pinyin"
        placeholders = ",".join("?" for _ in statuses)
        header_lines = [
            "# Rime dictionary generated by Rime Word Marker\n",
            "---\n",
            f"name: {dictionary_name}\n",
            f'version: "{self._now()}"\n',
            "sort: by_weight\n",
            "...\n",
        ]
        if not omit_yaml_header:
            for line in header_lines:
                yield line

        with self._managed_connection() as connection:
            rows = connection.execute(
                f"""
                WITH export_view AS (
                    SELECT
                        id,
                        phrase,
                        pinyin,
                        pinyin_locked,
                        weight,
                        CASE
                            WHEN status IN ('accepted', 'rejected') THEN status
                            WHEN ? = 1 AND status = 'pending' AND ai_label IN ('accepted', 'rejected')
                                THEN ai_label
                            ELSE status
                        END AS effective_status
                    FROM entries
                )
                SELECT phrase, pinyin, pinyin_locked, weight
                FROM export_view
                WHERE effective_status IN ({placeholders})
                    AND (
                        (? = 1 AND phrase GLOB '*[A-Za-z]*')
                        OR (? = 0 AND phrase NOT GLOB '*[A-Za-z]*')
                    )
                ORDER BY weight DESC, id ASC
                """,
                [
                    1 if include_ai_assist else 0,
                    *statuses,
                    1 if include_mixed else 0,
                    1 if include_mixed else 0,
                ],
            )
            for row in rows:
                phrase = row["phrase"]
                pinyin = row["pinyin"]
                if phrase_has_ascii_letters(phrase):
                    if not include_mixed:
                        continue
                    if not row["pinyin_locked"]:
                        pinyin = export_code_for_mixed_phrase(phrase, pinyin, mixed_scheme)

                columns = [phrase, pinyin]
                if include_weight:
                    columns.append(str(row["weight"]))
                yield "\t".join(columns) + "\n"

    def _iter_opencc_export_lines(
        self,
        statuses: list[str],
        include_ai_assist: bool,
    ):
        placeholders = ",".join("?" for _ in statuses)
        with self._managed_connection() as connection:
            rows = connection.execute(
                f"""
                WITH export_view AS (
                    SELECT
                        id,
                        phrase,
                        derivatives,
                        weight,
                        CASE
                            WHEN status IN ('accepted', 'rejected') THEN status
                            WHEN ? = 1 AND status = 'pending' AND ai_label IN ('accepted', 'rejected')
                                THEN ai_label
                            ELSE status
                        END AS effective_status
                    FROM entries
                )
                SELECT phrase, derivatives
                FROM export_view
                WHERE effective_status IN ({placeholders})
                    AND derivatives != '[]'
                ORDER BY weight DESC, id ASC
                """,
                [1 if include_ai_assist else 0, *statuses],
            )
            for row in rows:
                derivatives = self._load_derivatives(row["derivatives"])
                if derivatives:
                    phrase = row["phrase"]
                    yield f"{phrase}\t{' '.join([phrase, *derivatives])}\n"

    def count_export_entries(
        self,
        statuses: list[str] | None = None,
        include_ai_assist: bool = False,
        export_mode: str = "main",
        include_mixed: bool = False,
    ) -> int:
        normalized_statuses = self._normalize_statuses(statuses or DEFAULT_EXPORT_STATUSES)
        if include_mixed and export_mode == "main":
            export_mode = "mixed"
        export_mode = self._normalize_export_mode(export_mode)
        placeholders = ",".join("?" for _ in normalized_statuses)
        if export_mode == "opencc":
            with self._managed_connection() as connection:
                row = connection.execute(
                    f"""
                    WITH export_view AS (
                        SELECT
                            derivatives,
                            CASE
                                WHEN status IN ('accepted', 'rejected') THEN status
                                WHEN ? = 1 AND status = 'pending' AND ai_label IN ('accepted', 'rejected')
                                    THEN ai_label
                                ELSE status
                            END AS effective_status
                        FROM entries
                    )
                    SELECT COUNT(*) AS total
                    FROM export_view
                    WHERE effective_status IN ({placeholders})
                        AND derivatives != '[]'
                    """,
                    [1 if include_ai_assist else 0, *normalized_statuses],
                ).fetchone()
            return int(row["total"]) if row else 0

        include_mixed = export_mode == "mixed"

        with self._managed_connection() as connection:
            row = connection.execute(
                f"""
                WITH export_view AS (
                    SELECT
                        phrase,
                        CASE
                            WHEN status IN ('accepted', 'rejected') THEN status
                            WHEN ? = 1 AND status = 'pending' AND ai_label IN ('accepted', 'rejected')
                                THEN ai_label
                            ELSE status
                        END AS effective_status
                    FROM entries
                )
                SELECT COUNT(*) AS total
                FROM export_view
                WHERE effective_status IN ({placeholders})
                    AND (
                        (? = 1 AND phrase GLOB '*[A-Za-z]*')
                        OR (? = 0 AND phrase NOT GLOB '*[A-Za-z]*')
                    )
                """,
                [
                    1 if include_ai_assist else 0,
                    *normalized_statuses,
                    1 if include_mixed else 0,
                    1 if include_mixed else 0,
                ],
            ).fetchone()

        return int(row["total"]) if row else 0

    @staticmethod
    def _normalize_export_mode(export_mode: str) -> str:
        normalized = str(export_mode or "main").strip().lower()
        if normalized not in {"main", "mixed", "opencc"}:
            raise ValueError("无效导出类型。")
        return normalized

    def get_ai_overview(
        self,
        configured: bool = False,
        model_name: str | None = None,
        prompt_version: str | None = None,
    ) -> dict[str, Any]:
        current_prompt_version = (prompt_version or "").strip()
        with self._managed_connection() as connection:
            settings = self._get_settings(
                connection,
                [
                    AI_SETTING_ENABLED,
                    AI_SETTING_WORKER_STATUS,
                    AI_SETTING_LAST_SCAN_ID,
                    AI_SETTING_LAST_ERROR,
                    AI_SETTING_LAST_RUN_AT,
                    AI_SETTING_PROGRESS_SAMPLES,
                    AI_SETTING_REPROCESS_OUTDATED,
                ],
            )
            training_row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS accepted_count,
                    SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS rejected_count
                FROM entries
                """,
                (ACCEPTED, REJECTED),
            ).fetchone()
            ai_row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS pending_total,
                    SUM(
                        CASE
                            WHEN status = ? AND ai_label IS NULL
                                THEN 1
                            ELSE 0
                        END
                    ) AS unlabeled_total,
                    SUM(
                        CASE
                            WHEN status = ?
                                AND ai_label IS NOT NULL
                                AND ? != ''
                                AND (ai_prompt_version IS NULL OR ai_prompt_version != ?)
                                THEN 1
                            ELSE 0
                        END
                    ) AS outdated_total,
                    SUM(CASE WHEN status = ? AND ai_label = ? THEN 1 ELSE 0 END) AS ai_pending_total,
                    SUM(CASE WHEN status = ? AND ai_label = ? THEN 1 ELSE 0 END) AS ai_accepted_total,
                    SUM(CASE WHEN status = ? AND ai_label = ? THEN 1 ELSE 0 END) AS ai_rejected_total
                FROM entries
                """,
                (
                    PENDING,
                    PENDING,
                    PENDING,
                    current_prompt_version,
                    current_prompt_version,
                    PENDING,
                    PENDING,
                    PENDING,
                    ACCEPTED,
                    PENDING,
                    REJECTED,
                ),
            ).fetchone()

        accepted_count = int(training_row["accepted_count"] or 0)
        rejected_count = int(training_row["rejected_count"] or 0)
        pending_total = int(ai_row["pending_total"] or 0)
        unlabeled_total = int(ai_row["unlabeled_total"] or 0)
        outdated_total = int(ai_row["outdated_total"] or 0)
        remaining_total = unlabeled_total + outdated_total
        current_total = max(0, pending_total - remaining_total)
        progress = self._build_ai_progress(
            settings.get(AI_SETTING_PROGRESS_SAMPLES, "[]"),
            total=pending_total,
            current=current_total,
            unlabeled=unlabeled_total,
            outdated=outdated_total,
            remaining=remaining_total,
        )
        training_total = accepted_count + rejected_count
        sufficient = (
            training_total >= AI_MIN_TRAINING_TOTAL
            and accepted_count >= AI_MIN_CLASS_COUNT
            and rejected_count >= AI_MIN_CLASS_COUNT
        )
        worker_status = settings.get(AI_SETTING_WORKER_STATUS, AI_WORKER_DISABLED)
        if worker_status not in VALID_AI_WORKER_STATUSES:
            worker_status = AI_WORKER_DISABLED
        reprocess_outdated = settings.get(AI_SETTING_REPROCESS_OUTDATED, "0") == "1"

        return {
            "enabled": settings.get(AI_SETTING_ENABLED, "0") == "1",
            "configured": configured,
            "model": (model_name or "").strip(),
            "prompt_version": (prompt_version or "").strip(),
            "training": {
                "accepted": accepted_count,
                "rejected": rejected_count,
                "total": training_total,
                "minimum_total": AI_MIN_TRAINING_TOTAL,
                "minimum_each_class": AI_MIN_CLASS_COUNT,
                "sufficient": sufficient,
            },
            "queue": {
                "pending_total": pending_total,
                "unlabeled": unlabeled_total,
                "outdated": outdated_total,
                "remaining": remaining_total,
                "current": current_total,
                "ai_pending": int(ai_row["ai_pending_total"] or 0),
                "ai_accepted": int(ai_row["ai_accepted_total"] or 0),
                "ai_rejected": int(ai_row["ai_rejected_total"] or 0),
                "reprocess_outdated": reprocess_outdated,
            },
            "progress": progress,
            "worker_status": worker_status,
            "last_scan_id": int(settings.get(AI_SETTING_LAST_SCAN_ID, "0") or 0),
            "last_error": settings.get(AI_SETTING_LAST_ERROR, ""),
            "last_run_at": settings.get(AI_SETTING_LAST_RUN_AT, ""),
            "requirement_message": self.build_ai_training_requirement_message(
                accepted_count=accepted_count,
                rejected_count=rejected_count,
            ),
        }

    def _build_ai_progress(
        self,
        raw_samples: str,
        total: int,
        current: int,
        unlabeled: int,
        outdated: int,
        remaining: int,
    ) -> dict[str, Any]:
        samples = self._load_ai_progress_samples(raw_samples, now=self._now())
        rate_per_minute: float | None = None
        eta_seconds: int | None = None
        if len(samples) >= 2:
            first_time = datetime.fromisoformat(samples[0]["timestamp"])
            last_time = datetime.fromisoformat(samples[-1]["timestamp"])
            elapsed_seconds = max(0.0, (last_time - first_time).total_seconds())
            updated_total = sum(int(sample["updated"]) for sample in samples)
            if elapsed_seconds > 0 and updated_total > 0:
                rate_per_minute = updated_total / (elapsed_seconds / 60)
                if remaining > 0:
                    eta_seconds = int(math.ceil(remaining / (rate_per_minute / 60)))

        return {
            "total": max(0, int(total)),
            "current": max(0, int(current)),
            "unlabeled": max(0, int(unlabeled)),
            "outdated": max(0, int(outdated)),
            "remaining": max(0, int(remaining)),
            "rate_per_minute": rate_per_minute,
            "eta_seconds": eta_seconds,
            "sample_window_seconds": AI_PROGRESS_SAMPLE_WINDOW_SECONDS,
        }

    def _append_ai_progress_sample(
        self,
        raw_samples: str,
        updated_count: int,
        timestamp: str,
    ) -> str:
        samples = self._load_ai_progress_samples(raw_samples, now=timestamp)
        samples.append({"timestamp": timestamp, "updated": max(0, int(updated_count))})
        return json.dumps(samples[-200:], ensure_ascii=False)

    @staticmethod
    def _load_ai_progress_samples(raw_samples: str, now: str) -> list[dict[str, Any]]:
        try:
            parsed = json.loads(raw_samples or "[]")
        except json.JSONDecodeError:
            parsed = []
        if not isinstance(parsed, list):
            return []

        try:
            now_dt = datetime.fromisoformat(now)
        except ValueError:
            now_dt = datetime.now().astimezone()
        cutoff = now_dt.timestamp() - AI_PROGRESS_SAMPLE_WINDOW_SECONDS
        samples: list[dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                timestamp = str(item["timestamp"])
                updated = int(item["updated"])
                sample_dt = datetime.fromisoformat(timestamp)
            except (KeyError, TypeError, ValueError):
                continue
            if sample_dt.timestamp() >= cutoff and updated > 0:
                samples.append({"timestamp": timestamp, "updated": updated})
        samples.sort(key=lambda sample: sample["timestamp"])
        return samples

    def build_ai_training_requirement_message(
        self,
        accepted_count: int | None = None,
        rejected_count: int | None = None,
    ) -> str:
        if accepted_count is None or rejected_count is None:
            overview = self.get_ai_overview()
            accepted_count = overview["training"]["accepted"]
            rejected_count = overview["training"]["rejected"]
        total = accepted_count + rejected_count
        if (
            total >= AI_MIN_TRAINING_TOTAL
            and accepted_count >= AI_MIN_CLASS_COUNT
            and rejected_count >= AI_MIN_CLASS_COUNT
        ):
            return (
                f"人工样本已满足自动标注条件：接受 {accepted_count} 条、拒绝 {rejected_count} 条。"
            )
        return (
            "人工标注数据量不足。"
            f" 当前接受 {accepted_count} 条、拒绝 {rejected_count} 条、合计 {total} 条；"
            f" 至少需要合计 {AI_MIN_TRAINING_TOTAL} 条，且接受与拒绝各不少于 {AI_MIN_CLASS_COUNT} 条。"
        )

    def set_ai_enabled(
        self,
        enabled: bool,
        configured: bool = False,
        model_name: str | None = None,
        prompt_version: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        message = ""
        with self._managed_connection() as connection:
            accepted_count, rejected_count = self._get_ai_training_counts(connection)
            sufficient = (
                accepted_count + rejected_count >= AI_MIN_TRAINING_TOTAL
                and accepted_count >= AI_MIN_CLASS_COUNT
                and rejected_count >= AI_MIN_CLASS_COUNT
            )
            if enabled:
                if not configured:
                    enabled = False
                    message = "AI 接口尚未配置完整，无法开启自动标注。"
                elif not sufficient:
                    enabled = False
                    message = self.build_ai_training_requirement_message(
                        accepted_count=accepted_count,
                        rejected_count=rejected_count,
                    )

            self._set_setting(connection, AI_SETTING_ENABLED, "1" if enabled else "0")
            self._set_setting(
                connection,
                AI_SETTING_WORKER_STATUS,
                AI_WORKER_IDLE if enabled else AI_WORKER_DISABLED,
            )
            if not enabled and message:
                self._set_setting(connection, AI_SETTING_LAST_ERROR, message)
            elif not enabled:
                self._set_setting(connection, AI_SETTING_LAST_ERROR, "")
            elif enabled:
                self._set_setting(connection, AI_SETTING_LAST_ERROR, "")
            connection.commit()

        return (
            self.get_ai_overview(
                configured=configured,
                model_name=model_name,
                prompt_version=prompt_version,
            ),
            message,
        )

    def disable_ai(self, message: str = "") -> dict[str, Any]:
        with self._managed_connection() as connection:
            self._set_setting(connection, AI_SETTING_ENABLED, "0")
            self._set_setting(connection, AI_SETTING_WORKER_STATUS, AI_WORKER_DISABLED)
            self._set_setting(connection, AI_SETTING_LAST_ERROR, message)
            self._set_setting(connection, AI_SETTING_LAST_RUN_AT, self._now())
            connection.commit()
        return self.get_ai_overview()

    def request_ai_outdated_reprocess(
        self,
        configured: bool = False,
        model_name: str | None = None,
        prompt_version: str | None = None,
    ) -> dict[str, Any]:
        with self._managed_connection() as connection:
            self._set_setting(connection, AI_SETTING_REPROCESS_OUTDATED, "1")
            self._set_setting(connection, AI_SETTING_LAST_SCAN_ID, "0")
            self._set_setting(connection, AI_SETTING_LAST_RUN_AT, self._now())
            connection.commit()
        return self.get_ai_overview(
            configured=configured,
            model_name=model_name,
            prompt_version=prompt_version,
        )

    def clear_ai_outdated_reprocess(self) -> None:
        with self._managed_connection() as connection:
            self._set_setting(connection, AI_SETTING_REPROCESS_OUTDATED, "0")
            connection.commit()

    def update_ai_runtime_state(
        self,
        worker_status: str,
        last_error: str | None = None,
        last_scan_id: int | None = None,
    ) -> None:
        normalized_status = (
            worker_status if worker_status in VALID_AI_WORKER_STATUSES else AI_WORKER_ERROR
        )
        with self._managed_connection() as connection:
            self._set_setting(connection, AI_SETTING_WORKER_STATUS, normalized_status)
            if last_error is not None:
                self._set_setting(connection, AI_SETTING_LAST_ERROR, last_error)
            if last_scan_id is not None:
                self._set_setting(connection, AI_SETTING_LAST_SCAN_ID, str(max(0, int(last_scan_id))))
            self._set_setting(connection, AI_SETTING_LAST_RUN_AT, self._now())
            connection.commit()

    def sample_ai_training_examples(self, per_class: int = 768) -> list[dict[str, Any]]:
        per_class = max(1, min(int(per_class), 1024))
        while True:
            with self._ai_training_examples_cache_lock:
                cache_version = self._ai_training_examples_cache_version
                cached_examples = self._ai_training_examples_cache.get(per_class)
                if cached_examples is not None:
                    return self._clone_ai_training_examples(cached_examples)

            examples = self._build_ai_training_examples(per_class)
            with self._ai_training_examples_cache_lock:
                if cache_version == self._ai_training_examples_cache_version:
                    self._ai_training_examples_cache[per_class] = self._clone_ai_training_examples(examples)
                    return examples

    def _build_ai_training_examples(self, per_class: int) -> list[dict[str, Any]]:
        hard_limit = min(AI_HARD_EXAMPLES_PER_CLASS, max(1, per_class // 6))
        with self._managed_connection() as connection:
            accepted_rows = connection.execute(
                "SELECT phrase, status, ai_label FROM entries WHERE status = ? ORDER BY id ASC",
                (ACCEPTED,),
            ).fetchall()
            rejected_rows = connection.execute(
                "SELECT phrase, status, ai_label FROM entries WHERE status = ? ORDER BY id ASC",
                (REJECTED,),
            ).fetchall()

        accepted_hard = self._sample_rows(
            self._ai_disagreement_rows(accepted_rows),
            hard_limit,
            seed=f"{ACCEPTED}:hard",
            source="human_ai_disagreement",
        )
        rejected_hard = self._sample_rows(
            self._ai_disagreement_rows(rejected_rows),
            hard_limit,
            seed=f"{REJECTED}:hard",
            source="human_ai_disagreement",
        )
        hard_phrases = {item["phrase"] for item in [*accepted_hard, *rejected_hard]}

        accepted_examples = self._sample_rows(
            [row for row in accepted_rows if str(row["phrase"]) not in hard_phrases],
            per_class,
            seed=f"{ACCEPTED}:base",
        )
        rejected_examples = self._sample_rows(
            [row for row in rejected_rows if str(row["phrase"]) not in hard_phrases],
            per_class,
            seed=f"{REJECTED}:base",
        )
        examples = accepted_examples + rejected_examples
        hard_examples = accepted_hard + rejected_hard
        return self._stable_shuffle([*hard_examples, *examples], seed="ai-training-examples")

    def get_ai_batch_candidates(
        self,
        limit: int = 12,
        prompt_version: str | None = None,
        selection_mode: str = "sequential",
        include_outdated: bool = False,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_AI_BATCH_SIZE))
        current_prompt_version = (prompt_version or "").strip()
        primary_condition = "status = ? AND ai_label IS NULL"
        primary_parameters: list[Any] = [PENDING]
        fallback_condition = ""
        fallback_parameters: list[Any] = []
        if include_outdated and current_prompt_version:
            fallback_condition = (
                "status = ? AND ai_label IS NOT NULL "
                "AND (ai_prompt_version IS NULL OR ai_prompt_version != ?)"
            )
            fallback_parameters = [PENDING, current_prompt_version]

        with self._managed_connection() as connection:
            settings = self._get_settings(connection, [AI_SETTING_LAST_SCAN_ID])
            last_scan_id = int(settings.get(AI_SETTING_LAST_SCAN_ID, "0") or 0)
            primary_rows: list[sqlite3.Row]
            fallback_rows: list[sqlite3.Row] = []
            if selection_mode == "random":
                primary_rows = self._get_random_ai_batch_rows(
                    connection,
                    primary_condition,
                    primary_parameters,
                    limit,
                )
                if fallback_condition and len(primary_rows) < limit:
                    fallback_rows = self._get_random_ai_batch_rows(
                        connection,
                        fallback_condition,
                        fallback_parameters,
                        limit - len(primary_rows),
                    )
            else:
                primary_rows = self._get_sequential_ai_batch_rows(
                    connection,
                    primary_condition,
                    primary_parameters,
                    limit,
                    last_scan_id,
                )
                if fallback_condition and len(primary_rows) < limit:
                    fallback_rows = self._get_sequential_ai_batch_rows(
                        connection,
                        fallback_condition,
                        fallback_parameters,
                        limit - len(primary_rows),
                        last_scan_id,
                    )

        rows = [*primary_rows, *fallback_rows]
        items = [self._row_to_entry(row) for row in rows]
        if primary_rows:
            next_scan_id = int(primary_rows[-1]["id"])
        elif fallback_rows:
            next_scan_id = int(fallback_rows[-1]["id"])
        else:
            next_scan_id = 0
        return {"items": items, "next_scan_id": next_scan_id}

    def apply_ai_annotations(
        self,
        source_entries: list[dict[str, Any]],
        predictions: list[dict[str, Any]],
        model_name: str,
        prompt_version: str,
        next_scan_id: int = 0,
    ) -> int:
        if not source_entries or not predictions:
            return 0

        source_map = {int(entry["id"]): entry for entry in source_entries}
        updated_count = 0
        now = self._now()

        with self._managed_connection() as connection:
            for prediction in predictions:
                entry_id = int(prediction["id"])
                source = source_map.get(entry_id)
                if source is None:
                    continue
                score = max(0.0, min(1.0, float(prediction["score"])))
                ai_label = score_to_label(score)
                cursor = connection.execute(
                    """
                    UPDATE entries
                    SET ai_label = ?, ai_score = ?, ai_labeled_at = ?, ai_model = ?, ai_prompt_version = ?
                    WHERE id = ? AND phrase = ? AND status = ?
                    """,
                    (
                        ai_label,
                        score,
                        now,
                        model_name.strip(),
                        prompt_version.strip(),
                        entry_id,
                        source["phrase"],
                        PENDING,
                    ),
                )
                if cursor.rowcount:
                    updated_count += 1

            self._set_setting(connection, AI_SETTING_LAST_SCAN_ID, str(max(0, int(next_scan_id))))
            self._set_setting(connection, AI_SETTING_LAST_RUN_AT, now)
            if updated_count:
                self._set_setting(connection, AI_SETTING_LAST_ERROR, "")
                self._set_setting(
                    connection,
                    AI_SETTING_PROGRESS_SAMPLES,
                    self._append_ai_progress_sample(
                        self._get_settings(connection, [AI_SETTING_PROGRESS_SAMPLES]).get(
                            AI_SETTING_PROGRESS_SAMPLES,
                            "[]",
                        ),
                        updated_count,
                        now,
                    ),
                )
            connection.commit()

        return updated_count

    @staticmethod
    def _fetch_entry_import_summaries(
        connection: sqlite3.Connection,
        phrases: list[str],
    ) -> dict[str, dict[str, Any]]:
        unique_phrases = list(dict.fromkeys(phrase for phrase in phrases if phrase))
        if not unique_phrases:
            return {}

        summaries: dict[str, dict[str, Any]] = {}
        for start in range(0, len(unique_phrases), SQLITE_IN_MAX_VARIABLES):
            chunk = unique_phrases[start : start + SQLITE_IN_MAX_VARIABLES]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT phrase, status, weight, weight_defined
                FROM entries
                WHERE phrase IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            for row in rows:
                summaries[row["phrase"]] = {
                    "status": row["status"],
                    "weight": row["weight"],
                    "weight_defined": bool(row["weight_defined"]),
                }
        return summaries

    @staticmethod
    def _get_sequential_ai_batch_rows(
        connection: sqlite3.Connection,
        ai_queue_condition: str,
        ai_queue_parameters: list[Any],
        limit: int,
        last_scan_id: int,
    ) -> list[sqlite3.Row]:
        rows = connection.execute(
            f"""
            SELECT *
            FROM entries
            WHERE {ai_queue_condition} AND id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            [*ai_queue_parameters, last_scan_id, limit],
        ).fetchall()

        remaining = limit - len(rows)
        if remaining <= 0 or not last_scan_id:
            return rows

        wrapped_rows = connection.execute(
            f"""
            SELECT *
            FROM entries
            WHERE {ai_queue_condition} AND id <= ?
            ORDER BY id ASC
            LIMIT ?
            """,
            [*ai_queue_parameters, last_scan_id, remaining],
        ).fetchall()
        return [*rows, *wrapped_rows]

    @staticmethod
    def _get_random_ai_batch_rows(
        connection: sqlite3.Connection,
        ai_queue_condition: str,
        ai_queue_parameters: list[Any],
        limit: int,
    ) -> list[sqlite3.Row]:
        bounds = connection.execute(
            f"""
            SELECT MIN(id) AS min_id, MAX(id) AS max_id
            FROM entries
            WHERE {ai_queue_condition}
            """,
            ai_queue_parameters,
        ).fetchone()
        if bounds is None or bounds["min_id"] is None or bounds["max_id"] is None:
            return []

        target_id = random.randint(bounds["min_id"], bounds["max_id"])
        rows = connection.execute(
            f"""
            SELECT *
            FROM entries
            WHERE {ai_queue_condition} AND id >= ?
            ORDER BY id ASC
            LIMIT ?
            """,
            [*ai_queue_parameters, target_id, limit],
        ).fetchall()

        remaining = limit - len(rows)
        if remaining <= 0:
            return rows

        wrapped_rows = connection.execute(
            f"""
            SELECT *
            FROM entries
            WHERE {ai_queue_condition} AND id < ?
            ORDER BY id ASC
            LIMIT ?
            """,
            [*ai_queue_parameters, target_id, remaining],
        ).fetchall()
        return [*rows, *wrapped_rows]

    def get_stats(self) -> dict[str, int]:
        with self._managed_connection() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM entries
                GROUP BY status
                """
            ).fetchall()

        counts = {PENDING: 0, ACCEPTED: 0, REJECTED: 0}
        for row in rows:
            counts[row["status"]] = row["count"]

        counts["total"] = counts[PENDING] + counts[ACCEPTED] + counts[REJECTED]
        return counts

    def get_next_pending(self, after_id: int | None = None) -> dict[str, Any] | None:
        with self._managed_connection() as connection:
            if after_id is None:
                row = connection.execute(
                    """
                    SELECT *
                    FROM entries
                    WHERE status = ?
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (PENDING,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT *
                    FROM entries
                    WHERE status = ? AND id > ?
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (PENDING, after_id),
                ).fetchone()

                if row is None:
                    row = connection.execute(
                        """
                        SELECT *
                        FROM entries
                        WHERE status = ?
                        ORDER BY id ASC
                        LIMIT 1
                        """,
                        (PENDING,),
                    ).fetchone()

        return self._row_to_entry(row) if row else None

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT * FROM entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def delete_entry(self, entry_id: int) -> dict[str, Any]:
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT * FROM entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
            if row is None:
                raise LookupError("词条不存在。")
            entry = self._row_to_entry(row)
            connection.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            self._remove_entry_from_review_states(connection, entry_id)
            connection.commit()

        if entry["status"] in {ACCEPTED, REJECTED}:
            self._invalidate_ai_training_examples_cache()
        return entry

    def _remove_entry_from_review_states(
        self,
        connection: sqlite3.Connection,
        entry_id: int,
    ) -> None:
        rows = connection.execute(
            "SELECT session_key, history_json, pointer, mode FROM review_state"
        ).fetchall()
        for row in rows:
            try:
                history_ids = [int(item) for item in json.loads(row["history_json"])]
            except (TypeError, ValueError, json.JSONDecodeError):
                history_ids = []
            if entry_id not in history_ids:
                continue

            pointer = int(row["pointer"])
            mode = row["mode"] if row["mode"] in VALID_REVIEW_MODES else REVIEW_MODE_RANDOM
            current_id = history_ids[pointer] if 0 <= pointer < len(history_ids) else None
            next_history_ids = [item for item in history_ids if item != entry_id]

            if not next_history_ids:
                next_pointer = -1
            elif pointer >= len(history_ids):
                next_pointer = len(next_history_ids)
            elif current_id is not None and current_id in next_history_ids:
                next_pointer = next_history_ids.index(current_id)
            else:
                removed_before = sum(1 for item in history_ids[:pointer] if item == entry_id)
                next_pointer = min(max(0, pointer - removed_before), len(next_history_ids) - 1)

            self._save_review_state(
                connection,
                row["session_key"],
                next_history_ids,
                next_pointer,
                mode,
                commit=False,
            )

    def update_status(self, entry_id: int, status: str) -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise ValueError("无效状态。")

        with self._managed_connection() as connection:
            self._apply_status_update(connection, entry_id, status)
            connection.commit()

        self._invalidate_ai_training_examples_cache()
        entry = self.get_entry(entry_id)
        if entry is None:  # pragma: no cover - guarded above
            raise LookupError("词条不存在。")
        return entry

    def label_and_advance(
        self,
        entry_id: int,
        status: str,
        session_key: str = DEFAULT_REVIEW_SESSION,
        prefer_ai: bool = False,
    ) -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise ValueError("无效状态。")

        with self._managed_connection() as connection:
            self._apply_status_update(connection, entry_id, status)

            history_ids, pointer, _ = self._load_review_state(connection, session_key)
            mode = REVIEW_MODE_RANDOM
            history_ids, pointer, history_entries = self._resolve_review_history(connection, history_ids, pointer)

            if pointer < len(history_entries) - 1:
                pointer += 1
            else:
                next_row = self._get_next_review_row(connection, history_ids, prefer_ai=prefer_ai)
                if next_row is not None:
                    next_id = next_row["id"]
                    if not history_ids or history_ids[-1] != next_id:
                        history_ids.append(next_id)
                    history_ids, pointer = self._trim_review_history(history_ids, len(history_ids) - 1)
                    history_ids, pointer, history_entries = self._resolve_review_history(
                        connection,
                        history_ids,
                        pointer,
                    )
                elif history_entries:
                    pointer = len(history_entries)
                else:
                    pointer = -1

            self._save_review_state(connection, session_key, history_ids, pointer, REVIEW_MODE_RANDOM, commit=False)
            connection.commit()

            current_entry = history_entries[pointer] if 0 <= pointer < len(history_entries) else None
            updated_entry = self._row_to_entry(
                connection.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
            )

        self._invalidate_ai_training_examples_cache()
        return {
            "history": history_entries,
            "pointer": pointer,
            "current_entry": current_entry,
            "can_go_back": self._review_can_go_back(pointer, len(history_entries)),
            "mode": REVIEW_MODE_RANDOM,
            "updated_entry": updated_entry,
        }

    def batch_update_entries(
        self,
        entry_ids: list[int],
        updates: dict[str, Any],
        regenerate_pinyin: bool = False,
        clear_labeled_at: bool = False,
    ) -> dict[str, Any]:
        allowed_keys = {
            "pinyin",
            "pinyin_locked",
            "weight",
            "status",
            "imported_at",
            "labeled_at",
        }
        unexpected_keys = set(updates) - allowed_keys
        if unexpected_keys:
            raise ValueError(f"包含不支持的字段：{', '.join(sorted(unexpected_keys))}")

        normalized_ids = self._normalize_entry_ids(entry_ids)
        if not normalized_ids:
            raise ValueError("请至少选择一个词条。")

        if not updates and not regenerate_pinyin and not clear_labeled_at:
            raise ValueError("批量编辑至少需要一个修改项。")

        now = self._now()

        with self._managed_connection() as connection:
            placeholders = ",".join("?" for _ in normalized_ids)
            rows = connection.execute(
                f"SELECT * FROM entries WHERE id IN ({placeholders})",
                normalized_ids,
            ).fetchall()

            found_ids = {row["id"] for row in rows}
            missing_ids = [entry_id for entry_id in normalized_ids if entry_id not in found_ids]
            if missing_ids:
                raise LookupError(f"词条不存在：{', '.join(str(item) for item in missing_ids)}")

            weight = None
            if "weight" in updates:
                try:
                    weight = int(str(updates["weight"]).strip())
                except ValueError as exc:
                    raise ValueError("词频必须是整数。") from exc

            status = None
            status_provided = "status" in updates
            if status_provided:
                status = str(updates["status"]).strip()
                if status not in VALID_STATUSES:
                    raise ValueError("无效状态。")

            imported_at = None
            if "imported_at" in updates:
                imported_at = self._normalize_datetime_input(
                    updates["imported_at"],
                    field_name="导入时间",
                    allow_empty=False,
                )

            labeled_at = None
            labeled_at_provided = "labeled_at" in updates
            if labeled_at_provided:
                labeled_at = self._normalize_datetime_input(
                    updates["labeled_at"],
                    field_name="标注时间",
                    allow_empty=True,
                )

            manual_pinyin = None
            pinyin_provided = "pinyin" in updates
            if pinyin_provided:
                manual_pinyin = str(updates["pinyin"]).strip()

            pinyin_locked_provided = "pinyin_locked" in updates
            next_pinyin_locked = self._coerce_bool(updates.get("pinyin_locked")) if pinyin_locked_provided else None

            for row in rows:
                current = self._row_to_entry(row)
                next_pinyin = current["pinyin"]
                if regenerate_pinyin:
                    next_pinyin = transliterate_phrase(current["phrase"])
                elif pinyin_provided:
                    next_pinyin = manual_pinyin or transliterate_phrase(current["phrase"])

                weight_provided = weight is not None
                next_weight = weight if weight_provided else current["weight"]
                next_weight_defined = True if weight_provided else current["weight_defined"]
                next_status = status if status_provided else current["status"]
                next_imported_at = imported_at or current["imported_at"]

                if clear_labeled_at:
                    next_labeled_at = None
                elif labeled_at_provided:
                    next_labeled_at = labeled_at
                elif status_provided and next_status in {ACCEPTED, REJECTED}:
                    next_labeled_at = now
                else:
                    next_labeled_at = current["labeled_at"]

                next_locked = (
                    next_pinyin_locked if pinyin_locked_provided else current["pinyin_locked"]
                )
                connection.execute(
                    """
                    UPDATE entries
                    SET pinyin = ?, pinyin_locked = ?, weight = ?, weight_defined = ?,
                        status = ?, imported_at = ?, labeled_at = ?,
                        ai_label = ?, ai_score = ?, ai_labeled_at = ?, ai_model = ?, ai_prompt_version = ?
                    WHERE id = ?
                    """,
                    (
                        next_pinyin,
                        1 if next_locked else 0,
                        next_weight,
                        1 if next_weight_defined else 0,
                        next_status,
                        next_imported_at,
                        next_labeled_at,
                        current.get("ai_label"),
                        current.get("ai_score"),
                        current.get("ai_labeled_at"),
                        current.get("ai_model"),
                        current.get("ai_prompt_version"),
                        current["id"],
                    ),
                )

            connection.commit()

        if status_provided:
            self._invalidate_ai_training_examples_cache()
        updated_entries = [self.get_entry(entry_id) for entry_id in normalized_ids]
        return {
            "updated_count": len(normalized_ids),
            "entries": [entry for entry in updated_entries if entry is not None],
        }

    def bulk_update_derivatives(self, text: str, mode: str = "merge") -> dict[str, Any]:
        mode = str(mode or "merge").strip()
        if mode not in DERIVATIVE_BULK_MODES:
            raise ValueError("延伸词更新模式必须是 merge 或 overwrite。")

        mappings: dict[str, list[str]] = {}
        total_lines = 0
        skipped_invalid_count = 0
        invalid_lines: list[int] = []
        for line_number, raw_line in enumerate(str(text or "").splitlines(), 1):
            if line_number > DERIVATIVE_BULK_MAX_LINES:
                raise ValueError(f"一次最多导入 {DERIVATIVE_BULK_MAX_LINES} 行延伸词。")
            line = raw_line.strip()
            if not line:
                continue
            if len(line) > 20_000:
                skipped_invalid_count += 1
                if len(invalid_lines) < 20:
                    invalid_lines.append(line_number)
                continue
            total_lines += 1
            raw_parts = line.split("\t") if "\t" in line else DERIVATIVE_BULK_SPACE_FIELD_PATTERN.split(line)
            parts = [part.strip() for part in raw_parts if part.strip()]
            if len(parts) < 2:
                skipped_invalid_count += 1
                if len(invalid_lines) < 20:
                    invalid_lines.append(line_number)
                continue

            phrase = parts[0]
            derivatives = self._normalize_derivatives(parts[1:])
            derivatives = [
                item
                for item in derivatives
                if len(item) <= DERIVATIVE_BULK_MAX_ITEM_LENGTH
            ][:DERIVATIVE_BULK_MAX_DERIVATIVES_PER_PHRASE]
            if not phrase or not derivatives:
                skipped_invalid_count += 1
                if len(invalid_lines) < 20:
                    invalid_lines.append(line_number)
                continue

            if mode == "merge" and phrase in mappings:
                mappings[phrase] = self._normalize_derivatives([*mappings[phrase], *derivatives])
            else:
                mappings[phrase] = derivatives

        if total_lines == 0:
            raise ValueError("请先输入要更新的延伸词。")
        if not mappings:
            raise ValueError("没有可更新的延伸词行。")

        rows_by_phrase: dict[str, sqlite3.Row] = {}
        phrases = list(mappings)
        with self._managed_connection() as connection:
            for start in range(0, len(phrases), SQLITE_IN_MAX_VARIABLES):
                chunk = phrases[start : start + SQLITE_IN_MAX_VARIABLES]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT * FROM entries WHERE phrase IN ({placeholders})",
                    chunk,
                ).fetchall()
                rows_by_phrase.update({row["phrase"]: row for row in rows})

            updated_count = 0
            unchanged_count = 0
            missing_phrases: list[str] = []
            for phrase in phrases:
                row = rows_by_phrase.get(phrase)
                if row is None:
                    if len(missing_phrases) < 20:
                        missing_phrases.append(phrase)
                    continue

                current_derivatives = self._load_derivatives(row["derivatives"])
                incoming_derivatives = mappings[phrase]
                next_derivatives = (
                    self._normalize_derivatives([*current_derivatives, *incoming_derivatives])
                    if mode == "merge"
                    else incoming_derivatives
                )
                if next_derivatives == current_derivatives:
                    unchanged_count += 1
                    continue

                connection.execute(
                    "UPDATE entries SET derivatives = ? WHERE id = ?",
                    (json.dumps(next_derivatives, ensure_ascii=False), row["id"]),
                )
                updated_count += 1

            connection.commit()

        matched_count = len(rows_by_phrase)
        skipped_missing_count = len(phrases) - matched_count
        return {
            "mode": mode,
            "total_lines": total_lines,
            "valid_lines": len(mappings),
            "matched_count": matched_count,
            "updated_count": updated_count,
            "unchanged_count": unchanged_count,
            "skipped_missing_count": skipped_missing_count,
            "skipped_invalid_count": skipped_invalid_count,
            "missing_phrases": missing_phrases,
            "invalid_lines": invalid_lines,
        }

    def create_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed_keys = {
            "phrase",
            "pinyin",
            "pinyin_locked",
            "derivatives",
            "weight",
            "status",
            "imported_at",
            "labeled_at",
        }
        unexpected_keys = set(payload) - allowed_keys
        if unexpected_keys:
            raise ValueError(f"包含不支持的字段：{', '.join(sorted(unexpected_keys))}")

        phrase = str(payload.get("phrase", "")).strip()
        if not phrase:
            raise ValueError("词条不能为空。")

        pinyin = str(payload.get("pinyin", "") or "").strip()
        if not pinyin:
            pinyin = transliterate_phrase(phrase)

        pinyin_locked = self._coerce_bool(payload.get("pinyin_locked"))
        derivatives = self._normalize_derivatives(payload.get("derivatives"))

        raw_weight = payload.get("weight", "")
        if raw_weight is None or str(raw_weight).strip() == "":
            weight = 1
            weight_defined = False
        else:
            try:
                weight = int(str(raw_weight).strip())
            except ValueError as exc:
                raise ValueError("词频必须是整数。") from exc
            weight_defined = True

        status = str(payload.get("status", PENDING) or PENDING).strip()
        if status not in VALID_STATUSES:
            raise ValueError("无效状态。")

        imported_at = (
            self._normalize_datetime_input(
                payload.get("imported_at"),
                field_name="导入时间",
                allow_empty=False,
            )
            if "imported_at" in payload
            else self._now()
        )

        labeled_at_provided = "labeled_at" in payload
        if labeled_at_provided:
            labeled_at = self._normalize_datetime_input(
                payload.get("labeled_at"),
                field_name="标注时间",
                allow_empty=True,
            )
        elif status in {ACCEPTED, REJECTED}:
            labeled_at = self._now()
        else:
            labeled_at = None

        with self._managed_connection() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO entries (
                        phrase, pinyin, pinyin_locked, derivatives, weight, weight_defined,
                        status, imported_at, labeled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        phrase,
                        pinyin,
                        1 if pinyin_locked else 0,
                        json.dumps(derivatives, ensure_ascii=False),
                        weight,
                        1 if weight_defined else 0,
                        status,
                        imported_at,
                        labeled_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("词条已存在，无法新增重复词条。") from exc
            connection.commit()
            entry_id = int(cursor.lastrowid)

        if status in {ACCEPTED, REJECTED}:
            self._invalidate_ai_training_examples_cache()
        entry = self.get_entry(entry_id)
        if entry is None:  # pragma: no cover - guarded by insert
            raise LookupError("词条不存在。")
        return entry

    def update_entry(self, entry_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        allowed_keys = {
            "phrase",
            "pinyin",
            "pinyin_locked",
            "derivatives",
            "weight",
            "clear_weight",
            "status",
            "imported_at",
            "labeled_at",
        }
        unexpected_keys = set(updates) - allowed_keys
        if unexpected_keys:
            raise ValueError(f"包含不支持的字段：{', '.join(sorted(unexpected_keys))}")

        with self._managed_connection() as connection:
            existing = connection.execute(
                "SELECT * FROM entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("词条不存在。")

            current = self._row_to_entry(existing)
            phrase_changed = False

            phrase = current["phrase"]
            if "phrase" in updates:
                phrase = str(updates["phrase"]).strip()
                if not phrase:
                    raise ValueError("词条不能为空。")
                phrase_changed = phrase != current["phrase"]
                if phrase_changed:
                    duplicate = connection.execute(
                        "SELECT id FROM entries WHERE phrase = ? AND id != ?",
                        (phrase, entry_id),
                    ).fetchone()
                    if duplicate is not None:
                        raise ValueError("词条已存在，无法保存重复词条。")

            if "pinyin" in updates:
                pinyin = str(updates["pinyin"]).strip()
                if not pinyin:
                    pinyin = transliterate_phrase(phrase)
            elif phrase_changed:
                pinyin = transliterate_phrase(phrase)
            else:
                pinyin = current["pinyin"]

            if "pinyin_locked" in updates:
                pinyin_locked = self._coerce_bool(updates.get("pinyin_locked"))
            elif phrase_changed:
                pinyin_locked = False
            else:
                pinyin_locked = current["pinyin_locked"]

            derivatives = current["derivatives"]
            if "derivatives" in updates:
                derivatives = self._normalize_derivatives(updates["derivatives"])

            weight = current["weight"]
            weight_defined = current["weight_defined"]
            clear_weight = self._coerce_bool(updates.get("clear_weight")) if "clear_weight" in updates else False
            if clear_weight and "weight" in updates:
                raise ValueError("不能同时设置词频和删除词频。")
            if clear_weight:
                weight = 1
                weight_defined = False
            elif "weight" in updates:
                try:
                    weight = int(str(updates["weight"]).strip())
                except ValueError as exc:
                    raise ValueError("词频必须是整数。") from exc
                weight_defined = True

            status = current["status"]
            status_provided = "status" in updates
            if status_provided:
                status = str(updates["status"]).strip()
                if status not in VALID_STATUSES:
                    raise ValueError("无效状态。")

            imported_at = current["imported_at"]
            if "imported_at" in updates:
                imported_at = self._normalize_datetime_input(
                    updates["imported_at"],
                    field_name="导入时间",
                    allow_empty=False,
                )

            labeled_at = current["labeled_at"]
            labeled_at_provided = "labeled_at" in updates
            if labeled_at_provided:
                labeled_at = self._normalize_datetime_input(
                    updates["labeled_at"],
                    field_name="标注时间",
                    allow_empty=True,
                )
            elif status_provided and status in {ACCEPTED, REJECTED}:
                labeled_at = self._now()

            if phrase_changed:
                ai_label = None
                ai_score = None
                ai_labeled_at = None
                ai_model = None
                ai_prompt_version = None
            else:
                ai_label = current.get("ai_label")
                ai_score = current.get("ai_score")
                ai_labeled_at = current.get("ai_labeled_at")
                ai_model = current.get("ai_model")
                ai_prompt_version = current.get("ai_prompt_version")

            connection.execute(
                """
                UPDATE entries
                SET phrase = ?, pinyin = ?, pinyin_locked = ?, weight = ?, weight_defined = ?,
                    derivatives = ?, status = ?, imported_at = ?, labeled_at = ?,
                    ai_label = ?, ai_score = ?, ai_labeled_at = ?, ai_model = ?, ai_prompt_version = ?
                WHERE id = ?
                """,
                (
                    phrase,
                    pinyin,
                    1 if pinyin_locked else 0,
                    weight,
                    1 if weight_defined else 0,
                    json.dumps(derivatives, ensure_ascii=False),
                    status,
                    imported_at,
                    labeled_at,
                    ai_label,
                    ai_score,
                    ai_labeled_at,
                    ai_model,
                    ai_prompt_version,
                    entry_id,
                ),
            )

            connection.commit()

        if phrase_changed or status_provided:
            self._invalidate_ai_training_examples_cache()
        entry = self.get_entry(entry_id)
        if entry is None:  # pragma: no cover - guarded above
            raise LookupError("词条不存在。")
        return entry

    def list_entries(
        self,
        page: int = 1,
        page_size: int = 30,
        status: str | None = None,
        ai_status: str | None = None,
        query: str | None = None,
        min_weight: int | None = None,
        max_weight: int | None = None,
        has_derivatives: bool = False,
        pinyin_locked: bool = False,
    ) -> dict[str, Any]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)
        if min_weight is not None and max_weight is not None and min_weight > max_weight:
            raise ValueError("词频下限不能大于上限。")

        where_clauses: list[str] = []
        parameters: list[Any] = []

        if status and status != "all":
            if status not in VALID_STATUSES:
                raise ValueError("无效状态。")
            where_clauses.append("status = ?")
            parameters.append(status)

        if ai_status and ai_status != "all":
            if ai_status == "none":
                where_clauses.append("ai_label IS NULL")
            elif ai_status in VALID_STATUSES:
                where_clauses.append("ai_label = ?")
                parameters.append(ai_status)
            else:
                raise ValueError("无效 AI 标注状态。")

        if query:
            where_clauses.append("(phrase LIKE ? OR pinyin LIKE ?)")
            normalized_query = query.strip()
            keyword = f"%{normalized_query}%"
            parameters.extend([keyword, keyword])
        else:
            normalized_query = ""

        if min_weight is not None:
            where_clauses.append("weight >= ?")
            parameters.append(min_weight)

        if max_weight is not None:
            where_clauses.append("weight <= ?")
            parameters.append(max_weight)

        if has_derivatives:
            where_clauses.append("derivatives NOT IN ('', '[]')")

        if pinyin_locked:
            where_clauses.append("pinyin_locked = 1")

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        offset = (page - 1) * page_size

        with self._managed_connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS total FROM entries {where_sql}",
                parameters,
            ).fetchone()["total"]

            if normalized_query:
                order_sql = """
                ORDER BY
                    CASE
                        WHEN phrase = ? THEN 0
                        WHEN pinyin = ? THEN 1
                        ELSE 2
                    END,
                    id DESC
                """
                order_parameters: list[Any] = [normalized_query, normalized_query]
            else:
                order_sql = "ORDER BY id DESC"
                order_parameters = []

            rows = connection.execute(
                f"""
                SELECT *
                FROM entries
                {where_sql}
                {order_sql}
                LIMIT ? OFFSET ?
                """,
                [*parameters, *order_parameters, page_size, offset],
            ).fetchall()

        items = [self._row_to_entry(row) for row in rows]
        total_pages = max(1, math.ceil(total / page_size)) if total else 1

        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    def get_review_state(self, session_key: str = DEFAULT_REVIEW_SESSION) -> dict[str, Any]:
        with self._managed_connection() as connection:
            history_ids, pointer, _ = self._load_review_state(connection, session_key)
            mode = REVIEW_MODE_RANDOM
            history_ids, pointer, history_entries = self._resolve_review_history(connection, history_ids, pointer)
            self._save_review_state(connection, session_key, history_ids, pointer, REVIEW_MODE_RANDOM)
            current_entry = history_entries[pointer] if 0 <= pointer < len(history_entries) else None

        return {
            "history": history_entries,
            "pointer": pointer,
            "current_entry": current_entry,
            "can_go_back": self._review_can_go_back(pointer, len(history_entries)),
            "mode": mode,
        }

    def advance_review(
        self,
        session_key: str = DEFAULT_REVIEW_SESSION,
        prefer_ai: bool = False,
    ) -> dict[str, Any]:
        with self._managed_connection() as connection:
            history_ids, pointer, _ = self._load_review_state(connection, session_key)
            mode = REVIEW_MODE_RANDOM
            history_ids, pointer, history_entries = self._resolve_review_history(connection, history_ids, pointer)

            if pointer < len(history_entries) - 1:
                pointer += 1
            else:
                next_row = self._get_next_review_row(connection, history_ids, prefer_ai=prefer_ai)
                if next_row is not None:
                    next_id = next_row["id"]
                    if not history_ids or history_ids[-1] != next_id:
                        history_ids.append(next_id)
                    history_ids, pointer = self._trim_review_history(history_ids, len(history_ids) - 1)
                    history_ids, pointer, history_entries = self._resolve_review_history(
                        connection,
                        history_ids,
                        pointer,
                    )
                elif history_entries:
                    pointer = len(history_entries)
                else:
                    pointer = -1

            self._save_review_state(connection, session_key, history_ids, pointer, REVIEW_MODE_RANDOM)
            current_entry = history_entries[pointer] if 0 <= pointer < len(history_entries) else None

        return {
            "history": history_entries,
            "pointer": pointer,
            "current_entry": current_entry,
            "can_go_back": self._review_can_go_back(pointer, len(history_entries)),
            "mode": REVIEW_MODE_RANDOM,
        }

    def move_review_back(self, session_key: str = DEFAULT_REVIEW_SESSION) -> dict[str, Any]:
        with self._managed_connection() as connection:
            history_ids, pointer, _ = self._load_review_state(connection, session_key)
            history_ids, pointer, history_entries = self._resolve_review_history(connection, history_ids, pointer)

            if pointer == len(history_entries) and history_entries:
                pointer = len(history_entries) - 1
            elif pointer > 0:
                pointer -= 1

            self._save_review_state(connection, session_key, history_ids, pointer, REVIEW_MODE_RANDOM)
            current_entry = history_entries[pointer] if 0 <= pointer < len(history_entries) else None

        return {
            "history": history_entries,
            "pointer": pointer,
            "current_entry": current_entry,
            "can_go_back": self._review_can_go_back(pointer, len(history_entries)),
            "mode": REVIEW_MODE_RANDOM,
        }

    def preview_review_entries(
        self,
        count: int = 4,
        session_key: str = DEFAULT_REVIEW_SESSION,
    ) -> dict[str, Any]:
        count = max(0, min(int(count), 20))
        if count == 0:
            return {"entries": [], "mode": REVIEW_MODE_RANDOM}

        with self._managed_connection() as connection:
            history_ids, pointer, _ = self._load_review_state(connection, session_key)
            mode = REVIEW_MODE_RANDOM
            history_ids, pointer, history_entries = self._resolve_review_history(connection, history_ids, pointer)

            previews: list[dict[str, Any]] = []
            preview_history_ids = list(history_ids)

            if 0 <= pointer < len(history_entries) - 1:
                future_entries = history_entries[pointer + 1 : pointer + 1 + count]
                previews.extend(future_entries)

            while len(previews) < count:
                next_row = self._get_next_review_row(connection, preview_history_ids)
                if next_row is None:
                    break

                next_id = next_row["id"]
                if next_id in preview_history_ids:
                    break

                preview_history_ids.append(next_id)
                previews.append(self._row_to_entry(next_row))

        return {"entries": previews, "mode": mode}

    def set_review_mode(
        self,
        mode: str,
        session_key: str = DEFAULT_REVIEW_SESSION,
    ) -> dict[str, Any]:
        if mode not in VALID_REVIEW_MODES:
            raise ValueError("无效筛选模式。")
        mode = REVIEW_MODE_RANDOM

        with self._managed_connection() as connection:
            history_ids, pointer, _ = self._load_review_state(connection, session_key)
            history_ids, pointer, history_entries = self._resolve_review_history(connection, history_ids, pointer)
            self._save_review_state(connection, session_key, history_ids, pointer, mode)
            current_entry = history_entries[pointer] if 0 <= pointer < len(history_entries) else None

        return {
            "history": history_entries,
            "pointer": pointer,
            "current_entry": current_entry,
            "can_go_back": self._review_can_go_back(pointer, len(history_entries)),
            "mode": mode,
        }

    @staticmethod
    def _normalize_statuses(statuses: list[str]) -> list[str]:
        normalized = []
        for status in statuses:
            if status not in VALID_STATUSES:
                continue
            normalized.append(status)
        return normalized or list(DEFAULT_EXPORT_STATUSES)

    @staticmethod
    def normalize_export_dictionary_name(dictionary_name: str) -> str:
        normalized = EXPORT_DICTIONARY_NAME_UNSAFE_PATTERN.sub(
            "_",
            str(dictionary_name or "").strip(),
        ).strip("._-")
        return (normalized[:80] or DEFAULT_DICTIONARY_NAME)

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
        derivatives = WordService._load_derivatives(
            row["derivatives"] if "derivatives" in row.keys() else "[]"
        )
        return {
            "id": row["id"],
            "phrase": row["phrase"],
            "pinyin": row["pinyin"],
            "derivatives": derivatives,
            "derivatives_count": len(derivatives),
            "weight": row["weight"],
            "weight_defined": bool(row["weight_defined"]) if "weight_defined" in row.keys() else False,
            "pinyin_locked": bool(row["pinyin_locked"]) if "pinyin_locked" in row.keys() else False,
            "status": row["status"],
            "imported_at": row["imported_at"],
            "labeled_at": row["labeled_at"],
            "ai_label": row["ai_label"] if "ai_label" in row.keys() else None,
            "ai_score": row["ai_score"] if "ai_score" in row.keys() else None,
            "ai_labeled_at": row["ai_labeled_at"] if "ai_labeled_at" in row.keys() else None,
            "ai_model": row["ai_model"] if "ai_model" in row.keys() else None,
            "ai_prompt_version": row["ai_prompt_version"] if "ai_prompt_version" in row.keys() else None,
        }

    @staticmethod
    def _load_derivatives(raw_value: Any) -> list[str]:
        if isinstance(raw_value, list):
            return WordService._normalize_derivatives(raw_value)
        try:
            decoded = json.loads(str(raw_value or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return WordService._normalize_derivatives(decoded)

    @staticmethod
    def _normalize_derivatives(raw_value: Any) -> list[str]:
        if raw_value is None:
            raw_items: list[Any] = []
        elif isinstance(raw_value, str):
            raw_items = DERIVATIVE_SPLIT_PATTERN.split(raw_value)
        elif isinstance(raw_value, list):
            raw_items = raw_value
        else:
            raw_items = [raw_value]

        derivatives: list[str] = []
        seen: set[str] = set()
        for raw_item in raw_items:
            item = str(raw_item).strip()
            if not item or item in seen:
                continue
            seen.add(item)
            derivatives.append(item)
        return derivatives

    @staticmethod
    def _coerce_bool(raw_value: Any) -> bool:
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, (int, float)):
            return bool(raw_value)
        if isinstance(raw_value, str):
            return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return False

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _normalize_datetime_input(
        raw_value: Any,
        field_name: str,
        allow_empty: bool,
    ) -> str | None:
        if raw_value is None:
            if allow_empty:
                return None
            raise ValueError(f"{field_name}不能为空。")

        text = str(raw_value).strip()
        if not text:
            if allow_empty:
                return None
            raise ValueError(f"{field_name}不能为空。")

        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{field_name}格式不正确。") from exc

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)

        return parsed.isoformat(timespec="seconds")

    @staticmethod
    def _normalize_entry_ids(entry_ids: list[int]) -> list[int]:
        normalized: list[int] = []
        for raw_id in entry_ids:
            entry_id = int(raw_id)
            if entry_id not in normalized:
                normalized.append(entry_id)
        return normalized

    @classmethod
    def _sample_rows(
        cls,
        rows: list[sqlite3.Row],
        limit: int,
        seed: str,
        source: str = "human",
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        if len(rows) <= limit:
            selected = list(rows)
        else:
            selected = sorted(rows, key=lambda row: cls._stable_sample_key(row, seed))[:limit]
        return [
            {
                "phrase": str(row["phrase"]),
                "label": str(row["status"]),
                "score": 1.0 if str(row["status"]) == ACCEPTED else 0.0,
                "source": source,
                **(
                    {"previous_ai_label": str(row["ai_label"])}
                    if source == "human_ai_disagreement"
                    and "ai_label" in row.keys()
                    and row["ai_label"]
                    else {}
                ),
            }
            for row in selected
        ]

    @staticmethod
    def _ai_disagreement_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        return [
            row
            for row in rows
            if "ai_label" in row.keys()
            and row["ai_label"] in VALID_STATUSES
            and row["ai_label"] != row["status"]
        ]

    @staticmethod
    def _stable_sample_key(row: sqlite3.Row, seed: str) -> str:
        phrase = str(row["phrase"])
        status = str(row["status"])
        return hashlib.sha256(f"{seed}\0{status}\0{phrase}".encode("utf-8")).hexdigest()

    @staticmethod
    def _stable_shuffle(examples: list[dict[str, Any]], seed: str) -> list[dict[str, Any]]:
        return sorted(
            examples,
            key=lambda item: hashlib.sha256(
                f"{seed}\0{item.get('source', '')}\0{item.get('label', '')}\0{item.get('phrase', '')}".encode(
                    "utf-8"
                )
            ).hexdigest(),
        )

    @staticmethod
    def _get_ai_training_counts(connection: sqlite3.Connection) -> tuple[int, int]:
        row = connection.execute(
            """
            SELECT
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS accepted_count,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS rejected_count
            FROM entries
            """,
            (ACCEPTED, REJECTED),
        ).fetchone()
        return int(row["accepted_count"] or 0), int(row["rejected_count"] or 0)

    @staticmethod
    def _clone_ai_training_examples(examples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(example) for example in examples]

    def _invalidate_ai_training_examples_cache(self) -> None:
        with self._ai_training_examples_cache_lock:
            self._ai_training_examples_cache.clear()
            self._ai_training_examples_cache_version += 1

    @staticmethod
    def _ensure_setting(connection: sqlite3.Connection, key: str, default_value: str) -> None:
        connection.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (key, default_value),
        )

    @staticmethod
    def _get_settings(connection: sqlite3.Connection, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        rows = connection.execute(
            f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
            keys,
        ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    @staticmethod
    def _set_setting(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def _load_review_state(
        self,
        connection: sqlite3.Connection,
        session_key: str,
    ) -> tuple[list[int], int, str]:
        row = connection.execute(
            "SELECT history_json, pointer, mode FROM review_state WHERE session_key = ?",
            (session_key,),
        ).fetchone()

        if row is None:
            connection.execute(
                """
                INSERT INTO review_state (session_key, history_json, pointer, mode, updated_at)
                VALUES (?, '[]', -1, ?, ?)
                """,
                (session_key, REVIEW_MODE_RANDOM, self._now()),
            )
            connection.commit()
            return [], -1, REVIEW_MODE_RANDOM

        try:
            history_ids = [int(item) for item in json.loads(row["history_json"])]
        except (TypeError, ValueError, json.JSONDecodeError):
            history_ids = []

        pointer = int(row["pointer"])
        return history_ids, pointer, REVIEW_MODE_RANDOM

    def _save_review_state(
        self,
        connection: sqlite3.Connection,
        session_key: str,
        history_ids: list[int],
        pointer: int,
        mode: str,
        commit: bool = True,
    ) -> None:
        connection.execute(
            """
            INSERT INTO review_state (session_key, history_json, pointer, mode, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_key) DO UPDATE SET
                history_json = excluded.history_json,
                pointer = excluded.pointer,
                mode = excluded.mode,
                updated_at = excluded.updated_at
            """,
            (session_key, json.dumps(history_ids, ensure_ascii=False), pointer, mode, self._now()),
        )
        if commit:
            connection.commit()

    def _resolve_review_history(
        self,
        connection: sqlite3.Connection,
        history_ids: list[int],
        pointer: int,
    ) -> tuple[list[int], int, list[dict[str, Any]]]:
        history_ids = self._normalize_entry_ids(history_ids)
        if not history_ids:
            return [], -1, []

        placeholders = ",".join("?" for _ in history_ids)
        rows = connection.execute(
            f"SELECT * FROM entries WHERE id IN ({placeholders})",
            history_ids,
        ).fetchall()
        row_map = {row["id"]: self._row_to_entry(row) for row in rows}
        resolved_ids = [entry_id for entry_id in history_ids if entry_id in row_map]
        resolved_entries = [row_map[entry_id] for entry_id in resolved_ids]

        if not resolved_entries:
            return [], -1, []

        if pointer > len(resolved_entries):
            pointer = len(resolved_entries)
        if pointer < 0:
            pointer = len(resolved_entries) - 1

        return resolved_ids, pointer, resolved_entries

    @staticmethod
    def _trim_review_history(history_ids: list[int], pointer: int) -> tuple[list[int], int]:
        if len(history_ids) <= REVIEW_HISTORY_LIMIT:
            return history_ids, pointer

        overflow = len(history_ids) - REVIEW_HISTORY_LIMIT
        trimmed_ids = history_ids[overflow:]
        trimmed_pointer = max(-1, pointer - overflow)
        return trimmed_ids, trimmed_pointer

    def _get_next_review_row(
        self,
        connection: sqlite3.Connection,
        history_ids: list[int],
        prefer_ai: bool = False,
    ) -> sqlite3.Row | None:
        return self._get_random_pending_row(connection, history_ids, prefer_ai=prefer_ai)

    def _get_next_pending_row(
        self,
        connection: sqlite3.Connection,
        after_id: int | None = None,
    ) -> sqlite3.Row | None:
        if after_id is None:
            row = connection.execute(
                """
                SELECT *
                FROM entries
                WHERE status = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (PENDING,),
            ).fetchone()
            return row

        row = connection.execute(
            """
            SELECT *
            FROM entries
            WHERE status = ? AND id > ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (PENDING, after_id),
        ).fetchone()

        if row is None:
            row = connection.execute(
                """
                SELECT *
                FROM entries
                WHERE status = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (PENDING,),
            ).fetchone()

        return row

    def _get_random_pending_row(
        self,
        connection: sqlite3.Connection,
        exclude_ids: list[int],
        prefer_ai: bool = False,
    ) -> sqlite3.Row | None:
        conditions: list[tuple[str, list[Any]]] = []
        if prefer_ai:
            conditions.extend(
                [
                    ("status = ? AND ai_label = ?", [PENDING, PENDING]),
                    ("status = ? AND ai_label IN (?, ?)", [PENDING, ACCEPTED, REJECTED]),
                ]
            )
        else:
            conditions.append(("status = ? AND ai_label IS NULL", [PENDING]))

        conditions.append(("status = ?", [PENDING]))

        for condition_sql, condition_parameters in conditions:
            row = self._get_random_pending_row_for_condition(
                connection,
                exclude_ids,
                condition_sql,
                condition_parameters,
            )
            if row is not None:
                return row
        return None

    def _get_random_pending_row_for_condition(
        self,
        connection: sqlite3.Connection,
        exclude_ids: list[int],
        condition_sql: str,
        condition_parameters: list[Any],
    ) -> sqlite3.Row | None:
        bounds = connection.execute(
            f"""
            SELECT MIN(id) AS min_id, MAX(id) AS max_id
            FROM entries
            WHERE {condition_sql}
            """,
            condition_parameters,
        ).fetchone()
        if bounds is None or bounds["min_id"] is None or bounds["max_id"] is None:
            return None

        target_id = random.randint(bounds["min_id"], bounds["max_id"])
        row = self._find_pending_row_from(
            connection,
            target_id,
            exclude_ids,
            condition_sql,
            condition_parameters,
        )
        if row is None:
            row = self._find_pending_row_before(
                connection,
                target_id,
                exclude_ids,
                condition_sql,
                condition_parameters,
            )
        if row is None and exclude_ids:
            row = self._find_pending_row_from(
                connection,
                target_id,
                [],
                condition_sql,
                condition_parameters,
            )
            if row is None:
                row = self._find_pending_row_before(
                    connection,
                    target_id,
                    [],
                    condition_sql,
                    condition_parameters,
                )
        return row

    @staticmethod
    def _find_pending_row_from(
        connection: sqlite3.Connection,
        start_id: int,
        exclude_ids: list[int],
        condition_sql: str,
        condition_parameters: list[Any],
    ) -> sqlite3.Row | None:
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            return connection.execute(
                f"""
                SELECT *
                FROM entries
                WHERE {condition_sql} AND id >= ? AND id NOT IN ({placeholders})
                ORDER BY id ASC
                LIMIT 1
                """,
                [*condition_parameters, start_id, *exclude_ids],
            ).fetchone()

        return connection.execute(
            f"""
            SELECT *
            FROM entries
            WHERE {condition_sql} AND id >= ?
            ORDER BY id ASC
            LIMIT 1
            """,
            [*condition_parameters, start_id],
        ).fetchone()

    @staticmethod
    def _find_pending_row_before(
        connection: sqlite3.Connection,
        before_id: int,
        exclude_ids: list[int],
        condition_sql: str,
        condition_parameters: list[Any],
    ) -> sqlite3.Row | None:
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            return connection.execute(
                f"""
                SELECT *
                FROM entries
                WHERE {condition_sql} AND id < ? AND id NOT IN ({placeholders})
                ORDER BY id ASC
                LIMIT 1
                """,
                [*condition_parameters, before_id, *exclude_ids],
            ).fetchone()

        return connection.execute(
            f"""
            SELECT *
            FROM entries
            WHERE {condition_sql} AND id < ?
            ORDER BY id ASC
            LIMIT 1
            """,
            [*condition_parameters, before_id],
        ).fetchone()

    def _apply_status_update(
        self,
        connection: sqlite3.Connection,
        entry_id: int,
        status: str,
    ) -> None:
        existing = connection.execute(
            "SELECT id FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if existing is None:
            raise LookupError("词条不存在。")

        if status in {ACCEPTED, REJECTED}:
            connection.execute(
                """
                UPDATE entries
                SET status = ?, labeled_at = ?
                WHERE id = ?
                """,
                (status, self._now(), entry_id),
            )
        else:
            connection.execute(
                """
                UPDATE entries
                SET status = ?
                WHERE id = ?
                """,
                (status, entry_id),
            )

    @staticmethod
    def _review_can_go_back(pointer: int, history_length: int) -> bool:
        if history_length <= 0:
            return False
        return pointer > 0 or pointer == history_length
