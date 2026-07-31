# The Brunei Dispatch

A static broadsheet, rebuilt every morning, covering Brunei, Southeast Asia,
and the wider world. It reads RSS, has Gemini select and write the edition
from the source text (and nothing else), renders the fixed `template.html`, and
publishes to GitHub Pages. It costs nothing to run.

The design lives in `template.html` and is **not** edited by the build — the
build only fills it in. See `BRIEF.md` for the full specification.

---

## How it works

```
build.py          entry point (all four modes below)
sources.yaml      feed list, editable by hand
template.html     the design — DO NOT EDIT
requirements.txt
docs/index.html   build output — Pages serves from /docs
docs/archive/     one dated copy per edition
state.json        issue number + last-success timestamp
.github/workflows/build.yml
```

For each beat the build gathers the last ~36 hours of items, de-duplicates
near-identical stories, then makes three Gemini calls: **select** the lineup,
**write** the lead, **write** the cards. Generated prose is then checked
mechanically — every number and quotation must appear in that story's own
source text, or the story is dropped and the drop is logged. Headlines link
out to the original articles.

Markets come from Yahoo Finance (no key); weather from Open-Meteo (no key).
If either can't be fetched, that section is simply omitted — a missing section
is fine, a stale or fabricated one is not.

---

## Commands

```bash
python build.py                 # real build -> docs/index.html + dated archive
python build.py --check-feeds   # fetch every feed, report, exit!=0 if any dead
python build.py --dry-run       # build from live feeds WITHOUT Gemini -> preview
python build.py --fixtures      # render template from sample content, no network
```

Run `--check-feeds` before committing changes to `sources.yaml`. Run
`--fixtures` to eyeball the layout with no network or API key.

---

## First-time setup

1. **Make the repo public.** Public repos get unlimited free Actions minutes on
   standard runners. Nothing secret lives in git — only the API key, which goes
   in Actions Secrets.

2. **Create a Gemini API key** in [Google AI Studio](https://aistudio.google.com/apikey).
   The free tier needs no credit card (~15 requests/min, ~1,500/day; one edition
   uses about a dozen).

3. **Add the key as an Actions secret:** repo → Settings → Secrets and variables
   → Actions → *New repository secret* → name `GEMINI_API_KEY`.

4. **Add the model as an Actions variable** (same page, *Variables* tab):
   name `GEMINI_MODEL`, value `gemini-flash-latest`. (The code defaults to this
   if unset, but keeping it as a variable means you can change it with no code
   change — see breakages below.)

5. **Enable Pages:** repo → Settings → Pages → *Deploy from a branch* → branch
   `main`, folder `/docs`.

6. **Run it once by hand:** Actions tab → *Build The Brunei Dispatch* → *Run
   workflow*. Then visit the Pages URL.

The workflow also runs automatically at **06:00 Brunei time** every day
(`cron: "0 22 * * *"` — Actions cron is UTC, and Brunei is UTC+8 with no DST).

---

## Local development

```bash
pip install -r requirements.txt
python build.py --fixtures      # no network, no key needed
```

To test the real pipeline locally, set your own key in the shell first
(PowerShell: `$env:GEMINI_API_KEY = "..."`; bash: `export GEMINI_API_KEY=...`)
and run `python build.py`. Note: on Python 3.14 some wheels may lag; 3.11–3.12
is the tested range and matches CI.

---

## The two known breakages (and their fixes)

Both are external and expected — neither needs a code change under normal
circumstances.

1. **Gemini model name rotation.** Google removes old models from the free tier.
   If a build fails with a *model not found* error, check
   [Google's current model list](https://ai.google.dev/gemini-api/docs/models)
   and update the `GEMINI_MODEL` repo **variable** to a current free-tier model.
   No code change, no commit.

2. **Action / runner deprecation.** About once a year GitHub deprecates a runner
   image or an action major version, and runs start warning or failing. Bump the
   pinned versions in `.github/workflows/build.yml`
   (`actions/checkout`, `actions/setup-python`, `runs-on: ubuntu-24.04`).

A third thing worth knowing: if a **feed** dies, `--check-feeds` will tell you.
Remove or replace the dead entry in `sources.yaml` rather than leaving it to
fail every morning.

---

## Failure behaviour

- The build writes to a temp file, validates it parses and contains a lead
  headline, and only then moves it into `docs/index.html`. A failed or
  interrupted run leaves the previous edition intact.
- If the last successful build was more than 48 hours ago, the page renders a
  red staleness banner rather than quietly serving old news as if fresh.
- The workflow does not swallow exceptions. A broken build goes red and GitHub
  emails you.
