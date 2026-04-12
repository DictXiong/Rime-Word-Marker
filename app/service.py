from __future__ import annotations

import json
import math
import random
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from app.constants import ACCEPTED, DEFAULT_EXPORT_STATUSES, PENDING, REJECTED, VALID_STATUSES
from app.pinyin_utils import transliterate_phrase

YAML_HEADER_PATTERN = re.compile(r"^[A-Za-z_][\w-]*\s*:")
DEFAULT_REVIEW_SESSION = "default"
REVIEW_HISTORY_LIMIT = 500
REVIEW_MODE_SEQUENTIAL = "sequential"
REVIEW_MODE_RANDOM = "random"
VALID_REVIEW_MODES = {REVIEW_MODE_SEQUENTIAL, REVIEW_MODE_RANDOM}
SQLITE_IN_MAX_VARIABLES = 900


class WordService:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
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
                    weight INTEGER NOT NULL DEFAULT 1,
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
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(review_state)").fetchall()
            }
            if "mode" not in columns:
                connection.execute(
                    "ALTER TABLE review_state ADD COLUMN mode TEXT NOT NULL DEFAULT 'sequential'"
                )
                connection.commit()

    def import_text(self, raw_text: str) -> dict[str, Any]:
        inserted = 0
        skipped = 0
        parsed = 0
        accepted_existing = 0
        rejected_existing = 0
        imported_at = self._now()
        in_yaml_header = False
        parsed_entries: list[tuple[int, str, str, int]] = []

        for line_number, raw_line in enumerate(raw_text.splitlines(), start=1):
            stripped = raw_line.strip("\ufeff").strip()
            if stripped == "---":
                in_yaml_header = True
                continue
            if in_yaml_header:
                if stripped == "...":
                    in_yaml_header = False
                continue

            parsed_line = self._parse_import_line(raw_line)
            if parsed_line is None:
                continue

            parsed += 1
            phrase, pinyin, weight = parsed_line
            parsed_entries.append((line_number, phrase, pinyin, weight))

        with self._managed_connection() as connection:
            cursor = connection.cursor()
            known_statuses = self._fetch_entry_statuses(
                connection,
                [phrase for _, phrase, _, _ in parsed_entries],
            )

            for line_number, phrase, pinyin, weight in parsed_entries:
                try:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO entries (
                            phrase, pinyin, weight, status, imported_at, labeled_at
                        ) VALUES (?, ?, ?, ?, ?, NULL)
                        """,
                        (phrase, pinyin, weight, PENDING, imported_at),
                    )
                except RuntimeError:
                    raise
                except Exception as exc:  # pragma: no cover - defensive API path
                    raise ValueError(f"第 {line_number} 行导入失败：{exc}") from exc

                if cursor.rowcount == 1:
                    inserted += 1
                    known_statuses[phrase] = PENDING
                else:
                    skipped += 1
                    status = known_statuses.get(phrase)
                    if status == ACCEPTED:
                        accepted_existing += 1
                    elif status == REJECTED:
                        rejected_existing += 1

            connection.commit()

        return {
            "parsed": parsed,
            "inserted": inserted,
            "skipped": skipped,
            "accepted_existing": accepted_existing,
            "rejected_existing": rejected_existing,
            "imported_at": imported_at,
        }

    def _parse_import_line(self, raw_line: str) -> tuple[str, str, int] | None:
        line = raw_line.strip("\ufeff").rstrip("\n\r")
        stripped = line.strip()
        if not stripped:
            return None

        if stripped.startswith("#") or stripped in {"---", "..."}:
            return None

        if "\t" not in stripped and YAML_HEADER_PATTERN.match(stripped):
            return None

        parts = [part.strip() for part in line.split("\t")]
        phrase = parts[0].strip()
        if not phrase:
            return None

        pinyin = parts[1] if len(parts) > 1 and parts[1] else transliterate_phrase(phrase)
        weight = self._parse_weight(parts[2]) if len(parts) > 2 else 1
        return phrase, pinyin, weight

    @staticmethod
    def _parse_weight(raw_weight: str) -> int:
        try:
            return int(raw_weight.strip())
        except (TypeError, ValueError):
            return 1

    def export_dictionary(
        self,
        statuses: list[str] | None = None,
        include_weight: bool = False,
        dictionary_name: str = "rime_word_marker_export",
    ) -> str:
        return "".join(
            self.iter_export_dictionary_lines(
                statuses=statuses,
                include_weight=include_weight,
                dictionary_name=dictionary_name,
            )
        )

    def iter_export_dictionary_lines(
        self,
        statuses: list[str] | None = None,
        include_weight: bool = False,
        dictionary_name: str = "rime_word_marker_export",
    ):
        statuses = self._normalize_statuses(statuses or DEFAULT_EXPORT_STATUSES)
        placeholders = ",".join("?" for _ in statuses)
        header_lines = [
            "# Rime dictionary generated by Rime Word Marker\n",
            "---\n",
            f"name: {dictionary_name}\n",
            f'version: "{self._now()}"\n',
            "sort: by_weight\n",
            "...\n",
        ]
        for line in header_lines:
            yield line

        with self._managed_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT phrase, pinyin, weight
                FROM entries
                WHERE status IN ({placeholders})
                ORDER BY weight DESC, id ASC
                """,
                statuses,
            )
            for row in rows:
                columns = [row["phrase"], row["pinyin"]]
                if include_weight:
                    columns.append(str(row["weight"]))
                yield "\t".join(columns) + "\n"

    def count_export_entries(self, statuses: list[str] | None = None) -> int:
        normalized_statuses = self._normalize_statuses(statuses or DEFAULT_EXPORT_STATUSES)
        placeholders = ",".join("?" for _ in normalized_statuses)

        with self._managed_connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM entries
                WHERE status IN ({placeholders})
                """,
                normalized_statuses,
            ).fetchone()

        return int(row["total"]) if row else 0

    @staticmethod
    def _fetch_entry_statuses(
        connection: sqlite3.Connection,
        phrases: list[str],
    ) -> dict[str, str]:
        unique_phrases = list(dict.fromkeys(phrase for phrase in phrases if phrase))
        if not unique_phrases:
            return {}

        statuses: dict[str, str] = {}
        for start in range(0, len(unique_phrases), SQLITE_IN_MAX_VARIABLES):
            chunk = unique_phrases[start : start + SQLITE_IN_MAX_VARIABLES]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"SELECT phrase, status FROM entries WHERE phrase IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                statuses[row["phrase"]] = row["status"]
        return statuses

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

    def update_status(self, entry_id: int, status: str) -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise ValueError("无效状态。")

        with self._managed_connection() as connection:
            self._apply_status_update(connection, entry_id, status)
            connection.commit()

        entry = self.get_entry(entry_id)
        if entry is None:  # pragma: no cover - guarded above
            raise LookupError("词条不存在。")
        return entry

    def label_and_advance(
        self,
        entry_id: int,
        status: str,
        session_key: str = DEFAULT_REVIEW_SESSION,
    ) -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise ValueError("无效状态。")

        with self._managed_connection() as connection:
            self._apply_status_update(connection, entry_id, status)

            history_ids, pointer, mode = self._load_review_state(connection, session_key)
            history_ids, pointer, history_entries = self._resolve_review_history(connection, history_ids, pointer)

            if pointer < len(history_entries) - 1:
                pointer += 1
            else:
                next_row = self._get_next_review_row(connection, mode, history_ids)
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

            self._save_review_state(connection, session_key, history_ids, pointer, mode, commit=False)
            connection.commit()

            current_entry = history_entries[pointer] if 0 <= pointer < len(history_entries) else None
            updated_entry = self._row_to_entry(
                connection.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
            )

        return {
            "history": history_entries,
            "pointer": pointer,
            "current_entry": current_entry,
            "can_go_back": self._review_can_go_back(pointer, len(history_entries)),
            "mode": mode,
            "updated_entry": updated_entry,
        }

    def batch_update_entries(
        self,
        entry_ids: list[int],
        updates: dict[str, Any],
        regenerate_pinyin: bool = False,
        clear_labeled_at: bool = False,
    ) -> dict[str, Any]:
        allowed_keys = {"pinyin", "weight", "status", "imported_at", "labeled_at"}
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

            for row in rows:
                current = self._row_to_entry(row)
                next_pinyin = current["pinyin"]
                if regenerate_pinyin:
                    next_pinyin = transliterate_phrase(current["phrase"])
                elif pinyin_provided:
                    next_pinyin = manual_pinyin or transliterate_phrase(current["phrase"])

                next_weight = weight if weight is not None else current["weight"]
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

                connection.execute(
                    """
                    UPDATE entries
                    SET pinyin = ?, weight = ?, status = ?, imported_at = ?, labeled_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_pinyin,
                        next_weight,
                        next_status,
                        next_imported_at,
                        next_labeled_at,
                        current["id"],
                    ),
                )

            connection.commit()

        updated_entries = [self.get_entry(entry_id) for entry_id in normalized_ids]
        return {
            "updated_count": len(normalized_ids),
            "entries": [entry for entry in updated_entries if entry is not None],
        }

    def update_entry(self, entry_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        allowed_keys = {"phrase", "pinyin", "weight", "status", "imported_at", "labeled_at"}
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

            weight = current["weight"]
            if "weight" in updates:
                try:
                    weight = int(str(updates["weight"]).strip())
                except ValueError as exc:
                    raise ValueError("词频必须是整数。") from exc

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

            connection.execute(
                """
                UPDATE entries
                SET phrase = ?, pinyin = ?, weight = ?, status = ?, imported_at = ?, labeled_at = ?
                WHERE id = ?
                """,
                (phrase, pinyin, weight, status, imported_at, labeled_at, entry_id),
            )

            connection.commit()

        entry = self.get_entry(entry_id)
        if entry is None:  # pragma: no cover - guarded above
            raise LookupError("词条不存在。")
        return entry

    def list_entries(
        self,
        page: int = 1,
        page_size: int = 30,
        status: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)

        where_clauses: list[str] = []
        parameters: list[Any] = []

        if status and status != "all":
            if status not in VALID_STATUSES:
                raise ValueError("无效状态。")
            where_clauses.append("status = ?")
            parameters.append(status)

        if query:
            where_clauses.append("(phrase LIKE ? OR pinyin LIKE ?)")
            keyword = f"%{query.strip()}%"
            parameters.extend([keyword, keyword])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        offset = (page - 1) * page_size

        with self._managed_connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS total FROM entries {where_sql}",
                parameters,
            ).fetchone()["total"]

            rows = connection.execute(
                f"""
                SELECT *
                FROM entries
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                [*parameters, page_size, offset],
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
            history_ids, pointer, mode = self._load_review_state(connection, session_key)
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

    def advance_review(self, session_key: str = DEFAULT_REVIEW_SESSION) -> dict[str, Any]:
        with self._managed_connection() as connection:
            history_ids, pointer, mode = self._load_review_state(connection, session_key)
            history_ids, pointer, history_entries = self._resolve_review_history(connection, history_ids, pointer)

            if pointer < len(history_entries) - 1:
                pointer += 1
            else:
                next_row = self._get_next_review_row(connection, mode, history_ids)
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

            self._save_review_state(connection, session_key, history_ids, pointer, mode)
            current_entry = history_entries[pointer] if 0 <= pointer < len(history_entries) else None

        return {
            "history": history_entries,
            "pointer": pointer,
            "current_entry": current_entry,
            "can_go_back": self._review_can_go_back(pointer, len(history_entries)),
            "mode": mode,
        }

    def move_review_back(self, session_key: str = DEFAULT_REVIEW_SESSION) -> dict[str, Any]:
        with self._managed_connection() as connection:
            history_ids, pointer, mode = self._load_review_state(connection, session_key)
            history_ids, pointer, history_entries = self._resolve_review_history(connection, history_ids, pointer)

            if pointer == len(history_entries) and history_entries:
                pointer = len(history_entries) - 1
            elif pointer > 0:
                pointer -= 1

            self._save_review_state(connection, session_key, history_ids, pointer, mode)
            current_entry = history_entries[pointer] if 0 <= pointer < len(history_entries) else None

        return {
            "history": history_entries,
            "pointer": pointer,
            "current_entry": current_entry,
            "can_go_back": self._review_can_go_back(pointer, len(history_entries)),
            "mode": mode,
        }

    def preview_review_entries(
        self,
        count: int = 4,
        session_key: str = DEFAULT_REVIEW_SESSION,
    ) -> dict[str, Any]:
        count = max(0, min(int(count), 20))
        if count == 0:
            return {"entries": [], "mode": REVIEW_MODE_SEQUENTIAL}

        with self._managed_connection() as connection:
            history_ids, pointer, mode = self._load_review_state(connection, session_key)
            history_ids, pointer, history_entries = self._resolve_review_history(connection, history_ids, pointer)

            previews: list[dict[str, Any]] = []
            preview_history_ids = list(history_ids)

            if 0 <= pointer < len(history_entries) - 1:
                future_entries = history_entries[pointer + 1 : pointer + 1 + count]
                previews.extend(future_entries)

            while len(previews) < count:
                next_row = self._get_next_review_row(connection, mode, preview_history_ids)
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
    def _row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "phrase": row["phrase"],
            "pinyin": row["pinyin"],
            "weight": row["weight"],
            "status": row["status"],
            "imported_at": row["imported_at"],
            "labeled_at": row["labeled_at"],
        }

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
                (session_key, REVIEW_MODE_SEQUENTIAL, self._now()),
            )
            connection.commit()
            return [], -1, REVIEW_MODE_SEQUENTIAL

        try:
            history_ids = [int(item) for item in json.loads(row["history_json"])]
        except (TypeError, ValueError, json.JSONDecodeError):
            history_ids = []

        pointer = int(row["pointer"])
        mode = row["mode"] if row["mode"] in VALID_REVIEW_MODES else REVIEW_MODE_SEQUENTIAL
        return history_ids, pointer, mode

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
        mode: str,
        history_ids: list[int],
    ) -> sqlite3.Row | None:
        if mode == REVIEW_MODE_RANDOM:
            return self._get_random_pending_row(connection, history_ids)

        after_id = history_ids[-1] if history_ids else None
        return self._get_next_pending_row(connection, after_id)

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
    ) -> sqlite3.Row | None:
        bounds = connection.execute(
            """
            SELECT MIN(id) AS min_id, MAX(id) AS max_id
            FROM entries
            WHERE status = ?
            """,
            (PENDING,),
        ).fetchone()
        if bounds is None or bounds["min_id"] is None or bounds["max_id"] is None:
            return None

        target_id = random.randint(bounds["min_id"], bounds["max_id"])
        row = self._find_pending_row_from(connection, target_id, exclude_ids)
        if row is None:
            row = self._find_pending_row_before(connection, target_id, exclude_ids)
        if row is None and exclude_ids:
            row = self._find_pending_row_from(connection, target_id, [])
            if row is None:
                row = self._find_pending_row_before(connection, target_id, [])
        return row

    @staticmethod
    def _find_pending_row_from(
        connection: sqlite3.Connection,
        start_id: int,
        exclude_ids: list[int],
    ) -> sqlite3.Row | None:
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            return connection.execute(
                f"""
                SELECT *
                FROM entries
                WHERE status = ? AND id >= ? AND id NOT IN ({placeholders})
                ORDER BY id ASC
                LIMIT 1
                """,
                [PENDING, start_id, *exclude_ids],
            ).fetchone()

        return connection.execute(
            """
            SELECT *
            FROM entries
            WHERE status = ? AND id >= ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (PENDING, start_id),
        ).fetchone()

    @staticmethod
    def _find_pending_row_before(
        connection: sqlite3.Connection,
        before_id: int,
        exclude_ids: list[int],
    ) -> sqlite3.Row | None:
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            return connection.execute(
                f"""
                SELECT *
                FROM entries
                WHERE status = ? AND id < ? AND id NOT IN ({placeholders})
                ORDER BY id ASC
                LIMIT 1
                """,
                [PENDING, before_id, *exclude_ids],
            ).fetchone()

        return connection.execute(
            """
            SELECT *
            FROM entries
            WHERE status = ? AND id < ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (PENDING, before_id),
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
