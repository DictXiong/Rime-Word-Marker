from __future__ import annotations

import re

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # pragma: no cover - exercised in runtime when dependency is absent
    lazy_pinyin = None
    Style = None


TOKEN_PATTERN = re.compile(r"[\u3400-\u9fff]+|[A-Za-z0-9]+|[^A-Za-z0-9\u3400-\u9fff\s]+")


def _require_pypinyin() -> None:
    if lazy_pinyin is None or Style is None:
        raise RuntimeError(
            "缺少依赖 pypinyin。请先执行 `python3 -m pip install -r requirements.txt`。"
        )


def transliterate_phrase(phrase: str) -> str:
    """Generate standard pinyin for mixed Chinese and ASCII phrases."""
    _require_pypinyin()

    tokens: list[str] = []
    for token in TOKEN_PATTERN.findall(phrase):
        if not token.strip():
            continue

        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            tokens.extend(lazy_pinyin(token, style=Style.TONE, strict=False))
            continue

        if re.fullmatch(r"[A-Za-z0-9]+", token):
            tokens.append(token.lower())
            continue

        tokens.append(token)

    return " ".join(tokens).strip()
