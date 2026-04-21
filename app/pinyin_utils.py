from __future__ import annotations

import re
import unicodedata

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # pragma: no cover - exercised in runtime when dependency is absent
    lazy_pinyin = None
    Style = None


TOKEN_PATTERN = re.compile(r"[\u3400-\u9fff]+|[A-Za-z0-9]+|[^A-Za-z0-9\u3400-\u9fff\s]+")
ASCII_LETTER_PATTERN = re.compile(r"[A-Za-z]")
PINYIN_TOKEN_PATTERN = re.compile(r"\S+")
FULL_PINYIN_SCHEME = "full_pinyin"
VALID_MIXED_EXPORT_SCHEMES = {
    FULL_PINYIN_SCHEME,
    "ziranma",
    "abc",
    "flypy",
    "microsoft",
    "sogou",
    "ziguang",
}
SHUANGPIN_INITIALS = set("bpmfdtnlgkhjqxrzcswy")

TONE_TRANSLATION = str.maketrans(
    "āáǎàōóǒòēéěèīíǐìūúǔùǖǘǚǜüńňǹ",
    "aaaaooooeeeeiiiiuuuuvvvvvnnn",
)

SHUANGPIN_INITIAL_MAPS = {
    "ziranma": {"zh": "v", "ch": "i", "sh": "u"},
    "flypy": {"zh": "v", "ch": "i", "sh": "u"},
    "microsoft": {"zh": "v", "ch": "i", "sh": "u"},
    "sogou": {"zh": "v", "ch": "i", "sh": "u"},
    "abc": {"zh": "a", "ch": "e", "sh": "v"},
    "ziguang": {"zh": "u", "ch": "a", "sh": "i"},
}

SHUANGPIN_FINAL_MAPS = {
    "ziranma": {
        "iu": "q",
        "ia": "w",
        "ua": "w",
        "uan": "r",
        "van": "r",
        "ue": "t",
        "ve": "t",
        "ing": "y",
        "uai": "y",
        "uo": "o",
        "un": "p",
        "vn": "p",
        "ong": "s",
        "iong": "s",
        "iang": "d",
        "uang": "d",
        "en": "f",
        "eng": "g",
        "ang": "h",
        "ian": "m",
        "an": "j",
        "iao": "c",
        "ao": "k",
        "ai": "l",
        "ei": "z",
        "ie": "x",
        "ui": "v",
        "ou": "b",
        "in": "n",
        "er": "r",
    },
    "flypy": {
        "iu": "q",
        "ei": "w",
        "uan": "r",
        "van": "r",
        "ue": "t",
        "ve": "t",
        "un": "y",
        "vn": "y",
        "uo": "o",
        "ie": "p",
        "ong": "s",
        "iong": "s",
        "ing": "k",
        "uai": "k",
        "ai": "d",
        "en": "f",
        "eng": "g",
        "iang": "l",
        "uang": "l",
        "ang": "h",
        "ian": "m",
        "an": "j",
        "ou": "z",
        "ia": "x",
        "ua": "x",
        "iao": "n",
        "ao": "c",
        "ui": "v",
        "in": "b",
        "er": "r",
    },
    "microsoft": {
        "iu": "q",
        "ia": "w",
        "ua": "w",
        "er": "r",
        "uan": "r",
        "van": "r",
        "ue": "t",
        "ve": "t",
        "v": "y",
        "uai": "y",
        "uo": "o",
        "un": "p",
        "vn": "p",
        "ong": "s",
        "iong": "s",
        "iang": "d",
        "uang": "d",
        "en": "f",
        "eng": "g",
        "ang": "h",
        "ian": "m",
        "an": "j",
        "iao": "c",
        "ao": "k",
        "ai": "l",
        "ei": "z",
        "ie": "x",
        "ui": "v",
        "ou": "b",
        "in": "n",
        "ing": ";",
    },
    "sogou": {
        "iu": "q",
        "ia": "w",
        "ua": "w",
        "er": "r",
        "uan": "r",
        "van": "r",
        "ue": "t",
        "ve": "t",
        "v": "y",
        "uai": "y",
        "uo": "o",
        "un": "p",
        "vn": "p",
        "ong": "s",
        "iong": "s",
        "iang": "d",
        "uang": "d",
        "en": "f",
        "eng": "g",
        "ang": "h",
        "ian": "m",
        "an": "j",
        "iao": "c",
        "ao": "k",
        "ai": "l",
        "ei": "z",
        "ie": "x",
        "ui": "v",
        "ou": "b",
        "in": "n",
        "ing": ";",
    },
    "abc": {
        "ei": "q",
        "ian": "w",
        "er": "r",
        "iu": "r",
        "iang": "t",
        "uang": "t",
        "ing": "y",
        "uo": "o",
        "uan": "p",
        "van": "p",
        "ong": "s",
        "iong": "s",
        "ia": "d",
        "ua": "d",
        "en": "f",
        "eng": "g",
        "ang": "h",
        "an": "j",
        "iao": "z",
        "ao": "k",
        "in": "c",
        "uai": "c",
        "ai": "l",
        "ie": "x",
        "ou": "b",
        "un": "n",
        "vn": "n",
        "ue": "m",
        "ve": "m",
        "ui": "m",
    },
    "ziguang": {
        "en": "w",
        "eng": "t",
        "in": "y",
        "uai": "y",
        "uo": "o",
        "ai": "p",
        "iang": "g",
        "uang": "g",
        "ang": "s",
        "ie": "d",
        "ian": "f",
        "ong": "h",
        "iong": "h",
        "er": "j",
        "iu": "j",
        "ei": "k",
        "uan": "l",
        "van": "l",
        "ing": ";",
        "ou": "z",
        "ia": "x",
        "ua": "x",
        "iao": "b",
        "ue": "n",
        "ve": "n",
        "ui": "n",
        "un": "m",
        "vn": "m",
        "ao": "q",
        "an": "r",
    },
}


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


