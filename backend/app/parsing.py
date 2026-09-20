import re
from typing import List, Optional, Tuple

# =====================================================================
# 4. Language + amount understanding (deterministic, multilingual)
# =====================================================================
NUMBER_WORDS = {
    # English
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "one hundred": 100, "hundred": 100,
    # Bahasa Melayu
    "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6, "tujuh": 7,
    "lapan": 8, "sembilan": 9, "sepuluh": 10, "lima belas": 15, "dua puluh": 20,
    "tiga puluh": 30, "empat puluh": 40, "lima puluh": 50, "enam puluh": 60,
    "tujuh puluh": 70, "lapan puluh": 80, "sembilan puluh": 90, "seratus": 100,
    # Mandarin
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
    "八": 8, "九": 9, "十": 10, "十五": 15, "二十": 20, "三十": 30, "四十": 40,
    "五十": 50, "六十": 60, "七十": 70, "八十": 80, "九十": 90, "一百": 100,
}
SCALE_WORDS = {"thousand": 1000, "ribu": 1000, "千": 1000}
# a number word that also has a ×1000 reading is materially ambiguous when ASR is unsure (§A.5 B1)
AMBIGUOUS_SCALES = {"fifty", "lima puluh", "五十", "five", "lima", "五", "ten", "sepuluh", "十"}

# alias -> the display-name fragment it resolves to (the LLM proposes a phrase, never an id)
PAYEE_ALIASES = {
    "mei tan": "mei tan", "mei": "mei", "may": "mei", "mother": "mei", "mum": "mei", "mom": "mei",
    "john doe": "john doe", "john smith": "john smith", "john": "john",
    "alice": "alice",
}


def detect_language(text: str) -> str:
    has_zh = bool(re.search(r"[\u4e00-\u9fff]", text))
    bm_markers = re.search(
        r"\b(hantar|kepada|dolar|duit|saya|tidak|boleh|jumlah|akaun|bayar|sekarang|"
        r"lima|lapan|sepuluh|ribu|seratus|buang|had|cucu|cepat|maaf)\b", text, re.I)
    en_markers = re.search(r"\b(transfer|send|pay|dollars?|please|account|confirm)\b", text, re.I)
    signals = sum(bool(x) for x in (has_zh, bm_markers, en_markers))
    if has_zh and signals > 1:
        return "MIXED"
    if has_zh:
        return "ZH"
    if bm_markers:
        return "MIXED" if en_markers else "BM"
    return "EN"


def _number_word_matches(haystack_lowered: str, haystack_raw: str) -> List[Tuple[str, int]]:
    """Every number word present, with its value. Longest phrase wins on overlap."""
    found: List[Tuple[str, int]] = []
    for word, value in NUMBER_WORDS.items():
        hay = haystack_lowered if word.isascii() else haystack_raw
        pattern = rf"\b{re.escape(word)}\b" if word.isascii() else re.escape(word)
        if re.search(pattern, hay):
            found.append((word, value))
    return found


def _largest_number_word(haystack_lowered: str, haystack_raw: str) -> Tuple[Optional[str], int]:
    best: Tuple[Optional[str], int] = (None, 0)
    for word, value in _number_word_matches(haystack_lowered, haystack_raw):
        if value > best[1]:
            best = (word, value)
    return best


def _scale_word_in(lowered: str, raw: str) -> Optional[str]:
    for word in SCALE_WORDS:
        hay = lowered if word.isascii() else raw
        pattern = rf"\b{re.escape(word)}\b" if word.isascii() else re.escape(word)
        if re.search(pattern, hay):
            return word
    return None


def parse_amount(text: str) -> Tuple[Optional[int], bool]:
    """Return (amount_minor, scale_ambiguous).

    scale_ambiguous is True when the amount came from a number word that also has a x1000
    reading (fifty / fifty thousand). That is only a *material* ambiguity when the ASR
    evidence is also uncertain — the caller combines it with the ASR confidence (§A.5 B1).
    Integer minor units only (§8.7.3).

    Handles the compound form "<number word> thousand|ribu|千" (two thousand -> 2000),
    which is how the amounts are actually spoken in the demo scenarios.
    """
    lowered = text.lower()
    scale_word = _scale_word_in(lowered, text)

    # 1) Explicit digits win outright. "5 ribu" -> 5 x 1000.
    digits = re.search(r"(?<!\d)(?:s?\$|sgd)?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d{1,2})?)(?!\d)",
                       lowered)
    if digits:
        value = float(digits.group(1).replace(",", ""))
        if scale_word and value < 1000:
            value *= SCALE_WORDS[scale_word]
        return int(round(value * 100)), False

    # 2) Compound spoken form — the number word that precedes the scale word multiplies it.
    if scale_word:
        head_lowered, head_raw = lowered.split(scale_word, 1)[0], None
        if not scale_word.isascii():
            head_raw = text.split(scale_word, 1)[0]
        word, head_value = _largest_number_word(head_lowered, head_raw or head_lowered)
        if word is not None:
            return head_value * SCALE_WORDS[scale_word] * 100, False
        # "a thousand" / bare scale word — the scale is the whole value.
        return SCALE_WORDS[scale_word] * 100, False

    # 3) Plain number words.
    word, value = _largest_number_word(lowered, text)
    if word is None:
        return None, False
    return value * 100, word in AMBIGUOUS_SCALES


CORRECTION_MARKERS = re.compile(
    r"(?:i said|i meant|actually|should be|sepatutnya|betulnya|我说|应该是)\s+(.{1,40})", re.I)
CORRECTION_SPLIT = re.compile(r"[,.;!?]|\bnot\b|\bbukan\b|\bsalah\b|\binstead\b", re.I)


def parse_corrected_amount(text: str) -> Optional[int]:
    """A correction must read the value the customer is asserting, not the one they are rejecting.

    'Wah, no. I said fifteen, not fifty.' -> 1500 minor, not 5000.
    """
    marker = CORRECTION_MARKERS.search(text)
    if not marker:
        return None
    segment = CORRECTION_SPLIT.split(marker.group(1))[0]
    value, _ = parse_amount(segment)
    return value


def last_committed_amount(turns: List[str]) -> Optional[int]:
    """The effective amount is the one in the latest evidence turn that carries one.

    A later correction supersedes an earlier instruction; a clarification answer
    supersedes the ambiguous original.
    """
    for turn in reversed(turns):
        corrected = parse_corrected_amount(turn)
        if corrected is not None:
            return corrected
        value, _ = parse_amount(turn)
        if value is not None:
            return value
    return None


def parse_payee_phrase(text: str) -> Optional[str]:
    lowered = text.lower()
    for alias in sorted(PAYEE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return alias
    match = re.search(r"(?:kepada|to|给|pay|transfer to)\s+([A-Z][a-zA-Z]+)", text)
    if match:
        return match.group(1)
    return None


def parse_reference(text: str) -> Optional[str]:
    match = re.search(r"\b(?:for|reference|ref)\s+([A-Za-z][A-Za-z ]{1,24})$", text.strip(), re.I)
    if match:
        return match.group(1).strip().title()
    return None


