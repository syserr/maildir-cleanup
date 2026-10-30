"""Header-only parsing of single Maildir message files."""

import bz2
import gzip
import lzma
import os
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

MAX_HEADER = 256 * 1024

# Headers that only bulk-mail platforms set. Any one of them is a strong bulk signal.
BULK_HEADER_PREFIXES = (
    "feedback-id", "x-campaign", "x-mailchimp", "x-mc-", "x-sg-", "x-sfmc", "x-csa-complaints",
    "x-mailgun", "x-ses-", "x-bm-", "x-marketo", "x-emarsys", "x-responsys", "x-cmail",
    "x-exacttarget", "x-rpcampaign", "x-mailer-recptid", "x-listmember", "x-job",
)

# Two-label public suffixes common enough to matter for org-domain grouping.
TWO_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au", "co.nz",
    "co.jp", "ne.jp", "com.br", "com.mx", "co.in", "co.za", "com.cn", "com.tw", "co.kr",
}

_FLAG_SEPS = (":2,", ";2,", "!2,")


def split_name(name: str) -> tuple[str, str]:
    """Split a Maildir filename into its stable key and its flag letters."""
    for sep in _FLAG_SEPS:
        i = name.find(sep)
        if i >= 0:
            return name[:i], name[i + len(sep):]
    return name, ""


def org_domain(domain: str) -> str:
    parts = domain.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def open_message(path: str):
    """Open a message file, transparently handling Dovecot-style compression."""
    with open(path, "rb") as f:
        magic = f.read(6)
    if magic[:2] == b"\x1f\x8b":
        return gzip.open(path, "rb")
    if magic[:3] == b"BZh":
        return bz2.open(path, "rb")
    if magic[:6] == b"\xfd7zXZ\x00":
        return lzma.open(path, "rb")
    if magic[:4] == b"\x28\xb5\x2f\xfd":
        import zstandard

        return zstandard.ZstdDecompressor().stream_reader(open(path, "rb"), closefd=True)
    return open(path, "rb")


def read_header_bytes(path: str) -> bytes:
    buf = b""
    with open_message(path) as f:
        while len(buf) < MAX_HEADER:
            chunk = f.read(16384)
            if not chunk:
                break
            buf += chunk
            for sep in (b"\r\n\r\n", b"\n\n"):
                i = buf.find(sep)
                if i >= 0:
                    return buf[: i + len(sep)]
    return buf


def _text(value) -> str:
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value)))).strip()
    except Exception:
        return str(value).strip()


def _addr(value: str) -> str:
    return parseaddr(value or "")[1].strip().lower().strip("<>")


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[1].strip(".") if "@" in addr else ""


def _msgids(value: str) -> str:
    return " ".join(re.findall(r"<[^<>\s]+>", value or ""))


def _spam(h) -> tuple[float | None, int]:
    flag = 1 if (h.get("X-Spam-Flag") or "").strip().upper().startswith("YES") else 0
    status = h.get("X-Spam-Status") or ""
    if status.strip().upper().startswith("YES"):
        flag = 1
    for name in ("X-Spam-Score", "X-Rspamd-Score"):
        m = re.search(r"-?\d+(\.\d+)?", h.get(name) or "")
        if m:
            return float(m.group()), flag
    m = re.search(r"score=(-?\d+(\.\d+)?)", status)
    if m:
        return float(m.group(1)), flag
    level = h.get("X-Spam-Level")
    if level is not None:
        return float(str(level).count("*")), flag
    return None, flag


def _timestamp(h, name: str, mtime: float) -> int:
    """Date header if sane, else the delivery time in the Maildir filename, else mtime."""
    now = datetime.now(timezone.utc).timestamp()
    try:
        dt = parsedate_to_datetime(h.get("Date"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = dt.timestamp()
        if 631152000 < ts < now + 86400 * 2:  # after 1990, not in the future
            return int(ts)
    except Exception:
        pass
    m = re.match(r"(\d{9,10})\.", name)
    if m:
        return int(m.group(1))
    return int(mtime)


def parse_message(path: str) -> dict:
    """Return header fields for one message file. Never raises; errors land in 'error'."""
    name = os.path.basename(path)
    st = os.stat(path)
    out = {"size": st.st_size, "error": None}
    try:
        h = BytesHeaderParser().parsebytes(read_header_bytes(path))
    except Exception as e:
        out.update(ts=_timestamp({}, name, st.st_mtime), error=f"{type(e).__name__}: {e}")
        return out

    from_raw = _text(h.get("From"))
    from_name, from_addr = parseaddr(from_raw)
    from_addr = from_addr.strip().lower()
    if "@" not in from_addr:  # parseaddr gives up on odd display names like "[x] <a@b>"
        m = re.search(r"[\w.+'=-]+@[\w-]+(\.[\w-]+)+", from_raw)
        if m:
            from_addr = m.group().lower()
            from_name = from_raw[: m.start()].strip(' <"')
    from_domain = _domain(from_addr)
    recips = [a.lower() for _, a in getaddresses([str(v) for v in (h.get_all("To") or []) + (h.get_all("Cc") or [])]) if a]
    rp_domain = _domain(_addr(h.get("Return-Path") or ""))
    keys = {k.lower() for k in h.keys()}
    out.update(
        ts=_timestamp(h, name, st.st_mtime),
        from_addr=from_addr,
        from_name=from_name.strip(),
        from_domain=from_domain,
        org=org_domain(from_domain) if from_domain else "",
        to_addrs=" ".join(recips),
        subject=_text(h.get("Subject"))[:300],
        message_id=_msgids(h.get("Message-ID") or "")[:300],
        in_reply_to=_msgids(h.get("In-Reply-To") or "")[:1000],
        refs=_msgids(h.get("References") or "")[-2000:],
        list_id=(_msgids(h.get("List-Id") or "") or _text(h.get("List-Id")))[:200].strip("<>").lower(),
        list_unsub=1 if "list-unsubscribe" in keys else 0,
        precedence=(h.get("Precedence") or "").strip().lower(),
        auto_submitted=(h.get("Auto-Submitted") or "").strip().lower(),
        return_domain=rp_domain,
        bulk_hdrs=sum(1 for k in keys if k.startswith(BULK_HEADER_PREFIXES)),
        content_type=(h.get_content_type() or ""),
    )
    out["spam_score"], out["spam_flag"] = _spam(h)
    return out


MAX_BODY_READ = 512 * 1024   # bytes of a message read for text; attachments past this are skipped
_URL = re.compile(r"https?://([^/\s\"'<>]+)[^\s\"'<>]*")


def extract_text(path: str, limit: int = 3000) -> str:
    """Readable text of a message (plain part, else de-tagged HTML), URLs reduced to their host."""
    from email import policy
    from email.parser import BytesParser
    import html as html_mod

    try:
        with open_message(path) as f:
            data = f.read(MAX_BODY_READ)
        msg = BytesParser(policy=policy.default).parsebytes(data)
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        text = part.get_content()
        if part.get_content_type() == "text/html":
            text = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", text)
            text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
            text = html_mod.unescape(re.sub(r"<[^>]+>", " ", text))
    except Exception:
        return ""
    text = _URL.sub(lambda m: f" <{m.group(1).lower()}> ", text)
    text = re.sub(r"[ \t\r\f\v ​͏]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:limit]