def phrase_has_ascii_letters(phrase: str) -> bool:
    return bool(ASCII_LETTER_PATTERN.search(phrase or ""))


def normalize_mixed_export_scheme(scheme: str | None) -> str:
    normalized = (scheme or FULL_PINYIN_SCHEME).strip().lower()
    aliases = {
        "full": FULL_PINYIN_SCHEME,
        "pinyin": FULL_PINYIN_SCHEME,
        "quanpin": FULL_PINYIN_SCHEME,
        "natural": "ziranma",
        "zrm": "ziranma",
        "xiaohe": "flypy",
        "mspy": "microsoft",
        "ms": "microsoft",
        "zrm2000": "ziranma",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in VALID_MIXED_EXPORT_SCHEMES:
        raise ValueError("不支持的中英混杂词汇编码方案。")
    return normalized


def export_code_for_mixed_phrase(
    phrase: str,
    pinyin: str,
    scheme: str = FULL_PINYIN_SCHEME,
) -> str:
    """Build a no-space export code for phrases containing ASCII letters."""

    normalized_scheme = normalize_mixed_export_scheme(scheme)
    units = _align_phrase_and_pinyin(phrase, pinyin)
    if normalized_scheme == FULL_PINYIN_SCHEME:
        return "".join(value.lower() for kind, value in units if kind != "space")

    return "".join(
        _shuangpin_syllable(value, normalized_scheme) if kind == "pinyin" else value.lower()
        for kind, value in units
        if kind != "space"
    )


def _align_phrase_and_pinyin(phrase: str, pinyin: str) -> list[tuple[str, str]]:
    pinyin_tokens = PINYIN_TOKEN_PATTERN.findall(pinyin or "")
    token_index = 0
    units: list[tuple[str, str]] = []

    for token in TOKEN_PATTERN.findall(phrase or ""):
        if not token.strip():
            continue

        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            for char in token:
                if token_index < len(pinyin_tokens):
                    units.append(("pinyin", pinyin_tokens[token_index]))
                    token_index += 1
                else:
                    units.append(("pinyin", _fallback_char_pinyin(char)))
            continue

        if re.fullmatch(r"[A-Za-z0-9]+", token):
            units.append(("ascii", token.lower()))
            token_index = _consume_matching_ascii_tokens(pinyin_tokens, token_index, token)
            continue

        units.append(("literal", token.lower()))
        if token_index < len(pinyin_tokens) and pinyin_tokens[token_index] == token:
            token_index += 1

    return units


def _fallback_char_pinyin(char: str) -> str:
    try:
        return transliterate_phrase(char)
    except RuntimeError:
        return char


def _consume_matching_ascii_tokens(tokens: list[str], start: int, ascii_text: str) -> int:
    target = _normalize_ascii_token(ascii_text)
    if not target:
        return start

    combined = ""
    index = start
    while index < len(tokens) and len(combined) < len(target):
        next_part = _normalize_ascii_token(tokens[index])
        if not next_part:
            break
        candidate = combined + next_part
        if not target.startswith(candidate):
            break
        combined = candidate
        index += 1

    return index if combined == target else start


def _normalize_ascii_token(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_pinyin_tone(token).lower())


def strip_pinyin_tone(syllable: str) -> str:
    translated = (syllable or "").translate(TONE_TRANSLATION).lower()
    decomposed = unicodedata.normalize("NFD", translated)
    return "".join(
        char
        for char in decomposed
        if unicodedata.category(char) != "Mn" and ("a" <= char <= "z" or char == "v")
    )


def _shuangpin_syllable(syllable: str, scheme: str) -> str:
    plain = strip_pinyin_tone(syllable)
    if not plain:
        return syllable.lower()
    if plain == "ng":
        plain = "eng"
    elif plain in {"ńg", "ňg", "ǹg"}:
        plain = "eng"
    elif plain in {"ń", "ň", "ǹ"}:
        plain = "en"

    initial, final = _split_pinyin_syllable(plain)
    # Rime's jqxy-u -> jqxy-v rules are `derive`, i.e. alternative spellings.
    # Dictionary export should keep the primary code as xu/ju/qu/yu.
    if not initial:
        initial, final = _split_zero_initial(plain, scheme)

    initial_key = SHUANGPIN_INITIAL_MAPS[scheme].get(initial, initial)
    final_key = SHUANGPIN_FINAL_MAPS[scheme].get(final, final)
    return f"{initial_key}{final_key}"


def _split_pinyin_syllable(syllable: str) -> tuple[str, str]:
    for initial in ("zh", "ch", "sh"):
        if syllable.startswith(initial):
            return initial, syllable[len(initial) :]

    if syllable[:1] in SHUANGPIN_INITIALS:
        return syllable[:1], syllable[1:]

    return "", syllable


def _split_zero_initial(syllable: str, scheme: str) -> tuple[str, str]:
    if not syllable:
        return "", ""
    if scheme in {"abc", "ziguang"}:
        return "o", syllable
    return syllable[:1], syllable
