# news_crawler

Given a list of news article URLs, this tool saves an offline-viewable archive:

- `article.html`: cleaned article content with local image links
- `assets/`: downloaded images referenced by the article
- `metadata.json`: crawl/extraction details and image mappings
- `results.jsonl`: one JSON object per input URL (success/failure)

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

Optional (JS-render fallback):

```bash
pip install -e ".[render]"
playwright install
```

## Usage

Archive URLs from a text file (one URL per line):

```bash
news_crawler archive --input urls.txt --out output --concurrency 8 --render-fallback
```

JSONL input is also supported (each line like `{"url": "https://..."}`):

```bash
news_crawler archive --input urls.jsonl --out output
```

## Notes

- This is designed for offline reading (not pixel-perfect page capture).
- Some sites block scraping or require login; those will be recorded as failures and may save `raw.html` depending on options.
- Third-party library notes are documented in `THIRD_PARTY_NOTICES.md`.
