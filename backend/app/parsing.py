import calendar
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .config import Settings
from .repository import get_repository
# Imported under a local name so the call site reads as a question about the turn, not as a
# module reference. nicknames.py reads its file lazily, so importing it here costs no I/O.
from .nicknames import match_in_text as nickname_match

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
    # "seribu" (a thousand) is ONE word in Malay, so the scale word "ribu" can never see it -
    # \bribu\b has no boundary inside "seribu" - and it fell through to the plain number-word
    # branch with no entry there, so "seribu dollar" parsed as no amount at all. Listed here as
    # a number word; the compound form "dua ribu" still goes through SCALE_WORDS as before.
    "seribu": 1000,
    # Mandarin
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
    "八": 8, "九": 9, "十": 10, "十五": 15, "二十": 20, "三十": 30, "四十": 40,
    "五十": 50, "六十": 60, "七十": 70, "八十": 80, "九十": 90, "一百": 100,
}
SCALE_WORDS = {"thousand": 1000, "ribu": 1000, "千": 1000, "grand": 1000}
# a number word that also has a ×1000 reading is materially ambiguous when ASR is unsure (§A.5 B1)
AMBIGUOUS_SCALES = {"fifty", "lima puluh", "五十", "five", "lima", "五", "ten", "sepuluh", "十"}

# Slang scale words the customer might say out loud. "grand" and "k" are NOT standard number
# words, so when one is used the system states the interpretation it assumed and asks the
# customer to confirm it, the same "advise first" posture it takes for everything else.
# "thousand" / "ribu" / "千" are standard and are never flagged.
_SLANG_SCALE = ("grand", "k")
_WORD_NUMBERS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"}


def colloquial_scale_phrase(text: str) -> Optional[str]:
    """The spoken slang-scale phrase in `text`, or None.

    Returns the exact words the customer said so the confirm can quote them back, "a grand"
    (with its article) rather than just "grand", or "5k" / "five k". Standard scale words
    ("thousand", "ribu", "千") return None: they are not slang and need no confirmation.
    """
    if not text:
        return None
    lowered = text.lower()
    m = re.search(r"\b(a|an)\s+grand\b", lowered)
    if m:
        return m.group(0)
    if re.search(r"\bgrand\b", lowered):
        return "grand"
    m = re.search(r"\b\d+\s*k\b", lowered)
    if m:
        return m.group(0)
    m = re.search(rf"\b({'|'.join(_WORD_NUMBERS)})\s+k\b", lowered)
    if m:
        return m.group(0)
    if re.search(r"\bk\b", lowered):
        return "k"
    return None

# alias -> the display-name fragment it resolves to (the LLM proposes a phrase, never an id)
PAYEE_ALIASES = {
    "mei tan": "mei tan", "mei": "mei", "may": "mei", "mother": "mei", "mum": "mei", "mom": "mei",
    "john doe": "john doe", "john smith": "john smith", "john": "john",
    "alice": "alice",
}


# The three supported interaction languages, plus code-mixed speech. Module-level (not buried
# inside detect_language) so the vocabulary is defined once and can be tuned in one place.
# Only detect_language() reads them now: interface_language() used to consult them to pick a
# language for a code-mixed turn, but that made a handful of Han characters outvote the rest of
# the sentence, so MIXED now defaults to English (see messages.interface_language).
CJK_MARKERS = re.compile(r"[\u4e00-\u9fff]")
BM_MARKERS = re.compile(
    r"\b(hantar|kepada|dolar|dollar|duit|saya|tidak|boleh|jumlah|akaun|bayar|sekarang|"
    r"lima|lapan|sepuluh|ribu|seratus|buang|had|cucu|cepat|maaf)\b", re.I)
# NOTE: "dollars?" was deliberately REMOVED from EN_MARKERS. "dollar" is an English loanword
# spoken routinely inside Malay financial speech ("seribu dollar", "lima puluh dollar"), and it
# must NOT, by itself, flip a BM turn into MIXED and thereby route the answer to English. It is
# now a BM marker (above, next to "dolar") so a BM turn containing it stays BM. Pure-English
# turns are unaffected: transfer/send/pay/account/confirm still set en_markers.
EN_MARKERS = re.compile(r"\b(transfer|send|pay|please|account|confirm)\b", re.I)


def detect_language(text: str) -> str:
    has_zh = bool(CJK_MARKERS.search(text))
    bm_markers = BM_MARKERS.search(text)
    en_markers = EN_MARKERS.search(text)
    signals = sum(bool(x) for x in (has_zh, bm_markers, en_markers))
    if has_zh and signals > 1:
        return "MIXED"
    if has_zh:
        return "ZH"
    if bm_markers:
        return "MIXED" if en_markers else "BM"
    return "EN"


# Ordinary Malay words, for the MAJORITY count below. Deliberately wider than BM_MARKERS:
# BM_MARKERS only has to answer "is there any Malay here?", so it gets away with a handful of
# loud markers; a majority vote needs the small joining words too ("dan", "dari", "satu") or
# every Malay sentence scores as mostly English because its nouns happen to be English.
_BM_VOCAB = frozenset({
    "hantar", "kepada", "dolar", "dollar", "dollars", "duit", "saya", "tidak", "boleh", "jumlah", "akaun", "bayar",
    "sekarang", "lima", "lapan", "sepuluh", "ribu", "seratus", "buang", "had", "cucu", "cepat",
    "maaf", "dan", "atau", "dari", "ke", "di", "untuk", "dengan", "ini", "itu", "ada", "nak",
    "sudah", "sila", "tolong", "nanti", "satu", "dua", "tiga", "empat", "enam", "tujuh",
    "sembilan", "puluh", "ringgit", "semua", "lagi", "juga", "tapi", "kalau", "sebab", "macam",
    "mana", "apa", "siapa", "bil", "bayaran", "pindah", "pindahkan", "awak", "kami", "dia",
    "mereka", "bahasa", "wang", "selamat", "jangan", "tanya", "tak", "cakap", "buat", "kita",
})
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")


def language_word_counts(text: str) -> Dict[str, int]:
    """How much of each language the turn really contains: one point per Chinese run or word.

    A run of Chinese characters counts as ONE point, not one per character; otherwise a single
    four-character phrase outweighs four English words and "mostly Chinese" is decided by
    stroke count rather than by what the customer actually said.
    """
    counts = {"EN": 0, "ZH": 0, "BM": 0}
    counts["ZH"] = len(_CJK_RUN_RE.findall(text))
    for word in _LATIN_WORD_RE.findall(text.lower()):
        counts["BM" if word in _BM_VOCAB else "EN"] += 1
    return counts


def dominant_language(text: str) -> str:
    """The language the customer used MOST, for a turn that mixes several. EN wins a tie.

    This is what makes "帮我 bayar bil Starhub dari current account" an English answer: the
    Mandarin is three short runs and the rest of the breath is English, so English has the most
    words. It is a count, not a guess, and unlike answering in whatever script appears first,
    it cannot be flipped by one character.
    """
    counts = language_word_counts(text)
    best = max(counts.values())
    if best == 0:
        return "EN"
    for lang in ("EN", "ZH", "BM"):      # EN first, so it takes a tie as the neutral tongue
        if counts[lang] == best:
            return lang
    return "EN"


# A spoken "one fifty" is either S$1.50 or S$150, a LINGUISTIC ambiguity, not an ASR one
# (a cousin of the ×1000 scale ambiguity in §A.5 B1). A small number word ("one"…"twelve")
# followed by a sub-hundred number word, with no decimal point, no "hundred", and no explicit
# cent word, is ambiguous in the words themselves, so it is material at ANY asr confidence.
# ("one hundred fifty" is unambiguous, it has "hundred"; a literal "1.50" is parsed directly.)
_UNITS_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                "ten", "eleven", "twelve"}


def decimal_ambiguity_pair(text: str) -> Optional[Tuple[int, int]]:
    """(lower_minor, higher_minor) for a "one fifty" style amount, or None.

    lower = X dollars + Y cents (one fifty -> S$1.50); higher = the same digits read as whole
    dollars (S$150.00). Only when the turn has no decimal point, no "hundred", and no explicit
    cent word, otherwise the customer already disambiguated it. The Payment Agent and the
    orchestrator share this one predicate, so the question can never contradict the reason.
    """
    lowered = text.lower()
    # A genuine decimal point ("1.50") disambiguates the amount, so there is nothing to ask,
    # but a trailing sentence full stop must NOT count as one. Explicit cent words do too.
    if re.search(r"\d\.\d", lowered) or re.search(r"\b(cent|sen)s?\b", lowered):
        return None
    words = re.findall(r"[a-z]+", lowered)
    for i in range(len(words) - 1):
        a, b = words[i], words[i + 1]
        if a in _UNITS_WORDS and b in NUMBER_WORDS and 1 <= NUMBER_WORDS[b] <= 99:
            units = NUMBER_WORDS[a]
            sub = NUMBER_WORDS[b]
            low = units * 100 + sub            # S$1.50 when units=1, sub=50
            return low, low * 100              # S$150.00
    return None


def ambiguous_amount_pair(text: str, asr_confidence: float,
                          already_disambiguated: bool = False) -> Optional[Tuple[int, int]]:
    """The two readings of a *materially* ambiguous amount, or None.

    A "one fifty" style amount is ambiguous in the WORDS themselves, so it is material no
    matter how clear the audio was, that is checked first. "fifty" is either fifty dollars or
    fifty thousand dollars; that ×1000 reading is only material when the audio evidence was
    also uncertain (§A.5 B1), the customer has not already disambiguated the amount, and the
    turn is not an explicit correction (an explicit correction wins).

    Returns (lower_minor, higher_minor). Both the Payment Agent (which raises the ambiguity)
    and the orchestrator (which renders the question) call this one predicate, so the question
    can never disagree with the reason it was asked.
    """
    decimal = decimal_ambiguity_pair(text)
    if decimal is not None:
        return decimal
    if already_disambiguated or asr_confidence >= Settings.ASR_AMBIGUITY_THRESHOLD:
        return None
    if parse_corrected_amount(text) is not None:
        return None
    amount, scale_ambiguous = parse_amount(text)
    if amount is None or not scale_ambiguous:
        return None
    return amount, amount * 1000


