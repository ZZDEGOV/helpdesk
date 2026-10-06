"""Split a pasted Outlook thread into individual messages.

Outlook produces several quoting styles depending on client and version. Rather
than guessing which one, we look for any line that starts a new message and cut
there. Nothing is saved automatically — the caller shows the result for review
first, because a wrong split is worse than no split.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime

# A line that begins a new message in the chain.
_BOUNDARIES = [
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*_{10,}\s*$"),
    re.compile(r"^\s*From:\s*(?P<who>.+?)\s*$"),
    re.compile(r"^\s*On\s+(?P<when>.+?)\s+(?P<who2>.+?)\s+wrote:\s*$", re.I),
]

_HEADERS = {
    "from": re.compile(r"^\s*From:\s*(.+?)\s*$", re.I),
    "sent": re.compile(r"^\s*(?:Sent|Date):\s*(.+?)\s*$", re.I),
    "to": re.compile(r"^\s*To:\s*(.+?)\s*$", re.I),
    "cc": re.compile(r"^\s*Cc:\s*(.+?)\s*$", re.I),
    "subject": re.compile(r"^\s*Subject:\s*(.+?)\s*$", re.I),
}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Outlook writes dates a dozen ways depending on locale and client.
_DATE_FORMATS = [
    "%A, %B %d, %Y %I:%M %p", "%A, %d %B %Y %I:%M %p",
    "%B %d, %Y %I:%M %p", "%d %B %Y %I:%M %p",
    "%m/%d/%Y %I:%M %p", "%m/%d/%y %I:%M %p",
    "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%a, %d %b %Y %H:%M:%S", "%b %d, %Y %I:%M %p",
]


@dataclass
class Segment:
    author: str = ""
    author_email: str = ""
    sent_raw: str = ""
    sent_at: datetime | None = None
    to: str = ""
    cc: str = ""
    subject: str = ""
    body: str = ""
    guessed_kind: str = ""
    lines: list[str] = field(default_factory=list)


def parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    cleaned = re.sub(r"\s+", " ", raw.strip())
    cleaned = re.sub(r"\s*\((?:UTC|GMT)[^)]*\)\s*$", "", cleaned).strip()
    # Strip a trailing timezone abbreviation, but never AM/PM.
    cleaned = re.sub(r"\s+(?!AM$|PM$)[A-Z]{2,4}$", "", cleaned, flags=re.I).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def _is_boundary(line: str) -> bool:
    return any(rx.match(line) for rx in _BOUNDARIES)


def _clean_quotes(lines: list[str]) -> str:
    """Strip leading '>' quote markers and collapse runs of blank lines."""
    out, blanks = [], 0
    for line in lines:
        line = re.sub(r"^\s*>+\s?", "", line).rstrip()
        if not line.strip():
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        out.append(line)
    return "\n".join(out).strip()


def split_thread(text: str) -> list[Segment]:
    """Break a pasted thread into segments, newest first as Outlook writes them."""
    if not text or not text.strip():
        return []

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # Find where each message starts. Index 0 is implicit: the top message
    # usually has no header block of its own.
    starts = [i for i, line in enumerate(lines) if _is_boundary(line)]
    if not starts or starts[0] != 0:
        starts = [0] + starts

    segments: list[Segment] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        chunk = lines[start:end]
        if not any(l.strip() for l in chunk):
            continue
        segments.append(_parse_segment(chunk))

    return [s for s in segments if s.body or s.author]


def _parse_segment(chunk: list[str]) -> Segment:
    seg = Segment(lines=chunk)
    body_start = 0

    # Drop a leading divider ("-----Original Message-----", "______") so it
    # doesn't become a segment whose entire body is the divider itself.
    while chunk and (_BOUNDARIES[0].match(chunk[0]) or _BOUNDARIES[1].match(chunk[0])):
        chunk = chunk[1:]
        seg.lines = chunk
    if not chunk:
        return seg

    # "On <date>, <person> wrote:" carries its metadata inline
    for i, line in enumerate(chunk[:3]):
        match = _BOUNDARIES[3].match(line)
        if match:
            seg.author = match.group("who2").strip().rstrip(",")
            seg.sent_raw = match.group("when").strip()
            seg.sent_at = parse_date(seg.sent_raw)
            body_start = i + 1
            break

    # Otherwise read a conventional header block
    if not seg.author:
        for i, line in enumerate(chunk[:12]):
            matched = False
            for key, rx in _HEADERS.items():
                m = rx.match(line)
                if m:
                    value = m.group(1).strip()
                    if key == "from":
                        seg.author = value
                    elif key == "sent":
                        seg.sent_raw = value
                        seg.sent_at = parse_date(value)
                    elif key == "to":
                        seg.to = value
                    elif key == "cc":
                        seg.cc = value
                    elif key == "subject":
                        seg.subject = value
                    body_start = i + 1
                    matched = True
                    break
            if not matched and line.strip() and i > 0 and seg.author:
                break

    emails = _EMAIL.findall(seg.author)
    if emails:
        seg.author_email = emails[0].lower()
        name = _EMAIL.sub("", seg.author).strip(" <>\"'[](),;")
        seg.author = name or seg.author_email

    seg.body = _clean_quotes(chunk[body_start:])
    return seg


def guess_kinds(segments: list[Segment], customer_emails: set[str],
                owner_hints: set[str]) -> list[Segment]:
    """Pre-select a message kind per segment so the review screen starts close
    to correct. Always overridable — these are suggestions, not decisions."""
    customer_emails = {e.lower() for e in customer_emails if e}
    owner_hints = {h.lower() for h in owner_hints if h}

    for seg in segments:
        who = f"{seg.author} {seg.author_email}".lower()
        if seg.author_email and seg.author_email in customer_emails:
            seg.guessed_kind = "in_customer"
        elif any(hint and hint in who for hint in owner_hints):
            # From you — to the customer if they're addressed, else internal
            recipients = f"{seg.to} {seg.cc}".lower()
            seg.guessed_kind = ("out_customer"
                                if any(e in recipients for e in customer_emails)
                                else "out_internal")
        elif seg.author:
            seg.guessed_kind = "in_internal"
        else:
            seg.guessed_kind = "note"
    return segments
