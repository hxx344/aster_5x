"""Bounded, plain-text exchange diagnostics, without signed request details."""
import re
import unicodedata


MISSING_REJECT_REASON = "交易所未返回具体原因"
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_SENSITIVE_FIELD = re.compile(
    r'''(?ix)(["']?\b(?:signature|private[ _-]?key|api[ _-]?key|secret|token|authorization|cookie)\b["']?\s*[:=]\s*)'''
    r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\r\n,;&}]+)''')
_HEX_SECRET = re.compile(r"(?i)(?<![0-9a-z])(?:0x)?[0-9a-f]{40,}(?![0-9a-z])")


def exchange_reason(value, *, secrets=()):
    """Read only a structured msg string; never stringify a response or exception."""
    if not isinstance(value, str) or not value.strip():
        return ""
    # Do not truncate a huge raw value through the middle of a credential.
    if len(value) > 4096:
        return "交易所原因过长，已省略"
    for secret in secrets:
        if isinstance(secret, bytes):
            secret = secret.hex()
        if isinstance(secret, str) and secret:
            value = re.sub(re.escape(secret), "[已隐藏]", value, flags=re.IGNORECASE)
    value = _URL.sub("[请求地址已隐藏]", value)
    value = _SENSITIVE_FIELD.sub(lambda match: match[1] + "[已隐藏]", value)
    value = _HEX_SECRET.sub("[已隐藏]", value)
    value = "".join(" " if unicodedata.category(char).startswith("C") else char for char in value)
    value = " ".join(value.split())
    return value if len(value) <= 500 else value[:499] + "…"
