# Build brief — The Borneo Dispatch (self-hosted daily edition)

Paste this whole file into Claude Code. `template.html` sits next to it in the same folder.

---

## 0. Read this first

`template.html` is the design and it is **not up for redesign**. It is an existing,
finished broadsheet layout. Your job is to build the machinery that fills it in.

- Do **not** rewrite the CSS.
- Do **not** "improve", modernise, or restructure the markup.
- Do **not** swap the fonts, colours, or column rules.
- Do **not** add a CSS framework.

If you think something in the template is wrong, leave it alone and mention it at
the end instead. The whole point of this project is that the output looks exactly
like the template.

---

## 1. What we're building

A static page, rebuilt once every morning, that reads like a broadsheet newspaper
covering four beats:

1. **Brunei** — royal/government announcements, economy and energy, public services, community
2. **Borneo** — Sabah, Sarawak, and Kalimantan
3. **Southeast Asia** — ASEAN politics and diplomacy
4. **World, plus technology and business**

It publishes to GitHub Pages. It costs nothing to run.

---

## 2. Stack

- **Python 3.11+**
- `feedparser` — RSS ingestion
- `jinja2` — templating
- `httpx` — fetching
- `google-genai` — Gemini (the official SDK; do not use the deprecated `google-generativeai`)
- **GitHub Actions** on a cron schedule
- **GitHub Pages** for hosting

Keep the dependency list to roughly this. No database, no web framework, no build
tooling beyond `pip install -r requirements.txt`.

Repo layout:

```
/build.py            entry point
/sources.yaml        feed list, editable by hand
/template.html       DO NOT EDIT
/requirements.txt
/docs/index.html     build output — GitHub Pages serves from /docs
/docs/archive/       one dated copy per edition
/state.json          issue number, last successful build timestamp
/.github/workflows/build.yml
```

**Make the repo public.** Public repos get unlimited free GitHub Actions minutes on
standard runners; private repos are capped at 2,000 Linux minutes a month. Nothing
secret lives in this repo except the API key, which goes in Actions Secrets, not
in git.

---

## 3. Sources

Put these in `sources.yaml` with a `beat` label on each. **Verify every feed URL
actually resolves and returns items before committing it** — several of these are
guesses based on common CMS conventions, and a feed that 404s should be removed
from the file rather than left in to fail every morning.

Candidates to check:

| Outlet | Beat | Likely feed |
|---|---|---|
| Borneo Bulletin | brunei | `https://borneobulletin.com.bn/feed/` |
| The Scoop | brunei | `https://thescoop.co/feed/` |
| Borneo Post | borneo | `https://www.theborneopost.com/feed/` |
| Daily Express (Sabah) | borneo | `https://www.dailyexpress.com.my/rss/` |
| Antara News (English) | borneo | `https://en.antaranews.com/rss/top-news.xml` |
| The Star / AseanPlus | sea | `https://www.thestar.com.my/rss/editors-pick` |
| Channel NewsAsia | sea | `https://www.channelnewsasia.com/api/v1/rss-outbound-feed` |
| Al Jazeera | world | `https://www.aljazeera.com/xml/rss/all.xml` |
| Reuters tech / Ars Technica | tech | check |

Write a `python build.py --check-feeds` mode that fetches every feed, reports
item counts and the date of the newest entry, and exits non-zero if any feed is
dead. Run it before the first real build.

If a beat ends up with no working feed, say so plainly rather than padding the
section with material from another beat.

---

## 4. Gemini wiring

Use the free tier. It needs no credit card and allows roughly 15 requests per
minute and 1,500 per day — a single edition uses about a dozen, so the ceiling is
irrelevant.

**Do not hardcode the model name.** Google rotates these and removes old ones from
the free tier; a stale model string is the single most likely thing to break this
project six months from now. Read it from an environment variable:

```python
MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
```

Document in the README that if the build starts failing with a model-not-found
error, the fix is to check Google's current free-tier model list and update the
`GEMINI_MODEL` repo variable — no code change needed.

Handle a 429 by backing off and retrying twice, then degrading gracefully per §6.

---

## 5. The editorial pipeline

For each beat, gather the last 24–36 hours of items, deduplicate near-identical
stories across outlets, and then make three calls to Gemini:

**Call 1 — selection.** Give it the headline, summary, source, and timestamp for
every candidate. Ask it to return JSON: which single story is the lead, which six
go in Brunei & Borneo, which two in Southeast Asia, which three in Technology &
Business. Ask for a one-line reason per pick. It selects from the list; it does
not write anything yet.

