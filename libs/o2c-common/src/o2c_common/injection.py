"""Conservative detection of prompt-injection text in documents (ADR-010).

detect(text) returns findings for text that addresses an AI or tries to change how the
document is processed: "ignore previous instructions", "note to AI", "system:",
"pre-approved", "skip all checks", instructions to set prices or totals, in English and
German. The patterns are deliberately narrow: a finding sends the order to human review,
so a false positive costs a clerk's minute, but a pattern that fires on normal purchase
orders would make every order an exception. They are tested against every PO of
poc/data, ml/data/train and ml/data/holdout (no finding allowed).

Detection is a signal for the reviewer, not a defence on its own: the agent's checks in
code (prices, quantities, customer, limits) decide whether an order is posted.
"""

import re
from dataclasses import asdict, dataclass

_FLAGS = re.IGNORECASE | re.MULTILINE
_GAP = r"[^\n]{0,40}?"  # a few words on the same line

PATTERNS: dict[str, re.Pattern] = {
    # "Ignore previous instructions", "disregard all prior rules"
    "ignore_instructions": re.compile(
        r"\b(ignore|disregard|forget|override)\b"
        + _GAP
        + r"\b(previous|prior|above|earlier|all|any|your|the)\b"
        + _GAP
        + r"\b(instructions?|prompts?|rules?|guidelines?)\b",
        _FLAGS,
    ),
    "ignore_instructions_de": re.compile(
        r"\b(ignorier\w*|vergiss|vergessen\s+sie|missachte\w*)\b"
        + _GAP
        + r"\b(anweisung\w*|instruktion\w*|regeln|vorgaben|prompts?)\b",
        _FLAGS,
    ),
    # "Note to AI", "Hinweis an die KI", "AI assistant:", "you are an AI"
    "addressed_to_ai": re.compile(
        r"\b(note|message|instruction|hinweis|nachricht|anweisung)\s+"
        r"(to|for|an|für)\s+(the\s+|die\s+|den\s+)?"
        r"(ai|a\.i\.|ki|llm|gpt|chatgpt|assistant|assistent\w*|model|modell|bot)\b"
        r"|\b(ai|ki|llm)[ -]?(assistant|assistent\w*|agent|model|modell|system)\s*[:,]"
        r"|\byou\s+are\s+(an?\s+)?(ai|a\.i\.|llm|language\s+model|assistant|chatbot)\b"
        r"|\bdu\s+bist\s+(eine?n?\s+)?(ki|sprachmodell|assistent\w*|chatbot)\b",
        _FLAGS,
    ),
    # Chat-role markers at the start of a line: "system:", "assistant:", "[INST]"
    "role_marker": re.compile(
        r"^\s*(system|assistant|user)\s*:|<\|im_start\|>|\[/?INST\]|<<SYS>>",
        _FLAGS,
    ),
    "pre_approved": re.compile(
        r"\bpre[- ]?approved\b|\bvorab\s+(genehmigt|freigegeben)\b"
        r"|\bbereits\s+(genehmigt|freigegeben)\b",
        _FLAGS,
    ),
    # "skip all checks", "bypass validation", "Prüfungen überspringen"
    "skip_checks": re.compile(
        r"\b(skip|bypass|disable|omit|ignore)\b"
        + _GAP
        + r"\b(checks?|validations?|verifications?|reviews?|approvals?)\b"
        r"|\b(überspring\w*|umgeh\w*|deaktivier\w*)\b"
        + _GAP
        + r"\b(prüfung\w*|kontroll\w*|validierung\w*|freigabe\w*)\b"
        r"|\b(prüfung\w*|kontroll\w*|validierung\w*|freigabe\w*)\b"
        + _GAP
        + r"\b(überspringen|umgehen|deaktivieren|weglassen)\b",
        _FLAGS,
    ),
    # "set every unit_price to 0.01", "change the order total to 500.00",
    # "setze alle Preise auf 0,01"
    "set_values": re.compile(
        r"\b(set|change|update|make|replace|setze\w*|änder\w*|aendere\w*)\b"
        + _GAP
        + r"\b(unit[ _]?prices?|prices?|totals?|amounts?|quantit(y|ies)|"
        r"preis\w*|summe\w*|gesamt\w*|betrag\w*|menge\w*)\b"
        + _GAP
        + r"\b(to|auf)\s*[-+]?\s*(eur\s*)?\d",
        _FLAGS,
    ),
    # "post it immediately", "sofort buchen"
    "post_immediately": re.compile(
        r"\b(post|book|approve|release)\b"
        + r"[^\n]{0,20}?"
        + r"\b(immediately|automatically|without\s+review)\b"
        r"|\b(sofort|automatisch|ohne\s+prüfung)\s+(buchen|freigeben|verbuchen)\b",
        _FLAGS,
    ),
}


@dataclass(frozen=True)
class Finding:
    pattern: str  # name in PATTERNS
    match: str  # the matched text, whitespace collapsed

    def to_dict(self) -> dict:
        return asdict(self)


def detect(text: str) -> list[Finding]:
    """Every pattern match in the text, in pattern order, without duplicates."""
    findings = []
    for name, pattern in PATTERNS.items():
        for m in pattern.finditer(text):
            finding = Finding(name, " ".join(m.group(0).split()))
            if finding not in findings:
                findings.append(finding)
    return findings
