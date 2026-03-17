# Third-Party Libraries

This project depends on several open-source libraries. This file records what
they are used for in this repository and the license information visible from
the installed package metadata at the time this document was written.

## Runtime dependencies

### `httpx`
- Version checked: `0.28.1`
- Homepage: <https://github.com/encode/httpx>
- License: `BSD-3-Clause`
- Used for:
  - HTTP article fetches
  - image downloads
  - request timeout / retry handling
- Where it is used:
  - `news_crawler/archive.py`

### `beautifulsoup4`
- Version checked: `4.14.3`
- Homepage: <https://www.crummy.com/software/BeautifulSoup/bs4/>
- License: `MIT`
- Used for:
  - parsing article HTML fragments
  - reading metadata tags such as `canonical`, `og:image`, `twitter:image`
  - rewriting image tags to local paths
- Where it is used:
  - `news_crawler/extract.py`
  - `news_crawler/images.py`

### `readability-lxml`
- Version checked: `0.8.4.1`
- Homepage: <http://github.com/buriy/python-readability>
- License: `Apache License 2.0`
- Used for:
  - extracting the main readable article body from noisy page HTML
  - deriving a cleaner article title than the raw `<title>` tag in many cases
- Where it is used:
  - `news_crawler/extract.py`

### `lxml`
- Version checked: `6.0.2`
- Homepage: <https://lxml.de/>
- License: `BSD-3-Clause`
- Used for:
  - HTML parsing backend for BeautifulSoup
  - dependency of `readability-lxml`
- Where it is used:
  - indirectly in `news_crawler/extract.py`
  - indirectly in `news_crawler/images.py`

## Optional dependency

### `playwright`
- Version checked: `1.58.0`
- Homepage: <https://github.com/Microsoft/playwright-python>
- License: `Apache-2.0`
- Used for:
  - rendering JavaScript-heavy article pages when plain HTTP fetch is insufficient
  - fallback acquisition of DOM content from pages that require client-side rendering
- Where it is used:
  - `news_crawler/render.py`
  - optional code path in `news_crawler/archive.py`
- Installation note:
  - this package is optional and is only required when using render fallback
  - browser binaries must also be installed with `playwright install`

## Build / packaging tools

These are not part of the runtime archive pipeline, but they are used for local
development and packaging.

### `setuptools`
- Version checked: `82.0.1`
- License: `MIT`
- Used for:
  - packaging the project
  - editable installation via `pip install -e .`

### `wheel`
- Version checked: `0.46.3`
- License: `MIT`
- Used for:
  - Python wheel build support during packaging / installation

## Notes

- This file is a project note, not a replacement for the original upstream
  license texts.
- If you distribute this project publicly, you should verify the latest license
  terms and include any required notices from the upstream projects.
- Transitive dependencies exist beyond the libraries listed above. This file
  focuses on the direct dependencies declared by this repository or explicitly
  used in the code.