def spoken_digit_ambiguity_pair(text: str) -> Optional[Tuple[int, int]]:
    """(lower_minor, higher_minor) when a SPOKEN turn's amount is a bare number the words could
    have produced two ways, or None.

    A recogniser writes numbers as DIGITS, so the words are gone before the turn reaches us:
    "one fifty" and "one hundred and fifty" both arrive as "150". The first is two amounts
    (S$1.50 or S$150.00); the second is one. The transcript cannot tell them apart, so the amount
    is materially ambiguous and is put to the customer rather than resolved to the larger reading.

    Only the "X|YY" shape counts: a leading 1-12 and a trailing 10-99, so three digits in all.
    "100" ("one hundred"), "1500" ("fifteen hundred") and a decimal ("1.50") are all unambiguous.

    The caller gates this on the turn having come from a real recogniser. A TYPED "150" is exactly
    what the customer meant and must never be questioned.
    """
    if re.search(r"\d\.\d", text):
        return None
    # Only a SINGLE standalone number is considered. A turn carrying several numbers (a date, a
    # reference) is not this shape, and "150" is not the amount in "on the 28th, send 150".
    digits = re.findall(r"\d+", text.replace(",", ""))
    if len(digits) != 1:
        return None
    whole = int(digits[0])
    if not (100 <= whole <= 999):
        return None
    units, sub = divmod(whole, 100)
    if not (1 <= units <= 12 and 10 <= sub <= 99):
        return None
    low = units * 100 + sub            # S$1.50 when the digits are "150"
    return low, whole * 100            # S$150.00


def resolve_amount_choice(text: str, candidates: Optional[List[int]]) -> Optional[int]:
    """The amount the customer PICKED when answering "S$1.50 or S$150.00?", or None.

    Only a reading that was actually OFFERED is accepted. The question put exactly two amounts to
    the customer, so an answer that matches neither is far more likely to be a mis-parse or an
    unrelated number than a deliberate third value, and guessing at one is the one thing amount
    handling must never do. An answer that is still ambiguous in the words ("one fifty" said
    again) resolves to nothing and the question is simply asked again.

    Fail-closed on purpose: a customer who wants a different amount can restate the whole
    instruction, which re-derives it from scratch through the ordinary path.
    """
    if decimal_ambiguity_pair(text) is not None:
        return None
    amount, _ = parse_amount(instruction_text(text))
    if amount is None:
        return None
    if candidates and amount in candidates:
        return amount
    return None


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


# A turn can open with a confirmation/acknowledgement of the payment already on screen ("yes
# 1000 is correct.") and THEN name a fresh instruction ("send $50 to ah girl"). The confirmation's
# number must never be read as the new instruction's amount, that silently changes how much money
# the new instruction moves, the one thing amount parsing must never do. instruction_text() returns
# only the instruction-bearing tail so parse_amount sees just it.
# \b is a no-op (and worse, a non-match) around CJK characters - a CJK word char is always a
# word char, so \b对\b never matches "1000是对的" because 对 sits between 是 and 的. The Latin
# tokens keep their boundaries; the CJK affirmations are matched as bare substrings so an
# acknowledgement spoken in Mandarin ("对的", "是的", "对", "嗯") is still recognised.
# The Malay affirmations belong here too ("betul" = correct, "benar" = right). They were MISSING,
# which is what let "1000 dollar betul. hantarlah duitnya ke ah girl dari akun semasa." keep its
# "1000": instruction_text saw no affirmation, left the turn whole, and the multi-instruction merge
# adopted the 1000 as the ah girl payment's amount - the S$1,000-vs-S$50 bug. An affirmation list
# that covers EN and ZH but not BM is exactly the blind spot a trilingual demo cannot afford.
_AFFIRM_TOKENS = re.compile(
    r"\b(yes|yeah|yep|yah|ya|correct|right|ok|okay|sure|confirm|affirm|true|agreed|good|great|"
    r"fine|sounds|perfect|alright|setuju|betul|benar|ok lah|ya lah)\b"
    r"|对的|是的|没错|正确|对|嗯", re.I)
# Same CJK boundary caveat: 汇/给/帮我/转 are matched bare so "转给阿美" / "汇一千到联名账户"
# register as a payment verb even though they are flanked by other CJK characters.
# The Malay verbs carry inflections, and \bhantar\b does NOT match "hantarlah" (the -lah
# imperative the demo actually uses) nor "pindahkan" / "bayarkan". Each root therefore takes its
# optional -kan / -lah suffixes, and the roots that were simply absent (pindah, beri) are added.
# Without this, "1000 dollar betul. hantarlah duitnya ke ah girl…" still had no payment verb in
# its tail, so instruction_text refused to trim the acknowledgement and the 1000 was adopted.
_PAYMENT_VERBS = re.compile(
    r"\b(send|transfer(?:kan)?|pay|give|kirim"
    r"|hantar(?:kan|lah)?|bayar(?:kan|lah)?|pindah(?:kan|lah)?|buat(?:kan|lah)?|beri(?:kan|lah)?"
    r")\b"
    r"|汇|给|帮我|转", re.I)


# The boundary between a leading acknowledgement and the real instruction that follows it.
# A hard sentence boundary (. ! ? ;) AND the spoken connectors a customer actually uses to
# chain a confirmation onto a new instruction ("1000是对的，然后…", "yes, then send…", "betul,
# dan hantar…"). A bare ASCII comma is deliberately NOT here - "S$1,000" must never be cut.
_INSTRUCTION_BOUNDARY = re.compile(
    r"[.!?;]+(\s+|$)"
    r"|，|；"
    r"|然后|之后|接着|还有|再"
    r"|\s+and\s+|\s+then\s+"
    r"|\bkemudian\b|\blepas\s+itu\b|\blalu\b|\bdan\b|\bserta\b", re.I)


def instruction_text(turn: str) -> str:
    """The portion of a turn that carries a NEW payment instruction.

    A leading clause that is a pure acknowledgement of the payment already on screen
    ("yes 1000 is correct" / "1000是对的") and THEN names a fresh instruction
    ("send $50 to ah girl" / "然后从我的经常账户转给阿美") is trimmed to just the instruction
    tail. The acknowledgement's number must never be read as the new instruction's amount - that
    silently changes how much money the new instruction moves, the one thing amount parsing must
    never do.

    The leading acknowledgement is dropped only at a hard sentence boundary (. ! ? ;) or a spoken
    connector (， / 然后 / then / dan / …). It is dropped only when the prefix holds an
    affirmation/correctness token and NO payment verb, and the remainder still names a payment -
    so an ordinary two-payment breath whose first clause carries its own verb
    ("send 1000 to joint, then send 50 to Amei" / "从储蓄转一千到联名账户，然后转五十给阿美") is
    never cut down to one.

    "yes 1000 is correct. send $50 to ah girl" -> "send $50 to ah girl"
    "1000是对的，然后从我的经常账户转给阿美"  -> "然后从我的经常账户转给阿美"
    "send 1000 to ah girl"                      -> "send 1000 to ah girl"  (unchanged)
    """
    if not turn:
        return turn
    m = _INSTRUCTION_BOUNDARY.search(turn)
    if not m:
        return turn
    prefix, rest = turn[:m.start()].strip(), turn[m.end():].strip()
    if (prefix and rest and not _PAYMENT_VERBS.search(prefix)
            and _AFFIRM_TOKENS.search(prefix) and _PAYMENT_VERBS.search(rest)):
        return rest
    return turn


def parse_amount(text: str) -> Tuple[Optional[int], bool]:
    """Return (amount_minor, scale_ambiguous).

    scale_ambiguous is True when the amount came from a number word that also has a x1000
    reading (fifty / fifty thousand). That is only a *material* ambiguity when the ASR
    evidence is also uncertain, the caller combines it with the ASR confidence (§A.5 B1).
    Integer minor units only (§8.7.3).

    Handles the compound form "<number word> thousand|ribu|千" (two thousand -> 2000),
    which is how the amounts are actually spoken in the demo scenarios.

    Date expressions are masked out FIRST (see mask_dates). A day of the month is a number, and
    without this "on 28th september instead" read as S$28.00, a silent change to how much money
    moves, which is the one thing this parser must never do.
    """
    text = mask_dates(text)
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

    # 2) Compound spoken form, the number word that precedes the scale word multiplies it.
    if scale_word:
        head_lowered, head_raw = lowered.split(scale_word, 1)[0], None
        if not scale_word.isascii():
            head_raw = text.split(scale_word, 1)[0]
        word, head_value = _largest_number_word(head_lowered, head_raw or head_lowered)
        if word is not None:
            return head_value * SCALE_WORDS[scale_word] * 100, False
        # "a thousand" / bare scale word, the scale is the whole value.
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
        value, _ = parse_amount(instruction_text(turn))
        if value is not None:
            return value
    return None


def _segment_recipient(seg: str, customer_id: Optional[str]) -> Optional[str]:
    """The resolved recipient name a single instruction segment targets, or None.

    Covers all four transaction shapes (biller, card, own account, saved payee) so a
    follow-up turn can be matched to the instruction it refers to by recipient, not by
    whichever number happens to appear first in the turn.
    """
    return (parse_biller_phrase(seg, customer_id) or parse_card_phrase(seg, customer_id)
            or parse_own_account_phrase(seg, customer_id) or parse_payee_phrase(seg, customer_id))


