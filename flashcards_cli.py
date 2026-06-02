#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

import app


def build_deck_from_text(
    source_text,
    source_label,
    title,
    card_count,
    difficulty,
    offline=False,
    codex_cli=False,
    openai_api=False,
    source_type="google_doc",
):
    doc_text = app.normalize_doc_text(source_text)
    count = app.clamp_card_count(card_count)
    if offline:
        raw_deck, generator = app.local_fallback_deck(doc_text, title, count)
    elif openai_api:
        raw_deck, generator = app.generate_flashcards(doc_text, title, count, difficulty, provider="openai")
    elif codex_cli:
        raw_deck, generator = app.generate_flashcards(doc_text, title, count, difficulty, provider="codex")
    else:
        raw_deck, generator = app.generate_flashcards(doc_text, title, count, difficulty)
    deck_title = raw_deck.get("deck_title") or title or "Google Doc Flashcards"
    deck = {
        "title": deck_title,
        "summary": raw_deck.get("summary", ""),
        "source_url": source_label,
        "source_type": source_type,
        "created_at": app.utc_now(),
        "generator": generator,
        "cards": raw_deck.get("cards", []),
    }
    slug, path = app.save_deck(deck)
    deck["slug"] = slug
    deck["markdown_path"] = str(path)
    deck["markdown_url"] = f"/decks/{slug}.md"
    return deck


def read_source(args):
    if args.file:
        path = Path(args.file).expanduser()
        return path.read_text(encoding="utf-8"), str(path), "file"
    if args.doc:
        return app.fetch_google_doc_text(args.doc), args.doc, "google_doc"
    if args.webpage:
        return app.fetch_webpage_text(args.webpage), args.webpage, "webpage"
    raise SystemExit("Provide --doc, --webpage, or --file.")


def cmd_generate(args):
    source_text, source_label, source_type = read_source(args)
    deck = build_deck_from_text(
        source_text,
        source_label,
        args.title or "",
        args.cards,
        args.difficulty,
        offline=args.offline,
        codex_cli=args.codex_cli,
        openai_api=args.openai_api,
        source_type=source_type,
    )
    print(f"Saved {len(deck['cards'])} cards: {deck['markdown_path']}")
    print(f"Slug: {deck['slug']}")
    print(f"Generator: {deck['generator']}")
    return 0


def cmd_list(_args):
    decks = app.list_decks()
    if not decks:
        print("No decks found.")
        return 0
    for deck in decks:
        print(f"{deck['slug']}\t{deck['card_count']} cards\t{deck['title']}\t{deck['generator']}")
    return 0


def load_deck_by_slug(slug):
    deck_path = app.DECKS_DIR / f"{app.slugify(slug)}.md"
    if not deck_path.exists():
        raise SystemExit(f"Deck not found: {slug}")
    return app.load_deck(deck_path)


def cmd_review(args):
    deck = load_deck_by_slug(args.slug)
    print(f"\n{deck['title']}")
    print(f"{len(deck['cards'])} cards. Press Enter to reveal, then choose c=correct, a=again, q=quit.\n")
    correct = 0
    again = 0
    for index, card in enumerate(deck["cards"], start=1):
        print(f"Card {index}/{len(deck['cards'])}")
        print(f"Q: {card['question']}")
        command = input("Reveal answer...")
        if command.lower().strip() == "q":
            break
        print(f"A: {card['answer']}")
        if card.get("explanation"):
            print(f"Why: {card['explanation']}")
        command = input("[c]orrect / [a]gain / [q]uit: ").lower().strip()
        if command == "q":
            break
        if command == "a":
            again += 1
        else:
            correct += 1
        print("")
    print(f"Review result: {correct} correct, {again} again.")
    return 0


