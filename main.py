from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.ai import (
    AIAnnotationWorker,
    AIConfig,
    DEFAULT_AI_BATCH_SIZE,
    DEFAULT_AI_CANDIDATE_MODE,
    DEFAULT_AI_EXAMPLES_PER_CLASS,
    DEFAULT_AI_MAX_TOKENS,
    DEFAULT_AI_TIMEOUT,
    MAX_AI_BATCH_SIZE,
    MAX_AI_MAX_TOKENS,
    VALID_AI_CANDIDATE_MODES,
    estimate_ai_max_tokens,
)
from app.service import DEFAULT_REVIEW_SESSION, WordService
from app.pinyin_utils import transliterate_phrase

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "words.db"
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
SERVICE: WordService | None = None
AI_WORKER: AIAnnotationWorker | None = None
VERBOSE = False
PAGE_ROUTES = {
    "/": "/index.html",
    "/review": "/review.html",
    "/import": "/import.html",
    "/manage": "/manage.html",
}

ENTRY_DETAIL_RE = re.compile(r"^/api/entries/(\d+)$")
ENTRY_STATUS_RE = re.compile(r"^/api/entries/(\d+)/status$")
ENTRY_UPDATE_RE = re.compile(r"^/api/entries/(\d+)/update$")
SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=directory or str(STATIC_DIR), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api_get(parsed)
            return

        if parsed.path in PAGE_ROUTES:
            self.path = PAGE_ROUTES[parsed.path]

        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._send_json(404, {"error": "未找到接口。"})
            return

        self._handle_api_post(parsed)

    def _handle_api_get(self, parsed) -> None:
        try:
            if parsed.path == "/api/health":
                self._send_json(200, {"status": "ok"})
                return

            if parsed.path == "/api/stats":
                self._send_json(200, _service().get_stats())
                return

            if parsed.path == "/api/ai/overview":
                self._send_json(200, _service().get_ai_overview(**_ai_overview_context()))
                return

            if parsed.path == "/api/pinyin":
                query = parse_qs(parsed.query)
                phrase = query.get("phrase", [""])[0].strip()
                if not phrase:
                    self._send_json(400, {"error": "词条不能为空。"})
                    return
                self._send_json(200, {"phrase": phrase, "pinyin": transliterate_phrase(phrase)})
                return

            if parsed.path == "/api/review/state":
                review_state = _service().get_review_state(self._review_session_key())
                self._send_json(200, {**review_state, "stats": _service().get_stats()})
                return

            if parsed.path == "/api/review/prefetch":
                query = parse_qs(parsed.query)
                count = _to_int(query.get("count", ["4"])[0], default=4)
                preview = _service().preview_review_entries(
                    count=count,
                    session_key=self._review_session_key(),
                )
                self._send_json(200, preview)
                return

            if parsed.path == "/api/review/next":
                query = parse_qs(parsed.query)
                review_state = _service().advance_review(
                    self._review_session_key(),
                    prefer_ai=_load_bool(query.get("prefer_ai", ["0"])[0], False),
                )
                self._send_json(200, {**review_state, "stats": _service().get_stats()})
                return

            if parsed.path == "/api/entries":
                query = parse_qs(parsed.query)
                page = _to_int(query.get("page", ["1"])[0], default=1)
                page_size = _to_int(query.get("page_size", ["30"])[0], default=30)
                status = query.get("status", ["all"])[0]
                ai_status = query.get("ai_status", ["all"])[0]
                keyword = query.get("q", [""])[0]
                min_weight = _to_int(query.get("min_weight", [""])[0], default=None)
                max_weight = _to_int(query.get("max_weight", [""])[0], default=None)
                payload = _service().list_entries(
                    page=page,
                    page_size=page_size,
                    status=status,
                    ai_status=ai_status,
                    query=keyword,
                    min_weight=min_weight,
                    max_weight=max_weight,
                )
                self._send_json(200, payload)
                return

            if parsed.path == "/api/export":
                query = parse_qs(parsed.query)
                raw_statuses = query.get("statuses", ["accepted"])
                statuses = []
                for value in raw_statuses:
                    statuses.extend(item for item in value.split(",") if item)

                include_weight = query.get("include_weight", ["0"])[0] == "1"
                include_ai_assist = query.get("include_ai_assist", ["0"])[0] == "1"
                dictionary_name = WordService.normalize_export_dictionary_name(
                    query.get("name", ["rime_word_marker_export"])[0]
                )
                filename = f"{dictionary_name}.dict.yaml"
                self.send_response(200)
                self.send_header("Content-Type", "application/x-yaml; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{filename}"',
                )
                self.end_headers()
                for line in _service().iter_export_dictionary_lines(
                    statuses=statuses,
                    include_weight=include_weight,
                    include_ai_assist=include_ai_assist,
                    dictionary_name=dictionary_name,
                ):
                    self.wfile.write(line.encode("utf-8"))
                return

            if parsed.path == "/api/export/count":
                query = parse_qs(parsed.query)
                raw_statuses = query.get("statuses", ["accepted"])
                statuses = []
                for value in raw_statuses:
                    statuses.extend(item for item in value.split(",") if item)
                include_ai_assist = query.get("include_ai_assist", ["0"])[0] == "1"
                self._send_json(
                    200,
                    {
                        "count": _service().count_export_entries(
                            statuses=statuses,
                            include_ai_assist=include_ai_assist,
                        )
                    },
                )
                return

            match = ENTRY_DETAIL_RE.match(parsed.path)
            if match:
                entry = _service().get_entry(int(match.group(1)))
                if entry is None:
                    self._send_json(404, {"error": "词条不存在。"})
                    return
                self._send_json(200, entry)
                return

            self._send_json(404, {"error": "未找到接口。"})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive server path
            _log(f"[server] GET {parsed.path} failed {type(exc).__name__}: {exc}")
            self._send_json(500, {"error": f"服务器异常：{exc}"})

    def _handle_api_post(self, parsed) -> None:
        try:
            if parsed.path == "/api/import-file":
                query = parse_qs(parsed.query)
                overwrite_pinyin = _load_bool(query.get("overwrite_pinyin", ["0"])[0], False)
                overwrite_weight = _load_bool(query.get("overwrite_weight", ["1"])[0], True)
                mark_accepted = _load_bool(query.get("mark_accepted", ["0"])[0], False)
                ignore_pinyin = _load_bool(query.get("ignore_pinyin", ["0"])[0], False)
                raw_body = self._read_raw_body()
                if not raw_body.strip():
                    self._send_json(400, {"error": "导入文件不能为空。"})
                    return
                try:
                    text = raw_body.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise ValueError("导入文件不是合法的 UTF-8 文本。") from exc
                result = _service().import_text(
                    text,
                    overwrite_pinyin=overwrite_pinyin,
                    overwrite_weight=overwrite_weight,
                    mark_accepted=mark_accepted,
                    ignore_pinyin=ignore_pinyin,
                )
                _verbose_log_json(
                    "import-file",
                    {
                        "bytes": len(raw_body),
                        "chars": len(text),
                        "overwrite_pinyin": overwrite_pinyin,
                        "overwrite_weight": overwrite_weight,
                        "mark_accepted": mark_accepted,
                        "ignore_pinyin": ignore_pinyin,
                        "result": result,
                    },
                )
                self._send_json(200, {"result": result, "stats": _service().get_stats()})
                return

            payload = self._read_json_body()

            if parsed.path == "/api/import":
                text = payload.get("text", "")
                if not text.strip():
                    self._send_json(400, {"error": "导入内容不能为空。"})
                    return
                overwrite_pinyin = _load_bool(payload.get("overwrite_pinyin"), False)
                overwrite_weight = _load_bool(payload.get("overwrite_weight"), True)
                mark_accepted = _load_bool(payload.get("mark_accepted"), False)
                ignore_pinyin = _load_bool(payload.get("ignore_pinyin"), False)
                result = _service().import_text(
                    text,
                    overwrite_pinyin=overwrite_pinyin,
                    overwrite_weight=overwrite_weight,
                    mark_accepted=mark_accepted,
                    ignore_pinyin=ignore_pinyin,
                )
                _verbose_log_json(
                    "import-text",
                    {
                        "chars": len(text),
                        "overwrite_pinyin": overwrite_pinyin,
                        "overwrite_weight": overwrite_weight,
                        "mark_accepted": mark_accepted,
                        "ignore_pinyin": ignore_pinyin,
                        "result": result,
                    },
                )
                self._send_json(200, {"result": result, "stats": _service().get_stats()})
                return

            if parsed.path == "/api/maintenance/recompute-toneless-pinyin":
                result = _service().recompute_toneless_pinyin()
                _verbose_log_json("maintenance-recompute-toneless-pinyin", result)
                self._send_json(200, {"result": result, "stats": _service().get_stats()})
                return

            if parsed.path == "/api/review/next":
                review_state = _service().advance_review(
                    self._review_session_key(),
                    prefer_ai=bool(payload.get("prefer_ai")),
                )
                self._send_json(200, {**review_state, "stats": _service().get_stats()})
                return

            if parsed.path == "/api/review/back":
                review_state = _service().move_review_back(self._review_session_key())
                self._send_json(200, {**review_state, "stats": _service().get_stats()})
                return

            if parsed.path == "/api/review/mode":
                review_state = _service().set_review_mode(
                    payload.get("mode", ""),
                    self._review_session_key(),
                )
                _verbose_log_json(
                    "review-mode-updated",
                    {"session": self._review_session_key(), "mode": review_state.get("mode")},
                )
                self._send_json(200, {**review_state, "stats": _service().get_stats()})
                return

            if parsed.path == "/api/review/label":
                entry_id = int(payload.get("entry_id", 0))
                status = payload.get("status", "")
                review_state = _service().label_and_advance(
                    entry_id,
                    status,
                    self._review_session_key(),
                    prefer_ai=bool(payload.get("prefer_ai")),
                )
                _verbose_log_json(
                    "review-label-updated",
                    {
                        "session": self._review_session_key(),
                        "entry_id": entry_id,
                        "status": status,
                        "updated_entry": _compact_entry(review_state.get("updated_entry")),
                        "next_entry": _compact_entry(review_state.get("current_entry")),
                    },
                )
                self._send_json(200, {**review_state, "stats": _service().get_stats()})
                return

            if parsed.path == "/api/ai/toggle":
                overview_context = _ai_overview_context()
                overview, message = _service().set_ai_enabled(
                    bool(payload.get("enabled")),
                    configured=overview_context.get("configured", False),
                    model_name=overview_context.get("model_name"),
                    prompt_version=overview_context.get("prompt_version"),
                )
                if _ai_worker():
                    _ai_worker().wake()
                _verbose_log_json(
                    "ai-toggle-updated",
                    {"enabled": bool(payload.get("enabled")), "message": message, "overview": overview},
                )
                self._send_json(200, {"overview": overview, "message": message})
                return

            if parsed.path == "/api/entries/bulk-update":
                result = _service().batch_update_entries(
                    payload.get("ids", []),
                    payload.get("updates", {}),
                    regenerate_pinyin=bool(payload.get("regenerate_pinyin")),
                    clear_labeled_at=bool(payload.get("clear_labeled_at")),
                )
                _verbose_log_json(
                    "entries-bulk-updated",
                    {
                        "ids": payload.get("ids", []),
                        "updates": payload.get("updates", {}),
                        "regenerate_pinyin": bool(payload.get("regenerate_pinyin")),
                        "clear_labeled_at": bool(payload.get("clear_labeled_at")),
                        "updated_count": result.get("updated_count"),
                    },
                )
                self._send_json(200, {**result, "stats": _service().get_stats()})
                return

            match = ENTRY_STATUS_RE.match(parsed.path)
            if match:
                entry_id = int(match.group(1))
                status = payload.get("status", "")
                entry = _service().update_status(
                    entry_id,
                    status,
                )
                _verbose_log_json(
                    "entry-status-updated",
                    {"entry_id": entry_id, "status": status, "entry": _compact_entry(entry)},
                )
                self._send_json(200, {"entry": entry, "stats": _service().get_stats()})
                return

            match = ENTRY_UPDATE_RE.match(parsed.path)
            if match:
                entry_id = int(match.group(1))
                entry = _service().update_entry(
                    entry_id,
                    payload,
                )
                _verbose_log_json(
                    "entry-updated",
                    {"entry_id": entry_id, "updates": payload, "entry": _compact_entry(entry)},
                )
                self._send_json(200, {"entry": entry, "stats": _service().get_stats()})
                return

            self._send_json(404, {"error": "未找到接口。"})
        except json.JSONDecodeError:
            self._send_json(400, {"error": "请求体不是合法 JSON。"})
        except LookupError as exc:
            self._send_json(404, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive server path
            _log(f"[server] POST {parsed.path} failed {type(exc).__name__}: {exc}")
            self._send_json(500, {"error": f"服务器异常：{exc}"})

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        if not raw_body:
            return {}
        return json.loads(raw_body.decode("utf-8"))

    def _read_raw_body(self) -> bytes:
        content_length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(content_length) if content_length else b""

    def _send_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _review_session_key(self) -> str:
        raw_value = self.headers.get("X-Review-Session", "").strip()
        if raw_value and SESSION_KEY_RE.fullmatch(raw_value):
            return raw_value
        return DEFAULT_REVIEW_SESSION

    def log_message(self, format: str, *args) -> None:
        return


def _to_int(raw_value, default: int | None = None) -> int | None:
    if raw_value in (None, ""):
        return default
    return int(raw_value)


def _service() -> WordService:
    if SERVICE is None:  # pragma: no cover - guarded by main()
        raise RuntimeError("服务尚未初始化。")
    return SERVICE


def _ai_worker() -> AIAnnotationWorker | None:
    return AI_WORKER


def _ai_overview_context() -> dict:
    worker = _ai_worker()
    if worker is None:
        return {}
    descriptor = worker.describe()
    return {
        "configured": descriptor["configured"],
        "model_name": descriptor["model"],
        "prompt_version": descriptor["prompt_version"],
    }


def _verbose_log_json(title: str, payload: dict) -> None:
    if not VERBOSE:
        return
    print(f"[{_log_timestamp()}] [verbose] {title}:", flush=True)
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


def _log(message: str) -> None:
    print(f"[{_log_timestamp()}] {message}", flush=True)


def _log_timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _compact_entry(entry) -> dict | None:
    if not isinstance(entry, dict):
        return None
    return {
        "id": entry.get("id"),
        "phrase": entry.get("phrase"),
        "status": entry.get("status"),
        "pinyin": entry.get("pinyin"),
        "ai_label": entry.get("ai_label"),
        "ai_score": entry.get("ai_score"),
        "ai_prompt_version": entry.get("ai_prompt_version"),
    }


def _load_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}

    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise ValueError("配置文件必须是一个 JSON 对象。")
    return data


