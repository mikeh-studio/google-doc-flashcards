#!/usr/bin/env python3
import datetime as dt
import hashlib
import hmac
import http.cookies
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DECKS_DIR = ROOT / "decks"
APP_NAME = "Google Doc Flashcards"


def load_env_file():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()


# Optional shared passcode. When APP_PASSCODE is set, every request (except the login
# page and public static assets) requires a signed auth cookie. Leave it unset to keep
# the original open, localhost-only behavior. Intended to sit behind a private network
# (e.g. Tailscale), not the public internet.
APP_PASSCODE = os.getenv("APP_PASSCODE", "").strip()
AUTH_COOKIE = "dino_auth"


def auth_enabled():
    return bool(APP_PASSCODE)


def expected_auth_token():
    # Stateless token: only someone who knows the passcode can produce it, and it never
    # reveals the passcode. Survives restarts (no server-side session store).
    return hmac.new(APP_PASSCODE.encode("utf-8"), b"dino-decks-auth-v1", hashlib.sha256).hexdigest()


def is_public_path(path):
    if path in ("/login", "/logout", "/manifest.webmanifest", "/service-worker.js", "/favicon.ico"):
        return True
    return path.startswith("/static/")


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def slugify(value):
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "flashcard-deck"


def clamp_card_count(value):
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 12
    return max(10, min(20, count))


def extract_doc_id(doc_ref):
    doc_ref = (doc_ref or "").strip()
    match = re.search(r"/document/d/([a-zA-Z0-9_-]+)", doc_ref)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", doc_ref):
        return doc_ref
    return None


def fetch_url(url, headers=None, timeout=30):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"{APP_NAME}/0.1",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_google_doc_text(doc_ref):
    doc_id = extract_doc_id(doc_ref)
    if not doc_id:
        raise ValueError("Paste a Google Doc share URL or document id.")

    token = (
        os.getenv("GOOGLE_OAUTH_ACCESS_TOKEN")
        or os.getenv("GOOGLE_ACCESS_TOKEN")
        or os.getenv("GOOGLE_SLIDES_ACCESS_TOKEN")
    )

    if token:
        export_url = (
            "https://www.googleapis.com/drive/v3/files/"
            + urllib.parse.quote(doc_id)
            + "/export?mimeType=text/plain"
        )
        try:
            return fetch_url(export_url, headers={"Authorization": f"Bearer {token}"})
        except urllib.error.HTTPError:
            pass

    public_export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    try:
        return fetch_url(public_export_url)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise RuntimeError(
                "Could not read the document. Make it accessible to anyone with the link "
                "or set GOOGLE_OAUTH_ACCESS_TOKEN with Drive read access."
            ) from exc
        raise


class ReadableHTMLParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "template"}
    BLOCK_TAGS = {
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return
        if not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            self.parts.append(text)

    def text(self):
        return "\n".join(self.parts)


def html_to_readable_text(html):
    parser = ReadableHTMLParser()
    parser.feed(html or "")
    return parser.text()


def validate_public_webpage_url(page_url):
    parsed = urllib.parse.urlparse((page_url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.hostname:
        raise ValueError("Paste a full webpage URL starting with http:// or https://.")
    host = parsed.hostname
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve webpage host: {host}") from exc
        addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Webpage import only supports public internet hosts. Save private or local pages as a file instead.")
    return urllib.parse.urlunparse(parsed)


def fetch_webpage_text(page_url):
    page_url = validate_public_webpage_url(page_url)
    html = fetch_url(page_url)
    return html_to_readable_text(html)


def fetch_source_text(source_type, source_ref):
    source_type = (source_type or "google_doc").strip().lower()
    if source_type in ("webpage", "web", "url", "article"):
        return fetch_webpage_text(source_ref)
    if source_type in ("google_doc", "doc", "google-doc", "gdoc"):
        return fetch_google_doc_text(source_ref)
    raise ValueError(f"Unknown source type: {source_type}")


def normalize_doc_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if len(cleaned) < 200:
        raise ValueError("The source did not contain enough readable text to make a deck.")
    return cleaned


FLASHCARD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "deck_title": {"type": "string"},
        "summary": {"type": "string"},
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "explanation": {"type": "string"},
                    "source_cue": {"type": "string"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["question", "answer", "explanation", "source_cue", "tags"],
            },
        },
    },
    "required": ["deck_title", "summary", "cards"],
}

