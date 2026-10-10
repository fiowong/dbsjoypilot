"""Known scam and control-evasion patterns, as ONE deterministic table.

Each row is a scam typology (government impersonation, a secrecy instruction, a romance scam, ...)
with the cue words that identify it in English, Malay and Mandarin, the consequence it carries,
and the message the customer hears. The table is consulted after the Guardrails Agent: a match can
only ever RAISE the verdict (ALLOW < CLARIFY < SAFETY_INTERVENTION < BLOCK), never lower it, so a
model that misses a scam cannot let it through, and a model that is stricter still wins.

Why a table rather than more agent prompting: every row here is a typology published in scam
advisories, the consequence for it is a policy decision, and a policy decision belongs in
reviewable configuration, not in a probabilistic model (BRD §8.10). A row matches on CUES, never
on a whole demo sentence, and every row is pinned by a selftest case.

A row's `all` lists cue groups that must ALL be present (each group is a regex). A row matches
only when every group finds something in the turn, so "my grandson" alone is not a scam, "my
grandson ... new number" is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .models import GuardrailDecision, RiskCategory


@dataclass(frozen=True)
class ScamPattern:
    pattern_id: str
    typology: str
    decision: GuardrailDecision
    risk_category: RiskCategory
    message_key: str
    all: Tuple[str, ...]
    # Cues that make the row NOT apply (e.g. a payee is named, for the urgency-only row).
    unless: Tuple[str, ...] = ()


_I = re.I

# Shared cue fragments.
_MONEY = r"(s?\$\s?\d|\d+\s*(dollars?|dolar|ringgit|块|元|新元)|pay|send|transfer|kirim|hantar|bayar|转|汇|付)"
_FAMILY = (r"(son|daughter|grandson|granddaughter|grandchild|nephew|niece|cucu|anak|"
           r"孙子|孙女|儿子|女儿|外孙)")

PATTERNS: List[ScamPattern] = [
    ScamPattern(
        "SC-04", "Government impersonation",
        GuardrailDecision.SAFETY_INTERVENTION, RiskCategory.SCAM_OR_COERCION_SIGNAL,
        "SCAM_GOVERNMENT",
        all=(r"\b(ica|iras|cpf|spf|police|polis|court|mahkamah|immigration|imigresen|customs|"
             r"kastam|government|kerajaan|ministry|kementerian)\b|警察|政府|移民局|法院",
             r"\b(must|have to|need to|wajib|mesti|perlu|or (else|my|i|you)|will be (cancel|arrest|"
             r"suspend|block|charge)|cancel|arrest|warrant|fine|denda|tangkap)\w*|必须|否则|逮捕|取消",
             _MONEY)),
    ScamPattern(
        "SC-05", "Secrecy instruction",
        GuardrailDecision.SAFETY_INTERVENTION, RiskCategory.SCAM_OR_COERCION_SIGNAL,
        "SCAM_SECRECY",
        all=(r"\b(don'?t|do not|never|jangan|tak (boleh|usah)|tidak boleh)\s+(tell|inform|"
             r"beritahu|bagitahu|cakap|maklum)\w*|keep (it|this) (a )?secret|\brahsia\b|"
             r"别告诉|不要告诉|不能告诉|保密",
             _MONEY)),
    ScamPattern(
        "SC-06", "Coaching to bypass questions",
        GuardrailDecision.SAFETY_INTERVENTION, RiskCategory.SCAM_OR_COERCION_SIGNAL,
        "SCAM_COACHING",
        all=(r"\b(don'?t|do not|jangan)\s+ask\b|\bjust (follow|do) (what|whatever)\b|"
             r"\bikut (saja|je|sahaja) (apa|cakap)|不要问|别问|照他说的|听他的",)),
    ScamPattern(
        "SC-07", "Request to remove limits",
        GuardrailDecision.BLOCK, RiskCategory.UNSUPPORTED_ACTION,
        "SCAM_LIMITS",
        all=(r"\b(increase|raise|remove|lift|turn off|switch off|disable|bypass|naikkan|"
             r"buang|tutup)\b.{0,20}\b(limits?|had|safeguards?|safety|cooling[- ]off)\b|"
             r"提高限额|取消限额|关掉保护",)),
    ScamPattern(
        "SC-08", "Family-emergency impersonation",
        GuardrailDecision.SAFETY_INTERVENTION, RiskCategory.SCAM_OR_COERCION_SIGNAL,
        "SCAM_FAMILY",
        all=(_FAMILY,
             r"\bnew (number|phone|handphone|whatsapp)\b|changed? (his|her|their) (number|phone)|"
             r"lost (his|her|their) phone|nombor baru|telefon baru|新号码|换了号码|新的号码")),
    ScamPattern(
        "SC-09", "Guaranteed investment",
        GuardrailDecision.SAFETY_INTERVENTION, RiskCategory.SCAM_OR_COERCION_SIGNAL,
        "SCAM_INVESTMENT",
        all=(r"\b(invest\w*|crypto\w*|bitcoin|forex|shares?|stocks?|pelaburan|saham|trading)\b|"
             r"投资|股票|加密",
             r"\b(sure|guarantee\w*|confirm(ed)? (earn|profit|return)|pasti|jamin\w*|"
             r"double|no risk|risk[- ]free|earn|profit|returns?|untung)\b|保证|稳赚|一定赚|翻倍")),
    ScamPattern(
        "SC-10", "Romance scam",
        GuardrailDecision.SAFETY_INTERVENTION, RiskCategory.SCAM_OR_COERCION_SIGNAL,
        "SCAM_ROMANCE",
        all=(r"\b(boyfriend|girlfriend|lover|darling|fianc[eé]e?|sweetheart|teman lelaki|"
             r"teman wanita|kekasih|buah hati)\b|男朋友|女朋友|网友|情人",
             r"\b(never (met|seen)|haven'?t (met|seen)|not met|online|dating app|internet|"
             r"tak pernah jumpa|belum (pernah )?jumpa)\b|没见过|网上|没见面")),
    ScamPattern(
        "SC-11", "Technical-support scam",
        GuardrailDecision.SAFETY_INTERVENTION, RiskCategory.SCAM_OR_COERCION_SIGNAL,
        "SCAM_TECH_SUPPORT",
        all=(r"\b(microsoft|apple|google|windows|tech(nical)? support|it support|computer|laptop|"
             r"komputer)\b|电脑",
             r"\b(hack\w*|virus|infected|compromised|remote access|anydesk|teamviewer|"
             r"digodam)\b|被黑|中毒|病毒",
             _MONEY)),
    ScamPattern(
        "SC-13", "Split payments to evade controls",
        GuardrailDecision.BLOCK, RiskCategory.UNSUPPORTED_ACTION,
        "SCAM_STRUCTURING",
        all=(r"\b(split|divide|break)\b.{0,30}\b(payments?|transfers?|into|up)\b|"
             r"\b(\w+|several|multiple|many) (separate |small |smaller )?(payments|transfers)\b|"
             r"\bbahagi\w*|\bpecah\w*|分开|分成|拆开|分几次",
             r"\bwon'?t (stop|notice|flag|check|block|catch)|\bnot (stop|notice|flag|detect|catch)|"
             r"\b(avoid|evade|escape)\b|\bunder the limit\b|\btak (perasan|kesan|tahan)\b|"
             r"不会被|避开|躲过")),
    ScamPattern(
        "SC-14", "Conceal transaction",
        GuardrailDecision.BLOCK, RiskCategory.UNSUPPORTED_ACTION,
        "SCAM_CONCEAL",
        all=(r"\b(don'?t|do not|jangan)\s+(show|record|list|display|appear|tunjuk|rekod)\w*|"
             r"\b(hide|delete|erase|remove)\b.{0,25}\b(history|records?|statement)\b|"
             r"\bhide (it|this|the (payment|transaction))\b|"
             r"\bsembunyi\w*|不要显示|隐藏|删除记录|不要记录",)),
    ScamPattern(
        "SC-15", "Caller still present",
        GuardrailDecision.SAFETY_INTERVENTION, RiskCategory.SCAM_OR_COERCION_SIGNAL,
        "SCAM_CALLER_PRESENT",
        all=(r"\bstill on the (phone|line|call)\b|\btelling me what to (say|do|type)\b|"
             r"\b(officer|caller|bank|police|agent|man|lady|someone|stranger)\b.{0,25}"
             r"\bon the (phone|line|call)\b|"
             r"\bmasih (dalam|atas|di) talian\b|\bsuruh saya (cakap|kata)\b|还在电话|在电话上|"
             r"叫我说|教我说",)),
    # Last on purpose: urgency alone is weaker than every typology above, so a turn that is also
    # a family emergency or a government threat gets THAT message instead. It is a question, not a
    # freeze (urgency alone does not prove fraud), and it does not apply once a payee is named.
    ScamPattern(
        "SC-12", "Urgency pressure",
        GuardrailDecision.CLARIFY, RiskCategory.SCAM_OR_COERCION_SIGNAL,
        "SCAM_URGENCY",
        all=(r"(\bquick(ly)?\b|\bhurry\b|\bno time\b|\bimmediately\b|\bcepat\b|\bsegera\b|"
             r"快点|快|马上|立刻|赶快)",),
        unless=(r"\bto\s+[a-z]", r"\bkepada\b", r"给")),
]

_SEVERITY = {GuardrailDecision.ALLOW: 0, GuardrailDecision.CLARIFY: 1,
             GuardrailDecision.SAFETY_INTERVENTION: 2, GuardrailDecision.BLOCK: 3}

_URGENCY_CUES = re.compile(r"\bquick(ly)?\b|\bhurry\b|\bno time\b|\bimmediately\b|\bnow\b|"
                           r"\bcepat\b|\bsegera\b|快点|马上|立刻|赶快", _I)


def _urgency_count(text: str) -> int:
    return len({m.group(0).lower() for m in _URGENCY_CUES.finditer(text)})


def match_scam_pattern(text: str) -> Optional[ScamPattern]:
    """The first typology the turn matches, or None."""
    if not text:
        return None
    for row in PATTERNS:
        if not all(re.search(group, text, _I) for group in row.all):
            continue
        if any(re.search(u, text, _I) for u in row.unless):
            continue
        if row.pattern_id == "SC-12" and _urgency_count(text) < 2:
            # One "now" is how people talk ("send S$50 to Mei now"). Pressure is several at once.
            continue
        return row
    return None


def message_params(row: ScamPattern, text: str) -> dict:
    """Words from the customer's own turn that the row's message repeats back."""
    if row.pattern_id == "SC-08":
        m = re.search(_FAMILY, text, _I)
        word = m.group(0).lower() if m else ""
        return {"relative": word if word.isascii() and word else "family member"}
    return {}


def raises(current: GuardrailDecision, row: ScamPattern) -> bool:
    """True when the row's consequence is at least as strict as the verdict it would replace."""
    return _SEVERITY[row.decision] >= _SEVERITY[current]
