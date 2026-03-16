from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Dict, List, Tuple
from urllib.parse import urlparse, urlunparse

import httpx

from .extract import ExtractedArticle, extract_article, parse_content_fragment
from .images import iter_article_image_urls, rewrite_images_to_local
from .render import RenderContext
from .utils import (
    atomic_write_bytes,
    atomic_write_text,
    dataclass_to_dict,
    ensure_dir,
    guess_ext,
    host_slug,
    json_dumps,
    local_date_compact,
    sha256_bytes,
    short_id,
    utc_now_iso,
)


_URL_LINE_RE = re.compile(r"^\s*(https?://\S+)\s*$", re.IGNORECASE)
_DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class ArchiveConfig:
    input_path: Path
    out_dir: Path
    concurrency: int = 8
    per_domain: int = 2
    timeout_seconds: float = 20.0
    retries: int = 2
    render_fallback: bool = False
    only_http: bool = False
    only_render: bool = False
    force: bool = False
    max_images: int = 30
    max_image_bytes: int = 15_000_000
    user_agent: Optional[str] = None
    accept_language: str = "en-US,en;q=0.9"
    save_raw_on_failure: bool = True


@dataclass
class ImageRecord:
    original_url: str
    resolved_url: str
    local_path: Optional[str]
    sha256: Optional[str]
    content_type: Optional[str]
    bytes: Optional[int]
    error: Optional[str] = None


@dataclass
class ArchiveResult:
    input_url: str
    final_url: Optional[str]
    canonical_url: Optional[str]
    output_dir: Optional[str]
    status: str
    method: Optional[str]
    title: Optional[str]
    author: Optional[str]
    published_at: Optional[str]
    text_length: Optional[int]
    excerpt: Optional[str]
    images: list[ImageRecord]
    errors: list[str]
    failure_reason: Optional[str]
    failure_signals: list[str]
    fetched_at: str