SLIDES_APPS_SCRIPT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "apps_script": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["apps_script", "notes"],
}


def extract_response_text(payload):
    if isinstance(payload, dict) and isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    texts = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("type") in ("output_text", "text") and isinstance(value.get("text"), str):
                texts.append(value["text"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload.get("output", []) if isinstance(payload, dict) else payload)
    return "\n".join(texts).strip()


def validate_llm_deck(deck, card_count):
    cards = deck.get("cards") if isinstance(deck, dict) else None
    if not isinstance(cards, list):
        raise ValueError("LLM response did not include cards.")
    clean_cards = []
    for card in cards[:card_count]:
        clean_cards.append(
            {
                "question": str(card.get("question", "")).strip(),
                "answer": str(card.get("answer", "")).strip(),
                "explanation": str(card.get("explanation", "")).strip(),
                "source_cue": str(card.get("source_cue", "")).strip(),
                "tags": [
                    slugify(str(tag)).replace("-", "_")
                    for tag in card.get("tags", [])
                    if str(tag).strip()
                ][:4],
            }
        )
    clean_cards = [card for card in clean_cards if card["question"] and card["answer"]]
    if len(clean_cards) != card_count:
        raise ValueError(f"LLM response produced {len(clean_cards)} usable cards, expected {card_count}.")
    return {
        "deck_title": str(deck.get("deck_title", "Flashcard Deck")).strip()[:120],
        "summary": str(deck.get("summary", "")).strip(),
        "cards": clean_cards,
    }


def generate_with_openai(doc_text, requested_title, card_count, difficulty):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    trimmed_doc = doc_text[:36000]
    prompt = f"""
Generate exactly {card_count} study flashcards from the source text below.

Requirements:
- Each question must be answerable from the source text.
- Mix recall, conceptual, and application-style practice questions.
- Answers should be concise but complete.
- Explanations should help the learner understand why the answer matters.
- Source cues should quote or paraphrase a short anchor from the source text.
- Difficulty: {difficulty or "balanced"}.
- Prefer specific details over generic summaries.
- Return only data matching the provided JSON schema.

Requested deck title: {requested_title or "Auto-generated flashcards"}

Source text:
{trimmed_doc}
"""
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": "You are a precise study deck generator that creates high-signal flashcards.",
            },
            {"role": "user", "content": prompt},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "flashcard_deck",
                "strict": True,
                "schema": FLASHCARD_SCHEMA,
            }
        },
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/responses",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        data = json.loads(response.read().decode("utf-8"))
    output_text = extract_response_text(data)
    if not output_text:
        raise ValueError("OpenAI response did not contain text output.")
    return validate_llm_deck(json.loads(output_text), card_count), f"openai:{model}"


