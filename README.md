# SaaS Changelog & Status Page Watcher

**SaaS Changelog & Status Page Watcher** watches vendor changelog and status pages and tells you when something new appears: a release note, a feature announcement, an incident, or a maintenance window. Each run compares the page with the snapshot from the previous run and outputs a clean dataset of changes, with an optional webhook to Slack, Discord, Zapier, Make or n8n.

Built for competitive intel, SRE vendor watches, and product teams that need to know the same day a tool they rely on ships or breaks.

## What it does

- **Extracts structured events** from changelog and status pages (heading + date + summary).
- **Renders JavaScript pages automatically** (`auto` mode: fast HTTP first, Chromium only when needed).
- **Diffs against the previous run** using a named key-value store that survives between scheduled runs.
- **Outputs clear change types:** `event_added`, `event_removed`, `event_updated`, `page_text_changed`, plus baseline / no_change / error rows.
- **Sends one webhook** with all changes in a Slack- and Discord-compatible payload.

## Use cases

- Track competitor release notes (Vercel, Linear, Notion, Cursor, GitHub).
- Watch status pages for tools in your stack (Stripe, OpenAI, Anthropic, GitHub).
- Feed a Slack channel or newsletter with "what shipped / what broke today".
- Build a history of vendor reliability and product velocity.

## How to use

1. Add the changelog / status URLs. Optionally set `pageKind` to `changelog` or `status`, or a CSS selector.
2. Run once to save a **baseline** of detected events.
3. Schedule daily or hourly. Later runs output only the diffs (plus optional `no_change` rows).
4. Optional: add a **webhook URL**.

Keep the same **State store name** across runs.

## Input

| Field | Description |
| --- | --- |
| `urls` | Changelog / status pages. Each item can carry `selector`, `label`, `pageKind`. |
| `cssSelector` | Optional CSS selector for all pages without their own. |
| `pageKind` | `auto` (default), `changelog`, or `status`. |
| `renderMode` | `auto` / `http` / `browser`. |
| `includeUnchanged` | On by default: one `no_change` row per unchanged page. |
| `ignoreTextPatterns` | Regexes for noisy text to ignore. |
| `webhookUrl`, `webhookHeaders` | Where to POST changes. |
| `stateStoreName` | Snapshot store name (default `saas-changelog-status-state`). |
| `maxConcurrency` | Pages in parallel (1–10). |
| `proxyConfiguration` | Optional Apify Proxy. |

Example input:

```json
{
  "urls": [
    { "url": "https://vercel.com/changelog", "pageKind": "changelog" },
    { "url": "https://github.blog/changelog/", "pageKind": "changelog" },
    { "url": "https://status.stripe.com/", "pageKind": "status" }
  ],
  "renderMode": "auto",
  "webhookUrl": "https://hooks.slack.com/services/XXX/YYY/ZZZ"
}
```

## Output

Example of a new release:

```json
{
  "url": "https://vercel.com/changelog",
  "checkedAt": "2026-11-06T15:00:00Z",
  "previousCheckedAt": "2026-11-05T15:00:00Z",
  "fetchMethod": "http",
  "eventsDetected": 24,
  "changeType": "event_added",
  "eventTitle": "Fluid Compute now generally available",
  "eventDate": "Nov 5, 2026",
  "eventKind": "changelog",
  "summary": "New changelog: Fluid Compute now generally available"
}
```

### Change types

| `changeType` | Meaning |
| --- | --- |
| `baseline` | First run: events saved. |
| `event_added` / `event_removed` / `event_updated` | An event appeared, disappeared, or its summary changed. |
| `page_text_changed` | Other page text changed. |
| `no_change` | Checked, nothing changed. |
| `events_not_found` | Previously had events; now none — snapshot kept to avoid false removals. |
| `selector_not_found` / `fetch_error` | Selector or load failure. |

## Pricing

Pay per event: **$0.005 per page checked**. Platform usage is separate. Schedule wisely and keep memory at 512 MB for best margin.

## Tips

- Prefer dedicated changelog / status URLs over marketing homepages.
- If a page is a heavy SPA, set `renderMode` to `browser` or add a CSS selector around the feed.
- Some status pages (e.g. behind Cloudflare) may need Apify Proxy; most changelogs work from datacenter IPs.
- For SaaS **pricing** pages specifically, use [Pricing Page Change Monitor](https://apify.com/aibuildersuse/pricing-page-change-monitor) instead — it extracts plan cards and price diffs.