def committed_amount_for_recipient(customer_id: Optional[str], recipient_phrase: Optional[str],
                                   turns: List[str]) -> Optional[int]:
    """The amount most recently committed for a SPECIFIC recipient.

    When a follow-up turn names a payee but states no amount (e.g. "send ah girl money from my
    current account" after "… send fifty to Ah Girl"), the amount must come from the evidence
    segment that mentions THAT recipient, never from the first number in the turn, a
    multi-instruction turn like "a grand to joint, then fifty to Ah Girl" would otherwise leak
    the joint transfer's S$1,000 onto the Ah Girl payment. Deterministic and payee-scoped, so a
    model can never re-decide how much money moves.
    """
    if not recipient_phrase:
        return None
    target = recipient_phrase.lower()
    for turn in reversed(turns):
        for seg in split_instructions(turn, customer_id):
            r = _segment_recipient(seg, customer_id)
            if r and r.lower() == target:
                amount, _ = parse_amount(instruction_text(seg))
                if amount is not None:
                    return amount
    return None


# A spoken name is a run of CAPITALISED words: "Mei Ling", "John Doe", "Ah Girl". Capturing the
# whole run (rather than one word) is what stops "Send S$50 to Mei Ling" from being reduced to
# the alias "mei" and silently binding to Mei Tan, the customer named one person and the
# payment went to another.
#
# "Capitalised" means capital-then-lowercase, NOT merely uppercase. That distinction is what
# keeps a currency code from being read as a person: "Send US$200" used to capture "US" and ask
# the customer whether they wanted to pay somebody called "Us".
# The trailing \b stops the run landing in the MIDDLE of a CamelCase word: "Pay StarHub bill"
# used to capture "Star" and ask the customer whether they meant a payee called Star.
_NAME_RUN = r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b"


def parse_payee_phrase(text: str, customer_id: Optional[str] = None) -> Optional[str]:
    lowered = text.lower()
    # 0) A NICKNAME the customer uses out loud, "ah girl", "mum", "my daughter". Checked FIRST
    #    and case-insensitively, because spoken ASR hands us "send ah girl $100" in lowercase and
    #    the capitalised-name rule below cannot see lowercase words at all. This returns the
    #    CANONICAL display name, never the nickname: the resolver matches phrases against display
    #    names only, which is exactly why "send mother $50" used to fail even though "mother" was
    #    a known alias.
    if customer_id:
        hit = nickname_match(customer_id, text)
        if hit:
            return hit[1].display_name
    # 1) The name the customer actually said, in full.
    #    The CUE words are matched case-insensitively (a turn often starts "Send …" with a
    #    capital S) but the NAME is not, a capitalised run is what tells us it is a name at
    #    all, so lowercasing that would swallow ordinary words like "fifty dollar".
    run = re.search(rf"(?i:kepada|to|给|pay|transfer to|send)\s+({_NAME_RUN})", text)
    if run:
        phrase = run.group(1).strip()
        # A single-word name that IS a known alias still goes through the alias table, so
        # "Mum" and "mother" both resolve to the same person the way they always did.
        if phrase.lower() in PAYEE_ALIASES:
            return PAYEE_ALIASES[phrase.lower()]
        return phrase
    # 2) Otherwise fall back to the alias table (lowercase nicknames like "mother").
    #    Return the CANONICAL name the alias maps to, not the alias itself, returning "may"
    #    instead of "mei" left the resolver with a phrase that matched no display name, so
    #    "send may $50" was answered with "I couldn't find a saved payee called May" even though
    #    "may" had been in this table the whole time.
    for alias in sorted(PAYEE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return PAYEE_ALIASES[alias]
    return None


# ---------------------------------------------------------------------------
# A name the customer used that we hold nobody for, "ah girl", "husband", "my boss".
#
# "Send Ah Girl S$100" used to be answered with the generic "Mei Ling, who should I pay?", which
# is unanswerable for a customer who has just said exactly who they meant. The useful question is
# "who is Ah Girl?" together with the list of people we DO hold, and once the customer tells us,
# the name is remembered (see app/nicknames.py).
#
# This is deliberately the most conservative detector in the file. It fires ONLY once every other
# reading of the turn has been ruled out, no saved payee, no nickname, no alias, no biller, no
# card, no own account, no source account, because a false positive asks an elderly customer to
# define a name they never used. The implementation is therefore subtraction: remove everything we
# DO understand from the sentence, then ask whether a name-shaped leftover remains.
# ---------------------------------------------------------------------------
# Words a payment instruction can carry WITHOUT naming a person. This is a NOISE list (what to
# ignore), not a recognition list: widening it can only make the detector quieter, never louder.
_PERSON_NOISE_WORDS = (
    # politeness, filler, and the customer referring to themselves
    "please", "pls", "kindly", "can", "could", "would", "help", "me", "i", "you", "we",
    "my", "our", "the", "a", "an", "some", "any", "own", "want", "wanted", "like", "need",
    "just", "only", "already", "again", "now", "instead", "first", "really", "then", "and",
    "if", "it", "that", "there", "here", "back", "over", "about", "how", "much", "many",
    # the payment verbs, in all three languages
    "send", "sends", "sending", "transfer", "transfers", "pay", "pays", "move", "moves",
    "give", "make", "hantar", "bayar", "pindah", "kepada", "tolong", "saya", "nak", "mahu",
    # the prepositions a payment instruction is built from
    "to", "for", "from", "into", "at", "on", "by", "with",
    # money, and the words for an amount
    "money", "cash", "amount", "sum", "total", "dollar", "dollars", "ringgit", "yuan",
    "cent", "cents", "sen", "thousand", "hundred", "million", "ribu", "ratus", "juta",
    # accounts, bills and cards, each of these has its own detector
    "savings", "saving", "current", "multiplier", "usd", "account", "akaun",
    "bill", "bills", "invoice", "card", "credit",
    # when it should run
    "next", "this", "today", "tomorrow", "tonight",
    "morning", "afternoon", "evening", "night", "week", "weeks", "month", "months",
    "day", "days", "am", "pm", "oclock", "clock",
)


def _person_noise_re() -> "re.Pattern[str]":
    """Every word a payment instruction can carry without naming a person.

    Built from the live vocabularies rather than a hand-copied list, so a weekday or a month added
    to `_WEEKDAYS` / `_MONTHS` is ignored here too. The alternative is a name detector that starts
    reading "monday" as somebody's nickname, "send ah girl next monday" would become "ah girl
    monday" and be taught as a nickname.
    """
    words = set(_PERSON_NOISE_WORDS)
    words |= set(NUMBER_WORDS) | set(SCALE_WORDS) | set(AMBIGUOUS_SCALES)
    words |= set(_WEEKDAYS) | set(_MONTHS) | set(_ORDINAL_WORDS)
    return re.compile(
        r"\b(?:" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)
                            if w.strip()) + r")\b", re.IGNORECASE)


def is_known_payee_phrase(phrase: Optional[str], customer_id: Optional[str] = None) -> bool:
    """Whether a phrase names a payee we actually hold, a nickname, a full name, or a first name.

    Mirrors the resolver's own matching (display_name, then first name) closely enough that it
    never disagrees with it: when this says unknown, resolution says unknown too. Used to tell "the
    customer said a name I know" apart from "the customer said a name I don't", which
    parse_payee_phrase alone cannot do, it returns a capitalised name either way.
    """
    if not phrase:
        return False
    key = phrase.strip().lower()
    if key in PAYEE_ALIASES:
        return True
    if customer_id and nickname_match(customer_id, phrase):
        return True
    for payee in get_repository().get_saved_payees(customer_id or ""):
        name = payee.display_name.lower()
        if key == name or key == name.split()[0]:
            return True
    return False


def unknown_payee_phrase(text: str, customer_id: Optional[str] = None) -> Optional[str]:
    """The name-shaped phrase in the turn that we hold nobody for, or None.

    Returns the phrase lowercased, the way the customer would say it back ("ah girl", "husband").
    None means "there is no unknown name here", either the customer named somebody we know, or
    named no person at all, or said something we understand as a bill/card/account instead.
    """
    if not customer_id or not text:
        return None

    named = parse_payee_phrase(text, customer_id)
    if named is not None:
        # A capitalised name or a known alias. Known → not our problem. Unknown → this IS the name.
        return None if is_known_payee_phrase(named, customer_id) else named.strip().lower()

    leftover = mask_dates(text)
    leftover = re.sub(r"\d[\d,.]*", " ", leftover)
    leftover = _person_noise_re().sub(" ", leftover)
    leftover = re.sub(r"[^A-Za-z' ]+", " ", leftover)
    words = [w for w in leftover.split() if len(w) >= 2]
    # One to three words is a name or a nickname ("ah girl", "my boss"). Anything longer is prose
    # we failed to understand, and guessing a nickname out of it would be worse than asking the
    # generic question.
    if not words or len(words) > 3:
        return None
    phrase = " ".join(words).lower()

    # The leftover might BE something we understand rather than a person. Ask each of the other
    # detectors about the LEFTOVER, not about the whole turn: one turn can legitimately name both a
    # source account and a person, "help me send fifty to ah girl from savings", and vetoing on
    # the word "savings" would silently swallow the name the customer actually used.
    if (parse_biller_phrase(phrase, customer_id) or parse_card_phrase(phrase, customer_id)
            or parse_own_account_phrase(phrase, customer_id)
            or parse_source_account_phrase(phrase)
            or is_known_payee_phrase(phrase, customer_id)):
        return None
    return phrase


