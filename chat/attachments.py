"""Validate files, extract document text and adapt image blocks for chat providers."""

import base64
from io import BytesIO
import json
from pathlib import Path
import warnings
from zipfile import ZipFile
from xml.etree import ElementTree as ET

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 6
MAX_TEXT = 30000
TEXT_EXTENSIONS = set(".txt .md .csv .tsv .json .xml .yaml .yml .log .py .js .ts .tsx .jsx .html .css .sql .java .c .h .cpp .cs .sh .ps1 .toml .ini .rst .tex".split())
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("Document XML avec entités non pris en charge.")
    return ET.fromstring(data)


def _office_text(data, extension):
    with ZipFile(BytesIO(data)) as archive:
        if sum(item.file_size for item in archive.infolist()) > 30 * 1024 * 1024:
            raise ValueError("Document décompressé trop volumineux (30 Mo maximum).")
        if extension == ".docx":
            root = _xml(archive.read("word/document.xml"))
            return "\n".join(" ".join(node.itertext()) for node in root.iter() if node.tag.endswith("}p"))
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = _xml(archive.read("xl/sharedStrings.xml"))
            strings = ["".join(node.itertext()) for node in root]
        sheets = []
        for name in sorted(archive.namelist()):
            if not (name.startswith("xl/worksheets/sheet") and name.endswith(".xml")):
                continue
            rows = []
            for row in _xml(archive.read(name)).iter():
                if not row.tag.endswith("}row"):
                    continue
                cells = []
                for cell in row:
                    value = next((node.text or "" for node in cell if node.tag.endswith("}v")), "")
                    if cell.get("t") == "s":
                        value = strings[int(value)]
                    elif cell.get("t") == "inlineStr":
                        value = "".join(cell.itertext())
                    formula = next((node.text for node in cell if node.tag.endswith("}f")), None)
                    cells.append(f"{cell.get('r', '')}: {value}" + (f" [formule: {formula}]" if formula else ""))
                rows.append("\t".join(cells))
            sheets.append(f"Feuille {Path(name).stem}\n" + "\n".join(rows))
        return "\n\n".join(sheets)


