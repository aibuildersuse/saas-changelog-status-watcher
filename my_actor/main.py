"""SaaS Changelog & Status Page Watcher.

Fetches vendor changelog / status pages, extracts structured events (releases, incidents),
compares with the previous run's snapshot, and outputs one dataset item per change.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from apify import Actor, Event

from .monitor_core import (
    BrowserFetcher,
    event_key,
    extract_changelog_status,
    fetch_page,
    line_diff,
    short_hash,
)

PAGE_EVENT = 'page-checked'
SNAPSHOT_VERSION = 1
MAX_LINES_PER_CHANGE = 40


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def snapshot_key(url: str, selector: str | None) -> str:
    return 'snap-' + hashlib.sha1(f'{url}\n{selector or ""}'.encode()).hexdigest()[:20]


def normalize_url_input(item: Any) -> tuple[str, str | None, str | None, str | None] | None:
    if isinstance(item, str):
        url, selector, label, page_kind = item.strip(), None, None, None
    elif isinstance(item, dict):
        url = str(item.get('url') or '').strip()
        selector = item.get('selector') or item.get('cssSelector') or None
        user_data = item.get('userData') if isinstance(item.get('userData'), dict) else {}
        label = item.get('label') or user_data.get('label')
        page_kind = item.get('pageKind') or user_data.get('pageKind')
    else:
        return None
    if not re.match(r'^https?://', url, re.I):
        return None
    sel = selector.strip() if isinstance(selector, str) and selector.strip() else None
    return url, sel, label, page_kind


def filter_lines(lines: list[str], ignore: list[re.Pattern[str]]) -> list[str]:
    return [ln for ln in lines if not any(p.search(ln) for p in ignore)]


def build_snapshot(url: str, selector: str | None, fr: Any, ext: dict[str, Any], lines: list[str]) -> dict[str, Any]:
    return {
        'version': SNAPSHOT_VERSION,
        'url': url,
        'selector': selector,
        'checkedAt': now_iso(),
        'fetchMethod': fr.method,
        'title': ext.get('title'),
        'events': [
            {k: e[k] for k in ('title', 'date', 'kind', 'summary', 'textHash')}
            for e in ext.get('events', [])
        ],
        'textLines': lines[:4000],
        'textHash': short_hash('\n'.join(lines)),
    }


def diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    old_ev = {event_key(e): e for e in old.get('events', [])}
    new_ev = {event_key(e): e for e in new.get('events', [])}

    for k, ne in new_ev.items():
        oe = old_ev.get(k)
        if oe is None:
            changes.append({
                'changeType': 'event_added',
                'eventTitle': ne.get('title'),
                'eventDate': ne.get('date'),
                'eventKind': ne.get('kind'),
                'eventSummary': ne.get('summary'),
                'summary': f"New {ne.get('kind') or 'entry'}: {ne.get('title')}",
            })
            continue
        if oe.get('textHash') != ne.get('textHash') or oe.get('summary') != ne.get('summary'):
            changes.append({
                'changeType': 'event_updated',
                'eventTitle': ne.get('title'),
                'eventDate': ne.get('date'),
                'eventKind': ne.get('kind'),
                'eventSummary': ne.get('summary'),
                'summary': f"Updated {ne.get('kind') or 'entry'}: {ne.get('title')}",
            })
    for k, oe in old_ev.items():
        if k not in new_ev:
            changes.append({
                'changeType': 'event_removed',
                'eventTitle': oe.get('title'),
                'eventDate': oe.get('date'),
                'eventKind': oe.get('kind'),
                'eventSummary': oe.get('summary'),
                'summary': f"Removed {oe.get('kind') or 'entry'}: {oe.get('title')}",
            })

    if old.get('textHash') != new.get('textHash'):
        a, r = line_diff(old.get('textLines', []), new.get('textLines', []))
        reported = {c.get('eventTitle') for c in changes if c.get('eventTitle')}
        a = [ln for ln in a if not any(t and t in ln for t in reported)][:MAX_LINES_PER_CHANGE]
        r = [ln for ln in r if not any(t and t in ln for t in reported)][:MAX_LINES_PER_CHANGE]
        if a or r:
            changes.append({
                'changeType': 'page_text_changed',
                'eventTitle': None,
                'addedText': a,
                'removedText': r,
                'summary': f'Page text changed (+{len(a)} / -{len(r)} lines)',
            })
    return changes


async def send_webhook(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as c:
        last = None
        for attempt in range(3):
            try:
                r = await c.post(url, json=payload, headers={'Content-Type': 'application/json', **headers})
                last = {'status': r.status_code, 'ok': 200 <= r.status_code < 300}
                if last['ok']:
                    return last
            except httpx.HTTPError as e:
                last = {'status': None, 'ok': False, 'error': f'{type(e).__name__}: {e}'[:200]}
            await asyncio.sleep(2 * (attempt + 1))
        return last or {'ok': False}


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        raw_urls = inp.get('urls') or inp.get('startUrls') or []
        default_selector = (inp.get('cssSelector') or '').strip() or None
        default_kind = inp.get('pageKind') or 'auto'
        render_mode = inp.get('renderMode') or 'auto'
        store_name = (inp.get('stateStoreName') or 'saas-changelog-status-state').strip()
        include_unchanged = bool(inp.get('includeUnchanged', True))
        webhook_url = (inp.get('webhookUrl') or '').strip() or None
        webhook_on_baseline = bool(inp.get('webhookOnBaseline', False))
        webhook_headers = inp.get('webhookHeaders') or {}
        max_concurrency = max(1, min(int(inp.get('maxConcurrency') or 3), 10))
        ignore: list[re.Pattern[str]] = []
        for pat in inp.get('ignoreTextPatterns') or []:
            try:
                ignore.append(re.compile(pat, re.I))
            except re.error:
                Actor.log.warning(f'Ignoring invalid regex: {pat!r}')

        targets: list[tuple[str, str | None, str | None, str]] = []
        seen: set[tuple[str, str | None]] = set()
        for item in raw_urls:
            norm = normalize_url_input(item)
            if norm is None:
                Actor.log.warning(f'Skipping invalid URL entry: {item!r}')
                continue
            url, sel, label, page_kind = norm
            sel = sel or default_selector
            kind = (page_kind or default_kind or 'auto')
            if (url, sel) in seen:
                continue
            seen.add((url, sel))
            targets.append((url, sel, label, kind))
        if not targets:
            await Actor.fail(status_message='No valid URLs in input. Add at least one http(s) URL to "urls".')
            return
        if not re.fullmatch(r'[a-zA-Z0-9-]{1,63}', store_name):
            await Actor.fail(status_message='stateStoreName may contain only letters, digits and hyphens.')
            return

        store = await Actor.open_key_value_store(name=store_name)
        proxy_url = None
        if inp.get('proxyConfiguration'):
            pc = await Actor.create_proxy_configuration(actor_proxy_input=inp['proxyConfiguration'])
            proxy_url = await pc.new_url() if pc else None

        memory_mb = int(os.environ.get('ACTOR_MEMORY_MBYTES') or 1024)
        browser_tabs = max(1, min(max_concurrency, memory_mb // 1024))
        browser = BrowserFetcher(max_pages=browser_tabs) if render_mode in ('auto', 'browser') else None
        aborting = False

        async def on_abort(*_: Any) -> None:
            nonlocal aborting
            aborting = True
            Actor.log.warning('Run is aborting; stopping after pages in progress.')

        Actor.on(Event.ABORTING, on_abort)

        sem = asyncio.Semaphore(max_concurrency)
        charging = Actor.get_charging_manager()
        in_flight = 0

        def budget_allows_another() -> bool:
            left = charging.calculate_max_event_charge_count_within_limit(PAGE_EVENT)
            return left is None or left - in_flight > 0

        all_changes: list[dict[str, Any]] = []
        summary = {
            'pagesRequested': len(targets), 'pagesChecked': 0, 'pagesFailed': 0,
            'pagesChanged': 0, 'baselines': 0, 'changes': 0,
        }
        budget_hit = False
        run_started = now_iso()

        async with httpx.AsyncClient(proxy=proxy_url, http2=False) as client:

            async def process(url: str, selector: str | None, label: str | None, page_kind: str) -> None:
                nonlocal budget_hit, in_flight
                if aborting or budget_hit:
                    return
                async with sem:
                    if aborting or budget_hit:
                        return
                    if not budget_allows_another():
                        budget_hit = True
                        Actor.log.warning('Maximum cost per run reached; skipping remaining pages.')
                        return
                    in_flight += 1
                    try:
                        await check_page(url, selector, label, page_kind)
                    finally:
                        in_flight -= 1

            async def check_page(url: str, selector: str | None, label: str | None, page_kind: str) -> None:
                nonlocal budget_hit
                Actor.log.info(f'Checking {url}' + (f' (selector {selector})' if selector else ''))
                checked_at = now_iso()
                base = {'url': url, 'label': label, 'selector': selector, 'pageKind': page_kind, 'checkedAt': checked_at}
                try:
                    fr = await fetch_page(url, client=client, browser=browser, mode=render_mode)
                    if not fr.body:
                        raise RuntimeError(fr.error or 'empty response')
                    ext = extract_changelog_status(fr.body, selector=selector, page_kind=page_kind)
                except Exception as e:  # noqa: BLE001
                    fr, ext = None, None
                    err = f'{type(e).__name__}: {e}'[:300]
                else:
                    err = None

                key = snapshot_key(url, selector)
                old = await store.get_value(key)
                rows: list[dict[str, Any]] = []
                if ext is None:
                    summary['pagesFailed'] += 1
                    rows.append({**base, 'changeType': 'fetch_error', 'error': err,
                                 'previousCheckedAt': (old or {}).get('checkedAt'),
                                 'summary': f'Could not load page: {err}'})
                    await Actor.push_data(rows)
                    return
                if selector and not ext['selectorMatched']:
                    summary['pagesFailed'] += 1
                    rows.append({**base, 'changeType': 'selector_not_found', 'pageTitle': ext.get('title'),
                                 'fetchMethod': fr.method,
                                 'summary': f'CSS selector {selector!r} matched nothing on the page'})
                    await Actor.push_data(rows)
                    return

                lines = filter_lines(ext['textLines'], ignore)
                snap = build_snapshot(url, selector, fr, ext, lines)
                # Recompute text hash after ignore filter
                snap['textLines'] = lines[:4000]
                snap['textHash'] = short_hash('\n'.join(lines))
                common = {
                    **base, 'pageTitle': ext.get('title'), 'fetchMethod': fr.method,
                    'eventsDetected': len(snap['events']),
                    'previousCheckedAt': (old or {}).get('checkedAt'), 'snapshotKey': key,
                }

                if not old or old.get('version') != SNAPSHOT_VERSION:
                    summary['baselines'] += 1
                    if snap['events']:
                        for e in snap['events']:
                            rows.append({
                                **common, 'changeType': 'baseline',
                                'eventTitle': e.get('title'), 'eventDate': e.get('date'),
                                'eventKind': e.get('kind'), 'eventSummary': e.get('summary'),
                                'summary': f"Baseline saved: {e.get('kind') or 'entry'} '{e.get('title')}'",
                            })
                    else:
                        rows.append({**common, 'changeType': 'baseline',
                                     'summary': 'Baseline saved (no structured events; page text is tracked)'})
                    await store.set_value(key, snap)
                elif not snap['events'] and old.get('events'):
                    summary['pagesFailed'] += 1
                    rows.append({
                        **common, 'changeType': 'events_not_found',
                        'summary': (
                            f"No events detected now (previous run found {len(old['events'])}). "
                            'Snapshot kept; try renderMode=browser or a tighter CSS selector.'
                        ),
                    })
                else:
                    changes = diff_snapshots(old, snap)
                    if changes:
                        summary['pagesChanged'] += 1
                        for c in changes:
                            rows.append({**common, **c})
                    elif include_unchanged:
                        rows.append({
                            **common, 'changeType': 'no_change',
                            'summary': f"No change in {len(snap['events'])} events since {old.get('checkedAt')}",
                        })
                    await store.set_value(key, snap)

                summary['pagesChecked'] += 1
                real = [r for r in rows if r['changeType'] not in ('no_change',)]
                summary['changes'] += len([r for r in real if r['changeType'] != 'baseline'])
                all_changes.extend(r for r in real if r['changeType'] != 'baseline' or webhook_on_baseline)
                if rows:
                    await Actor.push_data(rows)
                res = await Actor.charge(event_name=PAGE_EVENT)
                if res.event_charge_limit_reached:
                    budget_hit = True
                    Actor.log.warning('Maximum cost per run reached; skipping remaining pages.')

            try:
                await asyncio.gather(*(process(u, s, l, k) for u, s, l, k in targets))
            finally:
                if browser is not None:
                    await browser.close()

        webhook_result = None
        if webhook_url and all_changes:
            payload = {
                'event': 'changelog_status_changes_detected',
                'runStartedAt': run_started,
                'runId': Actor.configuration.actor_run_id,
                'changeCount': len(all_changes),
                'text': 'Changelog/status changes detected:\n' + '\n'.join(
                    f"- {c['url']}: {c['summary']}" for c in all_changes[:30]),
                'changes': all_changes,
            }
            payload['content'] = payload['text'][:1900]
            webhook_result = await send_webhook(
                webhook_url, payload, {str(k): str(v) for k, v in webhook_headers.items()})
            Actor.log.info(f'Webhook result: {webhook_result}')

        summary['webhook'] = webhook_result
        summary['finishedAt'] = now_iso()
        await Actor.set_value('OUTPUT', summary)
        msg = (
            f"Checked {summary['pagesChecked']}/{summary['pagesRequested']} pages: "
            f"{summary['changes']} changes, {summary['baselines']} new baselines, "
            f"{summary['pagesFailed']} problems"
        )
        Actor.log.info(msg)
        await Actor.set_status_message(msg, is_terminal=True)
