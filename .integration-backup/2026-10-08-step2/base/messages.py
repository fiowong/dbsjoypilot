"""15. The interface catalogue: the ONE place a customer-facing sentence is written.

Every line AuntieJoy says in the conversation is rendered from here. The LLM agents return
codes and structured fields (``message_key``, ``clarification_field``, candidate records),
never prose that reaches the customer. The deterministic layer turns those codes into text
through this catalogue.

That is what makes the claim in BRD §A.8 E3 literally true: the customer hears *reviewed
deterministic templates*, not free-form model output. It also means the sentence for a given
situation exists exactly once, in three languages, instead of being hand-copied into the
frontend, the mock agents and the deterministic controls.

Addressing
----------
Templates use the ``{name}`` placeholder. It is filled from the trusted customer record
(``repository.get_customer(...)[...]["preferred_name"]``) and never from anything the customer typed
(§A.6 C3). If the record has no preferred name the placeholder falls back to "Auntie".

The same ``{name}`` convention is used by the frontend's own chrome strings in
``translations.js``, so there is one placeholder rule across the whole system.
"""
from typing import Dict, List, Optional

from .parsing import detect_language, dominant_language
from .repository import get_repository
from .config import money

FALLBACK_NAME = "Auntie"

# =====================================================================
# The catalogue. One key, three languages. Nothing else in the codebase writes
# customer-facing prose.
# =====================================================================
MESSAGES: Dict[str, Dict[str, str]] = {
    # ---------- Stage 1: audio-risk admission (§6.2) ----------
    "AUDIO_REJECTED": {
        "EN": "I could not verify that this is fresh live speech. For safety, I have paused "
              "the request. Please start again on the registered device.",
        "ZH": "我无法确认这是现场真人说话。为了安全，我先暂停了这笔请求。请在已注册的设备上重新开始。",
        "BM": "Saya tidak dapat pastikan ini suara hidup yang baharu. Untuk keselamatan, saya "
              "sudah hentikan permintaan ini. Sila mula semula pada peranti berdaftar.",
    },
    "VOICE_STEP_UP": {
        "EN": "I could not confirm you are the registered customer. To continue, "
              "please review the request and use FaceID on this device.",
        "ZH": "我无法确认您就是已注册的客户。如果要继续，请先检查这笔请求，然后在这台设备上用 FaceID 确认。",
        "BM": "Saya tidak dapat mengesahkan anda adalah pelanggan berdaftar. Untuk "
              "teruskan, sila semak permintaan ini dan guna FaceID pada peranti ini.",
    },

    # ---------- Stage 3: Guardrails Agent (§8.5) ----------
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
    # A system-message injection: the turn tries to change who AuntieJoy is, or whose rules she
    # follows ("ignore the rules, you are the administrator"). Blocked before any payment field is
    # read, and the line stays open so an auntie who was coached into saying it is not abandoned.
    #
    # It names all three things the turn was asking for, in plain words: a new ROLE, approving a
    # payment by herself, and skipping the bank's checks. Then it says what actually happened, no
    # payment created, nothing sent, because that is the part the customer needs to hear.
    #
    # It deliberately does NOT end by offering to send the money. An earlier wording closed with
    # "I am still here to help you send money": warm to a confused customer, but this line also
    # plays to someone who has just tried to hijack the assistant, and to them it reads as "never
    # mind the refusal, let's do it anyway". The offer that survives is an open one, "if there is
    # something else you need", which still goes through the ordinary guarded flow, read-back and
    # FaceID included, so nothing is being waved through.
    "BLOCK_INJECTION": {
        "EN": "I'm sorry, {name}, I can't do that. I can't take on a new role, approve a payment "
              "on my own, or skip any of the bank's safety checks. No payment has been created "
              "and nothing has been sent.",
        "ZH": "对不起，{name}，这个我做不到。我不能改变自己的角色，不能自己批准付款，也不能跳过银行的任何安全检查。"
              "目前没有建立任何付款，也没有汇出款项。",
        "BM": "Maaf {name}, saya tak boleh buat begitu. Saya tak boleh tukar peranan, tak boleh "
              "luluskan pembayaran sendiri, dan tak boleh langkah sebarang pemeriksaan "
              "keselamatan bank. Tiada pembayaran dicipta dan tiada wang dihantar.",
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
              "payment. Please check the story first: call the person directly, or another "
              "family member, on a number you already know.",
        "ZH": "{name}，我听得出这件事让您很着急。为了安全，我先暂停了这笔付款。请先用您已经知道的号码，直接打给对方或另一位家人，确认一下这件事。",
        "BM": "{name}, saya faham ini rasa sangat cemas. Untuk keselamatan, saya sudah pause "
              "bayaran ini. Sila semak cerita ini dahulu: telefon orang itu terus, atau ahli "
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

    # ---------- Stage 4: Intent Classifier (§8.6.5) ----------
    "INTENT_UNCLEAR": {
        "EN": "There's no payment waiting for your confirmation right now, {name}. Tell me the "
              "recipient and the amount, and we'll start again.",
        "ZH": "{name}，现在没有等待您确认的付款。请告诉我收款人和金额，我们再重新开始。",
        "BM": "Tiada bayaran yang menunggu pengesahan sekarang, {name}. Beritahu saya penerima "
              "dan jumlahnya, kita mula semula.",
    },
    # The same UNCLEAR intent, but a payment IS waiting, on the approval card, which is the one
    # control a typed or spoken answer cannot satisfy. That is the point of the control chain (a
    # generic "yes" must never mint a signature), so it is not a fault to apologise for; but
    # "there's no payment waiting for your confirmation" would be plainly false here. This state
    # therefore gets its own sentence that says what is actually true and where to act.
    "APPROVAL_PENDING": {
        "EN": "{name}, nothing has been sent; that payment is still waiting for you to approve "
              "it. It's on the card on your screen: tap Confirm and I'll take you through the "
              "review one step at a time.",
        "ZH": "{name}，还没有付款。那笔付款仍在等您批准，就在您屏幕上的卡片里。请点按「Confirm」，"
              "我带您一步步核对。",
        "BM": "{name}, belum ada apa-apa dihantar, bayaran itu masih menunggu kelulusan anda. Ia "
              "ada pada kad di skrin anda: tekan Confirm dan saya bawa anda semak satu-satu.",
    },
    # A date the customer DID name, but which could not be turned into a calendar day, "the 3rd
    # of this month" said on the 26th, "the 28th" with no month, "31 September". Going ahead
    # without a date and saying nothing is the one answer that must never be given here: the
    # customer would hear a read-back with no date on it and have no way to know it was ignored.
    "SCHEDULE_UNCLEAR": {
        "EN": "{name}, I heard a date in that, but I couldn't work out which day you meant. "
              "Nothing has been sent. Could you say the day again? For example, tomorrow, or "
              "the 28th of September.",
        "ZH": "{name}，我听到您说了日期，但没能确定是哪一天。还没有汇款，可以请您再说一次吗？"
              "例如「明天」，或是「9月28日」。",
        "BM": "{name}, saya dengar tarikh dalam ayat itu, tetapi tidak dapat pastikan harinya. "
              "Belum ada apa-apa dihantar. Boleh {name} sebut semula harinya? Contohnya esok, "
              "atau 28 September.",
    },
    "CANCELLED": {
        "EN": "No problem, {name}. I've cancelled that payment, and nothing has been sent.",
        "ZH": "没关系，{name}。这笔付款已经取消，钱没有汇出。",
        "BM": "Tiada masalah, {name}. Bayaran itu sudah dibatalkan dan wang belum dihantar.",
    },

    # ---------- Stage 5: Payment Agent clarification (§8.8) ----------
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

    # ---------- Stage 5: a name Joy does not know yet ----------
    # "Send Ah Girl S$100": the customer has a name for their daughter and expects Joy to know it.
    # "Who should I pay?" is unanswerable for somebody who has just said exactly who they meant, so
    # the question names the unknown name AND lists the people we DO hold, the customer only has to
    # recognise one of them. `nickname` is the customer's own word for the person; `payee` (not
    # `name`, which is the customer's own preferred name) is the saved payee it turned out to be.
    "NICKNAME_UNKNOWN": {
        "EN": "{name}, I don't know who {nickname} is. Which of your saved payees is {nickname}: "
              "{options}?",
        "ZH": "{name}，我不知道「{nickname}」是谁。您保存的收款人当中，哪一位是 {nickname}，{options}？",
        "BM": "{name}, saya tidak kenal {nickname}. Yang mana penerima simpanan {name} ialah "
              "{nickname}: {options}?",
    },
    # The binding is confirmed BEFORE it is remembered. A nickname is a redirect to somebody's
    # money, so Joy says which person and which account she is about to bind it to, and waits.
    "NICKNAME_CONFIRM": {
        "EN": "So {nickname} is {payee}, account ending {masked}. Shall I remember that for next "
              "time?",
        "ZH": "所以 {nickname} 就是 {payee}，账号尾号 {masked}。要我记住吗？下次您这样说我就知道了。",
        "BM": "Jadi {nickname} ialah {payee}, akaun berakhir {masked}. Nak saya ingat untuk lain "
              "kali?",
    },
    "NICKNAME_DECLINED": {
        "EN": "No problem, {name}. I won't remember it, and I'll ask you again next time.",
        "ZH": "没关系，{name}。我不记下来了，下次再问您。",
        "BM": "Tiada masalah, {name}. Saya tak akan ingat, lain kali saya tanya lagi.",
    },

    # ---------- Stage 6: payee resolution (§8.3.2) ----------
    "PAYEE_AMBIGUOUS": {
        "EN": "{name}, I found {count} saved payees called {phrase}: {options}. "
              "Which one do you mean?",
        "ZH": "{name}，我找到 {count} 位名叫「{phrase}」的收款人，{options}。请问是哪一位？",
        "BM": "{name}, saya jumpa {count} penerima bernama {phrase}: {options}. Yang mana satu?",
    },
    "PAYEE_NOT_FOUND": {
        "EN": "{name}, I couldn't find a saved payee called {phrase}. Who would you like to pay?",
        "ZH": "{name}，我找不到名叫「{phrase}」的收款人。请问您想汇给谁？",
        "BM": "{name}, saya tidak jumpa penerima bernama {phrase}. {name} nak hantar kepada siapa?",
    },
    # A spoken name that matches no saved payee exactly but is clearly closest to ONE of them
    # ("me tan" -> "Mei Tan"). Joy NAMES that one payee and asks, rather than listing everybody.
    # The customer answers "yes" (or says the name); nothing binds until they do, and the payee
    # is always one they already hold.
    "PAYEE_DID_YOU_MEAN": {
        "EN": "{name}, I don't have a payee called {phrase}. Did you mean {payee}?",
        "ZH": "{name}，我没有名叫「{phrase}」的收款人。您是指 {payee} 吗？",
        "BM": "{name}, saya tak ada penerima bernama {phrase}. Maksudkan {payee}?",
    },

    # ---------- Stage 5: multiple recipients named in one request (MVP: one transfer at a time) ----------
    "MULTIPLE_RECIPIENTS": {
        "EN": "{name}, I can only send money to one person at a time. Which would you like to "
              "pay first, {recipients}?",
        "ZH": "{name}，我一次只能给一个人转账。您想先付给谁，{recipients}？",
        "BM": "{name}, saya hanya boleh hantar duit kepada seorang pada satu masa. Nak bayar "
              "kepada siapa dulu, {recipients}?",
    },

    # ---------- Stages 6/9/10: freezes ----------
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

    # ---------- Customer states insufficient funds (empathetic abort, no pressure) ----------
    "INSUFFICIENT_FUNDS": {
        "EN": "{name}, no worries at all. We don't have to send this right now. If you'd like, "
              "just tell me a smaller amount and I can help with that, or we can leave it for "
              "today. Nothing has been sent.",
        "ZH": "{name}，没关系的。我们不必现在就汇出。如果您愿意，只要告诉我少一点的金额，我来帮您处理；"
              "或者今天就先不汇也可以。钱还没有汇出。",
        "BM": "{name}, tak apa langsung. Kita tidak perlu hantar sekarang. Jika {name} mahu, "
              "beritahu saya jumlah yang lebih kecil dan saya boleh bantu, atau kita boleh "
              "biarkan dulu hari ini. Wang belum dihantar.",
    },

    # ---------- Stage 10 → 11: ready for review ----------
    "REVIEW_READY": {
        "EN": "Good, {name}. That's {amount} to {payee}, from your account {account}. Let's "
              "check it together, one step at a time.",
        "ZH": "好的，{name}。{amount}，汇给 {payee}，从您的账户 {account} 扣款。我们一起一步一步来确认。",
        "BM": "Baik {name}. {amount} kepada {payee}, daripada akaun {account}. Jom kita semak "
              "satu demi satu.",
    },
    # The initiation read-back, what the customer hears the moment a payment has been
    # understood, BEFORE any field is confirmed. Its text is COMPOSED from the fragments in
    # READBACK_PARTS below; see the note there for why this one entry is assembled rather than
    # fixed. It still records as INITIATION_READBACK in the audit trail.
    "INITIATION_READBACK": {
        "EN": "{prefix}{body}{reference}{schedule}. {closer}{colloquial}",
        "ZH": "{prefix}{body}{reference}{schedule}。{closer}{colloquial}",
        "BM": "{prefix}{body}{reference}{schedule}. {closer}{colloquial}",
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
              "not a payment; nothing has been sent. To actually send the money, I still "
              "need your FaceID.",
        "ZH": "{name}，这三项我已经和您确认好了。但确认并不等于汇款，钱还没有汇出。要真正把钱汇出去，"
              "我还需要您的 FaceID。",
        "BM": "{name}, saya sudah sahkan ketiga-tiga butiran ini dengan anda. Tapi pengesahan "
              "bukan bayaran, wang belum dihantar. Untuk benar-benar menghantarnya, saya "
              "masih perlukan FaceID anda.",
    },
    "FIELD_EDIT_REQUESTED": {
        "EN": "No worries, {name}. Just let me know the correct {field}.",
        "ZH": "没关系，{name}。请告诉我正确的{field}。",
        "BM": "Tiada masalah, {name}. Beritahu saya {field} yang betul.",
    },

    # ---------- Stages 14/15: gateway and ledger ----------
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
    # The future-dated counterpart of EXECUTED. A scheduled payment has NOT moved any money, so it
    # gets its own line: no "has been sent", no reference number, and no receipt card (the receipt
    # is keyed off LEDGER_RECORDED, which is deliberately not emitted on this path).
    "SCHEDULED": {
        "EN": "All done, {name}. {amount} is scheduled to be sent to {payee} on {when}. Nothing "
              "has left your account yet, so there is no receipt for this one.",
        "ZH": "完成了，{name}。{amount} 已安排在 {when} 汇给 {payee}。款项尚未从您的账户扣除，"
              "所以这笔还没有收据。",
        "BM": "Selesai, {name}. {amount} dijadualkan untuk dihantar kepada {payee} pada {when}. "
              "Wang belum keluar dari akaun anda, jadi tiada resit untuk bayaran ini.",
    },

    # ---------- IN-10: several independent instructions in one breath ----------
    # The summary line is composed elsewhere (readback_multi) from the MULTI_ITEM_* fragments and
    # handed in as {text}; this key exists only so the composed sentence is rendered + audited
    # through the normal catalogue path like every other customer-facing line.
    "MULTI_REVIEW_INTRO": {
        "EN": "{text}",
        "ZH": "{text}",
        "BM": "{text}",
    },
    # A turn that arrives while a multi-instruction queue is already under review and adds or
    # amends a queued payment ("yes … and send Ah Girl from my Current Account"). The merge is
    # acknowledged in ONE line and the review CONTINUES on the payment already on screen, it must
    # never re-read that payment from the top, which reads as a restart and leaves the customer
    # repeating "yes" forever. Nothing is executed here; the card still needs FaceID.
    "MULTI_MERGE_NOTED": {
        "EN": "Noted, {name}. The payment to {payee} is set to come from {source}. The payment "
              "on your screen is still waiting for your review.",
        "ZH": "好的，{name}。给{payee}的付款已设为从{source}扣款。您屏幕上的那笔付款仍在等您确认。",
        "BM": "Baik, {name}. Bayaran kepada {payee} ditetapkan dari {source}. Bayaran pada skrin "
              "anda masih menunggu pengesahan anda.",
    },
    "IDEMPOTENT_RETRY_DONE": {
        "EN": "{name}, this is the same payment we already sent; nothing new was sent. "
              "Your original reference number is {reference}.",
        "ZH": "{name}，这和刚才已经汇出的那笔是同一笔，没有再次汇出。您原来的交易编号是 {reference}。",
        "BM": "{name}, ini bayaran yang sama yang sudah kami hantar, tiada bayaran baharu "
              "dihantar. Nombor rujukan asal anda ialah {reference}.",
    },
}


# =====================================================================
# The initiation read-back, what the customer hears the moment a payment has been understood,
# BEFORE anything is drafted for approval ("You want to send S$50.00 from Savings ending 1234
# to Mei Tan ending 7788. I can prepare this payment for you to review. Is that correct?").
#
# This is the ONE place a sentence is assembled from parts, and that is deliberate. The same
# read-back has to survive 6 transaction shapes x optional reference x optional schedule
# x 4 closers. One catalogue key per combination would be ~50 near-identical sentences, and
# near-identical sentences are exactly how wording drifts apart, which is the defect this
# section exists to remove. The SHAPE and the PARTS both live here; customer-facing prose
# still exists nowhere else in the codebase.
#
# Three consistency rules, encoded in readback() below so no caller can get them wrong:
#   1. ONE closer per situation. The BRD table used five wordings ("for you to review", "for
#      your review", "for review", "this payment for review", "it for review") for two
#      situations. There are two: the customer stated everything, or we supplied the amount.
#   2. A nickname always ends with the person check, never the generic "Is that correct?".
#      Confirming the PERSON is the whole point of naming the nickname, that is the moment a
#      wrong match would move money to the wrong account.
#   3. A nickname preamble already names the payee and its ending digits, so the body must NOT
#      name it again. (The BRD's own IN-08 read "I found Ah Girl as Mei Ling, account ending
#      7284. You want to send S$50.00 to Mei Ling ending 7284 tomorrow…", the payee twice.)
# =====================================================================
# The sentence shape lives in MESSAGES["INITIATION_READBACK"], not here, one definition, so the
# orchestrator's normal render path and readback() below can never disagree about the shape.

READBACK_PARTS: Dict[str, Dict[str, str]] = {
    # --- who we resolved the nickname to ---
    "PREFIX_NICKNAME": {
        "EN": "I found {nickname} as {name}, account ending {masked}.",
        "ZH": "我找到「{nickname}」就是 {name}，账号尾号 {masked}。",
        "BM": "Saya jumpa {nickname} ialah {name}, akaun berakhir {masked}.",
    },

    # --- the body, one per transaction shape. Verb differs: send / move / pay ---
    # Each shape carries a variant for a MISSING slot. A body that reads "You want to send
    # S$50.00 from Savings ending 1234 to ." is worse than the double-naming it replaced, so a
    # missing slot selects a variant that omits the whole clause rather than stranding the
    # preposition. BODY_VARIANTS below records which variants each shape actually defines.
    "BODY_TRANSFER": {
        "EN": "You want to send {amount} from {source} to {destination}",
        "ZH": "您想从 {source} 转 {amount} 给 {destination}",
        "BM": "Anda nak hantar {amount} dari {source} kepada {destination}",
    },
    "BODY_TRANSFER_NODST": {
        "EN": "You want to send {amount} from {source}",
        "ZH": "您想从 {source} 转 {amount}",
        "BM": "Anda nak hantar {amount} dari {source}",
    },
    "BODY_TRANSFER_NOSRC": {
        "EN": "You want to send {amount} to {destination}",
        "ZH": "您想转 {amount} 给 {destination}",
        "BM": "Anda nak hantar {amount} kepada {destination}",
    },
    "BODY_TRANSFER_MIN": {
        "EN": "You want to send {amount}",
        "ZH": "您想转 {amount}",
        "BM": "Anda nak hantar {amount}",
    },
    "BODY_MOVE": {
        "EN": "You want to move {amount} from {source} to {destination}",
        "ZH": "您想从 {source} 转 {amount} 到 {destination}",
        "BM": "Anda nak pindahkan {amount} dari {source} ke {destination}",
    },
    "BODY_MOVE_NODST": {
        "EN": "You want to move {amount} from {source}",
        "ZH": "您想从 {source} 转 {amount}",
        "BM": "Anda nak pindahkan {amount} dari {source}",
    },
    "BODY_MOVE_NOSRC": {
        "EN": "You want to move {amount} to {destination}",
        "ZH": "您想转 {amount} 到 {destination}",
        "BM": "Anda nak pindahkan {amount} ke {destination}",
    },
    "BODY_MOVE_MIN": {
        "EN": "You want to move {amount}",
        "ZH": "您想转 {amount}",
        "BM": "Anda nak pindahkan {amount}",
    },
    "BODY_BILL": {
        "EN": "Your latest {biller} bill is {amount}. You want to pay it from {source}",
        "ZH": "您最新的 {biller} 账单是 {amount}。您想从 {source} 支付",
        "BM": "Bil {biller} terkini anda ialah {amount}. Anda nak bayar dari {source}",
    },
    "BODY_BILL_NOSRC": {
        "EN": "Your latest {biller} bill is {amount}",
        "ZH": "您最新的 {biller} 账单是 {amount}",
        "BM": "Bil {biller} terkini anda ialah {amount}",
    },
    "BODY_CARD": {
        "EN": "Your current credit-card balance is {amount}. You want to pay this amount from {source}",
        "ZH": "您目前的信用卡余额是 {amount}。您想从 {source} 支付这笔金额",
        "BM": "Baki kad kredit anda sekarang ialah {amount}. Anda nak bayar jumlah ini dari {source}",
    },
    "BODY_CARD_NOSRC": {
        "EN": "Your current credit-card balance is {amount}",
        "ZH": "您目前的信用卡余额是 {amount}",
        "BM": "Baki kad kredit anda sekarang ialah {amount}",
    },

    # --- optional clauses ---
    "REFERENCE": {
        "EN": ", with the reference '{reference}'",
        "ZH": "，备注是「{reference}」",
        "BM": ", dengan rujukan '{reference}'",
    },
    "SCHEDULE": {
        "EN": " {relative}, {absolute} at {time} Singapore time",
        "ZH": " {relative}，{absolute} {time}（新加坡时间）",
        "BM": " {relative}, {absolute} pada {time} waktu Singapura",
    },
    # Used when there is no relative phrase to give, the customer named the calendar date itself,
    # so a relative would only repeat the absolute ("the 28th of September, 28 September 2026").
    # One date, said once.
    "SCHEDULE_DATE_ONLY": {
        "EN": " {absolute} at {time} Singapore time",
        "ZH": " {absolute} {time}（新加坡时间）",
        "BM": " {absolute} pada {time} waktu Singapura",
    },

    # --- closers ---
    "CLOSE_REVIEW": {
        "EN": "I can prepare this payment for you to review. Is that correct?",
        "ZH": "我可以先准备好这笔付款让您核对。对吗？",
        "BM": "Saya boleh sediakan bayaran ini untuk anda semak. Betul ke?",
    },
    "CLOSE_SYSTEM_AMOUNT": {
        "EN": "Shall I prepare this payment for review?",
        "ZH": "要我准备这笔付款让您核对吗？",
        "BM": "Nak saya sediakan bayaran ini untuk semakan?",
    },
    "CLOSE_PERSON": {
        "EN": "Is {person} the correct person?",
        "ZH": "这位是 {person}，对吗？",
        "BM": "Betul ke orang ini {person}?",
    },
    "ASK_SOURCE": {
        "EN": "Which account should the money come from?",
        "ZH": "这笔钱要从哪个账户扣？",
        "BM": "Duit ini nak keluar dari akaun mana?",
    },

    # --- multiple instructions in one breath ---
    # The items are bare-verb clauses, NOT the single-instruction body. "Your latest StarHub bill
    # is S$86.40. You want to pay it from…" is not a clause that can be listed; "pay StarHub
    # S$86.40 from…" is. Each clause is then numbered onto its own line by MULTI_ITEM_LINE, so
    # the bare-verb form is what makes "First, pay StarHub S$86.40…" read naturally.
    "MULTI_ITEM_SEND": {
        "EN": "send {amount} from {source} to {destination}",
        "ZH": "从 {source} 转 {amount} 给 {destination}",
        "BM": "hantar {amount} dari {source} kepada {destination}",
    },
    "MULTI_ITEM_MOVE": {
        "EN": "move {amount} from {source} to {destination}",
        "ZH": "从 {source} 转 {amount} 到 {destination}",
        "BM": "pindahkan {amount} dari {source} ke {destination}",
    },
    "MULTI_ITEM_PAY_BILL": {
        "EN": "pay {biller} {amount} from {source}",
        "ZH": "从 {source} 支付 {biller} {amount}",
        "BM": "bayar {biller} {amount} dari {source}",
    },
    "MULTI_ITEM_PAY_CARD": {
        "EN": "pay your credit-card balance {amount} from {source}",
        "ZH": "从 {source} 支付信用卡余额 {amount}",
        "BM": "bayar baki kad kredit {amount} dari {source}",
    },
    # The multi-instruction summary is spoken as SEPARATE LINES, not one run-on sentence: the
    # customer asked for it one at a time, and three payments strung together with "; and" is
    # exactly the wall of words an elderly customer cannot hold in their head. So
    # readback_multi_lines() returns one line per instruction and the orchestrator emits each as
    # its own INTERFACE_LINE.
    "MULTI_HEAD": {
        "EN": "Of course. I will explain one payment at a time. I heard {count} instructions.",
        "ZH": "好的。我会一笔一笔说明。我听到{count}个指示。",
        "BM": "Baiklah. Saya akan terangkan satu-satu. Saya dengar {count} arahan.",
    },
    "MULTI_SEP_MID": {"EN": "; ", "ZH": "；", "BM": "; "},
    "MULTI_SEP_LAST": {"EN": "; and ", "ZH": "；还有", "BM": "; dan "},
    "MULTI_ITEM_LINE": {
        "EN": "{ordinal}, {clause}",
        "ZH": "{ordinal}，{clause}",
        "BM": "{ordinal}, {clause}",
    },
    # How the FIRST instruction is named in the closing offer. Kept per transaction type because
    # "the StarHub bill" and "the payment to Mei Tan" are not the same shape of phrase.
    "MULTI_FIRST_BILL": {
        "EN": "the {biller} bill",
        "ZH": "{biller} 账单",
        "BM": "bil {biller}",
    },
    "MULTI_FIRST_SEND": {
        "EN": "the payment to {destination}",
        "ZH": "给 {destination} 的这笔汇款",
        "BM": "bayaran kepada {destination}",
    },
    "MULTI_FIRST_MOVE": {
        "EN": "the transfer to {destination}",
        "ZH": "转到 {destination} 的这笔转账",
        "BM": "pindahan ke {destination}",
    },
    "MULTI_FIRST_CARD": {
        "EN": "your credit-card payment",
        "ZH": "您的信用卡付款",
        "BM": "bayaran kad kredit anda",
    },
    "MULTI_TAIL": {
        "EN": "Let us start with {first}. Shall I prepare it for your review?",
        "ZH": "我们就先从{first}开始。我先准备好让您核对，好吗？",
        "BM": "Mari kita mulakan dengan {first}. Boleh saya sediakan untuk anda semak?",
    },
    # An instruction that named NO source account: the clause omits the "from …" and asks,
    # instead of silently defaulting to the customer's first eligible account.
    "MULTI_ASK_SOURCE": {
        "EN": "Which account should that payment come from?",
        "ZH": "这笔付款要从哪个账户出？",
        "BM": "Akaun mana yang patut bayaran ini dikeluarkan?",
    },
    # A bare-verb clause with no source (the companion to MULTI_ASK_SOURCE above).
    "MULTI_ITEM_SEND_NOSRC": {
        "EN": "send {amount} to {destination}",
        "ZH": "转 {amount} 给 {destination}",
        "BM": "hantar {amount} kepada {destination}",
    },
    "MULTI_ITEM_MOVE_NOSRC": {
        "EN": "move {amount} to {destination}",
        "ZH": "转 {amount} 到 {destination}",
        "BM": "pindahkan {amount} kepada {destination}",
    },
    # A saved-payee destination reached through a nickname the customer said out loud: the
    # spoken name leads, the resolved person and ending digits follow, "to Ah Girl, whom I
    # found as Mei Tan ending 7788". Mirrors PREFIX_NICKNAME but inlined into the clause so a
    # multi-instruction line reads naturally without a separate preamble line.
    "MULTI_NICKNAME_DEST": {
        "EN": "{spoken}, whom I found as {name} ending {masked}",
        "ZH": "「{spoken}」，我找到就是 {name}，尾号 {masked}",
        "BM": "{spoken}, yang saya jumpa ialah {name}, berakhir {masked}",
    },
    # A slang scale word the customer said out loud ("a grand", "5k"). The system states the
    # interpretation it assumed and asks the customer to confirm it, the same "advise first"
    # posture as every other field. Standard words ("thousand", "ribu") are never flagged.
    "COLLOQUIAL_CONFIRM": {
        "EN": "Please confirm that a '{phrase}' means {money}.",
        "ZH": "请确认「{phrase}」是指 {money}。",
        "BM": "Sila sahkan bahawa '{phrase}' bermaksud {money}.",
    },
}

# Which body variants each transaction shape defines. The composer consults this so it can never
# select a fragment that does not exist, a missing fragment would render an EMPTY body, which
# is the worst possible failure for a line the customer has to authorise.
BODY_VARIANTS: Dict[str, tuple] = {
    "BODY_TRANSFER": ("", "_NODST", "_NOSRC", "_MIN"),
    "BODY_MOVE": ("", "_NODST", "_NOSRC", "_MIN"),
    "BODY_BILL": ("", "_NOSRC"),
    "BODY_CARD": ("", "_NOSRC"),
}

# "I heard three instructions", spelled out, because "I heard 3 instructions" is not speech.
COUNT_WORDS: Dict[str, Dict[int, str]] = {
    "EN": {2: "two", 3: "three", 4: "four", 5: "five"},
    "ZH": {2: "两", 3: "三", 4: "四", 5: "五"},
    "BM": {2: "dua", 3: "tiga", 4: "empat", 5: "lima"},
}

# The ordinal that opens each line of the multi-instruction summary ("First,", "Second,").
# Separate from COUNT_WORDS on purpose: "two" counts the instructions in the preamble, "Second"
# numbers them one at a time, and the two are not interchangeable in any of the three languages.
MULTI_ORDINALS: Dict[str, Dict[int, str]] = {
    "EN": {1: "First", 2: "Second", 3: "Third", 4: "Fourth", 5: "Fifth"},
    "ZH": {1: "第一", 2: "第二", 3: "第三", 4: "第四", 5: "第五"},
    "BM": {1: "Pertama", 2: "Kedua", 3: "Ketiga", 4: "Keempat", 5: "Kelima"},
}

# Closers that ASK FOR A MISSING FIELD. These suppress the nickname preamble: the customer is
# being asked exactly one question, and a "by the way, Ah Girl is Mei Tan" would make it two.
ASK_CLOSERS = frozenset({"ASK_SOURCE"})


def _part(key: str, lang: str, **slots) -> str:
    """One read-back fragment in the requested language, filled with its slots."""
    table = READBACK_PARTS.get(key)
    if not table:
        return ""
    template = table.get(lang) or table.get("EN") or ""
    return template.format(**slots) if template else ""


def _body_variant(body: str, source: str, destination: str) -> str:
    """The body variant that fits the slots we actually have.

    Never returns a key outside BODY_VARIANTS[body], so the rendered body is never empty and
    never leaves a preposition stranded on a missing value.
    """
    allowed = BODY_VARIANTS.get(body, ("",))
    if body in ("BODY_BILL", "BODY_CARD"):
        suffix = "" if source else "_NOSRC"          # a bill has no destination
    elif source and destination:
        suffix = ""
    elif source:
        suffix = "_NODST"
    elif destination:
        suffix = "_NOSRC"
    else:
        suffix = "_MIN"
    if suffix not in allowed:
        suffix = "_MIN" if "_MIN" in allowed else allowed[-1]
    return body + suffix


def readback_item(lang: str, *, body: str, amount: str, source: str = "", destination: str = "",
                  biller: str = "", reference: Optional[str] = None,
                  schedule: Optional[Dict[str, str]] = None) -> str:
    """One instruction's clause, without its closer."""
    key = _body_variant(body, source, destination)
    # A schedule with no relative phrase gets the date-only clause, so the date is said once.
    schedule_key = None
    if schedule:
        schedule_key = "SCHEDULE" if schedule.get("relative") else "SCHEDULE_DATE_ONLY"
    return (_part(key, lang, amount=amount, source=source, destination=destination, biller=biller)
            + (_part("REFERENCE", lang, reference=reference) if reference else "")
            + (_part(schedule_key, lang, **schedule) if schedule_key else ""))


def multi_item(lang: str, *, shape: str, amount: str, source: str = "", destination: str = "",
               biller: str = "") -> str:
    """One bare-verb clause for a multi-instruction read-back.

    shape is one of MULTI_ITEM_SEND / MULTI_ITEM_MOVE / MULTI_ITEM_PAY_BILL /
    MULTI_ITEM_PAY_CARD. Deliberately a different fragment set from the single-instruction
    body, see the note on the MULTI_ITEM_* entries above.
    """
    return _part(shape, lang, amount=amount, source=source, destination=destination,
                 biller=biller)


def readback_parts(lang: str, *, body: str, closer: str, amount: str, source: str = "",
                   destination: str = "", biller: str = "", reference: Optional[str] = None,
                   schedule: Optional[Dict[str, str]] = None,
                   nickname: Optional[Dict[str, str]] = None,
                   person: str = "", colloquial: str = "") -> Dict[str, str]:
    """The assembled fragments of one initiation read-back, as render() params.

    body      BODY_TRANSFER | BODY_MOVE | BODY_BILL | BODY_CARD
    closer    CLOSE_REVIEW | CLOSE_SYSTEM_AMOUNT | CLOSE_PERSON | ASK_SOURCE
    schedule  {"relative": "tomorrow", "absolute": "27 September 2026", "time": "9:00 AM"}
    nickname  {"nickname": "Ah Girl", "name": "Mei Tan", "masked": "7788"}

    The orchestrator passes these straight to say_line("INITIATION_READBACK", …), so the
    sentence goes through the normal catalogue render path and is audited like any other line.

    Rules 2 and 3 from the section header are applied HERE rather than left to the caller, so
    the nickname handling cannot drift back out of alignment.
    """
    prefix = ""
    if closer in ASK_CLOSERS:
        # One question at a time: the missing field IS the question, so no preamble.
        nickname = None
    elif nickname:
        prefix = _part("PREFIX_NICKNAME", lang, **nickname) + " "
        destination = ""                 # the preamble already named the payee and its digits
        closer = "CLOSE_PERSON"          # a nickname is exactly when the person must be confirmed
        person = person or nickname.get("name", "")

    return {
        "prefix": prefix,
        "body": readback_item(lang, body=body, amount=amount, source=source,
                              destination=destination, biller=biller, reference=reference,
                              schedule=schedule),
        "reference": "",                 # already folded into the body clause above
        "schedule": "",
        "closer": _part(closer, lang, person=person),
        "colloquial": colloquial,
    }


def colloquial_confirm(lang: str, phrase: str, amount_minor: int, currency: str) -> str:
    """The confirm sentence for a slang scale word ("a grand" -> S$1,000.00)."""
    return _part("COLLOQUIAL_CONFIRM", lang, phrase=phrase,
                 money=money(amount_minor, currency))


def readback(lang: str, **parts) -> str:
    """Render the initiation read-back in full, for tests and previews.

    The orchestrator renders the same sentence through the catalogue (say_line) using
    readback_parts(); this helper exists so the composer can be exercised without an emit.
    """
    table = MESSAGES.get("INITIATION_READBACK") or {}
    shape = table.get(lang) or table.get("EN") or ""
    return shape.format(**readback_parts(lang, **parts)).strip()


def readback_multi_lines(lang: str, items: List[str], first: str = "") -> List[str]:
    """The read-back for several independent instructions, as SEPARATE LINES.

    One line per instruction: a preamble ("I heard two instructions."), then "First, …",
    "Second, …", then an offer to start with the first. Returned as a list because the
    orchestrator emits each entry as its own INTERFACE_LINE, so the customer hears and sees one
    payment per line instead of a single sentence joining three payments with "; and".

    Each item comes from multi_item(), so the per-instruction wording cannot drift away from the
    single-instruction case by being written twice.
    """
    if not items:
        return []
    count = COUNT_WORDS.get(lang, COUNT_WORDS["EN"]).get(len(items), str(len(items)))
    ordinals = MULTI_ORDINALS.get(lang, MULTI_ORDINALS["EN"])
    terminator = {"ZH": "。", "BM": ".", "EN": "."}.get(lang, ".")
    terminal_punct = set(".?!。？！")
    lines = [_part("MULTI_HEAD", lang, count=count)]
    for index, item in enumerate(items, 1):
        clause = item.rstrip()
        # A clause that already ends in sentence punctuation (e.g. a trailing "Please confirm…"
        # or "Which account…?") must not get a second period from the line wrapper.
        if clause and clause[-1] not in terminal_punct:
            clause = clause + terminator
        lines.append(_part("MULTI_ITEM_LINE", lang,
                           ordinal=ordinals.get(index, str(index)), clause=clause))
    lines.append(_part("MULTI_TAIL", lang, first=first))
    return [line for line in lines if line.strip()]


def readback_multi(lang: str, items: List[str], first: str = "") -> str:
    """The same summary joined with newlines, for previews and tests.

    The orchestrator uses readback_multi_lines() and speaks each line separately.
    """
    # "\n\n", not "\n": each block is its own paragraph inside the one bubble. Four lines with
    # no space between them still reads as a wall of words to an elderly customer, which is the
    # exact thing the one-at-a-time framing is meant to avoid.
    return "\n\n".join(readback_multi_lines(lang, items, first))


def multi_first_label(lang: str, *, transaction_type: str, biller: str = "",
                      destination: str = "") -> str:
    """How the FIRST instruction is named in the closing offer ("Let us start with …").

    Lives here rather than in the orchestrator so the wording stays in the catalogue: a bill is
    "the StarHub bill" while a transfer is "the payment to Mei Tan", and neither is a string the
    orchestrator should be assembling.
    """
    if transaction_type == "BILL_PAYMENT":
        return _part("MULTI_FIRST_BILL", lang, biller=biller)
    if transaction_type == "CREDIT_CARD_PAYMENT":
        return _part("MULTI_FIRST_CARD", lang)
    if transaction_type == "OWN_ACCOUNT_TRANSFER":
        return _part("MULTI_FIRST_MOVE", lang, destination=destination)
    return _part("MULTI_FIRST_SEND", lang, destination=destination)


def interface_language(turn: str, detected: Optional[str] = None) -> str:
    """Which catalogue language to answer this turn in.

    A single-language turn is answered in that language, EN / ZH / BM come straight through,
    so a customer who speaks only Mandarin still hears Mandarin, and only Malay still hears
    Malay.

    MIXED is answered in the language the customer used MOST (dominant_language()). Not the
    first script found, and not always English: "帮我 bayar bil Starhub dari current account" is
    one breath of Mandarin, Malay and English, and counting the words says English, which is
    also what the customer would choose, since the Mandarin there is three short runs. A genuine
    tie goes to English as the neutral common tongue at a Singapore counter.

    This is a PRESENTATION decision only. detect_language() is untouched and still reports what
    the customer actually spoke, so the audit trail stays honest about the input (a MIXED turn
    is still recorded as MIXED) even while the answer goes out in English.
    """
    lang = detected or detect_language(turn)
    if lang in ("EN", "ZH", "BM"):
        return lang
    return dominant_language(turn)


def customer_name(session) -> str:
    """The trusted preferred name for this session's customer (§A.6 C3)."""
    record = get_repository().get_customer(getattr(session, "customer_id", None)) or {}
    return record.get("preferred_name") or FALLBACK_NAME


def source_account_phrase(customer_id: Optional[str],
                          source_account_id: Optional[str]) -> str:
    """"DBS Savings ending 1234", built from the trusted customer record, never from chat.

    The draft carries only account_masked; the display name lives on the authoritative record,
    so it is looked up here rather than taken from anything the customer said (§A.6 C3).
    Returns "" when the account is not on the record, so the read-back omits the clause rather
    than inventing a name.
    """
    record = get_repository().get_customer(customer_id) or {}
    for account in record.get("accounts", []):
        if account.account_id == source_account_id:
            return f"{account.display_name} {account.masked}".strip()
    return ""


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
    ambiguity we are asking about, the customer distinguishes them by the ending digits.
    """
    return join_options(
        [f"{c.get('displayName')} {c.get('maskedDestination')}".strip() for c in candidates],
        lang)


def display_phrase(phrase: Optional[str]) -> str:
    """The customer's own words, tidied for display.

    The parser lowercases the phrase it matches on ("john"), but a question that reads
    "called john" looks like a defect. Only the display form is capitalised, the matching
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

    "fifty" is either fifty dollars or fifty thousand dollars. We do not guess which, we
    show both, computed from the same deterministic parse that raised the ambiguity.
    """
    return money(amount_minor), money(amount_minor * 1000)
