#!/usr/bin/env python3
import datetime as dt
import hashlib
import hmac
import http.client
import http.cookies
import ipaddress
import json
import os
import re
import shlex
import socket
import ssl
import subprocess
import tempfile
import textwrap
import threading
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
GOOGLE_AUTH_SCOPES = (
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
)
GOOGLE_AUTH_PROVIDER_HELP = (
    "Set GOOGLE_OAUTH_ACCESS_TOKEN, or run `python3 flashcards_cli.py google-auth --run` "
    "once and restart the app with GOOGLE_AUTH_PROVIDER=gcloud."
)


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
# Optional extra secret so a deployer can rotate/revoke every issued cookie without
# changing the passcode itself. Mixed into the token signing key below.
APP_AUTH_SECRET = os.getenv("APP_AUTH_SECRET", "").strip()
AUTH_COOKIE = "dino_auth"
# Auth cookies (and the signed tokens inside them) expire after this many days, bounding
# how long a leaked cookie stays usable. Minimum one day.
AUTH_TTL_SECONDS = max(1, int(os.getenv("APP_AUTH_TTL_DAYS", "30"))) * 24 * 60 * 60
# Set APP_COOKIE_SECURE=1 when serving over HTTPS (e.g. Tailscale Serve or a TLS proxy)
# so the auth cookie is only ever sent over encrypted connections.
COOKIE_SECURE = os.getenv("APP_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")


def auth_enabled():
    return bool(APP_PASSCODE)


def _auth_signing_key():
    # Only someone who knows the passcode (and optional rotation secret) can sign a token,
    # and the token never reveals either value.
    return f"{APP_PASSCODE}|{APP_AUTH_SECRET}".encode("utf-8")


def _sign_auth(issued_at):
    message = f"dino-decks-auth-v1|{issued_at}".encode("utf-8")
    return hmac.new(_auth_signing_key(), message, hashlib.sha256).hexdigest()


def issue_auth_token(issued_at=None):
    if issued_at is None:
        issued_at = int(dt.datetime.now(dt.timezone.utc).timestamp())
    issued_at = int(issued_at)
    return f"{issued_at}.{_sign_auth(issued_at)}"


def auth_token_valid(token):
    # Stateless, signed, time-bounded token. It survives restarts (no server-side session
    # store) but expires after AUTH_TTL_SECONDS and can be revoked by setting/rotating
    # APP_AUTH_SECRET.
    if not token or "." not in token:
        return False
    issued_str, _, signature = token.partition(".")
    try:
        issued_at = int(issued_str)
    except ValueError:
        return False
    if not hmac.compare_digest(signature, _sign_auth(issued_at)):
        return False
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    if issued_at > now + 300:  # reject future-dated tokens (allow small clock skew)
        return False
    return now - issued_at <= AUTH_TTL_SECONDS


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


MAX_FETCH_BYTES = 5 * 1024 * 1024


def google_auth_scopes():
    configured = os.getenv("GOOGLE_AUTH_SCOPES", "").strip()
    if configured:
        parts = [part.strip() for part in re.split(r"[\s,]+", configured) if part.strip()]
        if parts:
            return tuple(parts)
    return GOOGLE_AUTH_SCOPES


def google_auth_scopes_arg():
    return ",".join(google_auth_scopes())


def gcloud_bin():
    return os.getenv("GCLOUD_BIN", "gcloud").strip() or "gcloud"


