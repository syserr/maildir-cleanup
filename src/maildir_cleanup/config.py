"""Project settings from config.toml, with defaults for anything missing."""

import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


# Subjects of records worth keeping no matter who sent them: receipts, invoices, statements, order
# confirmations, tax forms. Case-insensitive regular expressions. Short-lived notices (shipped, delivered,
# sign-in alerts, verification codes, password resets) are deliberately absent: they age out with the
# rest of the sender's mail.
KEEP_SUBJECT_PATTERNS = [
    # orders
    r"\border (#|no\.?|number)\s*:?\s*[\w-]*\d", r"\border (confirm|received|placed|cancel)",
    r"\b(thank(s| you) for|confirm(ing|ation of)?) your (order|purchase|payment)",
    # money
    r"\b(your|new|an?) invoice\b", r"\binvoice\s*(#|no\.?|number|\d)", r"\binvoice (from|for)\b",
    r"\breceipt (for|from|#|no\.?)\b", r"\byour .{0,30}receipt\b", r"^receipt\b",
    r"\b(account|card|bank|monthly|e-?)statement\b", r"\bstatement (is )?(ready|available|now available)",
    r"\bbill(ing)? (is )?(ready|available|due)", r"\bpayment (received|confirm|due|failed|scheduled|posted)",
    r"\byour refund\b", r"\brefund (issued|processed|confirm|of \$|for your)",
    r"\byour .{0,20}tax (form|document|return|statement)s?\b", r"\b(1099|w-?2)(-[a-z]+)? (form|is ready|is available|statement)",
    r"\bpolicy (document|renewal)", r"\bwarranty (registration|claim|confirmation)",
]


@dataclass
class Config:
    mail_root: str = "mail"
    db: str = "data/inv.db"
    rules: str = "rules.toml"
    training: str = "training"
    cutoff_days: int = 365
    junk_any_age: bool = True
    trash_before_cutoff: bool = True
    spam_header_before_cutoff: bool = True
    protected_folders: list[str] = field(default_factory=list)
    keep_subject_patterns: list[str] = field(default_factory=lambda: list(KEEP_SUBJECT_PATTERNS))
    junk_threshold: float = 0.85
    workers: int = 0
    base: Path = Path(".")

    def path(self, p: str) -> Path:
        return (self.base / p).resolve()

    def cutoff_ts(self, override: str | None = None) -> int:
        if override:
            d = date.fromisoformat(override)
        else:
            d = date.today() - timedelta(days=self.cutoff_days)
        return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def load(path: str | None) -> Config:
    p = Path(path or "config.toml")
    cfg = Config(base=p.parent)
    if p.exists():
        data = tomllib.loads(p.read_text())
        for k, v in data.items():
            if not hasattr(cfg, k) or k == "base":
                raise SystemExit(f"{p}: unknown setting {k!r}")
            setattr(cfg, k, v)
    if not 0 < cfg.junk_threshold <= 1:
        raise SystemExit(f"{p}: junk_threshold must be between 0 and 1 (e.g. 0.7 for 70%)")
    return cfg