# Which source account the customer named. The customer says "from savings", "from current",
# "from my USD account"; deterministic code binds that phrase to an authoritative record.
SOURCE_ACCOUNT_ALIASES = {
    "savings": "savings", "saving": "savings", "savings account": "savings",
    "current": "current", "current account": "current",
    "everyday": "current", "everyday account": "current",
    "multiplier": "multiplier", "multiplier account": "multiplier",
    "usd": "usd", "usd account": "usd", "us dollar": "usd", "us dollar account": "usd",
    # Mandarin and Malay names for the same accounts, so "从我的存储账户" and "dari akun simpanan
    # saya" resolve the source the customer actually named instead of silently defaulting it.
    "存储账户": "savings", "储蓄账户": "savings", "储蓄户口": "savings",
    "akun simpanan": "savings", "akaun simpanan": "savings",
    "来往账户": "current", "支票账户": "current", "经常账户": "current",
    "akun semasa": "current", "akaun semasa": "current",
}

# A currency the customer explicitly named. Anything not USD reads as SGD, because the demo
# customer's everyday transfers are in Singapore dollars, but a currency they DID name must
# never be silently converted into another one.
CURRENCY_MARKERS = {
    "USD": re.compile(r"\b(?:us\$|usd|us\s+dollars?)\b", re.I),
}


def parse_currency(text: str) -> str:
    """The currency the customer named, defaulting to SGD.

    "Send US$200" is a US-dollar instruction. Silently rendering it as S$200.00 changed the
    value of the payment the customer was being asked to authorise, which is the one thing a
    read-back must never do.
    """
    for code, pattern in CURRENCY_MARKERS.items():
        if pattern.search(text):
            return code
    return "SGD"


def _phrase_pattern(phrase: str) -> str:
    """A word-boundary pattern that still works when the phrase is Mandarin.

    \\b is only a boundary next to a NON-word character, and CJK characters ARE word characters
    in Unicode mode. So "\\b存储账户\\b" can never match inside "从我的存储账户": the 的 in front
    of it is a word character, so there is no boundary there, and the source the customer named
    was silently defaulted instead.

    The anchor is kept exactly where it still means something, on an ASCII edge, so an English
    alias such as "savings" still cannot match inside a longer word.
    """
    body = re.escape(phrase)
    left = r"\b" if phrase[:1].isascii() and phrase[:1].isalnum() else ""
    right = r"\b" if phrase[-1:].isascii() and phrase[-1:].isalnum() else ""
    return f"{left}{body}{right}"


def parse_source_account_phrase(text: str) -> Optional[str]:
    """Which of the customer's own accounts they named as the source, or None."""
    lowered = text.lower()
    for alias in sorted(SOURCE_ACCOUNT_ALIASES, key=len, reverse=True):
        if re.search(_phrase_pattern(alias), lowered):
            return SOURCE_ACCOUNT_ALIASES[alias]
    return None


def parse_reference(text: str) -> Optional[str]:
    """The reference the customer asked to attach, or None.

    Trailing punctuation is not part of the reference. The old pattern anchored the capture
    at the very end of the string, so "…for birthday." never matched, the final full stop
    killed it and the reference was silently dropped from the read-back. Strip the punctuation
    first, then anchor.
    """
    cleaned = re.sub(r"[\s.,;:!?]+$", "", text.strip())
    match = re.search(r"\b(?:for|reference|ref)\s+([A-Za-z][A-Za-z ]{1,24})$", cleaned, re.I)
    if match:
        return match.group(1).strip().title()
    return None


def _billers_for(customer_id: Optional[str]) -> List:
    """The biller list for one customer, or every biller in the demo when customer_id is None
    (a single-customer demo, so flattening is safe and lets the Intent classifier detect a bill
    without knowing the session's customer_id)."""
    if customer_id:
        return get_repository().get_billers(customer_id)
    return get_repository().get_all_billers()


def parse_biller_phrase(text: str, customer_id: Optional[str] = None) -> Optional[str]:
    """The canonical display name of a known biller mentioned in the turn, else None.

    "Pay my StarHub bill" -> "StarHub". The match is on the full biller display name, so a
    generic "pay my phone bill" with no named company is NOT picked up here, it correctly
    falls through to clarification rather than being silently bound to the wrong biller.

    The match is SPACE-INSENSITIVE on purpose. Spoken ASR splits a compound brand into
    ordinary words, so it hands us "pay my star hub bill" when the customer said "StarHub";
    matching the literal "starhub" alone silently failed every spoken bill payment while the
    very same sentence typed worked. Each name is therefore also tried with optional
    whitespace between its characters: "star hub" and "starhub" both match, while
    "starbucks" still does not.
    """
    lowered = text.lower()
    for b in _billers_for(customer_id):
        name = b.display_name.lower()
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return b.display_name
        # Space-insensitive form: the name's letters with optional whitespace between them.
        flexible = r"\s*".join(re.escape(ch) for ch in name if not ch.isspace())
        if flexible and re.search(rf"\b{flexible}\b", lowered):
            return b.display_name
    return None


def lookup_biller(customer_id: Optional[str], name: str):
    """The authoritative Biller record for a named biller, or None."""
    needle = (name or "").lower()
    for b in _billers_for(customer_id):
        if b.display_name.lower() == needle:
            return b
    return None


# ---------------------------------------------------------------------------
# Credit-card + own-account (internal) destinations, the two remaining MVP
# transaction shapes (IN-07 / IN-05). Same deterministic-directory pattern as
# the biller: a known name in the turn resolves to an authoritative record.
# ---------------------------------------------------------------------------
def _cards_for(customer_id: Optional[str]) -> List:
    if customer_id:
        return get_repository().get_cards(customer_id)
    return get_repository().get_all_cards()


def _own_accounts_for(customer_id: Optional[str]) -> List:
    if customer_id:
        return get_repository().get_own_accounts(customer_id)
    return get_repository().get_all_own_accounts()


def parse_card_phrase(text: str, customer_id: Optional[str] = None) -> Optional[str]:
    """The canonical display name of a known credit card mentioned in the turn, else None.

    "Pay my credit card in full" -> "DBS Visa Credit Card" (the demo customer has one card, so a
    generic "credit card" resolves to it). As with the biller, the match is on the full display
    name, so a bare "card" that is not actually the customer's card is not silently bound.
    """
    lowered = text.lower()
    for c in _cards_for(customer_id):
        if re.search(rf"\b{re.escape(c.display_name.lower())}\b", lowered):
            return c.display_name
    if re.search(r"\b(credit card|my card|the card)\b", lowered):
        cards = _cards_for(customer_id)
        if cards:
            return cards[0].display_name
    return None


def lookup_card(customer_id: Optional[str], name: str):
    """The authoritative Card record for a named card, or None."""
    needle = (name or "").lower()
    for c in _cards_for(customer_id):
        if c.display_name.lower() == needle:
            return c
    return None


# The customer's own account named in the other two supported languages. A joint account is
# 联名账户 in Mandarin and "akun bersama" in Malay, and someone who switches language mid-sentence
# still means the one account they hold. Matched as whole PHRASES, never as single characters, so
# an unrelated word that merely shares a character can never bind a destination.
_OWN_ACCOUNT_ALIASES = re.compile(
    r"联名账户|联名户口|共同账户|共同户口"
    r"|\b(?:akun|akaun)\s+(?:bersama|kongsi)\b", re.I)


def parse_own_account_phrase(text: str, customer_id: Optional[str] = None) -> Optional[str]:
    """The canonical display name of a known own (internal) account in the turn, else None.

    "Move … to my joint account" -> "DBS Joint Account". Matched on the full display name, so a
    generic "account" that is not an internal destination is not silently bound.
    """
    lowered = text.lower()
    for oa in _own_accounts_for(customer_id):
        if re.search(rf"\b{re.escape(oa.display_name.lower())}\b", lowered):
            return oa.display_name
    # A bare "joint" counts too: "move S$1,000 to joint" is how the instruction is actually
    # spoken (IN-10). "joint" as a whole word never matches "join"/"jointly", so this stays tight.
    # The ZH and BM names for the same account count for the same reason.
    if re.search(r"\bjoint\b", lowered) or _OWN_ACCOUNT_ALIASES.search(lowered):
        oas = _own_accounts_for(customer_id)
        if oas:
            return oas[0].display_name
    return None


def lookup_own_account(customer_id: Optional[str], name: str):
    """The authoritative OwnAccount record for a named internal account, or None."""
    needle = (name or "").lower()
    for oa in _own_accounts_for(customer_id):
        if oa.display_name.lower() == needle:
            return oa
    return None


# ---------------------------------------------------------------------------
# Schedule parsing (IN-08), a future execution time the customer spoke.
#
# Deliberately split in two:
#   * an EXTRACTOR says WHAT the customer meant, either the deterministic patterns below, or
#     the Payment Agent's `schedule_hint` on the live path;
#   * ONE computer, compute_schedule(), says WHEN that is, from the current date.
#
# A model never produces a date here. It has no clock, and a model that emitted a date would be
# deciding when money moves, the same reason no other agent decides an amount. An extraction
# may also be malformed or wrong, so every field is validated in compute_schedule() before it
# can become a date.
#
# A turn with no day word yields None, so this never fires on an ordinary "send S$50 to Mei".
# The vocabulary is English-only, matching the rest of the schedule feature (the surrounding
# chat is trilingual, but a date phrase is not currently recognised in BM or Mandarin).
# ---------------------------------------------------------------------------
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_WEEKDAY_NAMES = [name for name, _ in sorted(_WEEKDAYS.items(), key=lambda kv: kv[1])]

