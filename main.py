from __future__ import annotations

import argparse
import json
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.service import DEFAULT_REVIEW_SESSION, WordService
from app.pinyin_utils import transliterate_phrase

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "words.db"
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
SERVICE: WordService | None = None
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
                review_state = _service().advance_review(self._review_session_key())
                self._send_json(200, {**review_state, "stats": _service().get_stats()})
                return

            if parsed.path == "/api/entries":
                query = parse_qs(parsed.query)
                page = _to_int(query.get("page", ["1"])[0], default=1)
                page_size = _to_int(query.get("page_size", ["30"])[0], default=30)
                status = query.get("status", ["all"])[0]
                keyword = query.get("q", [""])[0]
                payload = _service().list_entries(
                    page=page,
                    page_size=page_size,
                    status=status,
                    query=keyword,
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
                dictionary_name = query.get("name", ["rime_word_marker_export"])[0]
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
                self._send_json(200, {"count": _service().count_export_entries(statuses=statuses)})
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
            self._send_json(500, {"error": f"服务器异常：{exc}"})

    def _handle_api_post(self, parsed) -> None:
        try:
            if parsed.path == "/api/import-file":
                raw_body = self._read_raw_body()
                if not raw_body.strip():
                    self._send_json(400, {"error": "导入文件不能为空。"})
                    return
                try:
                    text = raw_body.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise ValueError("导入文件不是合法的 UTF-8 文本。") from exc
                result = _service().import_text(text)
                self._send_json(200, {"result": result, "stats": _service().get_stats()})
                return

            payload = self._read_json_body()

            if parsed.path == "/api/import":
                text = payload.get("text", "")
                if not text.strip():
                    self._send_json(400, {"error": "导入内容不能为空。"})
                    return
                result = _service().import_text(text)
                self._send_json(200, {"result": result, "stats": _service().get_stats()})
                return

            if parsed.path == "/api/review/next":
                review_state = _service().advance_review(self._review_session_key())
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
                self._send_json(200, {**review_state, "stats": _service().get_stats()})
                return

            if parsed.path == "/api/review/label":
                review_state = _service().label_and_advance(
                    int(payload.get("entry_id", 0)),
                    payload.get("status", ""),
                    self._review_session_key(),
                )
                self._send_json(200, {**review_state, "stats": _service().get_stats()})
                return

            if parsed.path == "/api/entries/bulk-update":
                result = _service().batch_update_entries(
                    payload.get("ids", []),
                    payload.get("updates", {}),
                    regenerate_pinyin=bool(payload.get("regenerate_pinyin")),
                    clear_labeled_at=bool(payload.get("clear_labeled_at")),
                )
                self._send_json(200, {**result, "stats": _service().get_stats()})
                return

            match = ENTRY_STATUS_RE.match(parsed.path)
            if match:
                entry = _service().update_status(
                    int(match.group(1)),
                    payload.get("status", ""),
                )
                self._send_json(200, {"entry": entry, "stats": _service().get_stats()})
                return

            match = ENTRY_UPDATE_RE.match(parsed.path)
            if match:
                entry = _service().update_entry(
                    int(match.group(1)),
                    payload,
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Rime Word Marker web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db-path", help="SQLite 数据库文件路径。")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径（JSON）。")
    args = parser.parse_args()

    config_path = _resolve_path(args.config, BASE_DIR) or DEFAULT_CONFIG_PATH
    config = _load_config(config_path)
    config_base_dir = config_path.parent if config_path.exists() else BASE_DIR

    host = str(config.get("host", args.host))
    port = int(config.get("port", args.port))
    db_path = _resolve_path(config.get("db_path"), config_base_dir) or DEFAULT_DB_PATH

    if args.host != parser.get_default("host"):
        host = args.host
    if args.port != parser.get_default("port"):
        port = args.port
    if args.db_path:
        db_path = _resolve_path(args.db_path, BASE_DIR) or DEFAULT_DB_PATH

    global SERVICE
    SERVICE = WordService(db_path)

    handler = partial(AppHandler, directory=str(STATIC_DIR))
    with ThreadingHTTPServer((host, port), handler) as httpd:
        print(f"Rime Word Marker running at http://{host}:{port}")
        print(f"Using database: {db_path}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
