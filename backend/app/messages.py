"""15. The interface catalogue — the ONE place a customer-facing sentence is written.

Every line AuntieJoy says in the conversation is rendered from here. The LLM agents return
codes and structured fields (``message_key``, ``clarification_field``, candidate records) —
never prose that reaches the customer. The deterministic layer turns those codes into text
through this catalogue.

That is what makes the claim in BRD §A.8 E3 literally true: the customer hears *reviewed
deterministic templates*, not free-form model output. It also means the sentence for a given
situation exists exactly once, in three languages, instead of being hand-copied into the
frontend, the mock agents and the deterministic controls.

Addressing
----------
Templates use the ``{name}`` placeholder. It is filled from the trusted customer record
(``data.CUSTOMERS[...]["preferred_name"]``) and never from anything the customer typed
(§A.6 C3). If the record has no preferred name the placeholder falls back to "Auntie".

The same ``{name}`` convention is used by the frontend's own chrome strings in
``translations.js``, so there is one placeholder rule across the whole system.
"""
from typing import Dict, List, Optional

from .parsing import CJK_MARKERS, BM_MARKERS, detect_language
from .data import CUSTOMERS
from .config import money

FALLBACK_NAME = "Auntie"

# =====================================================================
# The catalogue. One key, three languages. Nothing else in the codebase writes
# customer-facing prose.
# =====================================================================
MESSAGES: Dict[str, Dict[str, str]] = {
    # ---------- Stage 1 — audio-risk admission (§6.2) ----------
    "AUDIO_REJECTED": {
        "EN": "I could not verify that this is fresh live speech. For safety, I have paused "
              "the request. Please start again on the registered device.",
        "ZH": "我无法确认这是现场真人说话。为了安全，我先暂停了这笔请求。请在已注册的设备上重新开始。",
        "BM": "Saya tidak dapat pastikan ini suara hidup yang baharu. Untuk keselamatan, saya "
              "sudah hentikan permintaan ini. Sila mula semula pada peranti berdaftar.",
    },
    "VOICE_STEP_UP": {
        "EN": "I could not verify the registered customer from the voice input. To continue, "
              "please review the request and use FaceID on this device.",
        "ZH": "我无法从语音确认您是已注册的客户。如果要继续，请先检查这笔请求，然后在这台设备上用 FaceID 确认。",
        "BM": "Saya tidak dapat mengesahkan pelanggan berdaftar daripada suara itu. Untuk "
              "teruskan, sila semak permintaan ini dan guna FaceID pada peranti ini.",
    },

    # ---------- Stage 3 — Guardrails Agent (§8.5) ----------
    "BLOCK_BYPASS": {
        "EN": "I'm sorry, {name}. I can't skip checking with you first. Every payment needs "
              "your confirmation before it is sent.",
        "ZH": "对不起，{name}，我不能跳过确认这一步。每一笔付款都需要先跟您确认。",
        "BM": "Maaf {name}, saya tidak boleh langkau langkah pengesahan. Setiap bayaran perlu "
              "disahkan oleh {name} dahulu.",
    },
    "BLOCK_OVERRIDE": {
        "EN": "I'm sorry, {name}. I can't change that safety setting. It stays on to protect you.",
        "ZH": "对不起，{name}，我不能更改这个安全设置。它是为了保护您。",
        "BM": "Maaf {name}, saya tidak boleh menukar tetapan keselamatan itu. Ia kekal untuk "
              "melindungi {name}.",
    },
    "PAUSE_SCAM": {
        "EN": "{name}, please stop for a moment. This sounds like it could be a scam, so I will "
              "not send this payment. Please call the person or the bank directly, using a "
              "number you already know and trust.",
        "ZH": "{name}，请先停一下。这听起来可能是诈骗，所以我不会汇出这笔款项。请直接拨打您已经认识和信任的号码，联系对方或银行确认。",
        "BM": "{name}, sila berhenti sebentar. Ini berbunyi seperti penipuan, jadi saya tidak "
              "akan menghantar bayaran ini. Sila hubungi terus orang atau bank itu menggunakan "
              "nombor yang {name} sudah kenal dan percaya.",
    },
    "CLARIFY_INPUT": {
        "EN": "Sorry {name}, I didn't quite catch that. Could you please say it again?",
        "ZH": "对不起 {name}，我没听清楚。可以请您再说一次吗？",
        "BM": "Maaf {name}, saya tidak faham dengan jelas. Bolehkah {name} ulangi sekali lagi?",
    },
    "COOLING_OFF": {
        "EN": "{name}, this looks a little unusual, so I want to pause for a moment before we "
              "continue. Please take your time, and check with someone you trust if you are "
              "not sure.",
        "ZH": "{name}，这笔请求看起来有点不寻常，我想先暂停一下再继续。请您慢慢来，如果不确定，也可以问问您信任的人。",
        "BM": "{name}, permintaan ini kelihatan sedikit luar biasa, jadi saya ingin berhenti "
              "sebentar sebelum meneruskan. Sila ambil masa, dan semak dengan seseorang yang "
              "{name} percaya jika tidak pasti.",
    },
    "COOLING_OFF_SCAM": {
        "EN": "{name}, I can hear this feels very urgent. For your safety I've paused this "
              "payment. Please check the story first — call the person directly, or another "
              "family member, on a number you already know.",
        "ZH": "{name}，我听得出这件事让您很着急。为了安全，我先暂停了这笔付款。请先用您已经知道的号码，直接打给对方或另一位家人，确认一下这件事。",
        "BM": "{name}, saya faham ini rasa sangat cemas. Untuk keselamatan, saya sudah pause "
              "bayaran ini. Sila semak cerita ini dahulu — telefon orang itu terus, atau ahli "
              "keluarga lain, menggunakan nombor yang {name} sudah kenal.",
    },
    "SAFETY_HELP": {
        "EN": "{name}, I've stopped the payment and cancelled anything that was waiting for "
              "your approval. Nothing has been sent. If you think someone is trying to trick "
              "you, please call the bank using the number on your card.",
        "ZH": "{name}，我已经停止这笔付款，并取消了所有等待确认的请求。钱还没有汇出。如果您觉得有人想骗您，请用银行卡上的号码直接打给银行。",
        "BM": "{name}, saya sudah hentikan bayaran ini dan batalkan semua yang menunggu "
              "pengesahan. Wang belum dihantar. Jika {name} rasa ada orang cuba menipu, sila "
              "telefon bank menggunakan nombor pada kad {name}.",
    },

    # ---------- Stage 4 — Intent Classifier (§8.6.5) ----------
    "INTENT_UNCLEAR": {
        "EN": "There's no payment waiting for your confirmation right now, {name}. Tell me the "
              "recipient and the amount, and we'll start again.",
        "ZH": "{name}，现在没有等待您确认的付款。请告诉我收款人和金额，我们再重新开始。",
        "BM": "Tiada bayaran yang menunggu pengesahan sekarang, {name}. Beritahu saya penerima "
              "dan jumlahnya, kita mula semula.",
    },
    "CANCELLED": {
        "EN": "No problem, {name}. I've cancelled that payment, and nothing has been sent.",
        "ZH": "没关系，{name}。这笔付款已经取消，钱没有汇出。",
        "BM": "Tiada masalah, {name}. Bayaran itu sudah dibatalkan dan wang belum dihantar.",
    },

    # ---------- Stage 5 — Payment Agent clarification (§8.8) ----------
    "RECIPIENT_MISSING": {
        "EN": "{name}, who should I pay?",
        "ZH": "{name}，请问要汇给谁？",
        "BM": "{name}, nak hantar kepada siapa?",
    },
    "AMOUNT_MISSING": {
        "EN": "{name}, how much would you like to send to {phrase}?",
        "ZH": "{name}，您想汇多少钱给 {phrase}？",
        "BM": "{name}, berapa banyak {name} nak hantar kepada {phrase}?",
    },
    "AMOUNT_AMBIGUOUS": {
        "EN": "{name}, did you mean {option_a} or {option_b}?",
        "ZH": "{name}，您指的是 {option_a} 还是 {option_b}？",
        "BM": "{name}, maksudkan {option_a} atau {option_b}?",
    },

    # ---------- Stage 6 — payee resolution (§8.3.2) ----------
    "PAYEE_AMBIGUOUS": {
        "EN": "{name}, I found {count} saved payees called {phrase} — {options}. "
              "Which one do you mean?",
        "ZH": "{name}，我找到 {count} 位名叫「{phrase}」的收款人——{options}。请问是哪一位？",
        "BM": "{name}, saya jumpa {count} penerima bernama {phrase} — {options}. Yang mana satu?",
    },
    "PAYEE_NOT_FOUND": {
        "EN": "{name}, I couldn't find a saved payee called {phrase}. Who would you like to pay?",
        "ZH": "{name}，我找不到名叫「{phrase}」的收款人。请问您想汇给谁？",
        "BM": "{name}, saya tidak jumpa penerima bernama {phrase}. {name} nak hantar kepada siapa?",
    },

    # ---------- Stages 6/9/10 — freezes ----------
    "FROZEN_UNTRUSTED_METADATA": {
        "EN": "{name}, I found suspicious text inside {payee}'s saved-payee record. I have "
              "paused this payment for safety. {payee}'s verified destination was not changed, "
              "and no money was sent.",
        "ZH": "{name}，我在 {payee} 的收款人记录里发现可疑文字。为了安全，我先暂停了这笔付款。{payee} 已验证的收款账户没有被更改，钱也没有汇出。",
        "BM": "{name}, saya jumpa teks mencurigakan dalam rekod penerima {payee}. Untuk "
              "keselamatan, saya sudah hentikan bayaran ini. Destinasi {payee} yang telah "
              "disahkan tidak diubah, dan wang belum dihantar.",
    },
    "FROZEN_NO_ACCOUNT": {
        "EN": "{name}, there is no eligible account to pay from, so I've stopped this payment. "
              "No money was sent.",
        "ZH": "{name}，没有可以用来付款的账户，所以我停止了这笔付款。钱没有汇出。",
        "BM": "{name}, tiada akaun yang layak untuk membuat bayaran, jadi saya hentikan bayaran "
              "ini. Wang belum dihantar.",
    },
    "FROZEN_MISMATCH": {
        "EN": "{name}, the amount in the payment does not match what you said, so I have stopped "
              "it. No money was sent.",
        "ZH": "{name}，付款里的金额和您说的不一样，所以我把它停下来了。钱没有汇出。",
        "BM": "{name}, jumlah dalam bayaran tidak sama dengan apa yang {name} sebut, jadi saya "
              "sudah hentikannya. Wang belum dihantar.",
    },
    "FROZEN_RISK": {
        "EN": "{name}, I've paused this payment for safety. No money was sent.",
        "ZH": "{name}，为了安全，我先暂停了这笔付款。钱没有汇出。",
        "BM": "{name}, saya pause bayaran ini untuk keselamatan. Wang belum dihantar.",
    },

    # ---------- Stage 10 → 11 — ready for review ----------
    "REVIEW_READY": {
        "EN": "Good, {name}. That's {amount} to {payee}, from your account {account}. Let's "
              "check it together, one step at a time.",
        "ZH": "好的，{name}。{amount}，汇给 {payee}，从您的账户 {account} 扣款。我们一起一步一步来确认。",
        "BM": "Baik {name}. {amount} kepada {payee}, daripada akaun {account}. Jom kita semak "
              "satu demi satu.",
    },
    "STEP_UP_REQUIRED": {
        "EN": "{name}, this is a larger amount, so I'll ask you to confirm it extra carefully, "
              "one field at a time, before anything is sent.",
        "ZH": "{name}，这笔金额比较大，所以我会请您一步一步特别仔细地确认，确认之后才会汇出。",
        "BM": "{name}, ini jumlah yang agak besar, jadi saya akan minta anda sahkan langkah "
              "demi langkah dengan lebih berhati-hati sebelum apa-apa dihantar.",
    },
    "CONFIRM_NEEDS_BIOMETRIC": {
        "EN": "{name}, I've now confirmed those three details with you. But a confirmation is "
              "not a payment — nothing has been sent. To actually send the money, I still "
              "need your FaceID.",
        "ZH": "{name}，这三项我已经和您确认好了。但确认并不等于汇款——钱还没有汇出。要真正把钱汇出去，"
              "我还需要您的 FaceID。",
        "BM": "{name}, saya sudah sahkan ketiga-tiga butiran ini dengan anda. Tapi pengesahan "
              "bukan bayaran — wang belum dihantar. Untuk benar-benar menghantarnya, saya "
              "masih perlukan FaceID anda.",
    },
    "FIELD_EDIT_REQUESTED": {
        "EN": "No worries, {name}. Just let me know the correct {field}.",
        "ZH": "没关系，{name}。请告诉我正确的{field}。",
        "BM": "Tiada masalah, {name}. Beritahu saya {field} yang betul.",
    },

    # ---------- Stages 14/15 — gateway and ledger ----------
    "REJECTED": {
        "EN": "{name}, that payment could not be completed. No money was sent.",
        "ZH": "{name}，这笔付款无法完成。钱没有汇出。",
        "BM": "{name}, bayaran itu tidak dapat diselesaikan. Wang belum dihantar.",
    },
    "BIOMETRIC_FAILED": {
        "EN": "I could not verify the account owner, so I have stopped this request. "
              "No money was sent.",
        "ZH": "我无法确认账户持有人，所以已经停止这笔请求。钱没有汇出。",
        "BM": "Saya tidak dapat mengesahkan pemilik akaun, jadi saya sudah hentikan permintaan "
              "ini. Wang belum dihantar.",
    },
    "EXECUTED": {
        "EN": "All done, {name}. {amount} has been sent to {payee}. Your reference number is "
              "{reference}.",
        "ZH": "完成了，{name}。{amount} 已经汇给 {payee}，交易编号是 {reference}。",
        "BM": "Selesai, {name}. {amount} telah dihantar kepada {payee}. Nombor rujukan ialah "
              "{reference}.",
    },
    "IDEMPOTENT_RETRY_DONE": {
        "EN": "{name}, this is the same payment we already sent — nothing new was sent. "
              "Your original reference number is {reference}.",
        "ZH": "{name}，这和刚才已经汇出的那笔是同一笔——没有再次汇出。您原来的交易编号是 {reference}。",
        "BM": "{name}, ini bayaran yang sama yang sudah kami hantar — tiada bayaran baharu "
              "dihantar. Nombor rujukan asal anda ialah {reference}.",
    },
}


