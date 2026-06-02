# Google Doc Flashcards

Local app for turning a Google Doc or webpage into 10-20 flashcards, saving the deck as markdown, reviewing cards in an Obsidian-style UI, and exporting to Google Slides.

## Run

The app uses the Python standard library at runtime. Python 3.11 or newer is recommended.

```bash
python3 app.py
```

Open `http://127.0.0.1:8765`.

To test from a phone on your private network, bind the local server to all interfaces:

```bash
HOST=0.0.0.0 python3 app.py
```

Then open `http://<your-computer-ip>:8765` from the phone. This is intended for trusted private networks only. Do not expose this server directly to the public internet without adding authentication, HTTPS, and normal production hardening. For personal mobile access, use a private network tool such as Tailscale instead of port forwarding.

The web UI includes PWA metadata and a service worker, so mobile browsers can install it as a standalone app. The app shell and static assets are cached, and every deck is pre-cached locally on load, so decks generated on the desktop can be reviewed on mobile fully offline once the phone has synced once. Generating, exporting, and deleting decks still require the live Python backend.

## Examples

Neutral sample material is available in `examples/`:

- `examples/sample-source.md`: source text for a demo deck.
- `examples/sample-deck.md`: markdown deck with the hidden JSON block used by the app.
- `examples/screenshots/`: desktop and mobile screenshots.

## Review on mobile (desktop stays the source of truth)

Decks live as markdown files on whichever machine runs `app.py`. To review desktop-generated decks on a phone, give the phone a network path to the desktop server and protect it with a passcode.

1. **Set a passcode.** In your `.env` or shell: `APP_PASSCODE=your-strong-passphrase`. When set, every request requires signing in once per device (a signed, HttpOnly cookie). When unset, the app stays open and localhost-only as before.
2. **Bind to the network:** `HOST=0.0.0.0 APP_PASSCODE=... python3 app.py`. On startup the server prints the reachable URL.
3. **Reach it from anywhere with [Tailscale](https://tailscale.com).** Install Tailscale on both the desktop and the phone (same account), then open `http://<desktop-tailscale-name>:8765` on the phone. Traffic stays on an encrypted private mesh with nothing exposed to the public internet. On the same Wi-Fi you can instead use the `http://<desktop-ip>:8765` URL the server prints.
4. **Sign in once on the phone** with the passcode. Install to the home screen via the browser's "Add to Home Screen" for an app-like experience. After the first sync, all decks are cached for offline review; the desktop only needs to be awake to generate, export, or fetch new decks.

To sign out a device, visit `/logout`. This passcode gate is meant to sit behind a private network like Tailscale, not to harden a server exposed directly to the public internet (which would also need HTTPS and additional hardening).

## CLI

The CLI runs the same deck workflow without the local web API server.

Generate from a Google Doc:

```bash
python3 flashcards_cli.py generate --doc "https://docs.google.com/document/d/.../edit" --cards 12
```

Generate from a webpage:

```bash
python3 flashcards_cli.py generate --webpage "https://example.com/article" --cards 12
```

Webpage import is limited to public internet hosts. For private, local, or intranet pages, save the text locally and use `--file`.

Generate from the included example Google Doc without the OpenAI API:

```bash
python3 flashcards_cli.py generate \
  --doc "https://docs.google.com/document/d/15pB6lVQD-meMmq5q6s3WFQCUy75_EIey3NBuJ4tnj9o/edit?usp=sharing" \
  --no-llm \
  --title "Nvidia Products" \
  --cards 12
```

Generation defaults to Codex CLI. Use `--openai-api` to use the app OpenAI API instead:

```bash
python3 flashcards_cli.py generate \
  --doc "https://docs.google.com/document/d/15pB6lVQD-meMmq5q6s3WFQCUy75_EIey3NBuJ4tnj9o/edit?usp=sharing" \
  --openai-api \
  --title "Nvidia Products" \
  --cards 12
```

Generate without the OpenAI API from a local text or markdown file:

```bash
python3 flashcards_cli.py generate --file notes.md --no-llm --title "Review Notes"
```

Review and export from the terminal:

```bash
python3 flashcards_cli.py list
python3 flashcards_cli.py review review-notes
python3 flashcards_cli.py export-slides review-notes --script-out review-notes-slides.gs
python3 flashcards_cli.py export-slides review-notes --codex-cli --script-out review-notes-slides.gs
python3 flashcards_cli.py export-slides review-notes --gemini-cli --script-out review-notes-slides.gs
python3 flashcards_cli.py delete review-notes --yes
```

## Configure

Copy `.env.example` values into your shell or `.env` workflow.

- `CODEX_CLI_BIN` optionally points the default Codex CLI provider at a specific `codex` binary.
- `CODEX_CLI_TIMEOUT` optionally changes the Codex CLI generation timeout, default `180` seconds.
- `OPENAI_API_KEY` enables the optional OpenAI API generator.
- `OPENAI_MODEL` defaults to `gpt-4o-mini`.
- `GOOGLE_OAUTH_ACCESS_TOKEN` enables private Google Doc import and direct Google Slides export.
- `GEMINI_CLI_BIN` optionally points the Gemini CLI export provider at a specific `gemini` binary.
- `GEMINI_CLI_TIMEOUT` optionally changes the Gemini CLI export timeout, default `180` seconds.

Public Google Docs shared with anyone who has the link can be imported without a Google token. Without an OpenAI key, the app creates a local fallback deck for testing.
Webpage import reads public `http://` or `https://` pages and extracts readable text from the HTML. Pages that require sign-in or block server-side fetches will not import unless you save their text locally and use `--file`.
For Google Slides export, the web app provider selector and CLI flags can use the built-in Google API/Apps Script path, Codex CLI Apps Script generation, or Gemini CLI Apps Script generation.

## Development

Install test dependencies and run the suite:

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
```

## Output

Decks are saved as markdown files in `decks/`. Each file includes a hidden JSON block for the app plus readable markdown cards.