# A day named only to be REPLACED. "instead of monday make it tuesday" names two weekdays and
# means the second; "make it tuesday instead of monday" names the same two and means the first.
# Word order cannot tell those apart, but the cue can: whichever weekday follows the cue is the
# one being dropped. Trilingual for the same reason the other safeguards are.
_WEEKDAY_DISCARD_RE = re.compile(
    r"\b(instead of|instead|rather than|other than|not|no longer|replace|change from|"
    r"bukan|daripada)\b"
    r"|不是|改为|不要", re.IGNORECASE)
# How far past a cue its weekday can sit: "instead of next monday" is 13 characters, so 24 is
# generous without reaching so far that an unrelated later day is swallowed.
_WEEKDAY_DISCARD_WINDOW = 24

# Ordinals as actually spoken, "the third of next month", since a date phrase is usually said
# aloud rather than typed as "3rd". The digit form is handled by the same pattern.
_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
    "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20, "twenty-first": 21, "twenty-second": 22,
    "twenty-third": 23, "twenty-fourth": 24, "twenty-fifth": 25, "twenty-sixth": 26,
    "twenty-seventh": 27, "twenty-eighth": 28, "twenty-ninth": 29, "thirtieth": 30,
    "thirty-first": 31,
}

# The number forms accepted inside a day or offset phrase: digits, plus the spoken words small
# enough to be a count of days. Deliberately NOT all of NUMBER_WORDS, "one hundred days" is not
# a scheduling phrase, and every extra word is one more chance to match an amount by accident.
_SMALL_NUMBER_WORDS = {w: v for w, v in NUMBER_WORDS.items()
                       if 1 <= v <= 31 and w.isascii() and " " not in w}
_NUM = (r"\d{1,3}|"
        + "|".join(re.escape(w) for w in sorted(_SMALL_NUMBER_WORDS, key=len, reverse=True)))
_HOUR_NUM = (r"\d{1,2}|" + "|".join(
    re.escape(w) for w, v in sorted(_SMALL_NUMBER_WORDS.items(), key=lambda kv: -len(kv[0]))
    if 1 <= v <= 12))
# A day of the month can be spoken as a cardinal ("three") or an ordinal ("third"), so this one
# accepts both, longest form first so "fifteenth" is never matched as "fifteen".
_DAY_NUM = (r"3[01]|[0-2]?\d|" + "|".join(re.escape(w) for w in sorted(
    set(_SMALL_NUMBER_WORDS) | set(_ORDINAL_WORDS), key=len, reverse=True)))

_SCHEDULE_DAY_OF_MONTH_RE = re.compile(
    rf"\b(?:on\s+)?(?:the\s+)?({_DAY_NUM})\s*(?:st|nd|rd|th)?\s*(?:of\s+)?(next|this)\s+month\b")

# A date named with its month ("28th september", "september 28"), the way a customer actually
# says a specific day. The day and the month must be ADJACENT, which is what keeps the month
# words safe: "may" / "march" / "august" are ordinary English words too, but "may send" and
# "march on" have no day number beside them, so they never match.
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH = "|".join(re.escape(w) for w in sorted(_MONTHS, key=len, reverse=True))
# Two separate patterns, tried month-first, because a single alternation gets "send 50 september 28"
# wrong: re.search returns the LEFTMOST match, so the day-first branch would grab the amount "50"
# as the day and never see "september 28". Searching month-first first removes that trap.
_SCHEDULE_MONTH_FIRST_RE = re.compile(
    rf"\b(?P<m2>{_MONTH})\s+(?P<d2>{_DAY_NUM})\s*(?:st|nd|rd|th)?\b")
_SCHEDULE_DAY_FIRST_RE = re.compile(
    rf"\b(?:on\s+)?(?:the\s+)?(?P<d1>{_DAY_NUM})\s*(?:st|nd|rd|th)?\s*(?:of\s+)?(?P<m1>{_MONTH})\b")
# A bare "the 28th" names a day with no month at all. It is not resolvable to a date, but it must
# still be recognised, or "send 50 on the 28th" turns the day into S$28 and says nothing.
_SCHEDULE_BARE_DAY_RE = re.compile(
    rf"\b(?:on\s+)?the\s+(?P<d>{_DAY_NUM})\s*(?:st|nd|rd|th)?\b")
_SCHEDULE_END_OF_MONTH_RE = re.compile(
    r"\b(?:the\s+)?(?:end|last\s+day)\s+of\s+(?:(next|this)\s+)?(?:the\s+)?month\b"
    r"|\bmonth[-\s]?end\b")
_SCHEDULE_IN_WEEKS_RE = re.compile(rf"\bin\s+({_NUM})\s+weeks?(?:'s\s+time)?\b")
_SCHEDULE_IN_DAYS_RE = re.compile(rf"\bin\s+({_NUM})\s+days?(?:'s\s+time)?\b")
_SCHEDULE_IN_A_WEEK_RE = re.compile(r"\bin\s+a\s+week(?:'s\s+time)?\b")
_SCHEDULE_FORTNIGHT_RE = re.compile(r"\bin\s+(?:a\s+)?fortnight(?:'s\s+time)?\b")

# A bare number is only read as a TIME when the sentence says so. Without this cue
# "send ten dollars tomorrow" was scheduled at 10:00 AM, because the amount word was read as an
# hour, and extending the vocabulary to "in two weeks" would have made it worse.
_SCHEDULE_AMPM_RE = re.compile(rf"\b({_HOUR_NUM})(?::(\d{{2}}))?\s*(am|pm)\b")
_SCHEDULE_AT_HOUR_RE = re.compile(rf"\b(?:at|by)\s+({_HOUR_NUM})(?::(\d{{2}}))?\b")
_SCHEDULE_OCLOCK_RE = re.compile(rf"\b({_HOUR_NUM})\s*o'?clock\b")
_SCHEDULE_DAY_PART_RE = re.compile(r"\b(afternoon|evening|tonight|night|morning)\b")

# Every pattern whose match is a DATE EXPRESSION rather than a value. Used by mask_dates()
# below to keep the numbers inside a date out of the amount parser.
_DATE_PATTERNS = (
    _SCHEDULE_MONTH_FIRST_RE, _SCHEDULE_DAY_FIRST_RE, _SCHEDULE_DAY_OF_MONTH_RE,
    _SCHEDULE_END_OF_MONTH_RE, _SCHEDULE_IN_WEEKS_RE, _SCHEDULE_IN_DAYS_RE,
    _SCHEDULE_IN_A_WEEK_RE, _SCHEDULE_FORTNIGHT_RE, _SCHEDULE_BARE_DAY_RE,
)


def mask_dates(text: str) -> str:
    """Blank out every date expression, keeping the offsets, so its numbers cannot be an amount.

    "on 28th september instead" was read as S$28.00 because "28th" was the only number in the
    turn and the amount parser had no way to know it was a day. "send some money in two weeks"
    had the same fault with the number word "two". Masking the whole phrase, not just the
    number, fixes both, and an explicit amount elsewhere in the turn ("S$50 in two weeks")
    still survives untouched.
    """
    chars = list(text)
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            for i in range(*match.span()):
                chars[i] = " "
    return "".join(chars)


def _number_from(token: Optional[str]) -> Optional[int]:
    """A spoken or written number in a date phrase -> its value, or None."""
    token = (token or "").strip().lower()
    if token.isdigit():
        return int(token)
    return _SMALL_NUMBER_WORDS.get(token) or _ORDINAL_WORDS.get(token)


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _shift_month(year: int, month: int, months_ahead: int) -> Tuple[int, int]:
    index = (year * 12 + (month - 1)) + months_ahead
    return index // 12, index % 12 + 1


def _time_spec_from_text(lowered: str) -> Dict[str, int]:
    """The hour the customer named, or {} to mean "a day was named but no time, use the default".

    An explicit am/pm wins; otherwise an "at"/"by"/"o'clock" cue qualifies a bare hour, and a
    named part of the day ("tomorrow afternoon") supplies the meridiem. Anything else is not
    treated as a time at all.
    """
    hour: Optional[int] = None
    minute = 0
    meridiem: Optional[str] = None

    m = _SCHEDULE_AMPM_RE.search(lowered)
    if m:
        hour, meridiem, minute = _number_from(m.group(1)), m.group(3), int(m.group(2) or 0)
    else:
        part = _SCHEDULE_DAY_PART_RE.search(lowered)
        if part:
            meridiem = "am" if part.group(1) == "morning" else "pm"
        m = _SCHEDULE_AT_HOUR_RE.search(lowered)
        if m:
            hour, minute = _number_from(m.group(1)), int(m.group(2) or 0)
        else:
            m = _SCHEDULE_OCLOCK_RE.search(lowered)
            if m:
                hour = _number_from(m.group(1))

    if hour is None:
        return {}
    meridiem = meridiem or "am"          # a spoken hour with no qualifier is taken as morning
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return {"hour": hour, "minute": minute}


def _weekday_spec(lowered: str) -> Optional[dict]:
    """The weekday the customer means, or None when they named none.

    One weekday, nothing to choose. With TWO, the one that follows a discard cue is the day
    being replaced, not the day wanted, and it is dropped:

        "instead of monday make it tuesday"   -> tuesday
        "make it tuesday instead of monday"   -> tuesday

    Both phrasings name monday first or second and mean tuesday either way, so word order alone
    cannot answer this, the cue can. Anything the cue does not resolve keeps the day named
    first, which is exactly what this extractor did before, so no existing behaviour moves.
    """
    found = []
    for name, index in _WEEKDAYS.items():
        for match in re.finditer(rf"\b{name}\b", lowered):
            found.append((match.start(), index, name))
    if not found:
        return None
    found.sort()

    discarded = set()
    if len(found) > 1:
        for cue in _WEEKDAY_DISCARD_RE.finditer(lowered):
            limit = cue.end() + _WEEKDAY_DISCARD_WINDOW
            for start, _index, _name in found:
                # The FIRST weekday after the cue is the one the cue points at.
                if cue.end() <= start < limit:
                    discarded.add(start)
                    break
    kept = [entry for entry in found if entry[0] not in discarded]
    _start, index, name = kept[0] if kept else found[0]
    return {"kind": "weekday", "weekday": index,
            "next": bool(re.search(rf"\bnext\s+{name}\b", lowered))}