def interface_language(turn: str, detected: Optional[str] = None) -> str:
    """Which catalogue language to answer this turn in.

    EN / ZH / BM come straight through. MIXED is the interesting case: the customer used two
    languages in one sentence ("Hantar fifty dollars kepada Mei."), so we answer in the one
    they actually led with — script first, then BM, then EN. This is why a code-mixed BM/EN
    turn gets a BM answer and a code-mixed ZH/EN turn gets a ZH answer, deterministically.
    """
    lang = detected or detect_language(turn)
    if lang in ("EN", "ZH", "BM"):
        return lang
    if CJK_MARKERS.search(turn):
        return "ZH"
    if BM_MARKERS.search(turn):
        return "BM"
    return "EN"


def customer_name(session) -> str:
    """The trusted preferred name for this session's customer (§A.6 C3)."""
    record = CUSTOMERS.get(getattr(session, "customer_id", None)) or {}
    return record.get("preferred_name") or FALLBACK_NAME


def join_options(parts: List[str], lang: str) -> str:
    """Join a candidate list the way the target language would say it out loud."""
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if lang == "ZH":
        return "、".join(parts)
    if lang == "BM":
        return " atau ".join(parts)
    return " or ".join(parts)


def candidate_options(candidates: List[dict], lang: str) -> str:
    """Human-readable options for a duplicate-payee question.

    The masked destination travels WITH the name, because "John Doe" alone is exactly the
    ambiguity we are asking about — the customer distinguishes them by the ending digits.
    """
    return join_options(
        [f"{c.get('displayName')} {c.get('maskedDestination')}".strip() for c in candidates],
        lang)