def _resolve_path(raw_path: str | None, base_dir: Path) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _load_bool(raw_value, default: bool = False) -> bool:
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _load_ai_config(config: dict, verbose: bool = False) -> AIConfig:
    raw_ai = config.get("ai", {})
    if not isinstance(raw_ai, dict):
        raw_ai = {}

    endpoint = str(raw_ai.get("endpoint", raw_ai.get("base_url", ""))).strip()
    api_key = str(raw_ai.get("api_key", "")).strip()
    model = str(raw_ai.get("model", "")).strip()
    timeout = max(5, int(raw_ai.get("timeout", DEFAULT_AI_TIMEOUT) or DEFAULT_AI_TIMEOUT))
    batch_size = max(
        1,
        min(
            MAX_AI_BATCH_SIZE,
            int(raw_ai.get("batch_size", DEFAULT_AI_BATCH_SIZE) or DEFAULT_AI_BATCH_SIZE),
        ),
    )
    examples_per_class = max(
        1,
        min(
            1024,
            int(
                raw_ai.get("examples_per_class", DEFAULT_AI_EXAMPLES_PER_CLASS)
                or DEFAULT_AI_EXAMPLES_PER_CLASS
            ),
        ),
    )
    raw_max_tokens = raw_ai.get("max_tokens")
    if raw_max_tokens is None or (
        isinstance(raw_max_tokens, str) and not raw_max_tokens.strip()
    ):
        max_tokens = estimate_ai_max_tokens(batch_size)
    else:
        max_tokens = max(
            DEFAULT_AI_MAX_TOKENS,
            min(MAX_AI_MAX_TOKENS, int(raw_max_tokens)),
        )
    candidate_mode = str(raw_ai.get("candidate_mode", DEFAULT_AI_CANDIDATE_MODE)).strip()
    if candidate_mode not in VALID_AI_CANDIDATE_MODES:
        candidate_mode = DEFAULT_AI_CANDIDATE_MODE
    return AIConfig(
        endpoint=endpoint,
        api_key=api_key,
        model=model,
        timeout=timeout,
        batch_size=batch_size,
        examples_per_class=examples_per_class,
        max_tokens=max_tokens,
        candidate_mode=candidate_mode,
        retry_extreme_batches=_load_bool(raw_ai.get("retry_extreme_batches"), False),
        verbose=verbose,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Rime Word Marker web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db-path", help="SQLite 数据库文件路径。")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径（JSON）。")
    parser.add_argument("--verbose", action="store_true", help="打印详细调试日志。")
    args = parser.parse_args()

    config_path = _resolve_path(args.config, BASE_DIR) or DEFAULT_CONFIG_PATH
    config = _load_config(config_path)
    config_base_dir = config_path.parent if config_path.exists() else BASE_DIR

    host = str(config.get("host", args.host))
    port = int(config.get("port", args.port))
    db_path = _resolve_path(config.get("db_path"), config_base_dir) or DEFAULT_DB_PATH
    verbose = _load_bool(config.get("verbose"), False) or args.verbose
    ai_config = _load_ai_config(config, verbose=verbose)

    if args.host != parser.get_default("host"):
        host = args.host
    if args.port != parser.get_default("port"):
        port = args.port
    if args.db_path:
        db_path = _resolve_path(args.db_path, BASE_DIR) or DEFAULT_DB_PATH

    global SERVICE, AI_WORKER, VERBOSE
    VERBOSE = verbose
    SERVICE = WordService(db_path)
    AI_WORKER = AIAnnotationWorker(SERVICE, ai_config)
    AI_WORKER.start()

    handler = partial(AppHandler, directory=str(STATIC_DIR))
    try:
        with ThreadingHTTPServer((host, port), handler) as httpd:
            _log(f"Rime Word Marker running at http://{host}:{port}")
            _log(f"Using database: {db_path}")
            _log(f"Verbose logging: {'on' if verbose else 'off'}")
            if ai_config.is_configured():
                _log(f"AI annotation ready: {ai_config.model} @ {ai_config.request_url}")
            else:
                _log("AI annotation disabled: AI endpoint/model not configured")
            httpd.serve_forever()
    finally:
        if AI_WORKER is not None:
            AI_WORKER.stop()


if __name__ == "__main__":
    main()