def parse_json_object(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def generate_with_codex_cli(doc_text, requested_title, card_count, difficulty, codex_cmd=None):
    codex_cmd = codex_cmd or os.getenv("CODEX_CLI_BIN", "codex")
    trimmed_doc = doc_text[:36000]
    prompt = f"""
You are generating a flashcard deck for a local study app.

Return exactly {card_count} flashcards as JSON matching the provided output schema.
Do not edit files. Do not run commands. Only produce the final JSON object.

Requirements:
- Each question must be answerable from the source document.
- Mix recall, conceptual, and application-style practice questions.
- Answers should be concise but complete.
- Explanations should help the learner understand why the answer matters.
- Source cues should quote or paraphrase a short anchor from the source document.
- Difficulty: {difficulty or "balanced"}.
- Requested deck title: {requested_title or "Auto-generated flashcards"}.

Source document:
{trimmed_doc}
"""
    timeout = int(os.getenv("CODEX_CLI_TIMEOUT", "180"))
    with tempfile.TemporaryDirectory(prefix="doc-flashcards-codex-") as tmpdir:
        tmp_path = Path(tmpdir)
        schema_path = tmp_path / "flashcard_schema.json"
        output_path = tmp_path / "flashcard_deck.json"
        schema_path.write_text(json.dumps(FLASHCARD_SCHEMA), encoding="utf-8")
        command = [
            codex_cmd,
            "exec",
            "--skip-git-repo-check",
            "--cd",
            str(ROOT),
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            prompt,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Codex CLI generation failed: {details}")
        output_text = output_path.read_text(encoding="utf-8") if output_path.exists() else result.stdout
    deck = validate_llm_deck(parse_json_object(output_text), card_count)
    return deck, "codex-cli"


def slides_export_prompt(deck):
    deck_payload = {
        "title": deck.get("title", "Flashcard Deck"),
        "summary": deck.get("summary", ""),
        "cards": deck.get("cards", []),
    }
    return f"""
Generate Google Apps Script that creates a Google Slides flashcard deck.

Return only JSON with:
- apps_script: a complete self-contained Apps Script file
- notes: a one-sentence note about the script

Apps Script requirements:
- Define function createFlashcardDeck().
- Use SlidesApp.create(deck title).
- Remove the default first slide contents.
- Add one overview slide with the deck title and summary.
- Add one question slide and one answer slide for every card.
- Include answer, explanation, and source cue on answer slides when present.
- Include helper functions inside the script.
- Do not use external APIs, OAuth flows, HTML service, or file writes.

Deck JSON:
{json.dumps(deck_payload, ensure_ascii=False, indent=2)}
"""


def validate_apps_script_payload(payload, provider):
    script = str((payload or {}).get("apps_script", "")).strip()
    if not script:
        raise ValueError(f"{provider} CLI export did not return apps_script.")
    if "SlidesApp.create" not in script or "createFlashcardDeck" not in script:
        raise ValueError(f"{provider} CLI export did not return a usable Google Slides Apps Script.")
    return script


def generate_slides_script_with_codex_cli(deck, codex_cmd=None):
    codex_cmd = codex_cmd or os.getenv("CODEX_CLI_BIN", "codex")
    timeout = int(os.getenv("CODEX_CLI_TIMEOUT", "180"))
    with tempfile.TemporaryDirectory(prefix="doc-flashcards-slides-codex-") as tmpdir:
        tmp_path = Path(tmpdir)
        schema_path = tmp_path / "slides_apps_script_schema.json"
        output_path = tmp_path / "slides_apps_script.json"
        schema_path.write_text(json.dumps(SLIDES_APPS_SCRIPT_SCHEMA), encoding="utf-8")
        command = [
            codex_cmd,
            "exec",
            "--skip-git-repo-check",
            "--cd",
            str(ROOT),
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            slides_export_prompt(deck),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Codex CLI Slides export failed: {details}")
        output_text = output_path.read_text(encoding="utf-8") if output_path.exists() else result.stdout
    return validate_apps_script_payload(parse_json_object(output_text), "Codex")


def extract_gemini_output_text(output):
    output = (output or "").strip()
    if not output:
        return ""
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return output
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("response", "text", "content", "message", "output"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (dict, list)):
                return json.dumps(value)
        return json.dumps(payload)
    return output


def generate_slides_script_with_gemini_cli(deck, gemini_cmd=None):
    gemini_cmd = gemini_cmd or os.getenv("GEMINI_CLI_BIN", "gemini")
    timeout = int(os.getenv("GEMINI_CLI_TIMEOUT", "180"))
    command = [
        gemini_cmd,
        "--skip-trust",
        "--approval-mode",
        "plan",
        "--output-format",
        "json",
        "--prompt",
        slides_export_prompt(deck),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Gemini CLI Slides export failed: {details}")
    output_text = extract_gemini_output_text(result.stdout)
    return validate_apps_script_payload(parse_json_object(output_text), "Gemini")


def local_fallback_deck(doc_text, requested_title, card_count):
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", doc_text))
    candidates = [s.strip() for s in sentences if 80 <= len(s.strip()) <= 320]
    if len(candidates) < card_count:
        candidates = [s.strip() for s in sentences if len(s.strip()) >= 45]
    title = requested_title or first_heading(doc_text) or "Google Doc Flashcards"
    cards = []
    for idx, sentence in enumerate(candidates[:card_count], start=1):
        cue = sentence[:150]
        cards.append(
            {
                "question": f"What is the key idea in this source excerpt? ({idx})",
                "answer": sentence,
                "explanation": "This fallback card preserves an important source sentence. Set OPENAI_API_KEY for richer LLM-generated questions.",
                "source_cue": cue,
                "tags": ["fallback", "review"],
            }
        )
    if len(cards) < 8:
        raise ValueError("The source did not contain enough sentence-level material for fallback cards.")
    return {
        "deck_title": title,
        "summary": "Local fallback deck generated without an LLM API key.",
        "cards": cards,
    }, "local-fallback"


def first_heading(doc_text):
    for line in doc_text.splitlines():
        line = line.strip()
        if 5 <= len(line) <= 90 and not line.endswith("."):
            return line
    return None


def generate_flashcards(doc_text, title, card_count, difficulty, provider="codex"):
    provider = (provider or "codex").strip().lower()
    if provider == "local":
        return local_fallback_deck(doc_text, title, card_count)
    if provider == "codex":
        try:
            return generate_with_codex_cli(doc_text, title, card_count, difficulty)
        except Exception as exc:
            if os.getenv("REQUIRE_LLM", "").lower() in ("1", "true", "yes"):
                raise
            deck, generator = local_fallback_deck(doc_text, title, card_count)
            deck["summary"] += f" Codex CLI generation was skipped or failed: {exc}"
            return deck, generator
    if provider != "openai":
        raise ValueError(f"Unknown flashcard generator: {provider}")
    try:
        return generate_with_openai(doc_text, title, card_count, difficulty)
    except Exception as exc:
        if os.getenv("REQUIRE_LLM", "").lower() in ("1", "true", "yes"):
            raise
        deck, generator = local_fallback_deck(doc_text, title, card_count)
        deck["summary"] += f" LLM generation was skipped or failed: {exc}"
        return deck, generator


def serializable_deck(deck):
    return {
        "title": deck.get("title", "Flashcard Deck"),
        "summary": deck.get("summary", ""),
        "source_url": deck.get("source_url", ""),
        "source_type": deck.get("source_type", "google_doc"),
        "created_at": deck.get("created_at", ""),
        "generator": deck.get("generator", ""),
        "cards": deck.get("cards", []),
    }


def content_type_for_path(file_path):
    if file_path.suffix == ".css":
        return "text/css; charset=utf-8"
    if file_path.suffix == ".js":
        return "application/javascript; charset=utf-8"
    if file_path.suffix == ".webmanifest":
        return "application/manifest+json; charset=utf-8"
    if file_path.suffix == ".png":
        return "image/png"
    if file_path.suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if file_path.suffix == ".webp":
        return "image/webp"
    if file_path.suffix == ".md":
        return "text/markdown; charset=utf-8"
    if file_path.suffix == ".html":
        return "text/html; charset=utf-8"
    return "text/plain; charset=utf-8"


def deck_to_markdown(deck):
    deck = serializable_deck(deck)
    json_blob = json.dumps(deck, ensure_ascii=False, indent=2)
    frontmatter_tags = sorted(
        {
            tag
            for card in deck.get("cards", [])
            for tag in card.get("tags", [])
            if tag
        }
    )
    source_type = deck.get("source_type", "google_doc")
    source_tag = {
        "google_doc": "google-doc",
        "webpage": "webpage",
        "file": "file-source",
    }.get(source_type, slugify(source_type))
    tags_line = ", ".join(["flashcards", source_tag] + frontmatter_tags[:8])
    lines = [
        "---",
        "type: flashcard-deck",
        f"title: {json.dumps(deck['title'], ensure_ascii=False)}",
        f"source: {json.dumps(deck.get('source_url', ''), ensure_ascii=False)}",
        f"source_type: {json.dumps(deck.get('source_type', 'google_doc'), ensure_ascii=False)}",
        f"created: {json.dumps(deck.get('created_at', ''), ensure_ascii=False)}",
        f"cards: {len(deck.get('cards', []))}",
        f"generator: {json.dumps(deck.get('generator', ''), ensure_ascii=False)}",
        f"tags: [{tags_line}]",
        "---",
        "",
        "<!-- flashcard-deck-json",
        json_blob,
        "-->",
        "",
        f"# {deck['title']}",
        "",
        "> [!summary]",
        f"> {deck.get('summary', '').strip()}",
        "",
        f"- Source: {deck.get('source_url', '')}",
        f"- Source type: {deck.get('source_type', 'google_doc')}",
        f"- Created: {deck.get('created_at', '')}",
        f"- Cards: {len(deck.get('cards', []))}",
        f"- Generator: {deck.get('generator', '')}",
        "",
        "## Cards",
        "",
    ]
    for idx, card in enumerate(deck.get("cards", []), start=1):
        tags = ", ".join(card.get("tags", []))
        lines.extend(
            [
                f"### Card {idx}",
                "",
                "> [!question]",
                f"> {card['question']}",
                "",
                "> [!success] Answer",
                f"> {card['answer']}",
                "",
                "> [!note] Explanation",
                f"> {card.get('explanation', '')}",
                "",
                f"**Source cue:** {card.get('source_cue', '')}",
                "",
                f"**Tags:** {tags}",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def save_deck(deck):
    DECKS_DIR.mkdir(exist_ok=True)
    base_slug = slugify(deck["title"])
    slug = base_slug
    path = DECKS_DIR / f"{slug}.md"
    suffix = 2
    while path.exists():
        slug = f"{base_slug}-{suffix}"
        path = DECKS_DIR / f"{slug}.md"
        suffix += 1
    deck["slug"] = slug
    path.write_text(deck_to_markdown(deck), encoding="utf-8")
    return slug, path


def delete_deck(slug):
    deck_path = (DECKS_DIR / f"{slugify(slug)}.md").resolve()
    if not str(deck_path).startswith(str(DECKS_DIR.resolve())) or not deck_path.exists():
        return False
    deck_path.unlink()
    return True


def load_deck(path):
    markdown = path.read_text(encoding="utf-8")
    match = re.search(r"<!-- flashcard-deck-json\s*(.*?)\s*-->", markdown, re.S)
    if not match:
        raise ValueError(f"{path.name} does not contain flashcard deck metadata.")
    deck = json.loads(match.group(1))
    deck["slug"] = path.stem
    deck["markdown"] = markdown
    deck["markdown_path"] = str(path)
    deck["markdown_url"] = f"/decks/{path.stem}.md"
    return deck


def list_decks():
    decks = []
    for path in sorted(DECKS_DIR.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            deck = load_deck(path)
        except Exception:
            continue
        decks.append(
            {
                "slug": path.stem,
                "title": deck.get("title", path.stem),
                "summary": deck.get("summary", ""),
                "created_at": deck.get("created_at", ""),
                "card_count": len(deck.get("cards", [])),
                "generator": deck.get("generator", ""),
            }
        )
    return decks


def create_slide_text_requests(slide_id, title, body, y_offset=42):
    safe_title = title[:900]
    safe_body = body[:2800]
    title_id = f"{slide_id}_title"
    body_id = f"{slide_id}_body"
    return [
        {
            "createShape": {
                "objectId": title_id,
                "shapeType": "TEXT_BOX",
                "elementProperties": {
                    "pageObjectId": slide_id,
                    "size": {"height": {"magnitude": 70, "unit": "PT"}, "width": {"magnitude": 620, "unit": "PT"}},
                    "transform": {"scaleX": 1, "scaleY": 1, "translateX": 44, "translateY": y_offset, "unit": "PT"},
                },
            }
        },
        {"insertText": {"objectId": title_id, "text": safe_title}},
        {
            "updateTextStyle": {
                "objectId": title_id,
                "style": {"fontSize": {"magnitude": 24, "unit": "PT"}, "bold": True},
                "fields": "fontSize,bold",
            }
        },
        {
            "createShape": {
                "objectId": body_id,
                "shapeType": "TEXT_BOX",
                "elementProperties": {
                    "pageObjectId": slide_id,
                    "size": {"height": {"magnitude": 300, "unit": "PT"}, "width": {"magnitude": 620, "unit": "PT"}},
                    "transform": {"scaleX": 1, "scaleY": 1, "translateX": 44, "translateY": y_offset + 92, "unit": "PT"},
                },
            }
        },
        {"insertText": {"objectId": body_id, "text": safe_body}},
        {
            "updateTextStyle": {
                "objectId": body_id,
                "style": {"fontSize": {"magnitude": 15, "unit": "PT"}},
                "fields": "fontSize",
            }
        },
    ]


def create_cli_slides_export(deck, provider):
    if provider == "codex":
        script = generate_slides_script_with_codex_cli(deck)
        label = "Codex CLI"
    elif provider == "gemini":
        script = generate_slides_script_with_gemini_cli(deck)
        label = "Gemini CLI"
    else:
        raise ValueError(f"Unknown Slides export provider: {provider}")
    return {
        "mode": "manual",
        "provider": f"{provider}-cli",
        "message": f"Generated Apps Script with {label}. Paste it into script.google.com and run createFlashcardDeck().",
        "apps_script": script,
    }


def create_google_slides(deck, provider="google"):
    provider = (provider or "google").strip().lower()
    if provider in ("codex", "codex-cli"):
        return create_cli_slides_export(deck, "codex")
    if provider in ("gemini", "gemini-cli"):
        return create_cli_slides_export(deck, "gemini")
    if provider not in ("google", "direct", "apps-script", "manual"):
        raise ValueError(f"Unknown Slides export provider: {provider}")

    token = (
        os.getenv("GOOGLE_OAUTH_ACCESS_TOKEN")
        or os.getenv("GOOGLE_ACCESS_TOKEN")
        or os.getenv("GOOGLE_SLIDES_ACCESS_TOKEN")
    )
    if not token:
        return {
            "mode": "manual",
            "provider": "google-apps-script",
            "message": "Set GOOGLE_OAUTH_ACCESS_TOKEN with Slides and Drive scopes for direct export, or paste the Apps Script below into script.google.com.",
            "apps_script": deck_to_apps_script(deck),
        }

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    create_body = json.dumps({"title": deck["title"]}).encode("utf-8")
    create_request = urllib.request.Request(
        "https://slides.googleapis.com/v1/presentations",
        data=create_body,
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(create_request, timeout=30) as response:
        presentation = json.loads(response.read().decode("utf-8"))
    presentation_id = presentation["presentationId"]
    requests = []
    initial_slide_id = None
    if presentation.get("slides"):
        initial_slide_id = presentation["slides"][0].get("objectId")
    if initial_slide_id:
        requests.append({"deleteObject": {"objectId": initial_slide_id}})
    overview_id = "overview"
    requests.append({"createSlide": {"objectId": overview_id, "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
    requests.extend(create_slide_text_requests(overview_id, deck["title"], deck.get("summary", ""), 58))
    for idx, card in enumerate(deck.get("cards", []), start=1):
        q_slide = f"card_{idx}_question"
        a_slide = f"card_{idx}_answer"
        requests.append({"createSlide": {"objectId": q_slide, "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
        requests.extend(create_slide_text_requests(q_slide, f"Card {idx}: Question", card["question"]))
        answer_body = "\n\n".join(
            part
            for part in [
                "Answer:\n" + card["answer"],
                "Explanation:\n" + card.get("explanation", ""),
                "Source cue:\n" + card.get("source_cue", ""),
            ]
            if part.strip()
        )
        requests.append({"createSlide": {"objectId": a_slide, "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
        requests.extend(create_slide_text_requests(a_slide, f"Card {idx}: Answer", answer_body))

    batch_body = json.dumps({"requests": requests}).encode("utf-8")
    batch_request = urllib.request.Request(
        f"https://slides.googleapis.com/v1/presentations/{presentation_id}:batchUpdate",
        data=batch_body,
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(batch_request, timeout=60) as response:
        response.read()
    return {
        "mode": "direct",
        "provider": "google-api",
        "presentation_id": presentation_id,
        "url": f"https://docs.google.com/presentation/d/{presentation_id}/edit",
    }


def js_string(value):
    return json.dumps(str(value))


def deck_to_apps_script(deck):
    cards = deck.get("cards", [])
    card_data = json.dumps(cards, ensure_ascii=False, indent=2)
    return textwrap.dedent(
        f"""
        function createFlashcardDeck() {{
          const deckTitle = {js_string(deck["title"])};
          const deckSummary = {js_string(deck.get("summary", ""))};
          const cards = {card_data};
          const presentation = SlidesApp.create(deckTitle);
          const first = presentation.getSlides()[0];
          first.getShapes().forEach(shape => shape.remove());
          addText(first, deckTitle, deckSummary, true);

          cards.forEach((card, index) => {{
            const q = presentation.appendSlide(SlidesApp.PredefinedLayout.BLANK);
            addText(q, `Card ${{index + 1}}: Question`, card.question, true);
            const a = presentation.appendSlide(SlidesApp.PredefinedLayout.BLANK);
            addText(a, `Card ${{index + 1}}: Answer`, `Answer:\\n${{card.answer}}\\n\\nExplanation:\\n${{card.explanation}}\\n\\nSource cue:\\n${{card.source_cue}}`, false);
          }});
          Logger.log(presentation.getUrl());
        }}

        function addText(slide, title, body, largeTitle) {{
          const titleBox = slide.insertTextBox(title, 36, 40, 640, 80);
          titleBox.getText().getTextStyle().setBold(true).setFontSize(largeTitle ? 28 : 24);
          const bodyBox = slide.insertTextBox(body, 42, 130, 620, 300);
          bodyBox.getText().getTextStyle().setFontSize(16);
        }}
        """
    ).strip()


class FlashcardHandler(BaseHTTPRequestHandler):
    server_version = "GoogleDocFlashcards/0.1"

    def log_message(self, fmt, *args):
        return

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location, status=303):
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def cookie_value(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def is_authenticated(self):
        if not auth_enabled():
            return True
        token = self.cookie_value(AUTH_COOKIE)
        if not token:
            return False
        return hmac.compare_digest(token, expected_auth_token())

    def guard(self, path):
        """Returns True if the request was blocked (and a response already sent)."""
        if not auth_enabled() or is_public_path(path) or self.is_authenticated():
            return False
        if path.startswith("/api/") or path.startswith("/decks/"):
            self.send_json({"error": "Unauthorized.", "auth_required": True}, 401)
        else:
            self.redirect("/login")
        return True

    def handle_login(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        fields = urllib.parse.parse_qs(body)
        passcode = (fields.get("passcode", [""])[0]).strip()
        if auth_enabled() and hmac.compare_digest(passcode, APP_PASSCODE):
            self.send_response(303)
            self.send_header("Location", "/")
            cookie = http.cookies.SimpleCookie()
            cookie[AUTH_COOKIE] = expected_auth_token()
            morsel = cookie[AUTH_COOKIE]
            morsel["path"] = "/"
            morsel["httponly"] = True
            morsel["samesite"] = "Lax"
            morsel["max-age"] = 60 * 60 * 24 * 365
            self.send_header("Set-Cookie", morsel.OutputString())
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self.redirect("/login?error=1")

    def handle_logout(self):
        self.send_response(303)
        self.send_header("Location", "/login")
        cookie = http.cookies.SimpleCookie()
        cookie[AUTH_COOKIE] = ""
        morsel = cookie[AUTH_COOKIE]
        morsel["path"] = "/"
        morsel["max-age"] = 0
        self.send_header("Set-Cookie", morsel.OutputString())
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if self.guard(path):
            return
        file_path = None
        if path in ("/", "/index.html"):
            file_path = STATIC_DIR / "index.html"
        elif path == "/manifest.webmanifest":
            file_path = STATIC_DIR / "manifest.webmanifest"
        elif path == "/service-worker.js":
            file_path = STATIC_DIR / "service-worker.js"
        elif path.startswith("/static/"):
            rel = path.removeprefix("/static/")
            candidate = (STATIC_DIR / rel).resolve()
            if str(candidate).startswith(str(STATIC_DIR.resolve())) and candidate.exists():
                file_path = candidate
        elif path.startswith("/decks/") and path.endswith(".md"):
            slug = path.removeprefix("/decks/").removesuffix(".md")
            candidate = (DECKS_DIR / f"{slugify(slug)}.md").resolve()
            if str(candidate).startswith(str(DECKS_DIR.resolve())) and candidate.exists():
                file_path = candidate
        if file_path:
            self.send_response(200)
            self.send_header("Content-Type", content_type_for_path(file_path))
            self.send_header("Content-Length", str(file_path.stat().st_size))
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/login":
                if self.is_authenticated() and auth_enabled():
                    return self.redirect("/")
                return self.send_file(STATIC_DIR / "login.html", "text/html; charset=utf-8")
            if path == "/logout":
                return self.handle_logout()
            if path == "/api/config":
                return self.send_json({"auth_enabled": auth_enabled()})
            if self.guard(path):
                return
            if path in ("/", "/index.html"):
                return self.send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            if path == "/manifest.webmanifest":
                return self.send_file(STATIC_DIR / "manifest.webmanifest", "application/manifest+json; charset=utf-8")
            if path == "/service-worker.js":
                return self.send_file(STATIC_DIR / "service-worker.js", "application/javascript; charset=utf-8")
            if path == "/api/decks":
                return self.send_json({"decks": list_decks()})
            if path.startswith("/api/decks/"):
                slug = path.rsplit("/", 1)[-1]
                deck_path = DECKS_DIR / f"{slug}.md"
                if not deck_path.exists():
                    return self.send_json({"error": "Deck not found."}, 404)
                return self.send_json({"deck": load_deck(deck_path)})
            if path.startswith("/decks/") and path.endswith(".md"):
                slug = path.removeprefix("/decks/").removesuffix(".md")
                deck_path = (DECKS_DIR / f"{slugify(slug)}.md").resolve()
                if not str(deck_path).startswith(str(DECKS_DIR.resolve())) or not deck_path.exists():
                    return self.send_json({"error": "Deck not found."}, 404)
                return self.send_file(deck_path, "text/markdown; charset=utf-8")
            if path.startswith("/static/"):
                rel = path.removeprefix("/static/")
                file_path = (STATIC_DIR / rel).resolve()
                if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.exists():
                    return self.send_json({"error": "Static file not found."}, 404)
                return self.send_file(file_path, content_type_for_path(file_path))
            return self.send_json({"error": "Not found."}, 404)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/login":
            return self.handle_login()
        if self.guard(parsed.path):
            return
        try:
            payload = self.read_json()
            if parsed.path == "/api/generate":
                source_ref = payload.get("source_url") or payload.get("doc_url", "")
                source_type = payload.get("source_type", "google_doc")
                doc_text = normalize_doc_text(fetch_source_text(source_type, source_ref))
                count = clamp_card_count(payload.get("card_count", 12))
                raw_deck, generator = generate_flashcards(
                    doc_text,
                    payload.get("title", ""),
                    count,
                    payload.get("difficulty", "balanced"),
                    payload.get("provider", "codex"),
                )
                title = raw_deck.get("deck_title") or payload.get("title") or "Google Doc Flashcards"
                deck = {
                    "title": title,
                    "summary": raw_deck.get("summary", ""),
                    "source_url": source_ref,
                    "source_type": source_type,
                    "created_at": utc_now(),
                    "generator": generator,
                    "cards": raw_deck.get("cards", []),
                }
                slug, path = save_deck(deck)
                deck["slug"] = slug
                deck["markdown_path"] = str(path)
                deck["markdown_url"] = f"/decks/{slug}.md"
                return self.send_json({"deck": deck})
            if parsed.path == "/api/export/slides":
                slug = slugify(payload.get("slug", ""))
                deck_path = DECKS_DIR / f"{slug}.md"
                if not deck_path.exists():
                    return self.send_json({"error": "Deck not found."}, 404)
                result = create_google_slides(load_deck(deck_path), payload.get("provider", "google"))
                return self.send_json(result)
            return self.send_json({"error": "Not found."}, 404)
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            return self.send_json({"error": f"HTTP {exc.code}: {details}"}, 502)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, 500)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        if self.guard(parsed.path):
            return
        if parsed.path.startswith("/api/decks/"):
            slug = parsed.path.rsplit("/", 1)[-1]
            if delete_deck(slug):
                return self.send_json({"deleted": slug})
            return self.send_json({"error": "Deck not found."}, 404)
        return self.send_json({"error": "Not found."}, 404)


def lan_ip():
    """Best-effort primary LAN/Tailscale IP for mobile-access hints."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def main():
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8765"))
    DECKS_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((host, port), FlashcardHandler)
    print(f"{APP_NAME} running at http://{host}:{port}")
    print(f"Decks are saved to {DECKS_DIR}")
    if host == "0.0.0.0":
        ip = lan_ip()
        if ip:
            print(f"On this network / Tailscale, open: http://{ip}:{port}")
    if auth_enabled():
        print("Passcode protection: ON (set via APP_PASSCODE).")
    else:
        print("Passcode protection: OFF. Set APP_PASSCODE before exposing beyond localhost.")
    server.serve_forever()


if __name__ == "__main__":
    main()