def display_phrase(phrase: Optional[str]) -> str:
    """The customer's own words, tidied for display.

    The parser lowercases the phrase it matches on ("john"), but a question that reads
    "called john" looks like a defect. Only the display form is capitalised — the matching
    still happens on the lowercased phrase, so resolution is unaffected.
    """
    return (phrase or "").strip().title()


def render(message_key: str, lang: str, name: str, **params) -> str:
    """Render one catalogue entry. Unknown key or language returns "" (nothing is said)."""
    table = MESSAGES.get(message_key)
    if not table:
        return ""
    template = table.get(lang) or table.get("EN") or ""
    if not template:
        return ""
    return template.format(name=name, **params)


def say(emit, message_key: str, stage: Optional[int], session, lang: str, **params) -> str:
    """Render a catalogue entry and emit it as the turn's INTERFACE_LINE event.

    This is the only way a sentence reaches the conversation. It is emitted as a normal
    audited event, so the interface line is part of the hash-chained record alongside the
    control decision that produced it.
    """
    text = render(message_key, lang, customer_name(session), **params)
    if not text:
        return ""
    emit("INTERFACE_LINE", stage, {
        "message_key": message_key,
        "language": lang,
        "text": text,
        "rendered_by": "deterministic template catalogue (messages.py)",
        "note": "The conversation only ever speaks reviewed templates, never free-form "
                "model output (§A.8 E3).",
    })
    return text


def amount_pair(amount_minor: int) -> tuple:
    """The two readings of a materially ambiguous scale word (§A.5 B1).

    "fifty" is either fifty dollars or fifty thousand dollars. We do not guess which — we
    show both, computed from the same deterministic parse that raised the ambiguity.
    """
    return money(amount_minor), money(amount_minor * 1000)
