from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib import error, parse, request

from app.constants import ACCEPTED, PENDING, REJECTED


DEFAULT_AI_PROMPT_VERSION = "4.0"
DEFAULT_AI_TIMEOUT = 90
DEFAULT_AI_BATCH_SIZE = 24
MAX_AI_BATCH_SIZE = 128
DEFAULT_AI_EXAMPLES_PER_CLASS = 768
DEFAULT_AI_MAX_TOKENS = 4096
MAX_AI_MAX_TOKENS = 32768
AI_MAX_TOKENS_PER_BATCH_ITEM = 192
DEFAULT_AI_CANDIDATE_MODE = "sequential"
VALID_AI_CANDIDATE_MODES = {"sequential", "random"}
AI_REQUEST_RETRIES = 2


@dataclass(slots=True)
class AIConfig:
    endpoint: str = ""
    api_key: str = ""
    model: str = ""
    timeout: int = DEFAULT_AI_TIMEOUT
    batch_size: int = DEFAULT_AI_BATCH_SIZE
    examples_per_class: int = DEFAULT_AI_EXAMPLES_PER_CLASS
    max_tokens: int = DEFAULT_AI_MAX_TOKENS
    candidate_mode: str = DEFAULT_AI_CANDIDATE_MODE
    retry_extreme_batches: bool = False
    verbose: bool = False
    prompt_version: str = DEFAULT_AI_PROMPT_VERSION

    def is_configured(self) -> bool:
        return bool(self.endpoint.strip() and self.model.strip())

    @property
    def request_url(self) -> str:
        base = self.endpoint.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        parsed = parse.urlparse(base)
        if not parsed.path or parsed.path == "/":
            return f"{base}/v1/chat/completions"
        return f"{base}/chat/completions"


def estimate_ai_max_tokens(batch_size: int) -> int:
    safe_batch_size = max(1, min(MAX_AI_BATCH_SIZE, int(batch_size)))
    estimated = safe_batch_size * AI_MAX_TOKENS_PER_BATCH_ITEM
    return max(DEFAULT_AI_MAX_TOKENS, min(MAX_AI_MAX_TOKENS, estimated))


