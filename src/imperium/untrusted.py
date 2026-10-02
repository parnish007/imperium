"""Builder text is untrusted (DESIGN §5, P2-9).

At ingestion it is redacted (known key formats, high-entropy tokens, exact values of configured secrets),
stripped of control characters and capped. When shown to a model it is framed with a per-run random tag.
Limits, stated honestly: fragments, encodings and secrets Imperium was never told about are not caught.
"""
import math
import re

FEED_CAP = 300
STORE_CAP = 2000
MIN_SECRET_LEN = 6

_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]
_CANDIDATE = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\x1b\[[0-9;?]*[A-Za-z]")


def _entropy(s):
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def _looks_random(s):
    """Mixed letters and digits with high entropy; plain identifiers and hex digests are kept."""
    classes = sum(bool(re.search(p, s)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    return classes >= 3 and _entropy(s) >= 4.0


def redact(text, secrets=()):
    if not isinstance(text, str) or not text:
        return text
    for value in sorted({s for s in secrets if s and len(s) >= MIN_SECRET_LEN}, key=len, reverse=True):
        text = text.replace(value, "[REDACTED:configured]")
    for kind, pat in _PATTERNS:
        text = pat.sub(f"[REDACTED:{kind}]", text)

    def high_entropy(m):
        s = m.group(0)
        if "REDACTED" in s:
            return s
        return "[REDACTED:high-entropy]" if _looks_random(s) else s

    return _CANDIDATE.sub(high_entropy, text)


def _cap(s, cap):
    if len(s) <= cap:
        return s
    return s[:cap] + f"…[+{len(s) - cap} chars]"


def clean(value, secrets=(), cap=STORE_CAP):
    """Redact, strip control characters and cap every string inside a JSON-like value."""
    if isinstance(value, str):
        return _cap(_CONTROL.sub("", redact(value, secrets)), cap)
    if isinstance(value, list):
        return [clean(v, secrets, cap) for v in value]
    if isinstance(value, dict):
        return {k: clean(v, secrets, cap) for k, v in value.items()}
    return value


def for_feed(value, cap=FEED_CAP):
    """A shorter copy for feeds; `show` returns the stored (longer) value."""
    if isinstance(value, str):
        return _cap(value, cap)
    if isinstance(value, list):
        return [for_feed(v, cap) for v in value]
    if isinstance(value, dict):
        return {k: for_feed(v, cap) for k, v in value.items()}
    return value


def frame(text, tag):
    """Frame builder text for a model's context with an unguessable per-run tag [P7 §4.4]."""
    inner = (text.replace("\\", "\\\\").replace("«", "\\u00ab").replace("»", "\\u00bb")
             .replace("<", "\\u003c").replace(">", "\\u003e"))
    return f"«untrusted:{tag}»{inner}«/untrusted:{tag}»"