def schedule_spec_from_text(text: str) -> Optional[dict]:
    """The deterministic EXTRACTOR: a spoken future time -> a day/time spec, or None.

    It only says WHAT was meant. Turning that into a calendar date is compute_schedule()'s job,
    so the two stay separable: the live Payment Agent may extract a spec these patterns cannot
    reach, and it still never produces a date itself.
    """
    lowered = text.lower()
    day: Optional[dict] = None

    if re.search(r"\btoday\b", lowered):
        day = {"kind": "today"}
    elif re.search(r"\btomorrow\b", lowered):
        day = {"kind": "tomorrow"}

    if day is None:
        m = _SCHEDULE_DAY_OF_MONTH_RE.search(lowered)
        if m:
            day = {"kind": "day_of_month", "day": _number_from(m.group(1)),
                   "months_ahead": 1 if m.group(2) == "next" else 0}
    if day is None:
        # "september 28" then "28th september", a day named with its month. Month-first is tried
        # first for the reason given at _SCHEDULE_MONTH_FIRST_RE.
        m = _SCHEDULE_MONTH_FIRST_RE.search(lowered)
        if m:
            day = {"kind": "named_date", "day": _number_from(m.group("d2")),
                   "month": _MONTHS[m.group("m2")]}
        else:
            m = _SCHEDULE_DAY_FIRST_RE.search(lowered)
            if m:
                day = {"kind": "named_date", "day": _number_from(m.group("d1")),
                       "month": _MONTHS[m.group("m1")]}
    if day is None:
        # "the 28th" with no month. Deliberately produces a spec compute_schedule must refuse, so
        # the caller asks which month instead of ignoring the date (or reading it as money).
        m = _SCHEDULE_BARE_DAY_RE.search(lowered)
        if m:
            day = {"kind": "named_date", "day": _number_from(m.group("d")), "month": None}
    if day is None:
        m = _SCHEDULE_END_OF_MONTH_RE.search(lowered)
        if m:
            day = {"kind": "end_of_month", "months_ahead": 1 if m.group(1) == "next" else 0}
    if day is None:
        m = _SCHEDULE_IN_WEEKS_RE.search(lowered)
        if m:
            weeks = _number_from(m.group(1))
            day = {"kind": "in_days", "days": weeks * 7} if weeks else None
    if day is None:
        m = _SCHEDULE_IN_DAYS_RE.search(lowered)
        if m:
            days = _number_from(m.group(1))
            day = {"kind": "in_days", "days": days} if days else None
    if day is None and _SCHEDULE_FORTNIGHT_RE.search(lowered):
        day = {"kind": "in_days", "days": 14}
    if day is None and _SCHEDULE_IN_A_WEEK_RE.search(lowered):
        day = {"kind": "in_days", "days": 7}
    if day is None:
        day = _weekday_spec(lowered)
    if day is None:
        return None

    day.update(_time_spec_from_text(lowered))
    return day


def schedule_spec_from_hint(hint) -> Optional[dict]:
    """A model-EXTRACTED hint -> a validated spec, or None.

    Every field is checked here, so a hallucinated or malformed hint is dropped rather than
    turned into a date. A hint carrying a date of its own is ignored on that point: only
    compute_schedule() decides dates.
    """
    if hint is None:
        return None
    if hasattr(hint, "model_dump"):
        hint = hint.model_dump()
    if not isinstance(hint, dict):
        return None

    spec: dict = {}
    kind = str(hint.get("kind") or "").strip().lower()
    if kind in ("today", "tomorrow"):
        spec["kind"] = kind
    elif kind == "weekday":
        name = str(hint.get("weekday") or "").strip().lower()
        if name not in _WEEKDAYS:
            return None
        spec.update(kind="weekday", weekday=_WEEKDAYS[name], next=bool(hint.get("next")))
    elif kind == "in_days":
        days = hint.get("days")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 366:
            return None
        spec.update(kind="in_days", days=days)
    elif kind == "day_of_month":
        day_of_month = hint.get("day")
        if (isinstance(day_of_month, bool) or not isinstance(day_of_month, int)
                or not 1 <= day_of_month <= 31):
            return None
        spec.update(kind="day_of_month", day=day_of_month,
                    months_ahead=1 if hint.get("months_ahead") else 0)
    elif kind == "named_date":
        day_of_month, month = hint.get("day"), hint.get("month")
        if (isinstance(day_of_month, bool) or not isinstance(day_of_month, int)
                or not 1 <= day_of_month <= 31):
            return None
        if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
            return None
        spec.update(kind="named_date", day=day_of_month, month=month)
    elif kind == "end_of_month":
        spec.update(kind="end_of_month", months_ahead=1 if hint.get("months_ahead") else 0)
    else:
        return None

    for field, low, high in (("hour", 0, 23), ("minute", 0, 59)):
        value = hint.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
            spec[field] = value
    return spec


def compute_schedule(spec: Optional[dict], today: Optional[date] = None) -> Optional[Dict[str, str]]:
    """The ONE place a day/time spec becomes a calendar date -> {"relative","absolute","time"}.

    The relative phrase is built here too, from the spec's own numbers, never taken from the
    extractor, so an extraction can only ever say WHAT was meant, and every customer-facing word
    in the SCHEDULE clause still comes from deterministic code.

    Returns None for a spec it cannot trust (unknown kind, out-of-range day, a "this month" day
    that has already passed), which leaves the payment unscheduled rather than silently moved.
    """
    if not spec:
        return None
    base = today or datetime.now().date()
    kind = str(spec.get("kind") or "").lower()
    target: Optional[date] = None
    relative: Optional[str] = None

    if kind == "today":
        target, relative = base, "today"
    elif kind == "tomorrow":
        target, relative = base + timedelta(days=1), "tomorrow"
    elif kind == "weekday":
        index = spec.get("weekday")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 6:
            return None
        # The COMING occurrence, 1-7 days out (7 when the named day is today). That is also the
        # day "next <weekday>" means: the current week's copy of the day has already gone, so the
        # next one IS next week's. Adding a flat +7 on top of it - which this did - skipped a whole
        # week: "next Monday" said on a Tuesday landed 13 days out instead of 6, and "next Friday"
        # said on a Friday landed a fortnight out instead of 7 days. The word "next" still changes
        # the RELATIVE phrase below, because that is what the customer hears back.
        ahead = (index - base.weekday()) % 7 or 7
        is_next = bool(spec.get("next"))
        target = base + timedelta(days=ahead)
        name = _WEEKDAY_NAMES[index]
        relative = f"next {name}" if is_next else name
    elif kind == "in_days":
        days = spec.get("days")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 366:
            return None
        target = base + timedelta(days=days)
        if days % 7 == 0:
            weeks = days // 7
            relative = f"in {weeks} week" + ("" if weeks == 1 else "s")
        else:
            relative = f"in {days} day" + ("" if days == 1 else "s")
    elif kind == "day_of_month":
        day_of_month = spec.get("day")
        months_ahead = spec.get("months_ahead") or 0
        if (isinstance(day_of_month, bool) or not isinstance(day_of_month, int)
                or not 1 <= day_of_month <= 31 or months_ahead not in (0, 1)):
            return None
        year, month = _shift_month(base.year, base.month, months_ahead)
        if day_of_month > calendar.monthrange(year, month)[1]:
            return None
        target = date(year, month, day_of_month)
        if target <= base:
            # "the 3rd of this month" said on the 26th is in the past. Rather than silently
            # scheduling a date that has gone, leave it unscheduled, the read-back then carries
            # no date, and the customer hears that their date was not understood.
            return None
        relative = (f"the {_ordinal(day_of_month)} of "
                    + ("next month" if months_ahead else "this month"))
    elif kind == "named_date":
        # "28th september" / "september 28". The customer named the month, so the only thing left
        # for code to decide is the YEAR: this year if that day is still to come, next year if the
        # month has already gone. A day in the CURRENT month that has already passed is refused
        # outright (same rule as "the Nth of this month") rather than quietly pushed a year out.
        day_of_month = spec.get("day")
        month = spec.get("month")
        if (isinstance(day_of_month, bool) or not isinstance(day_of_month, int)
                or not 1 <= day_of_month <= 31
                or isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12):
            return None
        year = base.year
        if month < base.month:
            year += 1
        elif month == base.month and day_of_month <= base.day:
            return None
        if day_of_month > calendar.monthrange(year, month)[1]:
            return None
        target = date(year, month, day_of_month)
        # Deliberately NO relative phrase. The customer named the month themselves, so
        # "the 28th of September" and the absolute "28 September 2026" are the same date twice,
        # the read-back was saying "the 28th of September, 28 September 2026". The absolute carries
        # the year too, including the next-year case, so nothing is lost by dropping the relative.
        relative = None
    elif kind == "end_of_month":
        months_ahead = spec.get("months_ahead") or 0
        if months_ahead not in (0, 1):
            return None
        year, month = _shift_month(base.year, base.month, months_ahead)
        target = date(year, month, calendar.monthrange(year, month)[1])
        relative = "the end of " + ("next month" if months_ahead else "this month")
    else:
        return None

    hour = spec.get("hour")
    minute = spec.get("minute") or 0
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        hour = 9                          # a day named but no time: default to 9:00
    if isinstance(minute, bool) or not isinstance(minute, int) or not 0 <= minute <= 59:
        minute = 0

    display_hour = hour % 12 or 12
    ampm = "AM" if hour < 12 else "PM"
    return {"relative": relative, "absolute": target.strftime("%d %B %Y"),
            "time": f"{display_hour}:{minute:02d} {ampm}"}