class OpenAICompatClient:
    def __init__(self, config: AIConfig) -> None:
        self.config = config

    def is_configured(self) -> bool:
        return self.config.is_configured()

    def annotate_batch(
        self,
        examples: list[dict[str, Any]],
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not self.is_configured():
            raise RuntimeError("AI 接口尚未配置完整。")
        if not items:
            return []

        return self._annotate_batch_with_recovery(examples, items)

    def _annotate_batch_with_recovery(
        self,
        examples: list[dict[str, Any]],
        items: list[dict[str, Any]],
        max_tokens: int | None = None,
        allow_max_tokens_retry: bool = True,
    ) -> list[dict[str, Any]]:
        effective_max_tokens = max_tokens or self.config.max_tokens
        try:
            content = self._request_completion(
                _build_annotation_messages(self.config, examples, items),
                max_tokens=effective_max_tokens,
            )
            return _parse_prediction_content(content, items)
        except AIResponseTruncatedError:
            return self._recover_truncated_batch(
                examples,
                items,
                effective_max_tokens,
                allow_max_tokens_retry=allow_max_tokens_retry,
            )
        except AIResponseValidationError as exc:
            _log(f"[AI] invalid response; trying repair: {exc.public_message}")
            if self.config.verbose and exc.raw_content:
                _verbose_log_json("AI invalid response content", {"content": exc.raw_content})
            try:
                content = self._request_completion(
                    _build_repair_messages(
                        self.config,
                        examples,
                        items,
                        exc.raw_content,
                        exc.public_message,
                    ),
                    max_tokens=effective_max_tokens,
                )
                return _parse_prediction_content(content, items)
            except AIResponseTruncatedError:
                return self._recover_truncated_batch(
                    examples,
                    items,
                    effective_max_tokens,
                    allow_max_tokens_retry=allow_max_tokens_retry,
                )
            except AIResponseValidationError as repair_exc:
                _log(f"[AI] repair response invalid: {repair_exc.public_message}")
                if self.config.verbose and repair_exc.raw_content:
                    _verbose_log_json(
                        "AI invalid repair response content",
                        {"content": repair_exc.raw_content},
                    )
                if len(items) <= 1:
                    raise
                midpoint = len(items) // 2
                _log(
                    "[AI] splitting batch after validation failure "
                    f"size={len(items)} left={midpoint} right={len(items) - midpoint}"
                )
                return [
                    *self._annotate_batch_with_recovery(
                        examples,
                        items[:midpoint],
                        max_tokens=effective_max_tokens,
                    ),
                    *self._annotate_batch_with_recovery(
                        examples,
                        items[midpoint:],
                        max_tokens=effective_max_tokens,
                    ),
                ]

    def _recover_truncated_batch(
        self,
        examples: list[dict[str, Any]],
        items: list[dict[str, Any]],
        max_tokens: int,
        allow_max_tokens_retry: bool,
    ) -> list[dict[str, Any]]:
        next_max_tokens = _next_max_tokens(max_tokens)
        if allow_max_tokens_retry and next_max_tokens > max_tokens:
            _log(
                "[AI] retrying truncated batch with larger max_tokens "
                f"size={len(items)} max_tokens={max_tokens}->{next_max_tokens}"
            )
            return self._annotate_batch_with_recovery(
                examples,
                items,
                max_tokens=next_max_tokens,
                allow_max_tokens_retry=False,
            )

        if len(items) <= 1:
            raise AIResponseTruncatedError(
                "AI 输出被 max_tokens 截断，且自动提高 max_tokens 后单条词条仍无法恢复。"
            )
        midpoint = len(items) // 2
        _log(
            "[AI] splitting batch after max_tokens truncation "
            f"size={len(items)} left={midpoint} right={len(items) - midpoint}"
        )
        return [
            *self._annotate_batch_with_recovery(
                examples,
                items[:midpoint],
                max_tokens=max_tokens,
            ),
            *self._annotate_batch_with_recovery(
                examples,
                items[midpoint:],
                max_tokens=max_tokens,
            ),
        ]

    def _request_completion(self, messages: list[dict[str, str]], max_tokens: int | None = None) -> str:
        effective_max_tokens = max_tokens or self.config.max_tokens
        request_payload = {
            "model": self.config.model,
            "temperature": 0,
            "max_tokens": effective_max_tokens,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        if self.config.verbose:
            _verbose_log_json(
                "AI request",
                {"url": self.config.request_url, "payload": request_payload},
            )
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }
        if self.config.api_key.strip():
            headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"

        raw_body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        http_request = request.Request(
            self.config.request_url,
            data=raw_body,
            headers=headers,
            method="POST",
        )

        payload = self._send_request_with_retries(http_request)
        if self.config.verbose:
            _verbose_log_json("AI response", payload)

        if _extract_finish_reason(payload) == "length":
            message = "AI 输出被 max_tokens 截断。"
            _log(f"[AI] {message}")
            raise AIResponseTruncatedError(message)
        content = _extract_response_content(payload)
        if not content:
            raise RuntimeError("AI 接口没有返回可解析内容。")
        return content

    def _send_request_with_retries(self, http_request: request.Request) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(AI_REQUEST_RETRIES + 1):
            try:
                with request.urlopen(http_request, timeout=self.config.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace").strip()
                if exc.code not in {408, 409, 425, 429, 500, 502, 503, 504}:
                    message = f"AI 接口返回错误：HTTP {exc.code} {detail}"
                    _log(
                        "[AI] request failed "
                        f"attempt={attempt + 1}/{AI_REQUEST_RETRIES + 1}: {message}"
                    )
                    raise RuntimeError(message) from exc
                last_error = RuntimeError(f"AI 接口返回错误：HTTP {exc.code} {detail}")
            except error.URLError as exc:
                last_error = RuntimeError(f"无法连接 AI 接口：{exc.reason}")

            if last_error is not None:
                _log(
                    "[AI] request failed "
                    f"attempt={attempt + 1}/{AI_REQUEST_RETRIES + 1}: {last_error}"
                )
            if attempt < AI_REQUEST_RETRIES:
                time.sleep(0.8 * (attempt + 1))

        if last_error is None:  # pragma: no cover - defensive guard
            raise RuntimeError("AI 接口请求失败。")
        raise last_error


class AIResponseValidationError(RuntimeError):
    def __init__(self, public_message: str, raw_content: str = "") -> None:
        super().__init__(public_message)
        self.public_message = public_message
        self.raw_content = raw_content


class AIResponseTruncatedError(RuntimeError):
    pass


def _next_max_tokens(current_max_tokens: int) -> int:
    return min(MAX_AI_MAX_TOKENS, max(current_max_tokens + 1024, current_max_tokens * 2))


def _parse_prediction_content(content: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AIResponseValidationError(f"AI 返回不是合法 JSON：{content[:200]}", content) from exc

    items_payload = parsed.get("items")
    if not isinstance(items_payload, list):
        raise AIResponseValidationError("AI 返回缺少 items 数组。", content)

    result_map: dict[int, dict[str, Any]] = {}
    for raw_item in items_payload:
        if not isinstance(raw_item, dict):
            raise AIResponseValidationError("AI 返回的 items 项必须是对象。", content)
        try:
            entry_id = int(raw_item["id"])
            score = float(raw_item["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AIResponseValidationError(
                "AI 返回的 items 项缺少合法的 id 或 score。",
                content,
            ) from exc
        if entry_id in result_map:
            raise AIResponseValidationError(f"AI 返回了重复的 id：{entry_id}。", content)
        score = max(0.0, min(1.0, score))
        result_map[entry_id] = {"id": entry_id, "score": score}

    expected_ids = {int(item["id"]) for item in items}
    if set(result_map) != expected_ids:
        missing = sorted(expected_ids - set(result_map))
        extra = sorted(set(result_map) - expected_ids)
        fragments = []
        if missing:
            fragments.append(f"缺少 {missing}")
        if extra:
            fragments.append(f"多出 {extra}")
        raise AIResponseValidationError(
            f"AI 返回结果与请求词条不一致：{'；'.join(fragments)}。",
            content,
        )

    return [result_map[int(item["id"])] for item in items]


class AIAnnotationWorker:
    def __init__(self, service, config: AIConfig) -> None:
        self.service = service
        self.client = OpenAICompatClient(config)
        self.config = config
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="ai-annotation-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def wake(self) -> None:
        self._wake_event.set()

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def describe(self) -> dict[str, Any]:
        return {
            "configured": self.is_configured(),
            "model": self.config.model.strip(),
            "prompt_version": self.config.prompt_version,
            "batch_size": self.config.batch_size,
            "timeout": self.config.timeout,
            "examples_per_class": self.config.examples_per_class,
            "max_tokens": self.config.max_tokens,
            "candidate_mode": self.config.candidate_mode,
            "retry_extreme_batches": self.config.retry_extreme_batches,
            "verbose": self.config.verbose,
        }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                should_continue_fast = self._run_once()
                timeout = 1.0 if should_continue_fast else 6.0
            except Exception as exc:  # pragma: no cover - defensive background path
                _log(f"[AI] worker error {type(exc).__name__}: {exc}")
                self.service.update_ai_runtime_state("error", last_error=str(exc))
                timeout = 12.0

            self._wake_event.wait(timeout)
            self._wake_event.clear()

    def _run_once(self) -> bool:
        overview = self.service.get_ai_overview(
            configured=self.is_configured(),
            model_name=self.config.model,
            prompt_version=self.config.prompt_version,
        )
        if not overview["enabled"]:
            if overview["worker_status"] != "disabled":
                self.service.update_ai_runtime_state("disabled")
            return False

        if not self.is_configured():
            message = "AI 接口未配置完整，已暂停自动标注。"
            self.service.disable_ai(message)
            return False

        if not overview["training"]["sufficient"]:
            message = self.service.build_ai_training_requirement_message()
            self.service.disable_ai(message)
            return False

        batch = self.service.get_ai_batch_candidates(
            limit=self.config.batch_size,
            prompt_version=self.config.prompt_version,
            selection_mode=self.config.candidate_mode,
        )
        if not batch["items"]:
            self.service.update_ai_runtime_state("idle", last_error="")
            return False

        examples = self.service.sample_ai_training_examples(
            per_class=self.config.examples_per_class
        )
        if not examples:
            self.service.disable_ai("人工标注样本不足，已暂停自动标注。")
            return False

        self.service.update_ai_runtime_state("running", last_error="")
        predictions = self.client.annotate_batch(examples, batch["items"])
        label_counts = _summarize_predictions(predictions)
        if self.config.retry_extreme_batches and _is_extreme_prediction_batch(
            label_counts,
            len(batch["items"]),
        ):
            _log(
                "[AI] extreme batch detected; retrying once "
                f"input={len(batch['items'])} "
                f"accepted={label_counts[ACCEPTED]} rejected={label_counts[REJECTED]}"
            )
            predictions = self.client.annotate_batch(examples, batch["items"])
            label_counts = _summarize_predictions(predictions)
        updated_count = self.service.apply_ai_annotations(
            batch["items"],
            predictions,
            model_name=self.config.model,
            prompt_version=self.config.prompt_version,
            next_scan_id=batch["next_scan_id"],
        )
        _log(
            "[AI] batch "
            f"mode={self.config.candidate_mode} "
            f"input={len(batch['items'])} updated={updated_count} "
            f"accepted={label_counts[ACCEPTED]} "
            f"pending={label_counts[PENDING]} "
            f"rejected={label_counts[REJECTED]}"
        )
        if updated_count <= 0:
            self.service.update_ai_runtime_state("idle", last_error="")
            return False

        self.service.update_ai_runtime_state("running", last_error="")
        return True


def _build_system_prompt(prompt_version: str) -> str:
    return f"""
你是 Rime 词库人工审核的辅助模型。
当前提示词版本：{prompt_version}

请只根据词条本身 phrase 判断，不参考拼音。
你的目标是给出一个 0 到 1 的分数：
- 越接近 1，越应该接受
- 越接近 0，越应该拒绝
- 不确定时请保守，给 0.4 到 0.6 之间的分数

评分尺子：
- 0.00 到 0.15：单字、乱码、纯符号、明显错误词汇。
- 0.16 到 0.32：大概率拒绝，例如“常用词 + 单个助词”、“常用词 + 语气词”、自然但不适合作为词库词条的残片。
- 0.33 到 0.66：边界案例、语义不完整但不能确定错误、或你确实拿不准。
- 0.67 到 0.85：自然短语、可用词条、无明显错误的中短表达、自然句子。
- 0.86 到 1.00：非常稳定的词汇、专有名词、常见自然短语、姓名。

请重点遵守这些规则：
1. 严厉拒绝明显错误词汇、乱码、纯符号、单个汉字。
2. 严厉拒绝“常用词 + 单个助词”这类不适合作为词库词条的表达，例如“喝水了”“吃饭了”。
3. 更倾向接受两个字或更多字的稳定词汇。
4. 更倾向接受自然、无明显语法错误的短语和句子。
5. 合法、自然的中英混合词条可以接受，不应仅因包含英文、缩写、品牌名或专有名词就拒绝。例如“OpenAI助手”“ChatGPT插件”“Python脚本”通常可以接受。
6. 只有在中英混合形式明显混乱、像乱码、乱拼或不自然残片时，才应拒绝。
7. 不要为了凑结果而极端自信；拿不准时请给中间分。

输入 examples 是人工标注样本，label 和 score 都代表人工判断，应优先学习它们。
如果 example.source 是 "human_ai_disagreement"，说明旧 AI 判断曾与人工不同；这种 hard example 尤其应向人工 label/score 对齐。

下面是一些内置判断参考：
- “程序员” 应倾向接受
- “自然语言处理” 应倾向接受
- “今天天气不错” 应倾向接受
- “OpenAI助手” 应倾向接受
- “ChatGPT插件” 应倾向接受
- “有一定惯性” 应倾向接受
- “总体” 应倾向接受
- “做了很多事情” 应倾向接受
- “喝” 应倾向拒绝
- “喝水了” 应倾向拒绝
- “辛苦哦” 应倾向拒绝
- “总体的” 应倾向拒绝
- “abc的” 应倾向拒绝
- “这套呢” 应倾向拒绝
- “字段就在” 应倾向拒绝
- “最好把” 应倾向拒绝

你必须只返回一个 JSON 对象，格式严格如下：
{{
  "items": [
    {{"id": 123, "score": 0.91}},
    {{"id": 456, "score": 0.18}}
  ]
}}

输出硬性要求：
- 输出对象必须包含 "items" 数组。
- "items" 中的每个对象必须且只能包含 "id" 和 "score" 两个字段。
- "id" 必须原样使用输入 candidates 里的 id。
- "score" 必须是 0 到 1 之间的数字，不能是字符串，不能省略。
- 禁止在输出 items 中包含 "phrase"、"label"、"reason" 或其他字段。
- 禁止复述输入 candidates；你必须为每个候选词输出评分。
""".strip()


def _build_annotation_messages(
    config: AIConfig,
    examples: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _build_system_prompt(config.prompt_version)},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "请仅根据词条本身进行判断，不参考拼音。",
                    "examples": examples,
                    "candidates": [
                        {"id": item["id"], "phrase": item["phrase"]} for item in items
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]


def _build_repair_messages(
    config: AIConfig,
    examples: list[dict[str, Any]],
    items: list[dict[str, Any]],
    raw_content: str,
    error_message: str,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _build_system_prompt(config.prompt_version)},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "上一轮输出格式不合格。请忽略坏输出的格式，重新根据同一套规则为所有候选词评分。",
                    "error": error_message,
                    "examples": examples,
                    "candidates": [
                        {"id": item["id"], "phrase": item["phrase"]} for item in items
                    ],
                    "expected_ids": [int(item["id"]) for item in items],
                    "bad_output": raw_content[:4000],
                },
                ensure_ascii=False,
            ),
        },
    ]


