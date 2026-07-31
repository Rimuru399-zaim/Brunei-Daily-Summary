#!/usr/bin/env python3
"""The Brunei Dispatch — daily broadsheet builder.

Reads sources.yaml, gathers a day of RSS, has Gemini select and write the
edition, verifies the prose against the source text, and renders template.html.
See BRIEF.md for the full specification. template.html is not to be edited.

Modes:
  python build.py                 real build -> docs/index.html + archive
  python build.py --check-feeds   fetch every feed, report, exit!=0 if any dead
  python build.py --dry-run       build from live feeds WITHOUT Gemini, to a
                                  local preview file (no docs/ write)
  python build.py --fixtures      render the template from hardcoded sample
                                  content, no network at all
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import httpx
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent
TEMPLATE = "template.html"
SOURCES_FILE = ROOT / "sources.yaml"
STATE_FILE = ROOT / "state.json"
DOCS = ROOT / "docs"
ARCHIVE = DOCS / "archive"

MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
REPO_URL = os.environ.get("REPO_URL", "https://github.com/")
READER_NAME = os.environ.get("READER_NAME", "the morning reader")

USER_AGENT = (
    "BruneiDispatchBot/1.0 (+https://github.com/; static daily digest; "
    "contact via repo issues)"
)

# How far back a story may be to count as fresh.
MAX_AGE = dt.timedelta(hours=36)

# How stale the last successful build may be before the red banner shows.
STALE_AFTER = dt.timedelta(hours=48)

# Section quotas (see BRIEF §5). These are ceilings; fewer is fine.
QUOTA = {"borneo": 6, "sea": 2, "tech": 3, "briefs": 5}

BRUNEI_TZ = dt.timezone(dt.timedelta(hours=8))

# Watchlist via Yahoo Finance chart API (no key): one global-equity ETF plus
# three tech names. Stooq was dropped after it began gating the CSV endpoint
# behind a JavaScript bot-wall (2026-07); if Yahoo follows suit, the markets
# strip is simply omitted (BRIEF §6) — never carry forward or invent figures.
# Note VWRP.L is priced in GBP and the tech names in USD; the tiles show the
# raw last price with no currency symbol, same as the original design.
MARKET_SYMBOLS = [
    ("VWRP.L", "VWRP"),
    ("MRVL", "MRVL"),
    ("AMD", "AMD"),
    ("NET", "Cloudflare"),
]

# Bandar Seri Begawan
WEATHER_LAT, WEATHER_LON = 4.9031, 114.9398


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# Feeds
# --------------------------------------------------------------------------- #


def load_sources() -> list[dict]:
    data = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
    feeds = data.get("feeds", [])
    for f in feeds:
        if not all(k in f for k in ("name", "beat", "url")):
            raise ValueError(f"feed entry missing name/beat/url: {f!r}")
    return feeds


def fetch_url(url: str, client: httpx.Client) -> bytes | None:
    try:
        r = client.get(url, follow_redirects=True, timeout=25)
        r.raise_for_status()
        return r.content
    except Exception as e:  # noqa: BLE001 — report and move on
        log(f"    ! fetch failed: {e}")
        return None


def parse_feed(raw: bytes):
    return feedparser.parse(raw)


def entry_time(entry) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)
    return None


def strip_html(s: str) -> str:
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def entry_body(entry) -> str:
    """Prefer full content:encoded over the truncated summary (BRIEF §5)."""
    if entry.get("content"):
        blocks = [c.get("value", "") for c in entry["content"]]
        text = " ".join(blocks)
        if text.strip():
            return strip_html(text)
    return strip_html(entry.get("summary", "") or entry.get("description", ""))


# --- polite full-article enrichment ----------------------------------------- #

_ROBOTS: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def robots_allows(url: str, client: httpx.Client) -> bool:
    host = urlparse(url)._replace(path="", params="", query="", fragment="")
    base = host.geturl()
    if base not in _ROBOTS:
        rp = urllib.robotparser.RobotFileParser()
        raw = fetch_url(base + "/robots.txt", client)
        if raw is None:
            _ROBOTS[base] = None  # no robots.txt -> allowed
        else:
            rp.parse(raw.decode("utf-8", "replace").splitlines())
            _ROBOTS[base] = rp
    rp = _ROBOTS[base]
    if rp is None:
        return True
    return rp.can_fetch(USER_AGENT, url)


def enrich_body(item: dict, client: httpx.Client) -> None:
    """If the RSS body is thin, politely fetch the article and extract text.

    Respects robots.txt. Never bypasses paywalls; on any doubt keeps the
    summary. A short honest story beats a padded one (BRIEF §5).
    """
    if len(item["body"]) >= 600:
        return
    url = item["url"]
    try:
        if not robots_allows(url, client):
            log(f"    · robots.txt disallows {url} — keeping summary")
            return
    except Exception:
        return
    raw = fetch_url(url, client)
    if not raw:
        return
    text = strip_html(raw.decode("utf-8", "replace"))
    # crude but safe: only adopt if it plausibly grew the body
    if len(text) > len(item["body"]) * 1.5:
        item["body"] = text[:8000]


# --------------------------------------------------------------------------- #
# Gather + dedup
# --------------------------------------------------------------------------- #


def gather(feeds: list[dict], client: httpx.Client, enrich: bool) -> list[dict]:
    now = dt.datetime.now(dt.timezone.utc)
    items: list[dict] = []
    for f in feeds:
        log(f"  - {f['name']} [{f['beat']}]")
        raw = fetch_url(f["url"], client)
        if not raw:
            continue
        parsed = parse_feed(raw)
        for e in parsed.entries:
            when = entry_time(e)
            if when and (now - when) > MAX_AGE:
                continue
            title = strip_html(e.get("title", "")).strip()
            link = e.get("link", "").strip()
            if not title or not link:
                continue
            items.append(
                {
                    "beat": f["beat"],
                    "source": f["name"],
                    "url": link,
                    "headline": title,
                    "summary": strip_html(e.get("summary", "") or "")[:600],
                    "body": entry_body(e),
                    "when": when,
                }
            )
    log(f"  gathered {len(items)} items within {MAX_AGE}")
    items = dedup(items)
    log(f"  {len(items)} after dedup")
    if enrich:
        for it in items:
            enrich_body(it, client)
    return items


def _norm_title(t: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", t.lower())
    return {w for w in words if len(w) > 2}


def dedup(items: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for it in items:
        toks = _norm_title(it["headline"])
        dup_of = None
        for k in kept:
            ktoks = _norm_title(k["headline"])
            if not toks or not ktoks:
                continue
            j = len(toks & ktoks) / len(toks | ktoks)
            if j >= 0.6:
                dup_of = k
                break
        if dup_of is None:
            kept.append(it)
        elif len(it["body"]) > len(dup_of["body"]):
            # keep the richer version
            kept[kept.index(dup_of)] = it
    return kept


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #

WRITE_RULES = (
    "You may only compress, rephrase, and connect the material given to you. "
    "You may not add any fact, figure, name, date, or quotation that does not "
    "appear verbatim in the supplied source text. If the supplied material is "
    "too thin to support the requested length, write fewer paragraphs and stop. "
    "Never estimate a number. Never infer what someone probably said. Writing "
    "three honest sentences is a success; writing five padded ones is a failure. "
    "Restrained broadsheet news style, third person, no hype, no bullet points, "
    "no emoji."
)


def gemini_client():
    from google import genai  # imported lazily so --fixtures needs no key

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY is not set.")
    return genai.Client(api_key=key)


def gemini_json(client, prompt: str, system: str | None = None) -> dict | list:
    from google.genai import types

    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        system_instruction=system,
        temperature=0.4,
    )
    last = None
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model=MODEL, contents=prompt, config=cfg
            )
            return json.loads(resp.text)
        except Exception as e:  # noqa: BLE001
            last = e
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                wait = 20 * (attempt + 1)
                log(f"    429 from Gemini, backing off {wait}s")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Gemini failed after retries: {last}")


def call_select(client, items: list[dict]) -> dict:
    catalogue = [
        {
            "id": i,
            "beat": it["beat"],
            "source": it["source"],
            "headline": it["headline"],
            "summary": it["summary"][:300],
            "when": it["when"].isoformat() if it["when"] else "",
        }
        for i, it in enumerate(items)
    ]
    system = (
        "You are the section editor of a broadsheet morning paper covering "
        "Brunei, Southeast Asia, and the wider world. You SELECT "
        "stories from a supplied list. You do not write anything yet."
    )
    prompt = (
        "From the numbered candidates below choose the day's edition. Return "
        "STRICT JSON with these keys, each value a list of objects "
        '{"id": <int>, "reason": "<one line>"}:\n'
        f'  "lead"   : exactly 1 (the single most important story)\n'
        f'  "borneo" : up to {QUOTA["borneo"]} (Brunei stories — this fills the '
        f'"Brunei" section; prefer the brunei beat)\n'
        f'  "sea"    : up to {QUOTA["sea"]} (Southeast Asia)\n'
        f'  "tech"   : up to {QUOTA["tech"]} (technology and business / world)\n'
        f'  "briefs" : up to {QUOTA["briefs"]} short one-line world items\n'
        "Every id must come from the list. Do not reuse an id across lead/"
        "borneo/sea/tech. Prefer the beat that matches each section.\n\n"
        f"CANDIDATES:\n{json.dumps(catalogue, ensure_ascii=False)}"
    )
    return gemini_json(client, prompt, system)


def call_lead(client, item: dict) -> dict:
    system = WRITE_RULES
    prompt = (
        "Write the lead story of the paper as STRICT JSON with keys: "
        '"kicker" (<=6 words, uppercase topic label), "headline", '
        '"deck" (one italic sentence), "byline" (e.g. "By the Dispatch Desk"), '
        '"paras" (a list of 5-6 short paragraphs, FEWER if the source is thin).\n'
        "Obey the writing rules exactly. Use only the source text below.\n\n"
        f"SOURCE HEADLINE: {item['headline']}\n"
        f"SOURCE OUTLET: {item['source']}\n"
        f"SOURCE TEXT:\n{item['body'] or item['summary']}"
    )
    return gemini_json(client, prompt, system)


def call_cards(client, group: list[dict]) -> list[dict]:
    """One or two short paragraphs per story, in one call for the group."""
    if not group:
        return []
    system = WRITE_RULES
    payload = [
        {
            "id": i,
            "headline": it["headline"],
            "source": it["source"],
            "text": (it["body"] or it["summary"])[:4000],
        }
        for i, it in enumerate(group)
    ]
    prompt = (
        "For each story below write one or two short paragraphs. Return STRICT "
        'JSON: a list of {"id": <int>, "paras": ["...", ...]}. Keep the id. '
        "Obey the writing rules exactly; use only that story's own text. If a "
        "story's text is too thin, write a single sentence.\n\n"
        f"STORIES:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    out = gemini_json(client, prompt, system)
    return out if isinstance(out, list) else out.get("stories", [])


# --------------------------------------------------------------------------- #
# Hallucination verification (BRIEF §5)
# --------------------------------------------------------------------------- #

_NUM_RE = re.compile(r"\d[\d,\.]*")
_QUOTE_RE = re.compile(r"[\"“”‘’']([^\"“”]{6,}?)[\"“”‘’']")


def _num_cores(text: str) -> set[str]:
    cores = set()
    for m in _NUM_RE.findall(text):
        core = m.replace(",", "").rstrip(".")
        if core:
            cores.add(core)
    return cores


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_story(generated_paras: list[str], source_text: str) -> tuple[bool, str]:
    """Return (ok, reason). Enforces the number and quote rules mechanically."""
    gen = " ".join(generated_paras)
    src = source_text
    src_nums = _num_cores(src)
    for core in _num_cores(gen):
        if core in src_nums:
            continue
        # allow numbers that appear as a substring of a source number/word run
        if any(core in s for s in src_nums):
            continue
        return False, f"figure not in source: {core}"
    src_norm = _norm_ws(src)
    for q in _QUOTE_RE.findall(gen):
        if _norm_ws(q) not in src_norm:
            return False, f"quotation not in source: {q[:40]!r}"
    return True, ""


# --------------------------------------------------------------------------- #
# Markets + weather
# --------------------------------------------------------------------------- #


def fetch_market(symbol: str, name: str, client: httpx.Client) -> dict | None:
    """Last price and prior-session change from the Yahoo Finance chart API.

    The change is computed from the last two daily closes, NOT the meta field
    `chartPreviousClose` — that field reports the close from before the whole
    range window, which inflated (and even reversed) the daily move.
    """
    from urllib.parse import quote

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}"
        "?interval=1d&range=7d"  # 7d so two trading days survive holidays
    )
    raw = fetch_url(url, client)
    if not raw:
        return None
    try:
        result = json.loads(raw)["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"]
                  if c is not None]
        if len(closes) < 2:
            return None
        prev = float(closes[-2])
        last = float(result["meta"].get("regularMarketPrice") or closes[-1])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None
    change = last - prev
    pct = (change / prev * 100) if prev else 0.0
    return {
        "name": name,
        "value": f"{last:,.2f}",
        "dir": "up" if change >= 0 else "dn",
        "change": f"{abs(change):,.2f} ({abs(pct):.2f}%)",
    }


def fetch_markets(client: httpx.Client) -> tuple[list[dict], str | None]:
    out = []
    for sym, name in MARKET_SYMBOLS:
        m = fetch_market(sym, name, client)
        if m is None:
            log(f"    ! market fetch failed: {name} -- omitting markets strip")
            return [], None  # all-or-nothing; never a partial/stale strip
        out.append(m)
    note = f"Quotes via Yahoo Finance · {dt.date.today():%d %b %Y}"
    return out, note


_WMO = {
    0: "clear skies", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    80: "rain showers", 81: "rain showers", 82: "violent rain showers",
    95: "thunderstorms", 96: "thunderstorms with hail",
}


def fetch_weather(client: httpx.Client) -> str | None:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
        "&timezone=Asia/Brunei"
    )
    raw = fetch_url(url, client)
    if not raw:
        return None
    try:
        cur = json.loads(raw)["current"]
        temp = round(cur["temperature_2m"])
        hum = round(cur["relative_humidity_2m"])
        desc = _WMO.get(int(cur["weather_code"]), "changeable")
        wind = round(cur["wind_speed_10m"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return None
    return (
        f"Bandar Seri Begawan: {desc}, {temp}°C, humidity {hum}%, "
        f"wind {wind} km/h."
    )


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"issue_no": 0, "volume": 1, "last_success": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render(context: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(ROOT)),
        autoescape=select_autoescape(["html"]),
    )
    return env.get_template(TEMPLATE).render(**context)


def base_context(state: dict, is_stale: bool, build_stamp: str) -> dict:
    now = dt.datetime.now(BRUNEI_TZ)
    return {
        "date_short": now.strftime("%d %b %Y"),
        "date_long": now.strftime("%A, %d %B %Y"),
        "volume": state.get("volume", 1),
        "issue_no": state.get("issue_no", 0),
        "reader_name": READER_NAME,
        "is_stale": is_stale,
        "build_stamp": build_stamp,
        "repo_url": REPO_URL,
        "lead": None,
        "world_briefs": [],
        "weather": None,
        "region_today": [],
        "borneo": [],
        "sea": [],
        "tech": [],
        "markets": [],
        "markets_note": None,
    }


def card(item: dict, paras: list[str], tag: str) -> dict:
    return {
        "tag": tag,
        "url": item["url"],
        "headline": item["headline"],
        "paras": paras,
        "source": item["source"].upper(),
    }


TAGS = {"brunei": "Brunei", "borneo": "Borneo", "sea": "ASEAN",
        "world": "World", "tech": "Technology"}


# --------------------------------------------------------------------------- #
# Build orchestration
# --------------------------------------------------------------------------- #


def build_edition(items: list[dict], state: dict, is_stale: bool) -> dict:
    client_ai = gemini_client()
    stamp = dt.datetime.now(BRUNEI_TZ).strftime("%d %b %Y %H:%M %Z")
    ctx = base_context(state, is_stale, stamp)

    log("  Gemini call 1 — selection")
    sel = call_select(client_ai, items)

    def pick(key):
        out = []
        for entry in sel.get(key, []):
            i = entry.get("id")
            if isinstance(i, int) and 0 <= i < len(items):
                out.append(items[i])
        return out

    lead_items = pick("lead")
    if not lead_items:
        raise RuntimeError("selection returned no lead")
    lead_item = lead_items[0]

    log("  Gemini call 2 — the lead")
    lead = call_lead(client_ai, lead_item)
    ok, why = verify_story(
        [lead.get("headline", ""), lead.get("deck", ""), *lead.get("paras", [])],
        lead_item["body"] or lead_item["summary"],
    )
    if not ok:
        raise RuntimeError(f"lead failed verification ({why}); refusing to publish")
    ctx["lead"] = {
        "kicker": lead.get("kicker", TAGS.get(lead_item["beat"], "News")),
        "headline": lead.get("headline", lead_item["headline"]),
        "deck": lead.get("deck", ""),
        "byline": lead.get("byline", "By the Dispatch Desk"),
        "paras": lead.get("paras", []),
        "sources": [{"url": lead_item["url"], "name": lead_item["source"]}],
    }

    # Cards: borneo (3-wide), sea (2-wide), tech (3-wide)
    used_ids = {id(lead_item)}
    for key in ("borneo", "sea", "tech"):
        group = [it for it in pick(key) if id(it) not in used_ids]
        for it in group:
            used_ids.add(id(it))
        if not group:
            log(f"  section '{key}' has no stories")
            continue
        log(f"  Gemini call 3 — cards for {key} ({len(group)})")
        written = call_cards(client_ai, group)
        by_id = {w.get("id"): w.get("paras", []) for w in written}
        cards = []
        for i, it in enumerate(group):
            paras = by_id.get(i, [])
            ok, why = verify_story(paras, it["body"] or it["summary"])
            if not ok:
                log(f"    DROP '{it['headline'][:60]}' — {why}")
                continue
            if not paras:
                log(f"    DROP '{it['headline'][:60]}' — no prose returned")
                continue
            cards.append(card(it, paras, TAGS.get(it["beat"], "News")))
        ctx[key] = cards

    # World in Brief — one line each, verified.
    briefs = []
    for it in pick("briefs"):
        if id(it) in used_ids:
            continue
        line = it["summary"] or it["headline"]
        ok, _ = verify_story([line], it["body"] or it["summary"])
        if ok:
            briefs.append({"lead_in": it["source"] + ":", "text": line[:180]})
    ctx["world_briefs"] = briefs[: QUOTA["briefs"]]

    return ctx


def attach_data(ctx: dict, client: httpx.Client) -> None:
    markets, note = fetch_markets(client)
    ctx["markets"] = markets
    ctx["markets_note"] = note
    ctx["weather"] = fetch_weather(client)


# --------------------------------------------------------------------------- #
# Validation + publish
# --------------------------------------------------------------------------- #


def validate_html(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if "<h2>" not in text or "The Brunei Dispatch" not in text:
        raise RuntimeError("rendered page missing lead headline / masthead")


def publish(ctx: dict, state: dict) -> None:
    DOCS.mkdir(exist_ok=True)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    tmp = DOCS / "index.tmp.html"
    tmp.write_text(render(ctx), encoding="utf-8")
    validate_html(tmp)  # never publish a half-built page (BRIEF §7)

    dated = ARCHIVE / f"{dt.date.today():%Y-%m-%d}.html"
    final = DOCS / "index.html"
    tmp.replace(final)
    final_text = final.read_text(encoding="utf-8")
    dated.write_text(final_text, encoding="utf-8")

    state["issue_no"] = state.get("issue_no", 0) + 1
    state["last_success"] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_state(state)
    log(f"  published issue No. {state['issue_no']} -> {final}")


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #


def mode_check_feeds() -> int:
    feeds = load_sources()
    dead = []
    now = dt.datetime.now(dt.timezone.utc)
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        for f in feeds:
            raw = fetch_url(f["url"], client)
            if not raw:
                dead.append(f["name"])
                log(f"DEAD  {f['name']:<24} {f['url']}")
                continue
            parsed = parse_feed(raw)
            n = len(parsed.entries)
            newest = None
            for e in parsed.entries:
                t = entry_time(e)
                if t and (newest is None or t > newest):
                    newest = t
            age = f"{(now - newest).days}d ago" if newest else "no date"
            status = "OK  " if n else "EMPTY"
            if not n:
                dead.append(f["name"])
            log(f"{status}  {f['name']:<24} {n:>3} items  newest {age}")
    if dead:
        log(f"\n{len(dead)} feed(s) dead/empty: {', '.join(dead)}")
        return 1
    log("\nall feeds OK")
    return 0


def mode_fixtures() -> int:
    state = load_state()
    ctx = base_context(state, is_stale=False,
                       build_stamp="fixtures (no network)")
    ctx["issue_no"] = ctx["issue_no"] or 42
    ctx["lead"] = {
        "kicker": "Energy & Economy",
        "headline": "Brunei Signs Framework to Widen Regional Gas Cooperation",
        "deck": "Officials describe the accord as a step toward steadier "
                "cross-border supply.",
        "byline": "By the Dispatch Desk",
        "paras": [
            "Brunei has signed a framework agreement intended to broaden "
            "cooperation on natural gas across the region, according to a "
            "statement issued after the signing.",
            "The agreement sets out areas for future collaboration but leaves "
            "specific commitments to later negotiation, the statement said.",
            "Officials present at the ceremony described the accord as a step "
            "toward steadier cross-border supply, without giving figures.",
            "No timeline was announced for the next stage of talks.",
        ],
        "sources": [{"url": "https://example.com/a", "name": "Borneo Bulletin"}],
    }
    ctx["world_briefs"] = [
        {"lead_in": "Reuters:", "text": "Talks continued on a regional trade "
         "arrangement, officials said."},
        {"lead_in": "AP:", "text": "A summit communique was released without a "
         "joint declaration."},
    ]
    ctx["weather"] = ("Bandar Seri Begawan: partly cloudy, 31°C, humidity "
                      "78%, wind 9 km/h.")
    ctx["region_today"] = [
        {"date": "1965", "text": "Sample historical note for layout only."},
    ]
    sample_paras = [
        "A sample paragraph used to check the column layout and rules.",
        "A second short paragraph completes the card.",
    ]
    ctx["borneo"] = [  # the "Brunei" section
        card({"url": "https://example.com/1", "headline":
              f"Sample Brunei Story Number {i}",
              "source": "Borneo Bulletin"}, sample_paras, "Brunei")
        for i in range(1, 7)
    ]
    ctx["sea"] = [
        card({"url": "https://example.com/s", "headline":
              f"Sample Southeast Asia Story {i}", "source": "CNA"},
             sample_paras, "ASEAN")
        for i in range(1, 3)
    ]
    ctx["tech"] = [
        card({"url": "https://example.com/t", "headline":
              f"Sample Technology Story {i}", "source": "Ars Technica"},
             sample_paras, "Technology")
        for i in range(1, 4)
    ]
    ctx["markets"] = [
        {"name": "VWRP", "value": "140.54", "dir": "up",
         "change": "0.82 (0.59%)"},
        {"name": "MRVL", "value": "183.30", "dir": "dn",
         "change": "2.10 (1.13%)"},
        {"name": "AMD", "value": "485.39", "dir": "up",
         "change": "6.44 (1.34%)"},
        {"name": "Cloudflare", "value": "283.39", "dir": "dn",
         "change": "1.90 (0.67%)"},
    ]
    ctx["markets_note"] = "Sample figures for layout verification only."
    out = ROOT / "preview-fixtures.html"
    out.write_text(render(ctx), encoding="utf-8")
    log(f"wrote {out}")
    return 0


def mode_dry_run() -> int:
    """Build from live feeds but WITHOUT Gemini — deterministic fallback editor.

    Uses RSS summaries as the prose so the layout can be checked with real
    material. Writes a local preview; does not touch docs/.
    """
    feeds = load_sources()
    state = load_state()
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        items = gather(feeds, client, enrich=False)
        ctx = base_context(state, is_stale=False, build_stamp="dry-run (no AI)")
        attach_data(ctx, client)

    def take(beats, n):
        pool = [it for it in items if it["beat"] in beats]
        pool.sort(key=lambda it: it["when"] or dt.datetime.min.replace(
            tzinfo=dt.timezone.utc), reverse=True)
        return pool[:n]

    lead_pool = take({"brunei", "world", "sea", "tech"}, 1)
    if lead_pool:
        it = lead_pool[0]
        ctx["lead"] = {
            "kicker": TAGS.get(it["beat"], "News"),
            "headline": it["headline"],
            "deck": it["summary"][:140],
            "byline": "By the Dispatch Desk (dry-run)",
            "paras": [it["summary"] or it["headline"]],
            "sources": [{"url": it["url"], "name": it["source"]}],
        }
        used = {id(it)}
    else:
        used = set()

    def cards_for(beats, n, tag_beat=None):
        out = []
        for it in take(beats, n + 3):
            if id(it) in used:
                continue
            used.add(id(it))
            out.append(card(it, [it["summary"] or it["headline"]],
                            TAGS.get(it["beat"], "News")))
            if len(out) >= n:
                break
        return out

    ctx["borneo"] = cards_for({"brunei"}, QUOTA["borneo"])  # "Brunei" section
    ctx["sea"] = cards_for({"sea"}, QUOTA["sea"])
    ctx["tech"] = cards_for({"tech", "world"}, QUOTA["tech"])
    out = ROOT / "preview-dryrun.html"
    out.write_text(render(ctx), encoding="utf-8")
    log(f"wrote {out}")
    return 0


def mode_build() -> int:
    feeds = load_sources()
    state = load_state()

    # Staleness is about the PREVIOUS success; compute before we publish.
    last = state.get("last_success")
    is_stale = False
    if last:
        try:
            prev = dt.datetime.fromisoformat(last)
            is_stale = (dt.datetime.now(dt.timezone.utc) - prev) > STALE_AFTER
        except ValueError:
            pass

    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        items = gather(feeds, client, enrich=True)
        if not items:
            raise RuntimeError("no fresh items gathered; refusing to publish")
        ctx = build_edition(items, state, is_stale)
        attach_data(ctx, client)

    publish(ctx, state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build The Brunei Dispatch.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check-feeds", action="store_true",
                   help="fetch every feed, report, exit!=0 if any dead")
    g.add_argument("--dry-run", action="store_true",
                   help="build from live feeds without Gemini, to a preview")
    g.add_argument("--fixtures", action="store_true",
                   help="render the template from sample content, no network")
    args = ap.parse_args()

    if args.check_feeds:
        return mode_check_feeds()
    if args.fixtures:
        return mode_fixtures()
    if args.dry_run:
        return mode_dry_run()
    return mode_build()


if __name__ == "__main__":
    sys.exit(main())
