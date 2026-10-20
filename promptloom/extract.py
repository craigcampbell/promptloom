"""Text helpers: timestamps, links, secrets, fallback titles and action items."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

URL_RE = re.compile(r"https?://[^\s<>\"'`\]\)\}]+")
_TRAILING = ".,;:!?*_"

# Obvious credentials only; this is a safety net, not a guarantee.
SECRET_RES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]
# The value of an assignment whose name says it is a credential.
ASSIGNED_SECRET_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY)[A-Z0-9_]*\s*[=:]\s*)"
    r"[\"']?[^\s\"']{6,}[\"']?"
)

ACTION_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:todo\b[:\s]|remind me to\b|don'?t forget to\b|remember to\b|follow up\b)",
    re.I,
)


def norm_ts(value) -> str:
    """Normalise a timestamp (ISO string or epoch seconds/millis) to UTC ISO."""
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)):
            if value > 1e11:  # milliseconds
                value /= 1000
            dt = datetime.fromtimestamp(value, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def local(ts: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    dt = parse_ts(ts)
    return dt.astimezone().strftime(fmt) if dt else ""


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def urls(text: str) -> list[str]:
    found = []
    for match in URL_RE.findall(text or ""):
        url = match.rstrip(_TRAILING)
        if url not in found:
            found.append(url)
    return found


def redact(text: str) -> str:
    text = ASSIGNED_SECRET_RE.sub(r"\1[redacted secret]", text)
    for pattern in SECRET_RES:
        text = pattern.sub("[redacted secret]", text)
    return text


def fallback_title(text: str, limit: int = 60) -> str:
    """A title from the first meaningful line of the first prompt."""
    for line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip(" #>*-")
        if len(line) >= 3:
            if len(line) > limit:
                line = line[:limit].rsplit(" ", 1)[0] + "…"
            return line
    return "Untitled session"


def heuristic_actions(prompts: list[str]) -> list[str]:
    """Lines the user plainly wrote as a to-do. Used when no model is available."""
    found = []
    for text in prompts:
        for line in text.splitlines():
            if ACTION_RE.match(line) and 8 <= len(line.strip()) <= 200:
                found.append(line.strip(" -*"))
    return found


def item_id(text: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:8]