def _extract_response_content(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("AI 接口响应缺少 choices[0].message.content。") from exc

    if isinstance(content, str):
        return _extract_json_text(content)

    if isinstance(content, list):
        fragments = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                fragments.append(str(item.get("text", "")))
        return _extract_json_text("".join(fragments))

    return _extract_json_text(str(content))


def _extract_finish_reason(payload: dict[str, Any]) -> str:
    try:
        finish_reason = payload["choices"][0].get("finish_reason", "")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    return str(finish_reason or "")


def _extract_json_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end >= start:
        return stripped[start : end + 1]
    return stripped


def _summarize_predictions(predictions: list[dict[str, Any]]) -> dict[str, int]:
    counts = {ACCEPTED: 0, PENDING: 0, REJECTED: 0}
    for prediction in predictions:
        try:
            label = score_to_label(float(prediction["score"]))
        except (KeyError, TypeError, ValueError):
            continue
        counts[label] += 1
    return counts


def _is_extreme_prediction_batch(label_counts: dict[str, int], total: int) -> bool:
    return total > 1 and (
        label_counts.get(ACCEPTED, 0) == total or label_counts.get(REJECTED, 0) == total
    )


def _log(message: str) -> None:
    print(f"[{_log_timestamp()}] {message}", flush=True)


def _log_timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _verbose_log_json(title: str, payload: Any) -> None:
    print(f"[{_log_timestamp()}] [verbose] {title}:", flush=True)
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


def score_to_label(score: float) -> str:
    if score > 0.66:
        return ACCEPTED
    if score < 0.33:
        return REJECTED
    return PENDING