def parse_schedule(text: str, today: Optional[date] = None) -> Optional[Dict[str, str]]:
    """A spoken future time -> {"relative","absolute","time"} or None.

    "tomorrow morning at nine"  -> relative="tomorrow", absolute="27 September 2026",
                                    time="9:00 AM"  (absolute computed from today).
    "in two weeks"              -> relative="in 2 weeks", the date 14 days out.
    "the 3rd of next month"     -> relative="the 3rd of next month", that date.
    "28th september"            -> relative="the 28th of September", that date (next year if the
                                   month has already gone; None if it is this month and past).
    "end of the month"          -> relative="the end of this month", the last day of this month.

    The absolute date is computed from the phrase plus the current date, so it stays correct
    whenever the demo is run. `today` is injectable so tests can freeze the clock.
    """
    return compute_schedule(schedule_spec_from_text(text), today)


def schedule_from_hint(hint, today: Optional[date] = None) -> Optional[Dict[str, str]]:
    """A Payment Agent schedule_hint -> {"relative","absolute","time"} or None.

    The fallback extractor for a phrasings the patterns above cannot reach ("after the school
    holidays", "when my salary comes in"). The hint says what was meant; the date is still
    computed here, from the current date.
    """
    return compute_schedule(schedule_spec_from_hint(hint), today)


def schedule_named_but_unusable(text: str, today: Optional[date] = None) -> bool:
    """True when the turn NAMES a date that was recognised but could not be turned into one.

    "the 3rd of this month" said on the 26th, "31 September", "in 0 days". The customer did name
    a date, so going ahead WITHOUT one, silently, is the wrong answer: the read-back would carry
    no date at all and nothing would tell them it was ignored. The caller asks instead.

    A turn with no date phrase returns False, so an ordinary payment is never questioned.
    """
    spec = schedule_spec_from_text(text)
    return spec is not None and compute_schedule(spec, today) is None


# Cues that mark a turn as CORRECTING something already on screen rather than starting a new
# payment. Trilingual for the same reason the other safeguards are.
_SCHEDULE_CORRECTION_CUE_RE = re.compile(
    r"\b(instead|change|changed|make it|move it|actually|i said|should be|correction|"
    r"wrong|no|not|salah|bukan|sepatutnya|ubah|tukar)\b", re.IGNORECASE)


def is_schedule_correction(text: str) -> bool:
    """True when a turn is ONLY correcting the WHEN of the payment already on screen.

    Deliberately narrow, because the caller uses it to carry the existing payment forward instead
    of re-reading it from the text. It requires all four of:
      * a correction cue ("instead", "make it", "actually", …),
      * a date phrase the extractor recognises,
      * NO new amount, and
      * NO new payee.
    So "no, make it S$60" (amount only) and "actually send it to Ah Girl" (payee only) are NOT
    date corrections and still take the ordinary edit path, only a turn that changes the date and
    nothing else is carried forward, which is what makes keeping the other fields safe.
    """
    if not _SCHEDULE_CORRECTION_CUE_RE.search(text):
        return False
    if schedule_spec_from_text(text) is None:
        return False
    if parse_amount(text)[0] is not None:
        return False
    return parse_payee_phrase(mask_dates(text)) is None


def is_field_correction(text: str) -> bool:
    """True when a turn is correcting a FIELD of the payment already on screen.

    The wider sibling of is_schedule_correction, and it exists for the same reason: the live
    Guardrails model reads the bare correction verb as an attack on a mandatory control. It is not
    fussy about which field is named, either. Measured against the live model on an open draft:

        "change it to sixty"          -> BLOCK / UNSUPPORTED_ACTION    -> froze
        "make it 60 instead"          -> BLOCK / INSTRUCTION_OVERRIDE  -> froze
        "no, make it tomorrow"        -> BLOCK / UNUSABLE_INPUT        -> froze

    all on a ~20-character change to a payment the customer is looking at. Only the DATE case had a
    relaxer, because only the date case had been reported.

    Requires a correction cue AND at least one field the parser can actually read: an amount, a
    date phrase, or a payee. The second half is what keeps it honest -- a bare "no" names no field,
    so it is not a correction and takes the ordinary path. The caller pairs this with
    MANDATORY_CONTROL_RE, so "change my transfer limit to 5000" (a cue plus an amount, but a
    CONTROL) is still excluded there and the block stands.

    Deliberately does NOT require the value to resolve. A correction that cannot be resolved
    ("the 3rd of this month", said on the 26th) still has to REACH intent routing, which asks about
    it -- see schedule_named_but_unusable. A relaxer decides whether a turn gets processed; it never
    decides what is done with it.
    """
    if not _SCHEDULE_CORRECTION_CUE_RE.search(text):
        return False
    if parse_amount(text)[0] is not None:
        return True
    if schedule_spec_from_text(text) is not None:
        return True
    return parse_payee_phrase(mask_dates(text)) is not None


# ---------------------------------------------------------------------------
# An amount with no ceiling ("send everything in savings to John Doe")
# ---------------------------------------------------------------------------
# The customer asked to move an amount that is not a number: "everything", "all my money", "the
# whole balance". parse_amount correctly reads nothing from those words, so the turn reaches the
# ordinary "how much?" question - and that question is the wrong answer. It reads as a request for
# a number, when what actually happened is that Joy was asked for an UNBOUNDED amount, and for a
# transfer that is a way to empty the account.
#
# Every alternative below names a WHOLE, so a turn that does state a number is never caught
# ("send all 50 dollars"): the predicate stands itself down as soon as parse_amount finds one.
# Trilingual, like the other safeguards: the demo is spoken in EN, ZH and BM.
_OPEN_ENDED_AMOUNT_RE = re.compile(
    r"\b(everything|all of it|the lot|every cent|every dollar|as much as possible|"
    r"max(imum)? out|empty (my|the|your) account|clear out (my|the|your) account|"
    r"(whole|entire|full) (balance|savings|account)|"
    r"all (my|the|of my) (money|savings|balance|funds|cash|salary|pay)|"
    r"all my (money|savings|balance|funds|cash|salary|pay)|"
    r"whatever('s| is) (left|in))\b"
    r"|\b(semua(nya)?|habiskan|seluruh)\s+(duit|wang|simpanan|baki|harta)"
    r"|全部|全都|所有|一切|清空|整个账户",
    re.IGNORECASE)


def open_ended_amount(text: str) -> bool:
    """True when the turn asks to send an amount with no ceiling.

    "Send everything in savings to John Doe", "transfer all my money to Mei", "habiskan semua
    duit". The customer named no number because they did not mean a number, so the caller must not
    treat this as an ordinary missing amount: the answer is not "how much?" but a refusal to
    prepare an unbounded payment.

    Read from the INSTRUCTION part of the turn (the same `instruction_text` every other amount
    reader uses), so a side phrase cannot trip it - "explain everything, one at a time" is not an
    amount. A turn that does state a number returns False whatever else it says, so the caller's
    own amount check always wins.
    """
    instruction = instruction_text(text)
    if parse_amount(instruction)[0] is not None:
        return False
    return _OPEN_ENDED_AMOUNT_RE.search(instruction) is not None


# =====================================================================
# A destination or a source Joy cannot use.
#
# Every detector below reads the SENTENCE, and every one of them exists for the same reason: the
# resolver could only ever answer RESOLVED, "which payee?", or FREEZE, so a destination it could
# recognise but not pay collapsed into the generic "Mei Ling, who should I pay?" - the one answer
# that helps nobody. A customer who typed an account number, or named a fixed deposit as the
# funding account, was told Joy did not know who they meant, which is not what happened.
#
# Each detector is a FAMILY, not a phrase. "the blocked account I used last time" and "my frozen
# payee" and "冻结的账户" are the same situation to a customer, so they are the same situation here.
# =====================================================================

# The shared gate. Each of these is a REFUSAL, so each must be sure the customer is actually trying
# to move money: a guard that fired on "why is my account blocked?" would answer a question nobody
# asked, with a refusal to a payment nobody requested.
_MOVE_MONEY_RE = re.compile(
    r"\b(send|sends|sending|transfer|transfers|transferring|remit|pay|pays|paying|"
    r"move|moves|moving|hantar|kirim|pindah|bayar)\b"
    r"|汇款|转账|转钱|汇钱|付给|转给|打钱", re.IGNORECASE)


def asks_to_move_money(text: str) -> bool:
    """True when the turn is asking for money to move: a payment verb, or an amount.

    The gate for every destination refusal below. Deliberately generous about the VERB - "move",
    "pay", "hantar", "转给" are all the same request - because a refusal that never fires is the
    bug being fixed, while a refusal that fires on a non-payment is caught here.
    """
    if not text:
        return False
    return _MOVE_MONEY_RE.search(text) is not None or parse_amount(text)[0] is not None


# ---------------------------------------------------------------------------
# A beneficiary ACCOUNT NUMBER given in chat.
#
# DBS chat cannot register a new payee, so a number here is a destination Joy can recognise but
# must never bind: the destination for every payment comes from the trusted saved-payee record, and
# nothing in the turn may introduce one. Two shapes count as a number, and only two:
#   * an "account" word in front of it - "account 123", "this new account: 123-456-789";
#   * a number written in GROUPS - "123-456-789", "456 789 012". No amount is ever written with a
#     dash, so a grouped run is unambiguous, while a bare "1000" is an amount and never matches.
# ---------------------------------------------------------------------------
_ACCOUNT_WORD = r"(?:account|acct|a/c|akaun|户口|账户|帐号|账号)"
_NUMBER_WORD = r"(?:number|no\.?|nombor|号码|號碼)"
_DIGIT_RUN = r"\d(?:[\d\-\s]{0,20}\d)?"

