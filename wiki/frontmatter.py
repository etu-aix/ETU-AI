"""Read common Obsidian properties without changing the source Markdown.

The complete YAML block remains in the file. ETU only interprets simple scalar
and list properties needed for navigation; unfamiliar YAML is left untouched.
"""
import json
import re


KEY = re.compile(r"^([A-Za-z][\w-]*):(?:\s+(.*))?$")
ITEM = re.compile(r"^\s+-\s+(.+?)\s*$")


def scalar(raw):
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw[1:-1]
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw.split(" #", 1)[0].strip()


def properties(content):
    """Extract scalar/list properties from top-of-file YAML frontmatter."""
    if not content.startswith("---\n") and not content.startswith("---\r\n"):
        return {}, content
    lines = content.splitlines(keepends=True)
    closing = next((i for i, line in enumerate(lines[1:], 1)
                    if line.strip() in {"---", "..."}), None)
    if closing is None:
        return {}, content
    values = {}
    active = None
    for line in lines[1:closing]:
        stripped = line.rstrip("\r\n")
        field = KEY.fullmatch(stripped)
        if field:
            active = field.group(1).lower()
            value = field.group(2) or ""
            if value.startswith("[") and value.endswith("]"):
                # Obsidian also accepts YAML flow lists. Quoted commas are rare
                # in note aliases; preserve other complex structures verbatim.
                values[active] = [scalar(item) for item in value[1:-1].split(",") if item.strip()]
            else:
                values[active] = scalar(value) if value else []
        elif active and (item := ITEM.fullmatch(stripped)):
            if isinstance(values[active], list):
                values[active].append(scalar(item.group(1)))
        elif stripped and not stripped.startswith((" ", "\t", "#")):
            active = None
    return values, "".join(lines[closing + 1:])


def names(metadata, field):
    value = metadata.get(field, [])
    if not isinstance(value, list):
        value = [value]
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]