def cmd_export_slides(args):
    deck = load_deck_by_slug(args.slug)
    provider = "google"
    if args.codex_cli:
        provider = "codex"
    elif args.gemini_cli:
        provider = "gemini"
    result = app.create_google_slides(deck, provider=provider)
    if result["mode"] == "direct":
        print(result["url"])
        return 0
    if args.script_out:
        path = Path(args.script_out).expanduser()
        path.write_text(result["apps_script"], encoding="utf-8")
        print(f"Wrote Apps Script: {path}")
    else:
        print(result["message"])
        print(result["apps_script"])
    return 0


def cmd_delete(args):
    deck = load_deck_by_slug(args.slug)
    if not args.yes:
        answer = input(f"Delete deck '{deck['title']}' ({deck['slug']})? Type delete to confirm: ")
        if answer.strip().lower() != "delete":
            print("Delete cancelled.")
            return 1
    if not app.delete_deck(args.slug):
        raise SystemExit(f"Deck not found: {args.slug}")
    print(f"Deleted deck: {args.slug}")
    return 0


def cmd_show(args):
    deck = load_deck_by_slug(args.slug)
    if args.json:
        print(json.dumps(deck, ensure_ascii=False, indent=2))
        return 0
    print(deck["markdown"])
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="flashcards_cli.py",
        description="Generate, review, and export flashcard markdown decks without the web API server.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="Generate a markdown flashcard deck.")
    source = generate.add_mutually_exclusive_group(required=True)
    source.add_argument("--doc", help="Google Doc URL or document id.")
    source.add_argument("--webpage", help="Webpage URL to use as source.")
    source.add_argument("--file", help="Local text or markdown file to use as source.")
    generate.add_argument("--title", default="", help="Optional deck title.")
    generate.add_argument("--cards", type=int, default=12, help="Number of cards, clamped to 10-20.")
    generate.add_argument("--difficulty", default="balanced", help="balanced, introductory, exam prep, or deep review.")
    generate.add_argument(
        "--offline",
        "--no-llm",
        dest="offline",
        action="store_true",
        help="Skip the OpenAI API and use the local sentence-based fallback generator.",
    )
    generate.add_argument(
        "--codex-cli",
        action="store_true",
        help="Use Codex CLI (`codex exec`) as the structured flashcard generator. This is the default unless --openai-api or --no-llm is set.",
    )
    generate.add_argument(
        "--openai-api",
        action="store_true",
        help="Use the app OpenAI API generator instead of the default Codex CLI generator.",
    )
    generate.set_defaults(func=cmd_generate)

    list_cmd = subparsers.add_parser("list", help="List saved decks.")
    list_cmd.set_defaults(func=cmd_list)

    review = subparsers.add_parser("review", help="Review a deck in the terminal.")
    review.add_argument("slug", help="Deck slug from the list command.")
    review.set_defaults(func=cmd_review)

    show = subparsers.add_parser("show", help="Print a saved markdown deck.")
    show.add_argument("slug", help="Deck slug from the list command.")
    show.add_argument("--json", action="store_true", help="Print parsed JSON instead of markdown.")
    show.set_defaults(func=cmd_show)

    export = subparsers.add_parser("export-slides", help="Create Google Slides or emit Apps Script fallback.")
    export.add_argument("slug", help="Deck slug from the list command.")
    export.add_argument("--script-out", help="Path to write Apps Script when direct export is not configured.")
    export_provider = export.add_mutually_exclusive_group()
    export_provider.add_argument(
        "--codex-cli",
        action="store_true",
        help="Use Codex CLI to generate the Google Slides Apps Script export.",
    )
    export_provider.add_argument(
        "--gemini-cli",
        action="store_true",
        help="Use Gemini CLI to generate the Google Slides Apps Script export.",
    )
    export.set_defaults(func=cmd_export_slides)

    delete = subparsers.add_parser("delete", help="Delete a saved markdown deck.")
    delete.add_argument("slug", help="Deck slug from the list command.")
    delete.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    delete.set_defaults(func=cmd_delete)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