_ACCOUNT_NUMBER_RE = re.compile(
    rf"{_ACCOUNT_WORD}\s*(?:{_NUMBER_WORD})?\s*[:#\-]?\s*({_DIGIT_RUN})", re.IGNORECASE)
_GROUPED_NUMBER_RE = re.compile(r"(?<![\d,.])(\d{2,6}(?:[\-\s]\d{2,6}){1,4})(?![\d,.])")

# How many digits a real beneficiary account has. Fewer than this is an INCOMPLETE reference rather
# than an unverified one, and the two get different answers: "account 123" is a customer who did not
# finish typing, while "123-456-789" is a customer naming an account Joy does not hold.
BENEFICIARY_MIN_DIGITS = 6


def _digits_only(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def beneficiary_account_number(text: str, customer_id: Optional[str] = None) -> Optional[str]:
    """The beneficiary account number the customer gave in chat, as bare digits, or None.

    A number that matches one of the customer's OWN saved payees' masked endings is not a new
    destination, it is how they identified somebody they already hold, so it is left to the
    ordinary resolver rather than refused here.
    """
    if not text or not asks_to_move_money(text):
        return None
    match = _ACCOUNT_NUMBER_RE.search(text) or _GROUPED_NUMBER_RE.search(text)
    if not match:
        return None
    digits = _digits_only(match.group(1))
    if not digits:
        return None
    if customer_id:
        for payee in get_repository().get_saved_payees(customer_id):
            if digits and digits == _digits_only(payee.masked):
                return None
    return digits


# ---------------------------------------------------------------------------
# A source account that cannot fund a direct transfer.
#
# The customer is not wrong to hold these - a fixed deposit, a unit trust, CPF. The payment simply
# cannot come from there. Silently substituting Savings (what used to happen, because the account
# lookup only searches ELIGIBLE accounts and falls back to the first one) put a read-back in front of
# the customer naming an account they never chose, which is the one thing a read-back must not do.
# ---------------------------------------------------------------------------
_NON_TRANSFERABLE_PRODUCT = (
    r"fixed\s+deposit|time\s+deposit|fd|unit\s+trusts?|investment\s+account|investments?|"
    r"shares?|stocks?|equit(?:y|ies)|cpf|srs|supplementary\s+retirement|retirement\s+account|"
    r"insurance|endowment|savings\s+bond|ssb|treasury|t-?bills?|bonds?"
    r"|定期存款|定存|基金|信托|投资账户|公积金|保险"
)
# Anchored on a SOURCE cue, never on the product name alone. "transfer to my fixed deposit" is a
# DESTINATION the customer is asking for, not a funding account; refusing it as a source would be
# answering a question they did not ask.
_SOURCE_CUE = r"(?:from|out\s+of|using|use|via|dari|dgn|dengan|从|用|拿)"
_POSSESSIVE = r"(?:(?:my|our|the|saya|我(?:的)?)\s*)?"
_NON_TRANSFERABLE_RE = re.compile(
    rf"{_SOURCE_CUE}\s+{_POSSESSIVE}({_NON_TRANSFERABLE_PRODUCT})", re.IGNORECASE)


def non_transferable_account(text: str) -> Optional[str]:
    """The non-transferable product the customer named as the SOURCE, or None.

    Returns the product in the customer's own words ("fixed deposit"), so the refusal can name what
    they actually said rather than a generic "that account".
    """
    if not text or not asks_to_move_money(text):
        return None
    match = _NON_TRANSFERABLE_RE.search(text)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1).strip().lower())


# ---------------------------------------------------------------------------
# A destination the customer describes as BLOCKED.
#
# The customer does not have to name it; "the blocked account I used last time" is how somebody
# refers to a payee whose name they cannot recall. A blocked payee may still exist on the record -
# they saved it before the bank blocked it - so the record is checked too (controls.py), and this
# catches the customer who describes it instead of naming it.
#
# Cue AND destination noun, within a short window, so a stray "frozen" somewhere else in a long
# sentence cannot refuse an ordinary payment. Two patterns because `\b` is meaningless around CJK.
# ---------------------------------------------------------------------------
_BLOCKED_CUE = (r"(?:blocked|block|blacklist(?:ed)?|frozen|freeze|suspend(?:ed)?|banned|"
                r"restricted|disallowed|disekat|sekat|dibekukan|beku|diharamkan)")
_DEST_NOUN = r"(?:account|payee|beneficiary|recipient|person|contact|akaun|penerima|orang)"
_BLOCKED_CJK = r"(?:冻结|黑名单|封禁|禁止|封锁)"
_DEST_NOUN_CJK = r"(?:账户|户口|账号|收款人|对方|这个人)"

_BLOCKED_DESTINATION_RE = re.compile(
    rf"\b{_BLOCKED_CUE}\b[^.!?]{{0,30}}?\b{_DEST_NOUN}\b"
    rf"|\b{_DEST_NOUN}\b[^.!?]{{0,30}}?\b{_BLOCKED_CUE}\b"
    rf"|{_BLOCKED_CJK}[^。！？]{{0,20}}?{_DEST_NOUN_CJK}"
    rf"|{_DEST_NOUN_CJK}[^。！？]{{0,20}}?{_BLOCKED_CJK}", re.IGNORECASE)


def blocked_destination_cue(text: str) -> bool:
    """True when the turn describes the destination it wants to pay as blocked or frozen."""
    if not text or not asks_to_move_money(text):
        return False
    return _BLOCKED_DESTINATION_RE.search(text) is not None


# ---------------------------------------------------------------------------
# Multiple independent instructions in one breath (IN-10)
# Split a single turn such as
#   "Pay StarHub S$86.40 from savings, send Ah Girl S$50, and move S$1,000 to joint"
# into its separate instructions so each can be drafted and reviewed on its own.
#
# Only splits when the turn names 2+ DISTINCT payment targets (a biller, the customer's own
# card, their own account, or a saved payee). A plain single payment, even one that happens
# to contain the word "and", returns [text] unchanged, so the ordinary one-draft path is
# untouched. Two saved payees in one breath is handled upstream by detect_multiple_recipients
# (one transfer at a time), so this only ever splits genuinely independent instructions.
# ---------------------------------------------------------------------------
# The connectors that separate two independent instructions. Trilingual on purpose: the demo is
# spoken in EN, ZH and BM, and a customer who says 然后 or kemudian means exactly what "then"
# means - without them a two-payment breath stayed ONE clause and the second payment was never
# separated. CJK is matched as a bare substring because \b is a no-op around CJK, which is safe
# here: every one of these is a function word, never part of a name or an amount.
_INSTRUCTION_SEP = re.compile(
    r",\s+|，|；|;\s*|\s+and\s+|\s+then\s+"
    r"|然后|之后|接着|还有|再"
    r"|\bkemudian\b|\blepas\s+itu\b|\blalu\b|\bdan\b|\bserta\b", re.I)
_LEADING_CONJ = re.compile(r"^(?:and|then|also|and then)\s+", re.I)


def split_instructions(text: str, customer_id: Optional[str] = None) -> List[str]:
    """The distinct instructions in a turn, or [text] when there is only one.

    The split is conservative: it first confirms the whole turn names 2+ different payment
    targets, then cuts on connectors (", " / " and " / " then " / ";") and keeps only the
    pieces that still resolve to a target. A number like "S$1,000" is never cut because its
    comma is not followed by whitespace.
    """
    def _targets(t: str) -> set:
        found = set()
        if parse_biller_phrase(t, customer_id):
            found.add("BILL")
        if parse_card_phrase(t, customer_id):
            found.add("CARD")
        if parse_own_account_phrase(t, customer_id):
            found.add("OWN")
        # customer_id is passed through: a nickname the customer TAUGHT lives in the per-customer
        # store, so without it "ah girl" resolves to nothing here and a two-instruction breath
        # looks like one, the whole turn then never splits.
        pp = parse_payee_phrase(t, customer_id)
        if pp:
            found.add("PAYEE:" + pp)
        return found

    if len(_targets(text)) < 2:
        return [text]

    def _has_target(t: str) -> bool:
        return bool(parse_biller_phrase(t, customer_id)
                    or parse_card_phrase(t, customer_id)
                    or parse_own_account_phrase(t, customer_id)
                    or parse_payee_phrase(t, customer_id))

    pieces = [p.strip() for p in _INSTRUCTION_SEP.split(text) if p and p.strip()]
    # A comma inside ONE instruction is not the boundary between two.
    #
    # "from my savings account to my joint account, move a grand, then send one fifty to Ah Girl"
    # is two payments, but its first clause leads with the ACCOUNTS and puts the verb and the
    # amount after the comma ("... to my joint account, move a grand"). Cutting on that comma
    # left clause 1 holding the destination with no amount and clause 2 holding an amount with no
    # destination, so neither resolved and the whole breath collapsed into "I didn't catch that".
    #
    # A fragment that names no target of its own is therefore not an instruction, it is the TAIL
    # of the clause before it, and it is glued back on. The same breath then splits into the two
    # payments the customer actually asked for.
    #
    # This can only ever MERGE pieces, never split them, so it cannot invent an instruction that
    # was not spoken. A LEADING fragment with no clause in front of it stays a fragment and is
    # dropped by the target filter below, exactly as it was before.
    grouped: List[str] = []
    for p in pieces:
        p = _LEADING_CONJ.sub("", p).strip()
        if not p:
            continue
        if grouped and not _has_target(p):
            grouped[-1] = f"{grouped[-1]}, {p}"
            continue
        grouped.append(p)
    resolved: List[str] = [p for p in grouped if _has_target(p)]
    return resolved if len(resolved) >= 2 else [text]