**Call 2 — the lead.** Five to six paragraphs, restrained broadsheet news style,
third person, no hype, no bullet points, no emoji. Plus a kicker, an italic deck,
and a byline.

**Call 3 — the cards.** One or two short paragraphs per story.

### The hallucination rule — this is the important part

RSS gives you a headline and often a truncated one-sentence summary. Asking a model
to expand that into five paragraphs is exactly the setup that invents quotes,
figures, and dates. It will produce fluent, authoritative-sounding prose containing
things that never happened, which is worse than no story at all.

So constrain hard, in the system prompt for calls 2 and 3:

> You may only compress, rephrase, and connect the material given to you. You may
> not add any fact, figure, name, date, or quotation that does not appear verbatim
> in the supplied source text. If the supplied material is too thin to support the
> requested length, write fewer paragraphs and stop. Never estimate a number.
> Never infer what someone probably said. Writing three honest sentences is a
> success; writing five padded ones is a failure.

Then verify mechanically after generation, don't just trust it:

- Extract every number, percentage, and currency figure from the generated text.
  If a figure does not appear in that story's source text, drop the story and log it.
- Extract anything in quotation marks. Same rule.
- If the model returns a headline not present in your candidate list, drop it.

Log every drop to the Actions run output so it's visible why a section came up short.

**Prefer full article text over the RSS summary where you can get it politely.**
Many of these feeds include `content:encoded` with the full body — use it when
present. Where it isn't, respect `robots.txt`, identify yourself in the User-Agent,
and if a site disallows crawling, just use the summary and accept shorter stories.
Do not scrape anything that asks not to be scraped, and do not attempt to bypass
paywalls.

### Cards must link out

Every headline is an `<a href>` to the original article, and every card keeps its
small uppercase source credit. This is not optional — it's the thing that makes the
page honest about where the material came from.

---

## 6. Markets and weather

The four markets tiles need real figures. Use a free quotes source (Stooq's CSV
endpoint needs no key and is a reasonable starting point) for an Asian index, two
US indices, and Brent crude. Set `dir` to `up` or `dn` and the template colours it
green or red.

If quotes can't be fetched, **omit the markets strip entirely** — the template
already handles `markets` being empty. Do not carry yesterday's numbers forward
and do not invent them. Same for weather: if there's no current observation for
Brunei, leave `weather` unset and the box disappears.

This is the general rule for the whole project: a missing section is fine, a stale
or fabricated one is not.

---

## 7. Scheduling and failure behaviour

`.github/workflows/build.yml`:

- `on: schedule:` with cron for about 06:00 Brunei time. **Cron in Actions is UTC**,
  so that's `0 22 * * *` the previous day. Brunei is UTC+8 with no daylight saving,
  so this doesn't drift.
- Also `on: workflow_dispatch:` so it can be run by hand from the Actions tab.
- `GEMINI_API_KEY` from `secrets`, `GEMINI_MODEL` from `vars`.
- Commit `docs/index.html`, the dated archive copy, and `state.json` back to the repo.
- Pin actions to a major version and note in the README that these need bumping
  every year or so when GitHub deprecates runner images.

**Staleness.** The template accepts `is_stale` and renders a red banner when true.
Set it if the last successful build was more than 48 hours ago. A page that quietly
serves four-day-old news while looking freshly printed is the worst failure mode
here, and it's the one nobody notices.

**On failure**, let the workflow fail loudly — GitHub emails you. Do not swallow
exceptions to keep the run green. Never publish a half-built page: build to a temp
file, validate it parses and contains a lead headline, then move it into place.

`state.json` holds the issue number — increment by one per successful edition —
and the last-success timestamp.

---

## 8. Local development

`python build.py --dry-run` should build to a local file from cached feed data
without calling Gemini or writing to `docs/`, so the layout can be checked quickly.
Include a `--fixtures` mode that renders the template with hardcoded sample content,
so the design can be verified with no network at all.

Add a `README.md` covering: creating the Gemini key in Google AI Studio, adding it
as an Actions secret, enabling Pages on `/docs`, and the two known breakages
(model name rotation, action version deprecation) with their fixes.

---

## 9. Definition of done

- `--fixtures` renders a page visually identical to `template.html`'s intent
- `--check-feeds` passes on every feed left in `sources.yaml`
- A real build produces a page where every headline links to a live article
- Deliberately feeding a thin summary produces a *short* story, not an invented one
- Killing the network mid-build leaves the previous edition intact
