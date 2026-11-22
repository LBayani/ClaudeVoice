"""Turn chat/markdown text into something a Russian TTS can read aloud."""
from __future__ import annotations

import re

from num2words import num2words

_WORDS = {
    "claude": "клод", "python": "пайтон", "windows": "виндоус", "github": "гитхаб",
    "git": "гит", "ok": "окей", "okay": "окей", "wifi": "вайфай", "wi-fi": "вайфай",
    "usb": "ю эс би", "api": "эй пи ай", "json": "джейсон", "html": "эйч ти эм эл",
    "esp32": "е эс пэ тридцать два", "home": "хоум", "assistant": "ассистент",
    "proxmox": "проксмокс", "linux": "линукс", "docker": "докер", "ai": "эй ай",
    "ssh": "эс эс эйч", "url": "ю эр эл", "pc": "пи си", "cpu": "цэ пэ у", "gpu": "гэ пэ у",
}

_DIGRAPHS = [
    ("sch", "щ"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"),
    ("ya", "я"), ("yu", "ю"), ("yo", "ё"), ("ye", "е"), ("oo", "у"), ("ee", "и"),
    ("th", "т"), ("ph", "ф"), ("ck", "к"), ("qu", "кв"), ("ou", "ау"), ("ea", "и"),
]
_LETTERS = dict(zip("abcdefghijklmnopqrstuvwxyz",
                    ["а", "б", "к", "д", "е", "ф", "г", "х", "и", "дж", "к", "л", "м",
                     "н", "о", "п", "к", "р", "с", "т", "у", "в", "в", "кс", "й", "з"]))


_LETTER_NAMES = dict(zip("abcdefghijklmnopqrstuvwxyz",
                         ["а", "бэ", "цэ", "дэ", "е", "эф", "гэ", "аш", "ай", "джей", "ка", "эль", "эм",
                          "эн", "о", "пэ", "кью", "эр", "эс", "тэ", "ю", "вэ", "дабл ю", "икс", "уай", "зэд"]))


def _translit(word: str) -> str:
    low = word.lower()
    if low in _WORDS:
        return _WORDS[low]
    if len(low) == 1:                       # drive letters, single initials: "C" -> "цэ"
        return _LETTER_NAMES.get(low, low)
    if word.isupper() and len(word) <= 4:   # abbreviations: "SSD" -> "эс эс дэ"
        return " ".join(_LETTER_NAMES.get(c, c) for c in low)
    out, i = [], 0
    while i < len(low):
        for src, dst in _DIGRAPHS:
            if low.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            ch = low[i]
            if ch == "c" and i + 1 < len(low) and low[i + 1] in "eiy":
                out.append("с")
            else:
                out.append(_LETTERS.get(ch, ch))
            i += 1
    return "".join(out)


def _number(m: re.Match) -> str:
    s = m.group(0).replace(",", ".")
    try:
        return num2words(float(s) if "." in s else int(s), lang="ru")
    except Exception:
        return s


def to_speakable(text: str) -> str:
    t = re.sub(r"```.*?```", " Код показан на экране. ", text, flags=re.DOTALL)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)          # [text](url) -> text
    t = re.sub(r"https?://\S+", " ссылка ", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"^\s*\|?[-:| ]+\|[-:| ]*$", " ", t, flags=re.MULTILINE)  # table separators
    t = t.replace("|", ", ")
    t = re.sub(r"[*_#>~]+", " ", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"[\\/]", " ", t)
    t = t.replace("%", " процентов").replace("&", " и ").replace("+", " плюс ")
    for abbr, full in (("ТБ", "терабайт"), ("ГБ", "гигабайт"), ("МБ", "мегабайт"), ("КБ", "килобайт"),
                       ("ГГц", "гигагерц"), ("МГц", "мегагерц"), ("°C", " градусов"), ("°", " градусов")):
        t = re.sub(rf"(?<![А-Яа-яA-Za-z]){abbr}(?![А-Яа-яA-Za-z])", full, t)
    t = re.sub(r"\d+(?:[.,]\d+)?", _number, t)
    t = re.sub(r"[A-Za-z][A-Za-z\-]*", lambda m: _translit(m.group(0)), t)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


_SENT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def split_sentences(text: str, max_len: int = 220) -> list[str]:
    out: list[str] = []
    for s in _SENT_RE.split(text):
        s = s.strip(" ,;")
        if not s:
            continue
        while len(s) > max_len:  # break long sentences at a comma or space
            cut = s.rfind(",", 0, max_len)
            cut = cut if cut > 40 else s.rfind(" ", 0, max_len)
            cut = cut if cut > 0 else max_len
            out.append(s[:cut].strip(" ,"))
            s = s[cut:].strip(" ,")
        if re.search(r"[а-яА-ЯёЁ]", s):
            out.append(s)
    return out
