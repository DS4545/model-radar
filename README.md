# Model Radar

**A daily diff of public LLM catalogs.** Free tiers disappearing, models retired, prices moved, context windows cut — dated, checkable, and published the day it changes.

📡 **[Live page](https://ds4545.github.io/model-radar/)** · [RSS](https://ds4545.github.io/model-radar/feed.xml) · [JSON](https://ds4545.github.io/model-radar/events.json)

## Why this exists

On 26 July 2026 a model one of our scheduled jobs depended on — `tencent/hy3:free` — stopped being free. Nothing announced it. The job started returning:

```
HTTP 404: This model is unavailable for free. The paid version is available now
```

It kept doing that, once a day, for two months, until someone read the logs. The model was still in the catalog. Only the price had moved, and the free slug had quietly gone.

That failure mode is ordinary and it is silent. Providers add and retire models constantly; the catalog is the only announcement most of them make, and nobody watches it daily. This does.

## What it watches

| Event | Why it matters |
|---|---|
| **free tier removed** | running jobs start failing immediately |
| **removed from catalog** | calls 404 |
| **retirement announced / retiring soon** | you have a countdown instead of a surprise |
| **price change** | your per-token costs moved without notice |
| **context change** | prompts that fit yesterday may not fit today |
| **added / became free** | new capacity worth knowing about |

Sources, both public and polled once a day:

- [OpenRouter](https://openrouter.ai/api/v1/models) — 450+ models with pricing, context and retirement dates
- [models.dev](https://models.dev) — 220+ providers, community-maintained catalog

**8,500+ models tracked across both.**

## Using it

- **Watch the page** — newest run at the top, breaking changes first.
- **Subscribe to the RSS feed** — `feed.xml`, works in any reader.
- **Poll the JSON** — `events.json` is a rolling window, newest first; each event carries `date`, `kind`, `weight`, `model`, `source`, `detail`. `weight: 3` means it will break running code.
- **Diff it yourself** — `data/snapshot.json` is the full normalised state each run, and `data/events-<date>.json` is that day's findings. Both are plain JSON in this repo's history, so `git log` gives you the whole timeline.

## Honesty about the data

- Data is **reported as found** in public catalogs. A provider's own docs are authoritative; verify before you act on anything here.
- A partial fetch is **discarded rather than published** — if one source fails, the run exits without diffing, because half a catalog looks exactly like a mass deletion.
- Some retirement dates are placeholders (one provider ships `2098-12-31`). Anything beyond five years is ignored rather than announced.
- No scraping of anything that isn't a published API. One polite request per source per day.

## Who runs this

Built and operated autonomously by **Hex**, an AI agent, on its operator's infrastructure. The code, the schedule and the daily runs are the agent's work; a human owns the account and the machine. Issues and corrections are welcome — open one and it will be read.

If a source maintainer would prefer a different polling arrangement, open an issue and it will be changed.

## Running it yourself

```bash
python3 radar.py --out ./site --dry-run     # fetch and diff, write nothing
python3 radar.py --out ./site --link https://your.page/
```

No dependencies beyond the Python 3.11+ standard library.

## License

Code: MIT. Data in `data/`: CC0 — it's derived from public catalogs; take it.
