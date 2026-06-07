#!/usr/bin/env python3
# encoding=utf-8
"""
Find KKTIX events suitable for Level-1 dry-run practice with tickets_hunter.

Scans https://kktix.com/events, walks past Cloudflare with zendriver, probes
each event detail page, then prints a Markdown table of candidates that:

  * are FREE (default) or under --max-price
  * have registration still open ("立即報名" / "報名中" — no "已截止" / "額滿")
  * start within the next --days days
  * are not super-popular ranked URLs (popularity heuristic optional)

Designed to run INSIDE the dev container so it shares Chrome + zendriver
with tickets_hunter:

  docker compose exec hunter python /app/tools/find_kktix_practice_events.py
  docker compose exec hunter python /app/tools/find_kktix_practice_events.py \
      --limit 40 --max-price 200 --days 21 --headless

Default behavior (no flags): headed browser via noVNC, ≤25 events probed,
FREE only, within 30 days. Watch progress at http://localhost:6080/vnc.html .

⚠️  Respect rate limits — the script throttles 2s between event probes and
caps at --limit pages. Do NOT remove the throttle for KKTIX scraping.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

# Make sibling src/ modules importable so we can reuse chrome_downloader for
# the same Chrome-for-Testing binary tickets_hunter uses.
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import zendriver as zd  # noqa: E402  — installed in the dev container

try:
    import chrome_downloader  # noqa: E402
except ImportError:
    chrome_downloader = None  # script can still run if user has system chrome


EVENT_LIST_URL = "https://kktix.com/events"
# Cloudflare's managed challenge typically resolves within 4-7s; padding for slow
# container boots. If your network is fast, lower to ~5.
CLOUDFLARE_WAIT_S = 8


# -- browser ------------------------------------------------------------------


def _stealth_args() -> list[str]:
    # Subset of nodriver_common's args — enough for Cloudflare's managed mode.
    return [
        "--lang=zh-TW",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-pings",
        "--password-store=basic",
        "--homepage=about:blank",
    ]


async def open_browser(headless: bool) -> zd.Browser:
    chrome_path = None
    if chrome_downloader is not None:
        webdriver_dir = os.path.join(SRC, "webdriver")
        try:
            chrome_path = chrome_downloader.ensure_chrome_available(
                download_dir=webdriver_dir
            )
        except Exception as exc:
            print(f"[warn] chrome_downloader failed ({exc}); falling back to system chrome")

    # In Docker containers without SYS_ADMIN, Chrome refuses sandbox.
    sandbox = not _in_docker()

    conf = zd.Config(
        browser_args=_stealth_args(),
        sandbox=sandbox,
        headless=headless,
        browser_executable_path=chrome_path,
    )
    return await zd.start(conf)


def _in_docker() -> bool:
    return os.path.exists("/.dockerenv")


# -- listing ------------------------------------------------------------------


EXTRACT_LINKS_JS = r"""
(() => {
  const seen = new Set();
  const out = [];
  document.querySelectorAll('a[href]').forEach(a => {
    const href = a.href;
    if (!href) return;
    // Event detail pages: https://<community>.kktix.cc/events/<slug>
    // Also handle https://kktix.com/events/<slug>
    if (!/kktix\.(cc|com)\/events\/[^?#\/]+/.test(href)) return;
    // Skip the catch-all listing root itself
    if (/^https?:\/\/[^/]+\/events\/?$/.test(href)) return;
    // Strip query string / fragment
    const u = href.split('?')[0].split('#')[0];
    if (seen.has(u)) return;
    seen.add(u);
    out.push(u);
  });
  return out;
})()
"""


async def fetch_event_links(browser: zd.Browser, max_links: int) -> list[str]:
    tab = await browser.get(EVENT_LIST_URL)
    print(f"[load] {EVENT_LIST_URL} — waiting {CLOUDFLARE_WAIT_S}s for Cloudflare...")
    await asyncio.sleep(CLOUDFLARE_WAIT_S)

    # Encourage lazy-load
    for _ in range(3):
        await tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.2)

    urls = await tab.evaluate(EXTRACT_LINKS_JS)
    if not isinstance(urls, list):
        return []
    print(f"[step] discovered {len(urls)} unique event URLs")
    return urls[:max_links]


# -- per-event probe ----------------------------------------------------------


DETAIL_PROBE_JS = r"""
(() => {
  const txt = (document.body && document.body.innerText) || '';
  const title = (document.querySelector('h1.title, h1, .event-title')?.innerText
                 || document.title || '').trim();
  const host = location.hostname;

  // Ticket tiers — KKTIX renders these via Angular as <li> rows under the
  // registration widget. We extract name/price/remaining defensively across
  // a few possible class names.
  const tierNodes = [
    ...document.querySelectorAll(
      '.ticket-list li, .ticket-types li, ul.tickets li, [class*="ticket-row"]'
    )
  ];
  const tiers = tierNodes.map(li => {
    const t = (li.innerText || '').trim().replace(/\s+/g, ' ');
    // Heuristics: extract price + remaining count if present
    const priceMatch = t.match(/(?:NT\$|\$)\s*([0-9][0-9,]*)/);
    const freeMatch  = /免費|FREE|無料/i.test(t);
    const remainMatch = t.match(/(剩餘|還剩|尚餘|remaining)[^0-9]*([0-9]+)/i);
    const soldOut = /(售完|售畢|額滿|Sold ?Out)/i.test(t);
    return {
      text: t.slice(0, 120),
      price: freeMatch ? 0 : (priceMatch ? parseInt(priceMatch[1].replace(/,/g,''),10) : null),
      remaining: remainMatch ? parseInt(remainMatch[2], 10) : null,
      soldOut,
    };
  }).filter(x => x.text.length > 0);

  // Login required indicator
  const needLogin = !!document.querySelector('a[href*="/users/sign_in"], .login-required')
                    || /請先登入|請登入會員|Sign in to register/i.test(txt);

  // CAPTCHA presence
  const hasCaptcha = !!document.querySelector('img[src*="captcha"], canvas[id*="captcha"], #captcha, [class*="captcha"]');

  // Verify question (KKTIX uses custom questions in the registration form)
  // Look for radio/select question groups within the form.
  const questionNodes = [
    ...document.querySelectorAll(
      'div.form-group label, fieldset legend, .question-text, [class*="question"]'
    )
  ];
  const questions = questionNodes
    .map(n => (n.innerText || '').trim())
    .filter(t => t.length > 4 && t.length < 200 && /[？?]/.test(t))
    .slice(0, 5);

  // Member-code field
  const hasMemberCode = !!document.querySelector(
    'input[placeholder*="序號"], input[name*="member_code"], input[id*="member-code"]'
  );

  // Cloudflare Turnstile widget
  const hasTurnstile = !!document.querySelector(
    'iframe[src*="challenges.cloudflare.com"], .cf-turnstile, [data-sitekey]'
  );

  // Schema.org event metadata
  let startDate = null;
  try {
    for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
      const d = JSON.parse(s.textContent || '{}');
      const evs = Array.isArray(d) ? d : [d];
      for (const ev of evs) {
        if (ev && ev['@type'] === 'Event' && ev.startDate) {
          startDate = ev.startDate; break;
        }
      }
      if (startDate) break;
    }
  } catch(e){}

  return { title, host, startDate, tiers, needLogin, hasCaptcha,
           questions, hasMemberCode, hasTurnstile };
})()
"""

PROBE_JS = r"""
(() => {
  const txt = (document.body && document.body.innerText) || '';
  const titleEl = document.querySelector('h1.title, h1, .event-title, .name');
  const title = ((titleEl && titleEl.innerText) || document.title || '').trim();

  // Price detection: prefer the ticket type list rendered by the register widget
  //   <ul class="ticket-list">… or div with "ticket-price" classes.
  // Fall back to "$NNN" patterns near the word "票" or in price-looking lines.
  const priceNodes = [
    ...document.querySelectorAll('.ticket-price, .price, [class*="price"]')
  ].map(n => (n.innerText || '').trim()).filter(Boolean);
  const priceText = priceNodes.join('\n') || txt;
  const priceMatches = [...priceText.matchAll(/(?:NT\$|\$)\s*([0-9][0-9,]*)/g)];
  const prices = priceMatches.map(m => parseInt(m[1].replace(/,/g, ''), 10))
                              .filter(n => Number.isFinite(n) && n >= 0);
  // Free signal: explicit "免費" in price node OR a $0 tier
  const isFree = /免費|FREE|無料/i.test(priceText) || prices.some(p => p === 0);
  const paidPrices = prices.filter(p => p > 0);
  const minPrice = paidPrices.length ? Math.min(...paidPrices) : null;

  const closedKw = /(已截止|報名截止|售完|售畢|Sold ?Out|已額滿|報名已結束)/i.test(txt);
  const openKw   = /(立即報名|我要報名|尚有名額|可購買|報名中|現正報名)/i.test(txt);

  // Schema.org Event JSON-LD
  let startDate = null;
  try {
    for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
      const raw = s.textContent;
      if (!raw) continue;
      const d = JSON.parse(raw);
      const evs = Array.isArray(d) ? d : [d];
      for (const ev of evs) {
        if (ev && ev['@type'] === 'Event' && ev.startDate) {
          startDate = ev.startDate; break;
        }
      }
      if (startDate) break;
    }
  } catch (e) {}

  // Organizer / host name (community subdomain is also a signal)
  const host = location.hostname;
  return { title, isFree, minPrice, closedKw, openKw, startDate, host };
})()
"""


async def probe_event(browser: zd.Browser, url: str, settle_s: float = 4.0) -> dict | None:
    tab = await browser.get(url)
    # KKTIX uses AngularJS for the registration widget — give it time to render
    await asyncio.sleep(settle_s)
    try:
        info = await tab.evaluate(PROBE_JS)
    except Exception as exc:
        print(f"  ! evaluate failed: {exc}")
        return None
    if not isinstance(info, dict):
        return None
    info["url"] = url
    return info


# -- filtering & output -------------------------------------------------------


def _days_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        cleaned = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).days
    except Exception:
        return None


def filter_and_rank(results: list[dict], max_price: int, days: int,
                    strict_open: bool = False) -> list[dict]:
    """
    strict_open=True  → only events with explicit "立即報名" / "報名中" pass
    strict_open=False → also accept events with neither open NOR closed keyword
                        (likely "advance registration" / not yet on sale)
    """
    out = []
    for r in results:
        if r.get("closedKw"):
            continue
        if strict_open and not r.get("openKw"):
            continue
        price = 0 if r.get("isFree") else (r.get("minPrice") or 0)
        if max_price == 0 and not r.get("isFree"):
            continue
        if price > max_price:
            continue
        # Date filter: events older than --days in the past are stale.
        # Events with "立即報名" still showing pass even if startDate is past
        # (some are recurring / open-ended workshops).
        d = _days_until(r.get("startDate"))
        if d is not None and d > days:
            continue
        if d is not None and d < -days and not r.get("openKw"):
            continue
        r["price"] = price
        r["days_until"] = d if d is not None else 999
        r["open_signal"] = "explicit" if r.get("openKw") else "implied"
        out.append(r)
    # Rank priority:
    #   1. future events with explicit "立即報名" (best)
    #   2. future events with implied-open
    #   3. undated open-ended (workshops, series)
    #   4. recent past events that still answer (degraded — likely just
    #      a stale listing, less useful for practice)
    def _bucket(r):
        d = r["days_until"]
        explicit = r["open_signal"] == "explicit"
        if d == 999:
            return 2 if not explicit else 1  # undated, prefer explicit
        if d >= 0:
            return 0 if explicit else 1
        return 3  # past
    out.sort(key=lambda r: (_bucket(r), r["price"], abs(r["days_until"])))
    return out


def print_markdown(rows: list[dict]) -> None:
    print()
    print(f"## {len(rows)} suitable KKTIX events for Level-1 dry-run practice")
    print()
    if not rows:
        print("_No candidates found. Try relaxing --max-price or --days._")
        return
    print("| When | Price | Open | Host | Title | URL |")
    print("|---|---:|---|---|---|---|")
    for r in rows:
        title = (r.get("title") or "(no title)").replace("|", "/").strip()[:48]
        price = "FREE" if r["price"] == 0 else f"${r['price']}"
        d = r["days_until"]
        if d == 999:
            when = "open-ended"
        elif d > 0:
            when = f"+{d}d"
        elif d == 0:
            when = "today"
        else:
            when = f"{d}d (past)"
        host = (r.get("host") or "").replace("www.", "")
        signal = "✓" if r.get("open_signal") == "explicit" else "?"
        print(f"| {when} | {price} | {signal} | {host} | {title} | {r['url']} |")
    print()
    print("**Manual verification required.** Open ≥ 1 URL to confirm registration")
    print("is actually open before pointing tickets_hunter at it. Past events may")
    print("still appear if the listing is stale; `Open=?` means the AngularJS")
    print("widget didn't render in time — could be either open or closed.")
    print()


# -- main ---------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Find KKTIX events suitable for tickets_hunter dry-run practice",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--limit", type=int, default=25,
                   help="max events to probe (default 25)")
    p.add_argument("--max-price", type=int, default=0,
                   help="max price NTD; 0 = FREE only (default 0)")
    p.add_argument("--days", type=int, default=30,
                   help="event must start within N days (default 30)")
    p.add_argument("--headless", action="store_true",
                   help="run browser headless (faster, but harder to debug)")
    p.add_argument("--throttle", type=float, default=2.0,
                   help="sleep seconds between event probes (default 2.0, "
                        "do not lower for KKTIX)")
    p.add_argument("--settle", type=float, default=4.0,
                   help="seconds to wait after each page load for AngularJS "
                        "to render the register widget (default 4.0)")
    p.add_argument("--strict-open", action="store_true",
                   help="require explicit '立即報名'/'報名中' text; otherwise "
                        "accept events that lack any closed-keyword")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="print raw probe results for each event")
    p.add_argument("--url", type=str, default=None,
                   help="probe a single event URL in detail mode "
                        "(skips the listing scan)")
    return p.parse_args()


async def detail_probe(browser: zd.Browser, url: str, settle_s: float = 8.0) -> None:
    print(f"\n[detail] {url}")
    print(f"[detail] waiting {settle_s}s for Cloudflare + AngularJS...")
    tab = await browser.get(url)
    await asyncio.sleep(settle_s)
    info = await tab.evaluate(DETAIL_PROBE_JS)
    # Also grab a snippet of the visible body text so we can see what state
    # the page is actually in (pre-sale countdown / sold out / etc.)
    body_snippet = await tab.evaluate(
        "(() => (document.body && document.body.innerText || '').slice(0, 1500))()"
    )
    if not isinstance(info, dict):
        print("[error] probe returned no data — page may not have loaded")
        if body_snippet:
            print("--- body snippet ---")
            print(body_snippet)
        return

    print()
    print(f"## {info.get('title') or '(no title)'}")
    print(f"- Host:        {info.get('host')}")
    print(f"- Starts:      {info.get('startDate') or '(no schema.org date)'}")
    print(f"- Login req:   {info.get('needLogin')}")
    print(f"- Image CAPTCHA: {info.get('hasCaptcha')}")
    print(f"- CF Turnstile: {info.get('hasTurnstile')}")
    print(f"- Member code field: {info.get('hasMemberCode')}")
    print()

    tiers = info.get("tiers") or []
    if tiers:
        print(f"### Ticket tiers ({len(tiers)})")
        for i, t in enumerate(tiers, 1):
            price = "FREE" if t["price"] == 0 else (
                f"${t['price']}" if t["price"] else "?"
            )
            remain = t["remaining"] if t["remaining"] is not None else "?"
            flag = " [SOLD OUT]" if t["soldOut"] else ""
            print(f"  {i}. {price}  remaining={remain}{flag}")
            print(f"     {t['text']}")
    else:
        print("### Ticket tiers: none found")
        print("    (Either registration not yet open, ended, or selector miss.)")

    if body_snippet:
        print("\n### Visible page text (first 1500 chars)")
        print("-" * 60)
        print(body_snippet)
        print("-" * 60)

    qs = info.get("questions") or []
    if qs:
        print(f"\n### Possible verify questions ({len(qs)})")
        for q in qs:
            print(f"  • {q}")
    else:
        print("\n### Verify questions: none detected on initial form")
        print("    (May appear after selecting a ticket tier.)")

    print()
    print("---")
    print("### tickets_hunter settings.html checklist")
    print(f"  • Platform:         KKTIX")
    print(f"  • Target URL:       {url.rsplit('/registrations/new', 1)[0]}")
    print(f"  • Keyword (票種):    pick a phrase that matches the tier you want from above")
    print(f"  • Login:            store KKTIX cookie via noVNC (login once → settings saves)")
    if info.get('hasMemberCode'):
        print(f"  • Member code:      REQUIRED — fill 'kktix_member_code' in settings")
    if info.get('hasCaptcha'):
        print(f"  • CAPTCHA OCR:      enable ddddocr")
    if qs:
        print(f"  • User Guess Strings: prepare answer pool for the questions above")
    print(f"  • HUNTER_DRY_RUN:   already set to '1' in compose — won't actually buy")


async def main_async(args: argparse.Namespace) -> int:
    browser = await open_browser(args.headless)
    try:
        if args.url:
            await detail_probe(browser, args.url, settle_s=max(args.settle, 8.0))
            return 0

        urls = await fetch_event_links(browser, args.limit)
        if not urls:
            print("[error] no event links discovered — Cloudflare may have blocked.")
            print("        Try --headless removed so you can solve any challenge "
                  "interactively via noVNC: http://localhost:6080/vnc.html")
            return 2

        results: list[dict] = []
        for i, u in enumerate(urls, 1):
            print(f"[probe {i}/{len(urls)}] {u}")
            info = await probe_event(browser, u, settle_s=args.settle)
            if info:
                results.append(info)
                if args.verbose:
                    flags = (
                        f"free={info.get('isFree')} "
                        f"price={info.get('minPrice')} "
                        f"open={info.get('openKw')} "
                        f"closed={info.get('closedKw')} "
                        f"start={info.get('startDate')}"
                    )
                    print(f"        {flags}")
            await asyncio.sleep(args.throttle)

        ranked = filter_and_rank(
            results, args.max_price, args.days,
            strict_open=args.strict_open,
        )
        print_markdown(ranked)
        return 0
    finally:
        try:
            await browser.stop()
        except Exception:
            pass


def main() -> int:
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