def _read_input_urls(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(str(path))

    urls: list[str] = []
    if path.suffix.lower() == ".jsonl":
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            url = obj.get("url") if isinstance(obj, dict) else None
            if isinstance(url, str) and url.strip():
                urls.append(url.strip())
        return urls

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _URL_LINE_RE.match(line)
        if m:
            urls.append(m.group(1))
    return urls


def _load_success_set(results_path: Path) -> set[str]:
    if not results_path.exists():
        return set()
    success: set[str] = set()
    for line in results_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("status") == "success":
            u = obj.get("input_url")
            if isinstance(u, str):
                success.add(u)
            fu = obj.get("final_url")
            if isinstance(fu, str):
                success.add(fu)
    return success


def _default_headers(cfg: ArchiveConfig) -> dict[str, str]:
    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": cfg.accept_language,
        "Cache-Control": "max-age=0",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    if cfg.user_agent:
        headers["User-Agent"] = cfg.user_agent
    else:
        headers["User-Agent"] = _DEFAULT_BROWSER_UA
    return headers


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def _image_headers(img_url: str, referer: str, cfg: ArchiveConfig) -> Dict[str, str]:
    headers = {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": cfg.accept_language,
        "Referer": referer,
        "Origin": _origin(referer),
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-site" if _domain(img_url) == _domain(referer) else "cross-site",
    }
    if cfg.user_agent:
        headers["User-Agent"] = cfg.user_agent
    else:
        headers["User-Agent"] = _DEFAULT_BROWSER_UA
    return headers


def _classify_exception(exc: Exception) -> Tuple[Optional[str], List[str]]:
    signals: List[str] = []
    reason: Optional[str] = None

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        signals.append(f"http_status:{code}")
        if code == 401:
            reason = "unauthorized"
        elif code == 403:
            reason = "forbidden"
        elif code == 404:
            reason = "not_found"
        elif code == 408:
            reason = "timeout"
        elif code == 409:
            reason = "conflict"
        elif code == 410:
            reason = "gone"
        elif code == 429:
            reason = "rate_limited"
        elif 500 <= code <= 599:
            reason = "server_error"
        else:
            reason = "http_error"
    elif isinstance(exc, httpx.TimeoutException):
        reason = "timeout"
    elif isinstance(exc, httpx.ConnectError):
        reason = "connect_error"
    elif isinstance(exc, httpx.NetworkError):
        reason = "network_error"

    if reason is None:
        text = str(exc).lower()
        if "captcha" in text:
            reason = "captcha"
            signals.append("captcha")
        elif "timeout" in text:
            reason = "timeout"
        elif "ssl" in text:
            reason = "ssl_error"

    return reason, signals


def _classify_html_failure(html: Optional[str], *, status_code: Optional[int] = None) -> Tuple[Optional[str], List[str]]:
    if not html:
        return None, []

    text = html.lower()
    signals: List[str] = []
    reason: Optional[str] = None

    patterns = [
        ("cloudflare", "challenge"),
        ("cf-browser-verification", "challenge"),
        ("attention required", "challenge"),
        ("verify you are human", "challenge"),
        ("checking your browser", "challenge"),
        ("captcha", "captcha"),
        ("g-recaptcha", "captcha"),
        ("hcaptcha", "captcha"),
        ("subscribe to continue", "paywall"),
        ("subscription required", "paywall"),
        ("already a subscriber", "paywall"),
        ("sign in to continue", "paywall"),
        ("log in to continue", "paywall"),
        ("please log in", "paywall"),
        ("enable javascript", "javascript_required"),
        ("access denied", "access_denied"),
        ("request blocked", "blocked"),
        ("bot detected", "bot_detected"),
    ]
    for needle, label in patterns:
        if needle in text:
            signals.append(label)

    if "challenge" in signals:
        reason = "challenge"
    elif "captcha" in signals:
        reason = "captcha"
    elif "paywall" in signals:
        reason = "paywall"
    elif "access_denied" in signals:
        reason = "access_denied"
    elif "blocked" in signals or "bot_detected" in signals:
        reason = "blocked"
    elif "javascript_required" in signals:
        reason = "javascript_required"
    elif status_code == 403:
        reason = "forbidden"
    elif status_code == 429:
        reason = "rate_limited"

    return reason, signals


async def _fetch_html_http(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    resp = await client.get(url)
    resp.raise_for_status()
    final_url = str(resp.url)
    ct = resp.headers.get("content-type", "")
    if "html" not in ct.lower():
        # Still return; some sites mislabel.
        pass
    return final_url, resp.text


async def _download_image(
    client: httpx.AsyncClient,
    img_url: str,
    *,
    referer: str,
    assets_dir: Path,
    max_bytes: int,
    cfg: ArchiveConfig,
) -> Tuple[Optional[Path], Optional[str], Optional[str], Optional[int], Optional[str]]:
    headers = _image_headers(img_url, referer, cfg)
    try:
        resp = await client.get(img_url, headers=headers)
        resp.raise_for_status()
        content = await resp.aread()
        if len(content) > max_bytes:
            return None, None, resp.headers.get("content-type"), len(content), f"image too large: {len(content)} bytes"
        digest = sha256_bytes(content)
        ext = guess_ext(resp.headers.get("content-type"), img_url)
        out_path = assets_dir / f"{digest}{ext}"
        if not out_path.exists():
            atomic_write_bytes(out_path, content)
        return out_path, digest, resp.headers.get("content-type"), len(content), None
    except Exception as e:
        return None, None, None, None, str(e)


def _build_output_dir(cfg: ArchiveConfig, url: str) -> Path:
    return cfg.out_dir / f"{host_slug(url)}_{local_date_compact()}_{short_id(8)}"


def _wrap_article_html(*, title: Optional[str], source_url: str, fetched_at: str, content_html: str) -> str:
    safe_title = (title or "Article").strip()
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_escape_html(safe_title)}</title>
  <style>
    :root {{
      --fg: #111;
      --muted: #666;
      --bg: #fbfbf8;
      --link: #0b57d0;
      --max: 760px;
    }}
    html, body {{ background: var(--bg); color: var(--fg); margin: 0; padding: 0; }}
    body {{ font: 16px/1.55 ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; }}
    a {{ color: var(--link); }}
    .wrap {{ max-width: var(--max); margin: 0 auto; padding: 28px 18px 64px; }}
    header {{ margin-bottom: 18px; }}
    h1 {{ font-size: 28px; line-height: 1.2; margin: 0 0 8px; }}
    .meta {{ color: var(--muted); font-size: 13px; }}
    .content {{ font-size: 17px; }}
    img {{ max-width: 100%; height: auto; }}
    figure {{ margin: 18px 0; }}
    blockquote {{ border-left: 3px solid #ddd; margin: 16px 0; padding: 0 0 0 12px; color: #333; }}
    pre {{ overflow: auto; padding: 12px; background: #f2f2ee; }}
  </style>
  <base href="./" />
</head>
<body>
  <div class="wrap">
    <header>
      <h1>{_escape_html(safe_title)}</h1>
      <div class="meta">
        Source: <a href="{_escape_attr(source_url)}">{_escape_html(source_url)}</a><br/>
        Fetched: {_escape_html(fetched_at)}
      </div>
    </header>
    <main class="content">
      {content_html}
    </main>
  </div>
</body>
</html>
"""


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _escape_attr(text: str) -> str:
    return _escape_html(text)


def _excerpt(text: Optional[str], n: int = 280) -> Optional[str]:
    if not text:
        return None
    t = " ".join(text.split())
    if len(t) <= n:
        return t
    return t[: max(0, n - 1)] + "…"


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower() or "unknown"
    except Exception:
        return "unknown"


async def _with_retries(factory, retries: int, *, base_delay: float = 0.8):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return await factory()
        except Exception as e:
            last_exc = e
            if attempt >= retries:
                break
            await asyncio.sleep(base_delay * (2**attempt))
    assert last_exc is not None
    raise last_exc


async def _archive_one(
    url: str,
    *,
    cfg: ArchiveConfig,
    client: httpx.AsyncClient,
    render_ctx: Optional[RenderContext],
    global_sem: asyncio.Semaphore,
    domain_sems: dict[str, asyncio.Semaphore],
    results_lock: asyncio.Lock,
    results_path: Path,
) -> ArchiveResult:
    errors: List[str] = []
    fetched_at = utc_now_iso()

    out_dir = _build_output_dir(cfg, url)
    assets_dir = out_dir / "assets"
    ensure_dir(assets_dir)
    ensure_dir(out_dir)

    method_used: Optional[str] = None
    final_url: Optional[str] = None
    html: Optional[str] = None

    canonical_url: Optional[str] = None
    title: Optional[str] = None
    author: Optional[str] = None
    published_at: Optional[str] = None
    text: Optional[str] = None
    images: List[ImageRecord] = []
    failure_reason: Optional[str] = None
    failure_signals: List[str] = []
    last_status_code: Optional[int] = None

    content_html: Optional[str] = None
    content_root = None  # BeautifulSoup Tag wrapper

    async def http_fetch():
        return await _fetch_html_http(client, url)

    async def render_fetch():
        if not render_ctx:
            raise RuntimeError("render requested but Playwright is not available")
        rendered = await render_ctx.fetch_html(url, timeout_ms=int(cfg.timeout_seconds * 1000))
        return rendered.final_url, rendered.html

    domain = _domain(url)
    if domain not in domain_sems:
        domain_sems[domain] = asyncio.Semaphore(cfg.per_domain)
    domain_sem = domain_sems[domain]

    async with global_sem, domain_sem:
        # 1) Acquire HTML (HTTP first, optional render fallback)
        try:
            if cfg.only_render:
                final_url, html = await _with_retries(render_fetch, cfg.retries)
                method_used = "render"
            else:
                final_url, html = await _with_retries(http_fetch, cfg.retries)
                method_used = "http"
        except Exception as e:
            errors.append(f"html fetch failed ({'render' if cfg.only_render else 'http'}): {e}")
            reason, signals = _classify_exception(e)
            failure_reason = failure_reason or reason
            failure_signals.extend(signals)
            if isinstance(e, httpx.HTTPStatusError):
                last_status_code = e.response.status_code
            if cfg.render_fallback and not cfg.only_http:
                try:
                    final_url, html = await _with_retries(render_fetch, cfg.retries)
                    method_used = "render"
                except Exception as e2:
                    errors.append(f"render fallback failed: {e2}")
                    reason, signals = _classify_exception(e2)
                    failure_reason = failure_reason or reason
                    failure_signals.extend(signals)
                    html = None
            else:
                html = None

        # 2) Extract article content via readability
        if html:
            reason, signals = _classify_html_failure(html, status_code=last_status_code)
            failure_reason = failure_reason or reason
            failure_signals.extend(signals)
            try:
                extracted: ExtractedArticle = extract_article(html, final_url or url)
                canonical_url = extracted.canonical_url
                title = extracted.title
                author = extracted.author
                published_at = extracted.published_at
                text = extracted.text
                content_html = extracted.content_html
                if content_html:
                    content_root = parse_content_fragment(content_html)
            except Exception as e:
                errors.append(f"extract failed: {e}")
                reason, signals = _classify_exception(e)
                failure_reason = failure_reason or reason
                failure_signals.extend(signals)
                if cfg.save_raw_on_failure:
                    atomic_write_text(out_dir / "raw.html", html)

        # 3) If extraction is weak and render fallback is enabled, retry with render
        if (
            (not content_html or not text or len(text) < 200)
            and html
            and cfg.render_fallback
            and not cfg.only_http
            and method_used != "render"
        ):
            try:
                final_url2, html2 = await _with_retries(render_fetch, cfg.retries)
                extracted2 = extract_article(html2, final_url2)
                final_url = final_url2
                html = html2
                method_used = "render"
                canonical_url = extracted2.canonical_url
                title = extracted2.title
                author = extracted2.author
                published_at = extracted2.published_at
                text = extracted2.text
                content_html = extracted2.content_html
                if content_html:
                    content_root = parse_content_fragment(content_html)
            except Exception as e:
                errors.append(f"extract(render) failed: {e}")
                reason, signals = _classify_html_failure(html, status_code=last_status_code)
                failure_reason = failure_reason or reason
                failure_signals.extend(signals)
                if cfg.save_raw_on_failure and html:
                    atomic_write_text(out_dir / "raw.html", html)

        # 4) Download related images (content images + representative meta images)
        url_map: Dict[str, str] = {}
        resolved_seen: set[str] = set()

        if content_root and final_url:
            candidates = list(iter_article_image_urls(content_root, final_url))
            if cfg.max_images and len(candidates) > cfg.max_images:
                candidates = candidates[: cfg.max_images]

            for c in candidates:
                if c.resolved_url in resolved_seen:
                    continue
                resolved_seen.add(c.resolved_url)

                out_path, digest, ct, nbytes, err = await _download_image(
                    client,
                    c.resolved_url,
                    referer=final_url,
                    assets_dir=assets_dir,
                    max_bytes=cfg.max_image_bytes,
                    cfg=cfg,
                )
                local_rel = None
                if out_path:
                    local_rel = str(Path("assets") / out_path.name)
                    url_map[c.original_url] = local_rel
                images.append(
                    ImageRecord(
                        original_url=c.original_url,
                        resolved_url=c.resolved_url,
                        local_path=local_rel,
                        sha256=digest,
                        content_type=ct,
                        bytes=nbytes,
                        error=err,
                    )
                )

        if html and final_url:
            try:
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(html, "lxml")
                rep_urls: List[str] = []
                og = soup.find("meta", attrs={"property": "og:image"})
                tw = soup.find("meta", attrs={"name": "twitter:image"}) or soup.find(
                    "meta", attrs={"property": "twitter:image"}
                )
                if og and og.get("content"):
                    rep_urls.append(str(og.get("content")).strip())
                if tw and tw.get("content"):
                    rep_urls.append(str(tw.get("content")).strip())
                for rep in rep_urls:
                    if not rep:
                        continue
                    resolved = httpx.URL(final_url).join(rep).human_repr()
                    if resolved in resolved_seen:
                        continue
                    resolved_seen.add(resolved)
                    out_path, digest, ct, nbytes, err = await _download_image(
                        client,
                        resolved,
                        referer=final_url,
                        assets_dir=assets_dir,
                        max_bytes=cfg.max_image_bytes,
                        cfg=cfg,
                    )
                    local_rel = None
                    if out_path:
                        local_rel = str(Path("assets") / out_path.name)
                    images.append(
                        ImageRecord(
                            original_url=rep,
                            resolved_url=resolved,
                            local_path=local_rel,
                            sha256=digest,
                            content_type=ct,
                            bytes=nbytes,
                            error=err,
                        )
                    )
            except Exception as e:
                errors.append(f"rep image extraction failed: {e}")

        # 5) Rewrite content HTML to local paths
        if content_root and url_map:
            rewrite_images_to_local(content_root, url_map)
            content_html = content_root.decode_contents()

        status = "success" if (content_html and text and len(text) >= 200) else "failure"
        if status == "failure" and not failure_reason:
            failure_reason = "content_extraction_failed"
        if status == "success":
            failure_reason = None
            failure_signals = []

        # Write article.html if we have extracted content.
        if content_html and final_url:
            html_out = _wrap_article_html(
                title=title,
                source_url=final_url,
                fetched_at=fetched_at,
                content_html=content_html,
            )
            atomic_write_text(out_dir / "article.html", html_out)

        # Save raw snapshot for failures (if enabled).
        if status == "failure" and cfg.save_raw_on_failure and html and not (out_dir / "raw.html").exists():
            atomic_write_text(out_dir / "raw.html", html)

        result = ArchiveResult(
            input_url=url,
            final_url=final_url,
            canonical_url=canonical_url,
            output_dir=str(out_dir),
            status=status,
            method=method_used,
            title=title,
            author=author,
            published_at=published_at,
            text_length=(len(text) if text else None),
            excerpt=_excerpt(text),
            images=images,
            errors=errors,
            failure_reason=failure_reason,
            failure_signals=sorted(set(failure_signals)),
            fetched_at=fetched_at,
        )

        atomic_write_text(out_dir / "metadata.json", json_dumps(result))

        async with results_lock:
            ensure_dir(results_path.parent)
            with results_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(dataclass_to_dict(result), ensure_ascii=False) + "\n")

        return result


async def _run(cfg: ArchiveConfig) -> list[ArchiveResult]:
    ensure_dir(cfg.out_dir)
    results_path = cfg.out_dir / "results.jsonl"
    success_set = set() if cfg.force else _load_success_set(results_path)

    urls = _read_input_urls(cfg.input_path)
    urls = [u for u in urls if u not in success_set]

    global_sem = asyncio.Semaphore(cfg.concurrency)
    domain_sems: dict[str, asyncio.Semaphore] = {}
    results_lock = asyncio.Lock()

    render_ctx: Optional[RenderContext] = None
    if (cfg.render_fallback or cfg.only_render) and not cfg.only_http:
        render_ctx = await RenderContext.create(
            max_concurrency=min(2, cfg.concurrency),
            user_agent=cfg.user_agent,
            accept_language=cfg.accept_language,
        )

    timeout = httpx.Timeout(cfg.timeout_seconds)
    async with httpx.AsyncClient(follow_redirects=True, headers=_default_headers(cfg), timeout=timeout) as client:
        try:
            tasks = [
                asyncio.create_task(
                    _archive_one(
                        u,
                        cfg=cfg,
                        client=client,
                        render_ctx=render_ctx,
                        global_sem=global_sem,
                        domain_sems=domain_sems,
                        results_lock=results_lock,
                        results_path=results_path,
                    )
                )
                for u in urls
            ]
            if not tasks:
                return []
            return await asyncio.gather(*tasks)
        finally:
            if render_ctx:
                await render_ctx.close()


def run_archive(cfg: ArchiveConfig) -> None:
    # Synchronous entry point for CLI.
    asyncio.run(_run(cfg))
