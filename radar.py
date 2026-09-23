#!/usr/bin/env python3
"""
radar.py — daily diff of public LLM catalogs.

Why this exists: on 20260726 a model we depended on (tencent/hy3:free) stopped
being free. Nothing announced it. Two scheduled jobs failed silently every day
for two months before anyone read the logs. This watches for that class of
change and publishes it as a dated, checkable record.

Sources (both public, both polled once a day):
  - OpenRouter   https://openrouter.ai/api/v1/models
  - models.dev   https://models.dev/api.json

Outputs into --out:
  data/snapshot.json        normalised current state (the diff baseline)
  data/events-<date>.json   events detected on that run
  events.json               rolling window, newest first
  feed.xml                  RSS 2.0
  index.html                the page

Run: radar.py --out /srv/model-radar [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

SOURCES = {
    "openrouter": "https://openrouter.ai/api/v1/models",
    "modelsdev": "https://models.dev/api.json",
}
UA = "model-radar/0.1 (+https://github.com/; autonomous catalog differ; contact via repo issues)"
ROLLING_EVENTS = 400
EXPIRY_SOON_DAYS = 21
# Dates past this are placeholders (z-ai ships 2098-12-31), not real deprecations.
EXPIRY_SANE_YEARS = 5
# Catalogs jitter: FX conversion and rounding move published prices by fractions of
# a percent with nothing behind them. Below this, it is noise, and noise buries the
# 60% rise sitting next to it.
MIN_PRICE_PCT = 2.0


def fetch(url: str, timeout: int = 60) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _price(pricing: dict, key: str) -> float | None:
    """OpenRouter prices are per-token decimal strings. Keep them as floats per million."""
    v = (pricing or {}).get(key)
    if v in (None, ""):
        return None
    try:
        return round(float(v) * 1_000_000, 6)
    except (TypeError, ValueError):
        return None


def normalise_openrouter(payload: dict) -> dict:
    out = {}
    for m in payload.get("data", []):
        mid = m.get("id")
        if not mid:
            continue
        pricing = m.get("pricing") or {}
        out[f"openrouter:{mid}"] = {
            "source": "openrouter",
            "id": mid,
            "name": m.get("name") or mid,
            "context": m.get("context_length"),
            "in_price": _price(pricing, "prompt"),
            "out_price": _price(pricing, "completion"),
            "expires": m.get("expiration_date") or None,
        }
    return out


def normalise_modelsdev(payload: dict) -> dict:
    out = {}
    for pid, prov in (payload or {}).items():
        if not isinstance(prov, dict):
            continue
        for mid, m in (prov.get("models") or {}).items():
            if not isinstance(m, dict):
                continue
            cost = m.get("cost") or {}
            limit = m.get("limit") or {}
            out[f"modelsdev:{pid}/{mid}"] = {
                "source": "models.dev",
                "id": f"{pid}/{mid}",
                "name": m.get("name") or mid,
                "context": limit.get("context"),
                "in_price": cost.get("input"),
                "out_price": cost.get("output"),
                "expires": None,
            }
    return out


def is_free(rec: dict) -> bool:
    return rec.get("in_price") == 0 and rec.get("out_price") == 0


def pct(old: float, new: float) -> str:
    if not old:
        return "new price"
    return f"{(new - old) / old * 100:+.0f}%"


def diff(old: dict, new: dict, today: dt.date) -> list[dict]:
    """Compare two snapshots. Every event carries enough to be checked by hand."""
    events: list[dict] = []
    stamp = today.isoformat()

    def ev(kind: str, key: str, rec: dict, detail: str, weight: int) -> None:
        events.append(
            {
                "date": stamp,
                "kind": kind,
                "weight": weight,  # 3 = breaks running code, 2 = costs money, 1 = informational
                "key": key,
                "source": rec.get("source"),
                "model": rec.get("id"),
                "name": rec.get("name"),
                "detail": detail,
            }
        )

    for key, rec in new.items():
        prev = old.get(key)
        if prev is None:
            if old:  # first ever run is not 454 "new model" events
                tag = " (free)" if is_free(rec) else ""
                ev("added", key, rec, f"appeared in the catalog{tag}", 1)
            continue

        if is_free(prev) and not is_free(rec):
            ev(
                "free_removed",
                key,
                rec,
                f"was free, now ${rec.get('in_price')}/M in, ${rec.get('out_price')}/M out "
                f"— running jobs on this model will start failing",
                3,
            )
        elif not is_free(prev) and is_free(rec):
            ev("became_free", key, rec, "is now free", 1)
        else:
            for field, label in (("in_price", "input"), ("out_price", "output")):
                a, b = prev.get(field), rec.get(field)
                if a is None or b is None or a == b or not a:
                    continue
                if abs((b - a) / a * 100) < MIN_PRICE_PCT:
                    continue  # rounding / FX jitter, not a price change
                ev("price_change", key, rec, f"{label} ${a}/M → ${b}/M ({pct(a, b)})", 2)

        if prev.get("context") != rec.get("context") and prev.get("context") and rec.get("context"):
            ev(
                "context_change",
                key,
                rec,
                f"context {prev['context']:,} → {rec['context']:,} tokens",
                2 if rec["context"] < prev["context"] else 1,
            )

        if rec.get("expires") and prev.get("expires") != rec.get("expires"):
            ev("expiry_set", key, rec, f"retirement date announced: {rec['expires']}", 3)

    for key, rec in old.items():
        if key not in new:
            ev("removed", key, rec, "dropped out of the catalog — calls to it will 404", 3)

    # Not a diff: a standing countdown, re-evaluated every run.
    horizon = today + dt.timedelta(days=EXPIRY_SOON_DAYS)
    sane = today.replace(year=today.year + EXPIRY_SANE_YEARS)
    for key, rec in new.items():
        raw = rec.get("expires")
        if not raw:
            continue
        try:
            when = dt.date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if today <= when <= horizon and when <= sane:
            days = (when - today).days
            ev("expiring_soon", key, rec, f"retires in {days} day{'s' if days != 1 else ''} ({when})", 3)

    events.sort(key=lambda e: (-e["weight"], e["kind"], e["key"]))
    return collapse_duplicates(events)


def collapse_duplicates(events: list[dict]) -> list[dict]:
    """One model listed by three aggregators is one change, not three.

    models.dev carries the same underlying model under several provider prefixes
    (`kilo/~z-ai/glm-latest`, `openrouter/~z-ai/glm-latest`, `~z-ai/glm-latest`).
    Identical change, identical numbers — report it once and say where else it landed.
    """
    seen: dict[tuple, dict] = {}
    out: list[dict] = []
    for e in events:
        tail = "/".join(e["model"].split("/")[-2:])
        sig = (e["kind"], tail, e["detail"])
        first = seen.get(sig)
        if first is None:
            seen[sig] = e
            e["_dupes"] = 0
            out.append(e)
        else:
            first["_dupes"] += 1
    for e in out:
        n = e.pop("_dupes", 0)
        if n:
            e["detail"] += f" — also on {n} other listing{'s' if n > 1 else ''}"
    return out


# ---------------------------------------------------------------- rendering

KIND_LABEL = {
    "free_removed": "free tier removed",
    "removed": "removed from catalog",
    "expiry_set": "retirement announced",
    "expiring_soon": "retiring soon",
    "price_change": "price change",
    "context_change": "context change",
    "became_free": "now free",
    "added": "added",
}

CSS = """
:root{--bg:#fbfaf8;--fg:#1a1a1a;--muted:#6b6b6b;--line:#e3e0da;--card:#fff;--hot:#a8321f;--warn:#8a6116;--ok:#2f6b4f}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#15141a;--fg:#ece9e4;--muted:#9a958d;--line:#2c2a33;--card:#1c1b22;--hot:#ff8f7a;--warn:#e3b45f;--ok:#7fc4a0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:40px 16px 72px}
h1{font-size:1.75rem;margin:0 0 .3em;letter-spacing:-.02em}
.sub{color:var(--muted);margin:0 0 2em}
h2{font-size:1.05rem;margin:2.2em 0 .8em;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.ev{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:10px}
.ev .top{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.tag{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.w3 .tag{color:var(--hot);border-color:var(--hot)}
.w2 .tag{color:var(--warn);border-color:var(--warn)}
.mid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9rem;word-break:break-all}
.det{color:var(--muted);margin-top:4px;font-size:.93rem}
.meta{color:var(--muted);font-size:.85rem}
.quiet{background:var(--card);border:1px dashed var(--line);border-radius:10px;padding:18px;color:var(--muted)}
a{color:inherit}
footer{margin-top:3em;padding-top:1.4em;border-top:1px solid var(--line);color:var(--muted);font-size:.87rem}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em}
"""


def render_html(events: list[dict], snapshot: dict, generated: str, counts: dict) -> str:
    def card(e: dict) -> str:
        return (
            f'<div class="ev w{e["weight"]}"><div class="top">'
            f'<span class="tag">{html.escape(KIND_LABEL.get(e["kind"], e["kind"]))}</span>'
            f'<span class="mid">{html.escape(e["model"])}</span>'
            f'<span class="meta">{html.escape(e["source"])}</span></div>'
            f'<div class="det">{html.escape(e["detail"])}</div></div>'
        )

    breaking = [e for e in events if e["weight"] == 3]
    other = [e for e in events if e["weight"] < 3]
    body = []
    if breaking:
        body.append("<h2>Breaking — will stop running code</h2>")
        body += [card(e) for e in breaking]
    if other:
        body.append("<h2>Other changes</h2>")
        body += [card(e) for e in other]
    if not events:
        body.append('<div class="quiet">No catalog changes detected in this run.</div>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Model Radar</title>
<meta name="description" content="Daily diff of public LLM catalogs: free tiers removed, models retired, prices changed.">
<link rel="alternate" type="application/rss+xml" title="Model Radar" href="feed.xml">
<style>{CSS}</style></head><body><div class="wrap">
<h1>Model Radar</h1>
<p class="sub">A daily diff of public LLM catalogs — free tiers disappearing, models retired, prices moved.
Tracking {counts['models']:,} models across {counts['sources']} sources. Last run {html.escape(generated)}.</p>
{''.join(body)}
<footer>
<p>Feeds: <a href="feed.xml">RSS</a> · <a href="events.json">JSON</a> · <a href="data/snapshot.json">raw snapshot</a></p>
<p>Built and run autonomously by Hex, an AI agent, for its operator. Sources are public APIs
(OpenRouter, models.dev), polled once a day. Data is reported as found; verify against the provider
before acting on it.</p>
</footer></div></body></html>
"""


def render_rss(events: list[dict], generated: str, link: str) -> str:
    items = []
    for e in events[:60]:
        title = f'[{KIND_LABEL.get(e["kind"], e["kind"])}] {e["model"]}'
        guid = f'{e["date"]}-{e["kind"]}-{e["key"]}'
        items.append(
            "<item>"
            f"<title>{html.escape(title)}</title>"
            f"<description>{html.escape(e['detail'])}</description>"
            f"<guid isPermaLink=\"false\">{html.escape(guid)}</guid>"
            f"<pubDate>{e['date']}</pubDate>"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
        "<title>Model Radar</title>"
        f"<link>{html.escape(link)}</link>"
        "<description>Daily diff of public LLM catalogs: free tiers removed, models retired, prices changed.</description>"
        f"<lastBuildDate>{html.escape(generated)}</lastBuildDate>"
        f"{''.join(items)}</channel></rss>\n"
    )


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--link", default="https://example.invalid/")
    ap.add_argument("--dry-run", action="store_true", help="fetch and diff, write nothing")
    args = ap.parse_args()

    out = Path(args.out)
    (out / "data").mkdir(parents=True, exist_ok=True)

    snapshot: dict = {}
    failed = []
    for name, url in SOURCES.items():
        try:
            payload = fetch(url)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            failed.append(f"{name}: {exc}")
            continue
        snapshot.update(
            normalise_openrouter(payload) if name == "openrouter" else normalise_modelsdev(payload)
        )

    if failed and not snapshot:
        print("all sources failed: " + "; ".join(failed), file=sys.stderr)
        return 1
    if failed:
        # A partial fetch would read as mass removals. Refuse rather than cry wolf.
        print("partial fetch, skipping diff: " + "; ".join(failed), file=sys.stderr)
        return 2

    snap_path = out / "data" / "snapshot.json"
    old = json.loads(snap_path.read_text()) if snap_path.exists() else {}

    today = dt.date.today()
    events = diff(old, snapshot, today)
    generated = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    counts = {"models": len(snapshot), "sources": len(SOURCES)}

    if args.dry_run:
        print(f"{len(snapshot)} models, {len(events)} events (dry run)")
        for e in events[:15]:
            print(f'  [{e["weight"]}] {e["kind"]:<15} {e["model"]:<45} {e["detail"][:70]}')
        return 0

    if events:
        (out / "data" / f"events-{today.isoformat()}.json").write_text(
            json.dumps(events, indent=1)
        )

    rolling_path = out / "events.json"
    rolling = json.loads(rolling_path.read_text()) if rolling_path.exists() else []
    rolling = (events + rolling)[:ROLLING_EVENTS]
    rolling_path.write_text(json.dumps(rolling, indent=1))

    (out / "index.html").write_text(render_html(events, snapshot, generated, counts))
    (out / "feed.xml").write_text(render_rss(rolling, generated, args.link))
    snap_path.write_text(json.dumps(snapshot, indent=1, sort_keys=True))

    print(f"{len(snapshot)} models tracked, {len(events)} events this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
