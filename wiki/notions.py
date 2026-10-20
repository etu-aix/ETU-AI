"""Small, deterministic helpers for grounded Obsidian-style notion links."""
import re
import unicodedata


WORDS = re.compile(r"[^\W_]+", re.UNICODE)
LINKS = re.compile(r"\[\[([^\]\n]{1,220})\]\]")
GENERIC = {"je", "tu", "il", "elle", "nous", "vous", "ils", "elles", "mon", "ma", "mes",
           "un", "une", "le", "la", "les", "cette", "ce", "ici", "aujourd hui"}


def key(value):
    plain = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    return " ".join(WORDS.findall(plain))


def mention(source, value):
    """Return the original source span only when every notion word is present."""
    wanted = key(value).split()
    if not wanted:
        return None
    words = [(match.start(), match.end(), key(match.group())) for match in WORDS.finditer(source)]
    for start in range(len(words) - len(wanted) + 1):
        if [word[2] for word in words[start:start + len(wanted)]] == wanted:
            return source[words[start][0]:words[start + len(wanted) - 1][1]]
    return None


def valid(value):
    normalized = key(value)
    return (3 <= len(value) <= 80 and 1 <= len(normalized.split()) <= 6
            and normalized not in GENERIC and len(normalized) >= 3
            and not re.search(r"[/\\\[\]{}<>]|\b\d{5,}\b", value))


def rule_mentions(text):
    """Conservative fallback for common French studies and place statements."""
    found = []
    study = re.search(r"\b(?:BUT|BTS|licence|master|doctorat)\s+[^,.;:!?\n]{3,70}?"
                      r"(?=\s+(?:à|au|aux|dans|près de|chez)\b|[,.;:!?\n]|$)", text, re.I)
    if study:
        found.append(study.group().strip())
    place = re.search(r"\b(?:à|au|aux|près de)\s+([\wÀ-ÿ-]+(?:\s+[\wÀ-ÿ-]+){0,3})", text, re.I)
    if place:
        words = place.group(1).split()
        stop = {"et", "mais", "avec", "pour", "je", "qui", "où", "dans", "depuis"}
        words = words[:next((i for i, word in enumerate(words) if key(word) in stop), len(words))]
        if words:
            found.append(" ".join(words))
    venue = re.search(r"\b(?:café|restaurant|bar|librairie)\s+[^,.;:!?\n]{2,60}?"
                      r"(?=\s+(?:à|au|aux|dans|près de)\b|[,.;:!?\n]|$)", text, re.I)
    if venue:
        found.insert(0, venue.group().strip())
    return found


def sentences(text):
    parts = []
    for match in re.finditer(r"[^.!?\n]+(?:[.!?](?!\S))?", text):
        raw = match.group()
        if raw.strip():
            parts.append((match.start() + len(raw) - len(raw.lstrip()), raw.strip()))
    return parts


def source_sentence(text, quote):
    position = text.find(quote)
    if position < 0:
        return None
    for start, sentence in sentences(text):
        if start <= position and position + len(quote) <= start + len(sentence):
            return sentence
    return quote


def linked_sentence(quote, notions):
    """Make an inline linked copy; the plain quote remains in the document."""
    spans = []
    for item in notions:
        surface = mention(quote, item["label"])
        if not surface:
            continue
        start = quote.find(surface)
        spans.append((start, start + len(surface), item["path"].removesuffix(".md"), surface))
    output, cursor = [], 0
    for start, end, path, surface in sorted(spans, key=lambda part: (part[0], -(part[1] - part[0]))):
        if start < cursor:
            continue
        output.append(quote[cursor:start])
        output.append(f"[[{path}|{surface}]]")
        cursor = end
    output.append(quote[cursor:])
    return "".join(output)
