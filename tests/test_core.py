import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import app
import flashcards_cli


class FlashcardCoreTest(unittest.TestCase):
    def test_doc_id_and_card_count_bounds(self):
        doc_url = "https://docs.google.com/document/d/abcDEF_123456789012345/edit"
        self.assertEqual(app.extract_doc_id(doc_url), "abcDEF_123456789012345")
        self.assertEqual(app.clamp_card_count(3), 10)
        self.assertEqual(app.clamp_card_count(12), 12)
        self.assertEqual(app.clamp_card_count(99), 20)

    def test_pwa_static_files_and_content_types(self):
        manifest = app.STATIC_DIR / "manifest.webmanifest"
        service_worker = app.STATIC_DIR / "service-worker.js"
        self.assertTrue(manifest.exists())
        self.assertTrue(service_worker.exists())
        self.assertEqual(app.content_type_for_path(manifest), "application/manifest+json; charset=utf-8")
        self.assertEqual(app.content_type_for_path(service_worker), "application/javascript; charset=utf-8")

    def test_web_ui_exposes_google_slides_export_action(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        js = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="exportSlidesButton"', html)
        self.assertIn('id="slidesExportDialog"', html)
        self.assertIn("/api/export/slides", js)
        self.assertIn('provider: "direct"', js)

    def test_google_doc_fetch_uses_drive_token_or_public_export(self):
        seen = []

        def fake_fetch_url(url, headers=None, timeout=30):
            seen.append((url, headers or {}))
            return "Document body " * 20

        doc_url = "https://docs.google.com/document/d/abcDEF_123456789012345/edit"
        with mock.patch("app.fetch_url", fake_fetch_url):
            with mock.patch.dict(app.os.environ, {"GOOGLE_OAUTH_ACCESS_TOKEN": "token"}, clear=True):
                self.assertIn("Document body", app.fetch_google_doc_text(doc_url))
        self.assertIn("drive/v3/files/abcDEF_123456789012345/export", seen[0][0])
        self.assertEqual(seen[0][1]["Authorization"], "Bearer token")

        seen.clear()
        with mock.patch("app.fetch_url", fake_fetch_url):
            with mock.patch.dict(app.os.environ, {}, clear=True):
                app.fetch_google_doc_text(doc_url)
        self.assertIn("docs.google.com/document/d/abcDEF_123456789012345/export?format=txt", seen[0][0])

    def test_google_auth_provider_uses_gcloud_access_token(self):
        captured = []

        def fake_run(command, capture_output, text, timeout, check):
            captured.extend(command)
            return app.subprocess.CompletedProcess(command, 0, stdout="gcloud-token\n", stderr="")

        with mock.patch.dict(app.os.environ, {"GOOGLE_AUTH_PROVIDER": "gcloud"}, clear=True):
            with mock.patch("app.subprocess.run", fake_run):
                self.assertEqual(app.google_oauth_token(), "gcloud-token")

        self.assertEqual(captured[:3], ["gcloud", "auth", "application-default"])
        self.assertIn("print-access-token", captured)
        self.assertIn(f"--scopes={app.google_auth_scopes_arg()}", captured)

    def test_google_auth_login_command_includes_scopes_and_flags(self):
        command = app.google_auth_login_command("client.json", no_launch_browser=True)
        self.assertEqual(command[:4], ["gcloud", "auth", "application-default", "login"])
        self.assertIn(f"--scopes={app.google_auth_scopes_arg()}", command)
        self.assertIn("--client-id-file=client.json", command)
        self.assertIn("--no-launch-browser", command)

    def test_webpage_fetch_extracts_readable_text(self):
        html = """
        <!doctype html>
        <html>
          <head>
            <title>Ignored title chrome</title>
            <style>.hidden { display: none; }</style>
            <script>window.secret = "skip me";</script>
          </head>
          <body>
            <nav>Navigation should stay low value but readable.</nav>
            <main>
              <h1>Readable Article Title</h1>
              <p>This article explains an important source concept with enough concrete detail to support useful flashcard generation.</p>
              <p>It includes a second paragraph with implications, examples, and review-worthy facts that should survive HTML cleanup.</p>
              <p>A third paragraph gives the parser enough source text to pass normalization without relying on browser-only DOM APIs.</p>
            </main>
          </body>
        </html>
        """

        with mock.patch("app.fetch_public_url", return_value=html):
            text = app.normalize_doc_text(app.fetch_webpage_text("https://example.com/article"))

        self.assertIn("Readable Article Title", text)
        self.assertIn("important source concept", text)
        self.assertNotIn("window.secret", text)
        self.assertNotIn("display: none", text)

    def test_webpage_import_rejects_private_or_local_hosts(self):
        blocked_urls = [
            "http://127.0.0.1:8765",
            "http://localhost:8765",
            "http://10.0.0.4/article",
            "http://192.168.1.2/article",
            "http://[::1]/article",
        ]
        for url in blocked_urls:
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "public internet hosts|webpage URL"):
                    app.validate_public_webpage_url(url)

    def test_webpage_import_allows_mocked_public_dns(self):
        fake_info = [(app.socket.AF_INET, app.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with mock.patch("socket.getaddrinfo", return_value=fake_info):
            self.assertEqual(app.validate_public_webpage_url("https://example.com/article"), "https://example.com/article")

    def test_fetch_public_url_revalidates_redirect_targets(self):
        # A public page that 302s to a private/metadata host must be rejected on the
        # redirect hop, not blindly followed (SSRF via redirect + DNS rebinding).
        public_dns = [(app.socket.AF_INET, app.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        def fake_exchange(parsed, address, timeout, max_bytes):
            return 302, {"Location": "http://169.254.169.254/latest/meta-data/"}, ""

        with mock.patch("socket.getaddrinfo", return_value=public_dns):
            with mock.patch("app._http_exchange", side_effect=fake_exchange):
                with self.assertRaisesRegex(ValueError, "public internet hosts"):
                    app.fetch_public_url("https://example.com/start")

    def test_fetch_public_url_caps_response_size(self):
        # Drive the cap through _http_exchange directly with a stub response object.
        class _StubResponse:
            status = 200
            reason = "OK"

            def __init__(self):
                self.headers = app.http.client.HTTPMessage()

            def read(self, amount=None):
                return b"x" * (amount or 0)

        class _StubConn:
            def __init__(self, *_a, **_k):
                self.sock = None

            def request(self, *_a, **_k):
                pass

            def getresponse(self):
                return _StubResponse()

            def close(self):
                pass

        parsed = app.urllib.parse.urlparse("https://example.com/big")
        with mock.patch("socket.create_connection", return_value=mock.MagicMock()):
            with mock.patch("ssl.create_default_context"):
                with mock.patch("app.http.client.HTTPConnection", _StubConn):
                    with self.assertRaisesRegex(ValueError, "too large"):
                        app._http_exchange(parsed, (app.socket.AF_INET, "93.184.216.34"), 5, max_bytes=16)

    def test_auth_token_round_trip_revocation_and_expiry(self):
        with mock.patch.object(app, "APP_PASSCODE", "open-sesame"), \
             mock.patch.object(app, "APP_AUTH_SECRET", ""), \
             mock.patch.object(app, "AUTH_TTL_SECONDS", 3600):
            token = app.issue_auth_token()
            self.assertTrue(app.auth_token_valid(token))
            # Tampered or malformed tokens are rejected.
            self.assertFalse(app.auth_token_valid(token + "x"))
            self.assertFalse(app.auth_token_valid("not-a-token"))
            self.assertFalse(app.auth_token_valid(""))
            # Expired tokens (issued long ago) are rejected.
            self.assertFalse(app.auth_token_valid(app.issue_auth_token(issued_at=0)))
            # Future-dated tokens are rejected.
            future = int(app.dt.datetime.now(app.dt.timezone.utc).timestamp()) + 10_000
            self.assertFalse(app.auth_token_valid(app.issue_auth_token(issued_at=future)))
            # Rotating the secret revokes every previously issued token.
            with mock.patch.object(app, "APP_AUTH_SECRET", "rotated"):
                self.assertFalse(app.auth_token_valid(token))

    def test_sample_deck_round_trips(self):
        deck = app.load_deck(app.ROOT / "examples" / "sample-deck.md")
        self.assertEqual(deck["title"], "Habit Systems for Focused Study")
        self.assertEqual(deck["source_type"], "file")
        self.assertEqual(len(deck["cards"]), 10)

    def test_fetch_source_text_routes_webpages(self):
        with mock.patch("app.fetch_webpage_text", return_value="Readable webpage text"):
            self.assertEqual(app.fetch_source_text("webpage", "https://example.com/article"), "Readable webpage text")

        with mock.patch("app.fetch_google_doc_text", return_value="Readable doc text"):
            self.assertEqual(app.fetch_source_text("google_doc", "doc-id"), "Readable doc text")

        with self.assertRaisesRegex(ValueError, "Unknown source type"):
            app.fetch_source_text("rss", "https://example.com/feed.xml")

    def test_llm_deck_must_match_requested_card_count(self):
        valid_cards = [
            {
                "question": f"Question {idx}?",
                "answer": f"Answer {idx}",
                "explanation": "Because it follows from the source.",
                "source_cue": "source cue",
                "tags": ["core"],
            }
            for idx in range(10)
        ]
        deck = app.validate_llm_deck(
            {"deck_title": "Validated", "summary": "Summary", "cards": valid_cards},
            10,
        )
        self.assertEqual(len(deck["cards"]), 10)

        with self.assertRaisesRegex(ValueError, "expected 10"):
            app.validate_llm_deck(
                {"deck_title": "Short", "summary": "Summary", "cards": valid_cards[:9]},
                10,
            )

    def test_markdown_round_trip_is_obsidian_readable(self):
        deck = {
            "title": "Obsidian Deck",
            "summary": "A concise review deck.",
            "source_url": "https://docs.google.com/document/d/example/edit",
            "created_at": "2026-06-01T00:00:00+00:00",
            "generator": "unit-test",
            "cards": [
                {
                    "question": "What does the source claim?",
                    "answer": "It claims a specific point.",
                    "explanation": "The explanation grounds the answer.",
                    "source_cue": "specific point",
                    "tags": ["source", "review"],
                }
            ]
            * 10,
        }
        markdown = app.deck_to_markdown(deck)
        self.assertIn("type: flashcard-deck", markdown)
        self.assertIn("> [!question]", markdown)
        self.assertIn("> [!success] Answer", markdown)
        self.assertIn("<!-- flashcard-deck-json", markdown)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "obsidian-deck.md"
            path.write_text(markdown, encoding="utf-8")
            loaded = app.load_deck(path)
        self.assertEqual(loaded["title"], "Obsidian Deck")
        self.assertEqual(len(loaded["cards"]), 10)

        deck_with_runtime_fields = {
            **loaded,
            "markdown": markdown,
            "markdown_path": "/tmp/obsidian-deck.md",
            "markdown_url": "/decks/obsidian-deck.md",
        }
        rewritten = app.deck_to_markdown(deck_with_runtime_fields)
        self.assertNotIn('"markdown"', rewritten)
        self.assertNotIn('"markdown_path"', rewritten)
        with tempfile.TemporaryDirectory() as tmpdir:
            rewrite_path = Path(tmpdir) / "rewritten.md"
            rewrite_path.write_text(rewritten, encoding="utf-8")
            self.assertEqual(app.load_deck(rewrite_path).get("title"), "Obsidian Deck")

    def test_openai_generation_uses_structured_outputs(self):
        captured_payloads = []
        cards = [
            {
                "question": f"Question {idx}?",
                "answer": f"Answer {idx}",
                "explanation": "Explanation",
                "source_cue": "Cue",
                "tags": ["llm"],
            }
            for idx in range(1, 11)
        ]
        response_payload = {
            "output_text": json.dumps(
                {
                    "deck_title": "LLM Deck",
                    "summary": "Structured summary",
                    "cards": cards,
                }
            )
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(response_payload).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            captured_payloads.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse()

        with mock.patch.dict(app.os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True):
            with mock.patch("urllib.request.urlopen", fake_urlopen):
                deck, generator = app.generate_with_openai("Source text. " * 80, "Requested", 10, "balanced")

        payload = captured_payloads[0]
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(deck["deck_title"], "LLM Deck")
        self.assertEqual(len(deck["cards"]), 10)
        self.assertEqual(generator, "openai:gpt-4o-mini")

    def test_codex_cli_generation_uses_output_schema(self):
        cards = [
            {
                "question": f"Codex question {idx}?",
                "answer": f"Codex answer {idx}",
                "explanation": "Codex explanation",
                "source_cue": "Codex cue",
                "tags": ["codex"],
            }
            for idx in range(1, 11)
        ]
        response_payload = {
            "deck_title": "Codex Deck",
            "summary": "Generated through Codex CLI",
            "cards": cards,
        }
        captured_command = []

        def fake_run(command, capture_output, text, timeout, check):
            captured_command.extend(command)
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(response_payload), encoding="utf-8")
            return app.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch("subprocess.run", fake_run):
            deck, generator = app.generate_with_codex_cli("Source text. " * 80, "Requested", 10, "balanced")

        self.assertEqual(captured_command[:2], ["codex", "exec"])
        self.assertIn("--output-schema", captured_command)
        self.assertIn("--output-last-message", captured_command)
        self.assertEqual(deck["deck_title"], "Codex Deck")
        self.assertEqual(len(deck["cards"]), 10)
        self.assertEqual(generator, "codex-cli")

    def test_generate_flashcards_defaults_to_codex_cli(self):
        cards = [
            {
                "question": f"Default question {idx}?",
                "answer": f"Default answer {idx}",
                "explanation": "Default explanation",
                "source_cue": "Default cue",
                "tags": ["codex"],
            }
            for idx in range(1, 11)
        ]
        expected_deck = {
            "deck_title": "Default Codex Deck",
            "summary": "Generated by the default provider",
            "cards": cards,
        }

        with mock.patch("app.generate_with_codex_cli", return_value=(expected_deck, "codex-cli")) as generate:
            deck, generator = app.generate_flashcards("Source text. " * 80, "Requested", 10, "balanced")

        generate.assert_called_once()
        self.assertEqual(deck["deck_title"], "Default Codex Deck")
        self.assertEqual(generator, "codex-cli")

    def test_slides_export_fallback_and_direct_payload(self):
        deck = {
            "title": "Slides Deck",
            "summary": "Summary",
            "cards": [
                {
                    "question": f"Question {idx}?",
                    "answer": f"Answer {idx}",
                    "explanation": "Explanation",
                    "source_cue": "Cue",
                    "tags": [],
                }
                for idx in range(1, 11)
            ],
        }

        with mock.patch.dict(app.os.environ, {}, clear=True):
            fallback = app.create_google_slides(deck)
        self.assertEqual(fallback["mode"], "manual")
        self.assertIn("SlidesApp.create", fallback["apps_script"])
        self.assertIn("SlidesApp.ShapeType.RECTANGLE", fallback["apps_script"])
        self.assertIn("Instrument Serif", fallback["apps_script"])
        self.assertIn("setSolidFill(theme.bg)", fallback["apps_script"])

        with mock.patch.dict(app.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "Direct Google Slides export requires"):
                app.create_google_slides(deck, provider="direct")

        calls = []

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            calls.append((request.full_url, json.loads(request.data.decode("utf-8")) if request.data else None))
            if request.full_url.endswith("/presentations"):
                return FakeResponse(
                    {
                        "presentationId": "presentation123",
                        "slides": [{"objectId": "default_slide"}],
                    }
                )
            return FakeResponse({"replies": []})

        with mock.patch.dict(app.os.environ, {"GOOGLE_OAUTH_ACCESS_TOKEN": "token"}, clear=True):
            with mock.patch("urllib.request.urlopen", fake_urlopen):
                direct = app.create_google_slides(deck, provider="direct")

        self.assertEqual(direct["mode"], "direct")
        self.assertIn("presentation123", direct["url"])
        batch_requests = calls[1][1]["requests"]
        self.assertEqual(batch_requests[0], {"deleteObject": {"objectId": "default_slide"}})
        created_slides = [item for item in batch_requests if "createSlide" in item]
        self.assertEqual(len(created_slides), 21)
        inserted_text = [item.get("insertText", {}).get("text", "") for item in batch_requests if "insertText" in item]
        self.assertIn("Question 1?", inserted_text)
        self.assertIn("Question 2?", inserted_text)
        self.assertTrue(
            any(
                item.get("updatePageProperties", {})
                .get("pageProperties", {})
                .get("pageBackgroundFill", {})
                .get("solidFill", {})
                .get("color", {})
                .get("rgbColor")
                == app.slide_rgb(app.SLIDE_THEME["bg"])
                for item in batch_requests
            )
        )
        self.assertTrue(
            any(
                item.get("updateShapeProperties", {})
                .get("shapeProperties", {})
                .get("shapeBackgroundFill", {})
                .get("solidFill", {})
                .get("color", {})
                .get("rgbColor")
                == app.slide_rgb(app.SLIDE_THEME["surface"])
                for item in batch_requests
            )
        )
        self.assertTrue(
            any(
                item.get("updateTextStyle", {}).get("style", {}).get("fontFamily") == app.SLIDE_SERIF
                for item in batch_requests
            )
        )

    def test_codex_cli_slides_export_generates_apps_script(self):
        deck = {
            "title": "Codex Slides",
            "summary": "Summary",
            "cards": [
                {
                    "question": f"Question {idx}?",
                    "answer": f"Answer {idx}",
                    "explanation": "Explanation",
                    "source_cue": "Cue",
                    "tags": [],
                }
                for idx in range(1, 11)
            ],
        }
        response_payload = {
            "apps_script": "function createFlashcardDeck() { SlidesApp.create('Codex Slides'); }",
            "notes": "Creates a deck.",
        }
        captured_command = []

        def fake_run(command, capture_output, text, timeout, check):
            captured_command.extend(command)
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(response_payload), encoding="utf-8")
            return app.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch("subprocess.run", fake_run):
            result = app.create_google_slides(deck, provider="codex")

        self.assertEqual(captured_command[:2], ["codex", "exec"])
        self.assertIn("--output-schema", captured_command)
        self.assertEqual(result["mode"], "manual")
        self.assertEqual(result["provider"], "codex-cli")
        self.assertIn("SlidesApp.create", result["apps_script"])

    def test_gemini_cli_slides_export_generates_apps_script(self):
        deck = {
            "title": "Gemini Slides",
            "summary": "Summary",
            "cards": [
                {
                    "question": f"Question {idx}?",
                    "answer": f"Answer {idx}",
                    "explanation": "Explanation",
                    "source_cue": "Cue",
                    "tags": [],
                }
                for idx in range(1, 11)
            ],
        }
        response_text = json.dumps(
            {
                "apps_script": "function createFlashcardDeck() { SlidesApp.create('Gemini Slides'); }",
                "notes": "Creates a deck.",
            }
        )
        captured_command = []

        def fake_run(command, capture_output, text, timeout, check):
            captured_command.extend(command)
            stdout = json.dumps({"response": response_text})
            return app.subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with mock.patch("subprocess.run", fake_run):
            result = app.create_google_slides(deck, provider="gemini")

        self.assertEqual(captured_command[0], "gemini")
        self.assertIn("--prompt", captured_command)
        self.assertIn("--output-format", captured_command)
        self.assertEqual(result["mode"], "manual")
        self.assertEqual(result["provider"], "gemini-cli")
        self.assertIn("SlidesApp.create", result["apps_script"])

    def test_cli_generate_list_show_and_export_without_web_api(self):
        source_text = " ".join(
            [
                f"This CLI source section explains concept {idx} with enough detail to support a review card, including a concrete implication for practice and a useful source cue."
                for idx in range(1, 13)
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            source_path = tmp_path / "notes.md"
            source_path.write_text(source_text, encoding="utf-8")
            with mock.patch.object(app, "DECKS_DIR", tmp_path / "decks"):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        flashcards_cli.main(
                            [
                                "generate",
                                "--file",
                                str(source_path),
                                "--no-llm",
                                "--title",
                                "CLI Deck",
                                "--cards",
                                "10",
                            ]
                        ),
                        0,
                    )
                decks = app.list_decks()
                self.assertEqual(len(decks), 1)
                self.assertEqual(decks[0]["title"], "CLI Deck")
                self.assertEqual(decks[0]["card_count"], 10)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(flashcards_cli.main(["show", decks[0]["slug"]]), 0)
                script_path = tmp_path / "slides.gs"
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        flashcards_cli.main(["export-slides", decks[0]["slug"], "--script-out", str(script_path)]),
                        0,
                    )
                self.assertIn("SlidesApp.create", script_path.read_text(encoding="utf-8"))

    def test_cli_google_auth_prints_and_runs_gcloud_command(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(flashcards_cli.main(["google-auth", "--client-id-file", "client.json"]), 0)
        setup_text = output.getvalue()
        self.assertIn("gcloud auth application-default login", setup_text)
        self.assertIn("--client-id-file=client.json", setup_text)
        self.assertIn("GOOGLE_AUTH_PROVIDER=gcloud python3 app.py", setup_text)

        captured = []

        def fake_run(command, check):
            captured.extend(command)
            return app.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch("flashcards_cli.subprocess.run", fake_run):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    flashcards_cli.main(
                        ["google-auth", "--run", "--client-id-file", "client.json", "--no-launch-browser"]
                    ),
                    0,
                )

        self.assertEqual(captured[:4], ["gcloud", "auth", "application-default", "login"])
        self.assertIn("--client-id-file=client.json", captured)
        self.assertIn("--no-launch-browser", captured)

    def test_cli_generate_from_webpage_without_web_api(self):
        source_text = " ".join(
            [
                f"This webpage section explains concept {idx} with enough detail to support review cards, including a concrete example and useful source cue."
                for idx in range(1, 13)
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            with mock.patch.object(app, "DECKS_DIR", tmp_path / "decks"):
                with mock.patch("app.fetch_webpage_text", return_value=source_text):
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(
                            flashcards_cli.main(
                                [
                                    "generate",
                                    "--webpage",
                                    "https://example.com/article",
                                    "--no-llm",
                                    "--title",
                                    "Webpage Deck",
                                    "--cards",
                                    "10",
                                ]
                            ),
                            0,
                        )
                decks = app.list_decks()
                self.assertEqual(len(decks), 1)
                loaded = app.load_deck(tmp_path / "decks" / f"{decks[0]['slug']}.md")
                self.assertEqual(loaded["title"], "Webpage Deck")
                self.assertEqual(loaded["source_url"], "https://example.com/article")
                self.assertEqual(loaded["source_type"], "webpage")

    def test_delete_deck_helper_and_cli(self):
        deck = {
            "title": "Delete Me",
            "summary": "Temporary deck.",
            "source_url": "test",
            "created_at": "2026-06-01T00:00:00+00:00",
            "generator": "unit-test",
            "cards": [
                {
                    "question": f"Question {idx}?",
                    "answer": f"Answer {idx}",
                    "explanation": "Explanation",
                    "source_cue": "Cue",
                    "tags": [],
                }
                for idx in range(1, 11)
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(app, "DECKS_DIR", Path(tmpdir)):
                slug, path = app.save_deck(deck)
                self.assertTrue(path.exists())
                self.assertTrue(app.delete_deck(slug))
                self.assertFalse(path.exists())

                slug, path = app.save_deck(deck)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(flashcards_cli.main(["delete", slug, "--yes"]), 0)
                self.assertFalse(path.exists())

    def test_manual_card_mutations_rewrite_markdown(self):
        deck = {
            "title": "Editable Deck",
            "summary": "Temporary deck.",
            "source_url": "https://example.com/source",
            "source_type": "webpage",
            "created_at": "2026-06-01T00:00:00+00:00",
            "generator": "unit-test",
            "cards": [
                {
                    "question": "Original question one?",
                    "answer": "Original answer one.",
                    "explanation": "Explanation",
                    "source_cue": "Cue",
                    "tags": [],
                },
                {
                    "question": "Original question two?",
                    "answer": "Original answer two.",
                    "explanation": "Explanation",
                    "source_cue": "Cue",
                    "tags": [],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(app, "DECKS_DIR", Path(tmpdir)):
                slug, path = app.save_deck(deck)

                updated = app.add_manual_card(slug, "Manual question?", "Manual answer.")
                self.assertEqual(len(updated["cards"]), 3)
                self.assertEqual(updated["cards"][2]["tags"], ["manual"])

                updated = app.update_card_answer(slug, 2, "Edited manual answer.")
                self.assertEqual(updated["cards"][2]["answer"], "Edited manual answer.")

                deleted_card = json.loads(json.dumps(updated["cards"][0]))
                updated = app.delete_card(slug, 0)
                self.assertEqual(len(updated["cards"]), 2)
                self.assertEqual(updated["cards"][0]["question"], "Original question two?")

                updated = app.insert_card(slug, 0, deleted_card)
                self.assertEqual(len(updated["cards"]), 3)
                self.assertEqual(updated["cards"][0]["question"], "Original question one?")
                self.assertEqual(updated["cards"][1]["question"], "Original question two?")

                round_tripped = app.load_deck(path)
                self.assertEqual(round_tripped["cards"][2]["answer"], "Edited manual answer.")

    def test_delete_card_rejects_last_card(self):
        deck = {
            "title": "Single Card Deck",
            "summary": "Temporary deck.",
            "source_url": "https://example.com/source",
            "source_type": "webpage",
            "created_at": "2026-06-01T00:00:00+00:00",
            "generator": "unit-test",
            "cards": [
                {
                    "question": "Only question?",
                    "answer": "Only answer.",
                    "explanation": "Explanation",
                    "source_cue": "Cue",
                    "tags": [],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(app, "DECKS_DIR", Path(tmpdir)):
                slug, _path = app.save_deck(deck)
                with self.assertRaisesRegex(ValueError, "at least one card"):
                    app.delete_card(slug, 0)

    def test_refresh_and_generate_more_cards_rewrite_existing_deck(self):
        deck = {
            "title": "Source Deck",
            "summary": "Temporary deck.",
            "source_url": "https://example.com/source",
            "source_type": "webpage",
            "created_at": "2026-06-01T00:00:00+00:00",
            "generator": "codex-cli",
            "difficulty": "deep review",
            "cards": [
                {
                    "question": "Existing question one?",
                    "answer": "Existing answer one.",
                    "explanation": "Explanation",
                    "source_cue": "Cue",
                    "tags": [],
                },
                {
                    "question": "Existing question two?",
                    "answer": "Existing answer two.",
                    "explanation": "Explanation",
                    "source_cue": "Cue",
                    "tags": [],
                },
            ],
        }
        calls = []

        def fake_generate(doc_text, title, count, difficulty, provider):
            calls.append((doc_text, title, count, difficulty, provider))
            cards = [
                {
                    "question": f"Generated question {idx}?",
                    "answer": f"Generated answer {idx}.",
                    "explanation": "Generated explanation.",
                    "source_cue": "Generated cue",
                    "tags": ["generated"],
                }
                for idx in range(1, count + 1)
            ]
            return {"deck_title": title, "summary": "Generated summary.", "cards": cards}, "codex-cli"

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(app, "DECKS_DIR", Path(tmpdir)):
                with mock.patch("app.fetch_source_text", return_value="Readable source text. " * 20):
                    with mock.patch("app.generate_flashcards", fake_generate):
                        slug, path = app.save_deck(deck)

                        refreshed = app.refresh_deck(slug)
                        self.assertEqual(path.name, f"{slug}.md")
                        self.assertEqual(len(refreshed["cards"]), 2)
                        self.assertEqual(refreshed["cards"][0]["question"], "Generated question 1?")

                        expanded = app.generate_more_cards(slug)
                        self.assertEqual(len(expanded["cards"]), 7)
                        self.assertEqual(expanded["cards"][-1]["question"], "Generated question 5?")

        self.assertEqual(calls[0][2], 2)
        self.assertEqual(calls[0][3], "deep review")
        self.assertEqual(calls[0][4], "codex")
        self.assertEqual(calls[1][2], 5)

    def test_generate_more_cards_preserves_edits_made_during_generation(self):
        deck = {
            "title": "Race Deck",
            "summary": "Temporary deck.",
            "source_url": "https://example.com/source",
            "source_type": "webpage",
            "created_at": "2026-06-01T00:00:00+00:00",
            "generator": "codex-cli",
            "cards": [
                {
                    "question": "Existing question one?",
                    "answer": "Existing answer one.",
                    "explanation": "Explanation",
                    "source_cue": "Cue",
                    "tags": [],
                },
                {
                    "question": "Existing question two?",
                    "answer": "Existing answer two.",
                    "explanation": "Explanation",
                    "source_cue": "Cue",
                    "tags": [],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(app, "DECKS_DIR", Path(tmpdir)):
                slug, _path = app.save_deck(deck)

                def fake_generate(doc_text, title, count, difficulty, provider):
                    # Simulate another request editing the deck while the slow
                    # generation call is still running.
                    app.update_card_answer(slug, 0, "Edited during generation.")
                    cards = [
                        {
                            "question": f"Generated question {idx}?",
                            "answer": f"Generated answer {idx}.",
                            "explanation": "Generated explanation.",
                            "source_cue": "Generated cue",
                            "tags": ["generated"],
                        }
                        for idx in range(1, count + 1)
                    ]
                    return {"deck_title": title, "summary": "Generated summary.", "cards": cards}, "codex-cli"

                with mock.patch("app.fetch_source_text", return_value="Readable source text. " * 20):
                    with mock.patch("app.generate_flashcards", fake_generate):
                        expanded = app.generate_more_cards(slug)

                self.assertEqual(len(expanded["cards"]), 7)
                self.assertEqual(expanded["cards"][0]["answer"], "Edited during generation.")

    def test_local_fallback_deck_accepts_short_sources(self):
        sentence = (
            "This sentence describes one distinct key concept from the source material "
            "in enough detail to support a useful review card, number {}."
        )
        ten_sentences = " ".join(sentence.format(idx) for idx in range(1, 11))
        three_sentences = " ".join(sentence.format(idx) for idx in range(1, 4))

        deck, generator = app.local_fallback_deck(ten_sentences, "Short Source", 20)
        self.assertEqual(generator, "local-fallback")
        self.assertEqual(len(deck["cards"]), 10)

        deck, _generator = app.local_fallback_deck(three_sentences, "Tiny Deck", 3)
        self.assertEqual(len(deck["cards"]), 3)

        with self.assertRaisesRegex(ValueError, "sentence-level material"):
            app.local_fallback_deck(three_sentences, "Too Small", 12)

    def test_refresh_requires_original_source(self):
        deck = {
            "title": "No Source Deck",
            "summary": "Temporary deck.",
            "source_url": "",
            "source_type": "webpage",
            "created_at": "2026-06-01T00:00:00+00:00",
            "generator": "unit-test",
            "cards": [
                {
                    "question": "Question?",
                    "answer": "Answer.",
                    "explanation": "Explanation",
                    "source_cue": "Cue",
                    "tags": [],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(app, "DECKS_DIR", Path(tmpdir)):
                slug, _path = app.save_deck(deck)
                with self.assertRaisesRegex(ValueError, "original source"):
                    app.refresh_deck(slug)


if __name__ == "__main__":
    unittest.main()
