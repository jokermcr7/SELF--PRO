import ast
import operator
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
    return re.sub(r"[\[\]*_`]", "", name or "").strip() or "کاربر"


# ---------- fonts ----------
def _mk(up, lo):
    def f(t):
        out = []
        for ch in t:
            if "A" <= ch <= "Z":
                out.append(chr(up + ord(ch) - 65))
            elif "a" <= ch <= "z":
                out.append(chr(lo + ord(ch) - 97))
            else:
                out.append(ch)
        return "".join(out)
    return f


FONTS = {
    "1": _mk(0x1D400, 0x1D41A),  # bold
    "2": _mk(0x1D468, 0x1D482),  # bold italic
    "3": _mk(0x1D670, 0x1D68A),  # monospace
    "4": _mk(0x1D5D4, 0x1D5EE),  # sans bold
    "5": _mk(0xFF21, 0xFF41),    # fullwidth
    "6": _mk(0x24B6, 0x24D0),    # circled
}

# ---------- safe calculator ----------
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def calc(expr: str):
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(b) > 100 or abs(a) > 1e6):
                raise ValueError("too big")
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("bad expression")

    e = to_en_digits(expr).replace("^", "**").replace("×", "*").replace("÷", "/")
    res = ev(ast.parse(e, mode="eval"))
    if abs(res) > 1e100:
        raise ValueError("too big")
    return res
