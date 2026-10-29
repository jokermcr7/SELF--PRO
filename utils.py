import re

_TR = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def to_en_digits(s: str) -> str:
    return (s or "").translate(_TR)


def digits_only(s: str) -> str:
    return re.sub(r"\D", "", to_en_digits(s))


def normalize_phone(raw: str):
    """Accepts +98912..., 0098912..., 0912..., 912..., '+98 912-345 6789', Persian digits, ..."""
    d = digits_only(raw)
    if d.startswith("00"):
        d = d[2:]
    if len(d) == 11 and d.startswith("09"):
        d = "98" + d[1:]
    elif len(d) == 10 and d.startswith("9"):
        d = "98" + d
    if not 8 <= len(d) <= 15:
        return None
    return "+" + d


def clean_name(name: str) -> str:
    return re.sub(r"[\[\]*_`~|]", "", name or "").strip() or "کاربر"


def money(n: int) -> str:
    return f"{int(n):,}"
