"""Data redundancy removal engine.

Every incoming record goes through this pipeline before it may touch the database:

  1. VALIDATE   required fields present, email and phone well-formed  -> else INVALID (rejected)
  2. NORMALIZE  lowercase, trim, strip accents, phone -> last 10 digits
  3. EXACT      SHA-256 fingerprint of the normalized record exists  -> REDUNDANT
  4. KEY MATCH  same email or same phone already stored               -> REDUNDANT
  5. FUZZY      compare with candidate records:
                  similar name AND nearly identical email/phone -> REDUNDANT (typo duplicate)
                  weighted score >= 0.60 -> FALSE_POSITIVE (looks alike, e.g. same name,
                                    but identifiers differ -> a different person;
                                    stored and flagged for review)
                  otherwise      -> UNIQUE (stored)

Only UNIQUE and FALSE_POSITIVE records are appended. The database also enforces
UNIQUE constraints on fingerprint, email and phone, so duplicates cannot slip in
even when two requests arrive at the same time.
"""

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

NAME_MATCH = 0.85    # names this similar may be the same person...
EMAIL_MATCH = 0.85   # ...if the email is this similar (typo: gmial.com)
PHONE_MATCH = 0.90   # ...or the phone differs by about one digit
FALSE_POSITIVE_THRESHOLD = 0.60
WEIGHTS = {"name": 0.40, "email": 0.30, "phone": 0.20, "city": 0.10}

UNIQUE, FALSE_POSITIVE, REDUNDANT, INVALID = "unique", "false_positive", "redundant", "invalid"

EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")


@dataclass
class CleanRecord:
    name: str
    email: str
    phone: str
    city: str

    @property
    def fingerprint(self) -> str:
        raw = "|".join([self.name, self.email, self.phone, self.city])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def blocks(self) -> dict:
        """Cheap keys used to fetch only plausible duplicates instead of the whole table."""
        return {
            "name_block": self.name.replace(" ", "")[:4],
            "email_block": self.email.split("@")[0][:4],
            "phone_block": self.phone[-6:],
        }


@dataclass
class Decision:
    status: str
    reason: str
    score: float = 0.0
    matched_id: int | None = None
    record: CleanRecord | None = None
    errors: list = field(default_factory=list)
    record_id: int | None = None  # set after a successful insert

    @property
    def should_insert(self) -> bool:
        return self.status in (UNIQUE, FALSE_POSITIVE)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["score"] = round(self.score, 3)
        return d


# ---------- 1-2. validation and normalization ----------

def _clean_text(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().lower()


def normalize_name(value) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", "", _clean_text(value))).strip()


def normalize_phone(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:]  # ignore country code / leading zero differences


def validate(raw: dict):
    """Return (CleanRecord, []) if valid, else (None, [error messages])."""
    errors = []
    name = normalize_name(raw.get("name"))
    email = _clean_text(raw.get("email")).replace(" ", "")
    phone_digits = re.sub(r"\D", "", str(raw.get("phone") or ""))
    city = normalize_name(raw.get("city"))

    if len(name) < 2:
        errors.append("name is required")
    if not EMAIL_RE.match(email):
        errors.append("email is invalid")
    if not 10 <= len(phone_digits) <= 15:
        errors.append("phone must have 10-15 digits")
    if errors:
        return None, errors
    return CleanRecord(name, email, normalize_phone(phone_digits), city), []


# ---------- 5. similarity ----------

def _ratio(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def name_similarity(a: str, b: str) -> float:
    """Handles reordered names: 'smith john' vs 'john smith'."""
    return max(_ratio(a, b), _ratio(" ".join(sorted(a.split())), " ".join(sorted(b.split()))))


def similarity(a: CleanRecord, b: CleanRecord):
    """Return (weighted score 0-1, per-field similarities)."""
    parts = {
        "name": name_similarity(a.name, b.name),
        "email": _ratio(a.email, b.email),
        "phone": _ratio(a.phone, b.phone),
        "city": _ratio(a.city, b.city),
    }
    return sum(WEIGHTS[k] * v for k, v in parts.items()), parts


def is_near_duplicate(parts: dict) -> bool:
    """Same person = similar name AND a nearly identical identifier (typo in email/phone).
    A similar name alone is never enough: two different people can share a name."""
    return parts["name"] >= NAME_MATCH and (parts["email"] >= EMAIL_MATCH or parts["phone"] >= PHONE_MATCH)


# ---------- full pipeline ----------

def classify(raw: dict, store) -> Decision:
    """Classify one raw record against existing data in `store` (see database.py)."""
    record, errors = validate(raw)
    if record is None:
        return Decision(INVALID, "failed validation: " + "; ".join(errors), errors=errors)

    existing = store.find_by_fingerprint(record.fingerprint)
    if existing:
        return Decision(REDUNDANT, "exact duplicate", 1.0, existing["id"], record)

    existing = store.find_by_email(record.email)
    if existing:
        return Decision(REDUNDANT, "email already exists", 1.0, existing["id"], record)

    existing = store.find_by_phone(record.phone)
    if existing:
        return Decision(REDUNDANT, "phone already exists", 1.0, existing["id"], record)

    best_score, best_id = 0.0, None
    for cand in store.candidates(record.blocks):
        other = CleanRecord(cand["name"], cand["email"], cand["phone"], cand["city"])
        score, parts = similarity(record, other)
        if is_near_duplicate(parts):
            return Decision(REDUNDANT, "near-duplicate (same name, almost identical email/phone)",
                            score, cand["id"], record)
        if score > best_score:
            best_score, best_id = score, cand["id"]

    if best_score >= FALSE_POSITIVE_THRESHOLD:
        return Decision(FALSE_POSITIVE, "similar to an existing record but verified as distinct",
                        best_score, best_id, record)
    return Decision(UNIQUE, "no matching record", best_score, best_id, record)
