"""Conservative comparison keys for names printed on staff pages."""

import re
from unicodedata import normalize


_SPELLING = str.maketrans({"ё": "е", "‑": "-", "‐": "-", "’": "'"})


def full_name_key(name: str | None) -> str | None:
    """Accept three unabbreviated words, regardless of display order."""
    if not name:
        return None
    words = normalize("NFC", name).casefold().translate(_SPELLING).split()
    if len(words) != 3:
        return None
    for word in words:
        parts = re.split("[-']", word)
        if not all(part.isalpha() for part in parts) or max(map(len, parts)) < 2:
            return None
    return " ".join(sorted(words))