def env_google_oauth_token():
    for name in ("GOOGLE_OAUTH_ACCESS_TOKEN", "GOOGLE_ACCESS_TOKEN", "GOOGLE_SLIDES_ACCESS_TOKEN"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def google_auth_provider():
    return os.getenv("GOOGLE_AUTH_PROVIDER", "").strip().lower()


def google_auth_login_command(client_id_file=None, no_launch_browser=False):
    command = [
        gcloud_bin(),
        "auth",
        "application-default",
        "login",
        f"--scopes={google_auth_scopes_arg()}",
    ]
    if client_id_file:
        command.append(f"--client-id-file={client_id_file}")
    if no_launch_browser:
        command.append("--no-launch-browser")
    return command


def google_auth_login_command_text(client_id_file=None, no_launch_browser=False):
    return shlex.join(google_auth_login_command(client_id_file, no_launch_browser))


def gcloud_access_token():
    command = [
        gcloud_bin(),
        "auth",
        "application-default",
        "print-access-token",
        f"--scopes={google_auth_scopes_arg()}",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=int(os.getenv("GCLOUD_AUTH_TIMEOUT", "20")),
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("gcloud was not found. Install Google Cloud CLI or set GCLOUD_BIN.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        suffix = f" Details: {detail}" if detail else ""
        raise RuntimeError(
            "Could not get a Google access token from gcloud. "
            f"{GOOGLE_AUTH_PROVIDER_HELP}{suffix}"
        ) from exc
    token = result.stdout.strip()
    if not token:
        raise RuntimeError(f"gcloud did not return a Google access token. {GOOGLE_AUTH_PROVIDER_HELP}")
    return token


def google_oauth_token():
    token = env_google_oauth_token()
    if token:
        return token
    if google_auth_provider() in ("gcloud", "adc"):
        return gcloud_access_token()
    return None


def fetch_url(url, headers=None, timeout=30, max_bytes=MAX_FETCH_BYTES):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"{APP_NAME}/0.1",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError("The source response was too large to import.")
    return body.decode(charset, errors="replace")


def fetch_google_doc_text(doc_ref):
    doc_id = extract_doc_id(doc_ref)
    if not doc_id:
        raise ValueError("Paste a Google Doc share URL or document id.")

    token_error = None
    try:
        token = google_oauth_token()
    except RuntimeError as exc:
        token = None
        token_error = exc

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
            auth_hint = f" Google auth also failed: {token_error}" if token_error else ""
            raise RuntimeError(
                "Could not read the document. Make it accessible to anyone with the link "
                f"or configure Google auth with Drive read access. {GOOGLE_AUTH_PROVIDER_HELP}{auth_hint}"
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


def _port_for(parsed):
    return parsed.port or (443 if parsed.scheme == "https" else 80)


def resolve_public_addresses(host, port):
    """Resolve a host to its IPs and ensure every one is a public internet address.

    Returns the resolved, validated (family, ip) tuples so callers can connect to a vetted
    IP directly instead of re-resolving, which is what defeats DNS-rebinding TOCTOU."""
    try:
        literal = ipaddress.ip_address(host)
        family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        results = [(family, str(literal))]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve webpage host: {host}") from exc
        results = [(info[0], info[4][0]) for info in infos]
    addresses = {ipaddress.ip_address(ip) for _, ip in results}
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Webpage import only supports public internet hosts. Save private or local pages as a file instead.")
    return results


def validate_public_webpage_url(page_url):
    parsed = urllib.parse.urlparse((page_url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.hostname:
        raise ValueError("Paste a full webpage URL starting with http:// or https://.")
    resolve_public_addresses(parsed.hostname, _port_for(parsed))
    return urllib.parse.urlunparse(parsed)


def _http_exchange(parsed, address, timeout, max_bytes):
    """Perform one HTTP(S) request pinned to the already-validated `address`.

    Connecting to the vetted IP (rather than letting the client re-resolve the hostname)
    is what closes the DNS-rebinding window. Returns (status, headers, body_text); raises
    HTTPError for >= 400 responses."""
    _family, ip = address
    host = parsed.hostname
    port = _port_for(parsed)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    sock = socket.create_connection((ip, port), timeout=timeout)
    conn = None
    try:
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            # SNI and certificate validation use the real hostname; the socket stays pinned to ip.
            sock = context.wrap_socket(sock, server_hostname=host)
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = sock
        conn.request(
            "GET",
            path,
            headers={
                "Host": parsed.netloc,
                "User-Agent": f"{APP_NAME}/0.1",
                "Accept": "text/html, text/plain, */*",
                "Connection": "close",
            },
        )
        response = conn.getresponse()
        status = response.status
        headers = response.headers
        if 300 <= status < 400:
            response.read()
            return status, headers, ""
        if status >= 400:
            response.read()
            raise urllib.error.HTTPError(parsed.geturl(), status, response.reason, headers, None)
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("The webpage is too large to import. Save it as a file and import it that way instead.")
        charset = headers.get_content_charset() or "utf-8"
        return status, headers, body.decode(charset, errors="replace")
    finally:
        if conn is not None:
            conn.close()
        else:
            sock.close()


def fetch_public_url(url, timeout=30, max_redirects=5, max_bytes=MAX_FETCH_BYTES):
    """Fetch a URL that must resolve to a public host, re-validating every redirect hop.

    Each hop is independently validated and pinned, so neither an attacker-controlled
    redirect (e.g. to cloud metadata or localhost) nor DNS rebinding can reach a private
    address."""
    current = url
    for _ in range(max_redirects + 1):
        parsed = urllib.parse.urlparse((current or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.hostname:
            raise ValueError("Paste a full webpage URL starting with http:// or https://.")
        address = resolve_public_addresses(parsed.hostname, _port_for(parsed))[0]
        status, headers, body = _http_exchange(parsed, address, timeout, max_bytes)
        if 300 <= status < 400:
            location = headers.get("Location")
            if not location:
                raise RuntimeError("Webpage redirect was missing a target.")
            current = urllib.parse.urljoin(current, location)
            continue
        return body
    raise RuntimeError("Too many redirects while fetching the webpage.")


def fetch_webpage_text(page_url):
    return html_to_readable_text(fetch_public_url(page_url))


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
- Match the Dino Decks app style: dark #252625 background, #2f302e card panel, #9bd2bc accent bar, #f3f0ea titles, #c1bcb4 body text, square-edged layout, Instrument Serif title style, and Instrument Sans body style.
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
    # Accept a smaller deck when the source runs short of the requested count,
    # but keep the original quality floor of 8 cards for larger requests.
    if len(cards) < min(card_count, 8):
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
        "difficulty": deck.get("difficulty", "balanced"),
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


_SAVE_LOCK = threading.RLock()


def save_deck(deck):
    DECKS_DIR.mkdir(exist_ok=True)
    base_slug = slugify(deck["title"])
    # Serialize slug allocation + write so concurrent requests can't pick the same slug.
    with _SAVE_LOCK:
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


def deck_path_for_slug(slug):
    clean_slug = slugify(slug)
    deck_path = (DECKS_DIR / f"{clean_slug}.md").resolve()
    decks_root = DECKS_DIR.resolve()
    if deck_path.parent != decks_root:
        raise ValueError("Invalid deck slug.")
    return clean_slug, deck_path


def load_deck_by_slug(slug):
    clean_slug, deck_path = deck_path_for_slug(slug)
    if not deck_path.exists():
        raise FileNotFoundError(clean_slug)
    return load_deck(deck_path)


def write_existing_deck(slug, deck):
    clean_slug, deck_path = deck_path_for_slug(slug)
    if not deck_path.exists():
        raise FileNotFoundError(clean_slug)
    with _SAVE_LOCK:
        deck_path.write_text(deck_to_markdown(deck), encoding="utf-8")
    return load_deck(deck_path)


def mutate_deck(slug, mutator):
    # Hold the lock across load + mutate + write so concurrent mutations
    # (the server is threaded) cannot overwrite each other's changes.
    with _SAVE_LOCK:
        deck = load_deck_by_slug(slug)
        mutator(deck)
        return write_existing_deck(slug, deck)


def delete_deck(slug):
    _, deck_path = deck_path_for_slug(slug)
    if not deck_path.exists():
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


def infer_generation_provider(deck):
    generator = str(deck.get("generator", "")).lower()
    if generator.startswith("openai"):
        return "openai"
    if generator.startswith("local"):
        return "local"
    return "codex"


def deck_source_text(deck):
    source_ref = deck.get("source_url", "")
    if not source_ref:
        raise ValueError("This deck does not include an original source to regenerate from.")
    return normalize_doc_text(fetch_source_text(deck.get("source_type", "google_doc"), source_ref))


def generated_deck_from_source(source_ref, source_type, title, card_count, difficulty, provider):
    doc_text = normalize_doc_text(fetch_source_text(source_type, source_ref))
    raw_deck, generator = generate_flashcards(doc_text, title, card_count, difficulty, provider)
    return {
        "title": raw_deck.get("deck_title") or title or "Google Doc Flashcards",
        "summary": raw_deck.get("summary", ""),
        "source_url": source_ref,
        "source_type": source_type,
        "created_at": utc_now(),
        "generator": generator,
        "difficulty": difficulty or "balanced",
        "cards": raw_deck.get("cards", []),
    }


def refresh_deck(slug, provider=None, difficulty=None):
    deck = load_deck_by_slug(slug)
    source_ref = deck.get("source_url", "")
    source_type = deck.get("source_type", "google_doc")
    if not source_ref:
        raise ValueError("This deck does not include an original source to refresh from.")
    count = max(1, len(deck.get("cards", [])))
    next_deck = generated_deck_from_source(
        source_ref,
        source_type,
        deck.get("title", ""),
        count,
        difficulty or deck.get("difficulty", "balanced"),
        provider or infer_generation_provider(deck),
    )
    return write_existing_deck(slug, next_deck)


def generate_more_cards(slug, count=5, provider=None, difficulty=None):
    deck = load_deck_by_slug(slug)
    doc_text = deck_source_text(deck)
    card_count = max(1, int(count or 5))
    raw_deck, generator = generate_flashcards(
        doc_text,
        deck.get("title", ""),
        card_count,
        difficulty or deck.get("difficulty", "balanced"),
        provider or infer_generation_provider(deck),
    )

    # Generation can take minutes; re-apply to a fresh copy under the lock so
    # card edits made in the meantime are preserved.
    def append_generated(fresh_deck):
        fresh_deck["cards"] = fresh_deck.get("cards", []) + raw_deck.get("cards", [])
        if not fresh_deck.get("summary"):
            fresh_deck["summary"] = raw_deck.get("summary", "")
        if fresh_deck.get("generator") and fresh_deck.get("generator") != generator:
            fresh_deck["generator"] = "mixed"
        else:
            fresh_deck["generator"] = generator
        fresh_deck["difficulty"] = difficulty or fresh_deck.get("difficulty", "balanced")

    return mutate_deck(slug, append_generated)


def normalized_card(card):
    if not isinstance(card, dict):
        raise ValueError("Card must be an object.")
    question = str(card.get("question", "")).strip()
    answer = str(card.get("answer", "")).strip()
    if not question or not answer:
        raise ValueError("Cards need both a question and an answer.")
    tags = card.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    clean_card = dict(card)
    clean_card["question"] = question
    clean_card["answer"] = answer
    clean_card["explanation"] = str(clean_card.get("explanation", "")).strip()
    clean_card["source_cue"] = str(clean_card.get("source_cue", "")).strip()
    clean_card["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()]
    return clean_card


def manual_card(question, answer):
    return normalized_card(
        {
            "question": question,
            "answer": answer,
            "explanation": "Manual card.",
            "source_cue": "Manual entry",
            "tags": ["manual"],
        }
    )


def add_manual_card(slug, question, answer):
    card = manual_card(question, answer)

    def append_card(deck):
        deck["cards"] = deck.get("cards", []) + [card]

    return mutate_deck(slug, append_card)


def insert_card(slug, index, card):
    clean_card = normalized_card(card)
    requested_index = parse_card_index(index)

    def insert(deck):
        cards = deck.get("cards", [])
        cards.insert(min(requested_index, len(cards)), clean_card)
        deck["cards"] = cards

    return mutate_deck(slug, insert)


def parse_card_index(value):
    try:
        index = int(value)
    except (TypeError, ValueError):
        raise ValueError("Card index must be a number.")
    if index < 0:
        raise ValueError("Card index must be zero or greater.")
    return index


def card_at(deck, index):
    cards = deck.get("cards", [])
    if index >= len(cards):
        raise IndexError("Card not found.")
    return cards[index]


def update_card_answer(slug, index, answer):
    clean_index = parse_card_index(index)
    answer = str(answer or "").strip()
    if not answer:
        raise ValueError("Answer cannot be empty.")

    def set_answer(deck):
        card_at(deck, clean_index)["answer"] = answer

    return mutate_deck(slug, set_answer)


def delete_card(slug, index):
    clean_index = parse_card_index(index)

    def remove(deck):
        cards = deck.get("cards", [])
        if len(cards) <= 1:
            raise ValueError("A deck must keep at least one card.")
        if clean_index >= len(cards):
            raise IndexError("Card not found.")
        cards.pop(clean_index)
        deck["cards"] = cards

    return mutate_deck(slug, remove)


SLIDE_THEME = {
    "bg": "#252625",
    "surface": "#2f302e",
    "surface_muted": "#383936",
    "text": "#f3f0ea",
    "muted": "#c1bcb4",
    "line": "#494a45",
    "line_strong": "#686a62",
    "accent": "#9bd2bc",
    "contrast": "#dea06b",
}
SLIDE_SANS = "Instrument Sans"
SLIDE_SERIF = "Instrument Serif"


def slide_text(value, limit):
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def slide_rgb(hex_value):
    value = hex_value.lstrip("#")
    return {
        "red": int(value[0:2], 16) / 255,
        "green": int(value[2:4], 16) / 255,
        "blue": int(value[4:6], 16) / 255,
    }


def slide_solid_fill(hex_value):
    return {"solidFill": {"color": {"rgbColor": slide_rgb(hex_value)}}}


def slide_optional_color(hex_value):
    return {"opaqueColor": {"rgbColor": slide_rgb(hex_value)}}


def create_box_request(object_id, slide_id, x, y, width, height, shape_type="RECTANGLE"):
    return {
        "createShape": {
            "objectId": object_id,
            "shapeType": shape_type,
            "elementProperties": {
                "pageObjectId": slide_id,
                "size": {
                    "height": {"magnitude": height, "unit": "PT"},
                    "width": {"magnitude": width, "unit": "PT"},
                },
                "transform": {"scaleX": 1, "scaleY": 1, "translateX": x, "translateY": y, "unit": "PT"},
            },
        }
    }


def style_box_request(object_id, fill, outline=None, weight=1):
    outline = outline or fill
    return {
        "updateShapeProperties": {
            "objectId": object_id,
            "shapeProperties": {
                "shapeBackgroundFill": slide_solid_fill(fill),
                "outline": {
                    "outlineFill": slide_solid_fill(outline),
                    "weight": {"magnitude": weight, "unit": "PT"},
                },
            },
            "fields": "shapeBackgroundFill.solidFill.color,outline.outlineFill.solidFill.color,outline.weight",
        }
    }


def create_text_requests(object_id, slide_id, text, x, y, width, height, font_size, color, font_family, bold=False):
    safe_text = slide_text(text, 2600)
    return [
        create_box_request(object_id, slide_id, x, y, width, height, "TEXT_BOX"),
        {"insertText": {"objectId": object_id, "text": safe_text}},
        {
            "updateTextStyle": {
                "objectId": object_id,
                "style": {
                    "foregroundColor": slide_optional_color(color),
                    "fontFamily": font_family,
                    "fontSize": {"magnitude": font_size, "unit": "PT"},
                    "bold": bold,
                },
                "fields": "foregroundColor,fontFamily,fontSize,bold",
            }
        },
    ]


def create_styled_slide_requests(slide_id, eyebrow, title, body, footer="", progress_index=None, progress_total=None):
    requests = [
        {
            "updatePageProperties": {
                "objectId": slide_id,
                "pageProperties": {"pageBackgroundFill": slide_solid_fill(SLIDE_THEME["bg"])},
                "fields": "pageBackgroundFill.solidFill.color",
            }
        },
        create_box_request(f"{slide_id}_panel", slide_id, 50, 38, 620, 314),
        style_box_request(f"{slide_id}_panel", SLIDE_THEME["surface"], SLIDE_THEME["line"]),
        create_box_request(f"{slide_id}_accent", slide_id, 50, 38, 620, 4),
        style_box_request(f"{slide_id}_accent", SLIDE_THEME["accent"], SLIDE_THEME["accent"]),
    ]
    requests.extend(
        create_text_requests(
            f"{slide_id}_eyebrow",
            slide_id,
            slide_text(eyebrow, 120).upper(),
            74,
            68,
            560,
            24,
            9,
            SLIDE_THEME["muted"],
            SLIDE_SANS,
            True,
        )
    )
    requests.extend(
        create_text_requests(
            f"{slide_id}_title",
            slide_id,
            slide_text(title, 520),
            74,
            101,
            560,
            112,
            29,
            SLIDE_THEME["text"],
            SLIDE_SERIF,
        )
    )
    if body:
        requests.extend(
            create_text_requests(
                f"{slide_id}_body",
                slide_id,
                body,
                76,
                222,
                556,
                86,
                14,
                SLIDE_THEME["muted"],
                SLIDE_SANS,
            )
        )
    if footer:
        requests.extend(
            create_text_requests(
                f"{slide_id}_footer",
                slide_id,
                footer,
                76,
                317,
                350,
                22,
                8,
                SLIDE_THEME["muted"],
                SLIDE_SANS,
                True,
            )
        )
    if progress_index and progress_total:
        track_width = 196
        fill_width = max(6, min(track_width, int(track_width * progress_index / progress_total)))
        requests.extend(
            [
                create_box_request(f"{slide_id}_progress_track", slide_id, 438, 326, track_width, 3),
                style_box_request(f"{slide_id}_progress_track", SLIDE_THEME["line"], SLIDE_THEME["line"]),
                create_box_request(f"{slide_id}_progress_fill", slide_id, 438, 326, fill_width, 3),
                style_box_request(f"{slide_id}_progress_fill", SLIDE_THEME["accent"], SLIDE_THEME["accent"]),
            ]
        )
    return requests


def card_answer_body(card):
    parts = [("Answer", card.get("answer", "")), ("Explanation", card.get("explanation", "")), ("Source cue", card.get("source_cue", ""))]
    return "\n\n".join(f"{label}:\n{slide_text(value, 900)}" for label, value in parts if str(value or "").strip())


def create_google_slides_requests(deck, initial_slide_id=None):
    cards = deck.get("cards", [])
    requests = []
    if initial_slide_id:
        requests.append({"deleteObject": {"objectId": initial_slide_id}})
    overview_id = "overview"
    requests.append({"createSlide": {"objectId": overview_id, "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
    overview_body = "\n\n".join(
        part
        for part in [
            slide_text(deck.get("summary", ""), 700),
            f"{len(cards)} cards. Question and answer slides alternate for review.",
        ]
        if part.strip()
    )
    requests.extend(
        create_styled_slide_requests(
            overview_id,
            "Dino Decks",
            deck.get("title", "Flashcard Deck"),
            overview_body,
            "Google Slides review deck",
        )
    )
    total = max(1, len(cards))
    for idx, card in enumerate(cards, start=1):
        q_slide = f"card_{idx}_question"
        a_slide = f"card_{idx}_answer"
        requests.append({"createSlide": {"objectId": q_slide, "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
        requests.extend(
            create_styled_slide_requests(
                q_slide,
                f"Card {idx} of {total} / Question",
                card.get("question", ""),
                "",
                "Answer on next slide",
                idx,
                total,
            )
        )
        requests.append({"createSlide": {"objectId": a_slide, "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
        requests.extend(
            create_styled_slide_requests(
                a_slide,
                f"Card {idx} of {total} / Answer",
                "Answer",
                card_answer_body(card),
                slide_text(card.get("question", ""), 110),
                idx,
                total,
            )
        )
    return requests


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
    if provider in ("apps-script", "manual"):
        return {
            "mode": "manual",
            "provider": "google-apps-script",
            "message": "Paste the Apps Script below into script.google.com and run createFlashcardDeck().",
            "apps_script": deck_to_apps_script(deck),
        }

    token = google_oauth_token()
    if not token:
        if provider == "direct":
            raise ValueError(
                "Direct Google Slides export requires a Google OAuth token with Slides and Drive scopes. "
                f"{GOOGLE_AUTH_PROVIDER_HELP}"
            )
        return {
            "mode": "manual",
            "provider": "google-apps-script",
            "message": (
                "Configure Google auth for direct export, or paste the Apps Script below into script.google.com. "
                f"{GOOGLE_AUTH_PROVIDER_HELP}"
            ),
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
    initial_slide_id = None
    if presentation.get("slides"):
        initial_slide_id = presentation["slides"][0].get("objectId")
    requests = create_google_slides_requests(deck, initial_slide_id)

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
        "message": "Google Slides deck created.",
    }


def js_string(value):
    return json.dumps(str(value))


def deck_to_apps_script(deck):
    cards = deck.get("cards", [])
    card_data = json.dumps(cards, ensure_ascii=False, indent=2)
    title = js_string(deck.get("title", "Flashcard Deck"))
    summary = js_string(deck.get("summary", ""))
    return textwrap.dedent(
        f"""
        function createFlashcardDeck() {{
          const deckTitle = {title};
          const deckSummary = {summary};
          const cards = {card_data};
          const theme = {{
            bg: '#252625',
            surface: '#2f302e',
            mutedSurface: '#383936',
            text: '#f3f0ea',
            muted: '#c1bcb4',
            line: '#494a45',
            accent: '#9bd2bc'
          }};
          const presentation = SlidesApp.create(deckTitle);
          const first = presentation.getSlides()[0];
          buildSlide(first, theme, 'Dino Decks', deckTitle, overviewBody(deckSummary, cards.length), 'Google Slides review deck');

          cards.forEach((card, index) => {{
            const q = presentation.appendSlide(SlidesApp.PredefinedLayout.BLANK);
            buildSlide(
              q,
              theme,
              `Card ${{index + 1}} of ${{cards.length}} / Question`,
              card.question || '',
              '',
              'Answer on next slide',
              index + 1,
              cards.length
            );
            const a = presentation.appendSlide(SlidesApp.PredefinedLayout.BLANK);
            buildSlide(
              a,
              theme,
              `Card ${{index + 1}} of ${{cards.length}} / Answer`,
              'Answer',
              answerBody(card),
              truncate(card.question || '', 110),
              index + 1,
              cards.length
            );
          }});
          Logger.log(presentation.getUrl());
          return presentation.getUrl();
        }}

        function buildSlide(slide, theme, eyebrow, title, body, footer, progressIndex, progressTotal) {{
          resetSlide(slide, theme);
          addPanel(slide, theme);
          addText(slide, String(eyebrow || '').toUpperCase(), 74, 68, 560, 24, 9, theme.muted, 'Instrument Sans', true);
          addText(slide, truncate(title || '', 520), 74, 101, 560, 112, 29, theme.text, 'Instrument Serif', false);
          if (body) {{
            addText(slide, truncate(body, 2600), 76, 222, 556, 86, 14, theme.muted, 'Instrument Sans', false);
          }}
          if (footer) {{
            addText(slide, footer, 76, 317, 350, 22, 8, theme.muted, 'Instrument Sans', true);
          }}
          if (progressIndex && progressTotal) {{
            addProgress(slide, theme, progressIndex, progressTotal);
          }}
        }}

        function resetSlide(slide, theme) {{
          slide.getShapes().forEach(shape => shape.remove());
          slide.getBackground().setSolidFill(theme.bg);
        }}

        function addPanel(slide, theme) {{
          const panel = slide.insertShape(SlidesApp.ShapeType.RECTANGLE, 50, 38, 620, 314);
          panel.getFill().setSolidFill(theme.surface);
          panel.getBorder().getLineFill().setSolidFill(theme.line);
          panel.getBorder().setWeight(1);
          const accent = slide.insertShape(SlidesApp.ShapeType.RECTANGLE, 50, 38, 620, 4);
          accent.getFill().setSolidFill(theme.accent);
          accent.getBorder().getLineFill().setSolidFill(theme.accent);
          accent.getBorder().setWeight(1);
        }}

        function addText(slide, value, x, y, width, height, fontSize, color, fontFamily, bold) {{
          const box = slide.insertTextBox(value || '', x, y, width, height);
          const style = box.getText().getTextStyle();
          style.setForegroundColor(color);
          style.setFontFamily(fontFamily);
          style.setFontSize(fontSize);
          style.setBold(Boolean(bold));
          return box;
        }}

        function addProgress(slide, theme, progressIndex, progressTotal) {{
          const width = 196;
          const fillWidth = Math.max(6, Math.min(width, Math.floor(width * progressIndex / progressTotal)));
          const track = slide.insertShape(SlidesApp.ShapeType.RECTANGLE, 438, 326, width, 3);
          track.getFill().setSolidFill(theme.line);
          track.getBorder().getLineFill().setSolidFill(theme.line);
          track.getBorder().setWeight(1);
          const fill = slide.insertShape(SlidesApp.ShapeType.RECTANGLE, 438, 326, fillWidth, 3);
          fill.getFill().setSolidFill(theme.accent);
          fill.getBorder().getLineFill().setSolidFill(theme.accent);
          fill.getBorder().setWeight(1);
        }}

        function overviewBody(summary, count) {{
          return [summary, `${{count}} cards. Question and answer slides alternate for review.`].filter(Boolean).join('\\n\\n');
        }}

        function answerBody(card) {{
          return [
            sectionText('Answer', card.answer),
            sectionText('Explanation', card.explanation),
            sectionText('Source cue', card.source_cue)
          ].filter(Boolean).join('\\n\\n');
        }}

        function sectionText(label, value) {{
          return value ? `${{label}}:\\n${{truncate(value, 900)}}` : '';
        }}

        function truncate(value, limit) {{
          const text = String(value || '').trim();
          return text.length > limit ? text.slice(0, limit - 3).trimEnd() + '...' : text;
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
        return auth_token_valid(self.cookie_value(AUTH_COOKIE))

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
            cookie[AUTH_COOKIE] = issue_auth_token()
            morsel = cookie[AUTH_COOKIE]
            morsel["path"] = "/"
            morsel["httponly"] = True
            morsel["samesite"] = "Lax"
            morsel["max-age"] = AUTH_TTL_SECONDS
            if COOKIE_SECURE:
                morsel["secure"] = True
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
        if COOKIE_SECURE:
            morsel["secure"] = True
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
                slug = slugify(path.rsplit("/", 1)[-1])
                deck_path = (DECKS_DIR / f"{slug}.md").resolve()
                if not str(deck_path).startswith(str(DECKS_DIR.resolve())) or not deck_path.exists():
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
        except (ValueError, RuntimeError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception:
            return self.send_json({"error": "Internal server error."}, 500)

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
                count = clamp_card_count(payload.get("card_count", 12))
                deck = generated_deck_from_source(
                    source_ref,
                    source_type,
                    payload.get("title", ""),
                    count,
                    payload.get("difficulty", "balanced"),
                    payload.get("provider", "codex"),
                )
                slug, path = save_deck(deck)
                deck["slug"] = slug
                deck["markdown_path"] = str(path)
                deck["markdown_url"] = f"/decks/{slug}.md"
                return self.send_json({"deck": deck})
            restore_match = re.fullmatch(r"/api/decks/([^/]+)/cards/restore", parsed.path)
            if restore_match:
                slug = urllib.parse.unquote(restore_match.group(1))
                deck = insert_card(slug, payload.get("index", 0), payload.get("card", {}))
                return self.send_json({"deck": deck}, 201)
            deck_action = re.fullmatch(r"/api/decks/([^/]+)/(refresh|more|cards)", parsed.path)
            if deck_action:
                slug = urllib.parse.unquote(deck_action.group(1))
                action = deck_action.group(2)
                if action == "refresh":
                    deck = refresh_deck(slug, payload.get("provider"), payload.get("difficulty"))
                    return self.send_json({"deck": deck})
                if action == "more":
                    count = int(payload.get("count", 5) or 5)
                    deck = generate_more_cards(slug, count, payload.get("provider"), payload.get("difficulty"))
                    return self.send_json({"deck": deck})
                deck = add_manual_card(slug, payload.get("question", ""), payload.get("answer", ""))
                return self.send_json({"deck": deck}, 201)
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
        except FileNotFoundError:
            return self.send_json({"error": "Deck not found."}, 404)
        except IndexError as exc:
            return self.send_json({"error": str(exc)}, 404)
        except (ValueError, RuntimeError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception:
            return self.send_json({"error": "Internal server error."}, 500)

    def do_PATCH(self):
        parsed = urllib.parse.urlparse(self.path)
        if self.guard(parsed.path):
            return
        try:
            payload = self.read_json()
            card_match = re.fullmatch(r"/api/decks/([^/]+)/cards/(\d+)", parsed.path)
            if card_match:
                slug = urllib.parse.unquote(card_match.group(1))
                deck = update_card_answer(slug, card_match.group(2), payload.get("answer", ""))
                return self.send_json({"deck": deck})
            return self.send_json({"error": "Not found."}, 404)
        except FileNotFoundError:
            return self.send_json({"error": "Deck not found."}, 404)
        except IndexError as exc:
            return self.send_json({"error": str(exc)}, 404)
        except (ValueError, RuntimeError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception:
            return self.send_json({"error": "Internal server error."}, 500)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        if self.guard(parsed.path):
            return
        try:
            card_match = re.fullmatch(r"/api/decks/([^/]+)/cards/(\d+)", parsed.path)
            if card_match:
                slug = urllib.parse.unquote(card_match.group(1))
                deck = delete_card(slug, card_match.group(2))
                return self.send_json({"deck": deck})
            if parsed.path.startswith("/api/decks/"):
                slug = parsed.path.rsplit("/", 1)[-1]
                if delete_deck(slug):
                    return self.send_json({"deleted": slug})
                return self.send_json({"error": "Deck not found."}, 404)
            return self.send_json({"error": "Not found."}, 404)
        except FileNotFoundError:
            return self.send_json({"error": "Deck not found."}, 404)
        except IndexError as exc:
            return self.send_json({"error": str(exc)}, 404)
        except (ValueError, RuntimeError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception:
            return self.send_json({"error": "Internal server error."}, 500)


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