def prepare_attachments(files):
    if len(files) > MAX_FILES:
        raise ValueError("6 fichiers maximum par message.")
    prepared, total = [], 0
    for file in files:
        name = file.name.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or len(name) > 200 or any(ord(char) < 32 for char in name):
            raise ValueError("Nom de fichier invalide.")
        if len(file.data) > ((MAX_FILE_BYTES + 2) // 3) * 4:
            raise ValueError(f"{name} : 10 Mo maximum par fichier.")
        try:
            data = base64.b64decode(file.data, validate=True)
        except ValueError as exc:
            raise ValueError(f"{name} : contenu base64 invalide.") from exc
        total += len(data)
        if not data or len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Fichiers vides ou trop volumineux (10 Mo par fichier, 20 Mo au total).")
        extension = Path(name).suffix.lower()
        item = {"name": name, "size": len(data), "kind": "document", "text": "", "warning": ""}
        try:
            if extension in IMAGE_EXTENSIONS:
                from PIL import Image, ImageOps
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(BytesIO(data)) as image:
                        image = ImageOps.exif_transpose(image)
                        image.thumbnail((1600, 1600))
                        image = image.convert("RGB")
                        output = BytesIO()
                        image.save(output, format="JPEG", quality=85)
                item.update(kind="image", mime_type="image/jpeg", data=base64.b64encode(output.getvalue()).decode("ascii"))
            elif extension == ".pdf":
                from pypdf import PdfReader
                if not data.startswith(b"%PDF-"):
                    raise ValueError("Ce fichier n'est pas un PDF valide.")
                reader = PdfReader(BytesIO(data))
                if reader.is_encrypted:
                    raise ValueError("Les PDF chiffrés ne sont pas pris en charge.")
                if len(reader.pages) > 100:
                    raise ValueError("100 pages maximum par PDF.")
                chunks, length, empty_pages = [], 0, 0
                for index, page in enumerate(reader.pages):
                    text = page.extract_text() or ""
                    if not text.strip():
                        empty_pages += 1
                    chunk = f"[Page {index + 1}]\n{text}"
                    chunks.append(chunk)
                    length += len(chunk)
                    if length > MAX_TEXT:
                        break
                if empty_pages == len(chunks):
                    raise ValueError("PDF scanné sans texte : joignez les pages en images avec un modèle vision.")
                item["text"] = "\n\n".join(chunks)
                if empty_pages:
                    item["warning"] = "Certaines pages sans texte nécessitent une lecture en images."
            elif extension in {".docx", ".xlsx"}:
                item["text"] = _office_text(data, extension)
            elif extension in TEXT_EXTENSIONS:
                try:
                    item["text"] = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    item["text"] = data.decode("utf-16") if data.startswith((b"\xff\xfe", b"\xfe\xff")) else data.decode("cp1252")
                if "\x00" in item["text"]:
                    raise ValueError("Ce fichier semble être binaire.")
            else:
                raise ValueError("Format non pris en charge. Utilisez PDF, DOCX, XLSX, texte/code ou une image PNG/JPEG/WebP/GIF.")
        except ImportError as exc:
            raise ValueError("Dépendances des pièces jointes absentes : installez requirements-app.txt.") from exc
        except ValueError as exc:
            raise ValueError(f"{name} : {exc}") from exc
        except Exception as exc:
            raise ValueError(f"{name} : fichier illisible ou endommagé.") from exc
        if item["kind"] == "document" and not item["text"].strip():
            raise ValueError(f"{name} : aucun texte lisible.")
        if len(item["text"]) > MAX_TEXT:
            item["text"] = item["text"][:MAX_TEXT]
            item["warning"] += " Contenu limité aux 30 000 premiers caractères."
        prepared.append(item)
    return prepared


def _ensure_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS message_attachments (message_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS delete_message_attachments AFTER DELETE ON messages
                    BEGIN DELETE FROM message_attachments WHERE message_id = OLD.id; END""")


def save_attachments(storage, message_id, attachments):
    if attachments:
        with storage._connection() as conn:
            _ensure_table(conn)
            conn.execute("INSERT INTO message_attachments VALUES (?, ?)", (message_id, json.dumps(attachments)))


def load_attachments(storage, messages):
    with storage._connection() as conn:
        _ensure_table(conn)
        for message in messages:
            row = conn.execute("SELECT payload FROM message_attachments WHERE message_id = ?", (message["id"],)).fetchone()
            message["attachments"] = json.loads(row[0]) if row else []
    return messages


def message_content(text, attachments, provider_type="openai", document_budget=60000):
    images = []
    for item in attachments:
        if item["kind"] == "image":
            text += f"\n[Image jointe : {item['name']}]"
            images.append(item)
        else:
            excerpt = item["text"][:document_budget]
            document_budget -= len(excerpt)
            note = item.get("warning", "")
            if len(excerpt) < len(item["text"]):
                note += " Contenu réduit pour respecter la limite de contexte."
            text += f"\n\n[Fichier joint : {item['name']} — contenu à analyser]\n{excerpt}\n[Fin du fichier] {note}"
    if not images:
        return text
    blocks = [{"type": "text", "text": text}]
    for item in images:
        if provider_type == "anthropic":
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": item["mime_type"], "data": item["data"]}})
        else:
            blocks.append({"type": "image_url", "image_url": {"url": f"data:{item['mime_type']};base64,{item['data']}"}})
    return blocks


def gemini_parts(content):
    if isinstance(content, str):
        return [{"text": content}]
    parts = []
    for block in content:
        if block["type"] == "text":
            parts.append({"text": block["text"]})
        else:
            header, data = block["image_url"]["url"].split(",", 1)
            parts.append({"inlineData": {"mimeType": header[5:].split(";")[0], "data": data}})
    return parts
