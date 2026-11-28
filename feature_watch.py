"""Watch Telegram for new features and tell the owner and the adoption agent about them.

Run daily on the server (cron): `.venv/bin/python feature_watch.py --notify`.
Sources, all public:
- Telegram's API schema as published in Telegram Desktop's repository (the newest layer clients use);
- the Telegram blog (human descriptions of new features);
- the latest Telethon release on PyPI (the library this connector is built on).

The report lives in private/feature-watch.json. The connector serves it through the read-only tool
get_telegram_updates, so the weekly adoption agent and any assistant can see what is new.
Nothing here changes code or sends anything except, with --notify, a short note to Saved Messages.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

SCHEMA_URL = ('https://raw.githubusercontent.com/telegramdesktop/tdesktop/dev/'
              'Telegram/SourceFiles/mtproto/scheme/api.tl')
BLOG_URL = 'https://telegram.org/blog'
PYPI_URL = 'https://pypi.org/pypi/Telethon/json'
REPORT = 'feature-watch.json'
LINE = re.compile(r'^([a-zA-Z0-9_.]+)#([0-9a-f]+)\s(.*?)=\s*([^;]+);')


def fetch(url, timeout=30):
    request = urllib.request.Request(url, headers={'User-Agent': 'telegram-account-mcp feature watch'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode('utf-8', 'replace')


def parse_schema(text):
    """TL text → {'layer': int, 'items': {name: {'id', 'kind', 'params', 'type'}}}."""
    items, kind = {}, 'type'
    for raw in text.splitlines():
        line = raw.strip()
        if line == '---functions---':
            kind = 'method'
        elif line == '---types---':
            kind = 'type'
        match = LINE.match(line)
        if match:
            name, cid, params, result = match.groups()
            items[name] = {'id': int(cid, 16), 'kind': kind, 'type': result.strip(),
                           'params': [p.split(':')[0] for p in params.split() if ':' in p and not p.startswith('{')]}
    layer = re.search(r'//\s*LAYER\s+(\d+)', text)
    return {'layer': int(layer.group(1)) if layer else None, 'items': items}


def blog_posts(html):
    seen = []
    for slug in re.findall(r'href="/blog/([a-z0-9-]+)"', html):
        if slug not in seen:
            seen.append(slug)
    return seen[:20]


def telethon_state():
    import telethon
    from telethon.tl.alltlobjects import LAYER, tlobjects
    return telethon.__version__, LAYER, set(tlobjects)


def version_key(version):
    return tuple(int(x) for x in re.findall(r'\d+', version))


def diff_items(old, new):
    added = sorted(n for n in new if n not in old)
    changed = sorted(n for n in new if n in old and new[n]['params'] != old[n]['params'])
    return added, changed


def describe(name, item, old=None):
    params = item['params']
    if old is not None:
        params = [p for p in params if p not in old['params']]
        return f"{name}: new parameters {', '.join(params) or '(changed)'}"
    return f"{name} ({item['kind']}) → {item['type']}" + (f"; params: {', '.join(params)}" if params else '')


def check(private, previous=None):
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    schema = parse_schema(fetch(SCHEMA_URL))
    installed, telethon_layer, known_ids = telethon_state()
    latest = json.loads(fetch(PYPI_URL))['info']['version']
    try:
        posts = blog_posts(fetch(BLOG_URL))
    except Exception:  # The blog is a nice-to-have; the schema is what matters.
        posts = (previous or {}).get('blog', {}).get('posts', [])
    items = schema['items']
    missing = sorted(n for n, i in items.items() if i['id'] not in known_ids)
    old_items = (previous or {}).get('schema_items', {})
    added, changed = diff_items(old_items, items) if old_items else ([], [])
    old_posts = set((previous or {}).get('blog', {}).get('posts', []))
    new_posts = [p for p in posts if old_posts and p not in old_posts]
    report = {
        'checked_at': now,
        'telegram_layer': schema['layer'],
        'telethon': {'installed': installed, 'layer': telethon_layer, 'latest': latest,
                     'upgrade_available': version_key(latest) > version_key(installed)},
        'missing_in_telethon': {'count': len(missing), 'names': missing[:200]},
        'new_since_last_check': {
            'added': [describe(n, items[n]) for n in added][:200],
            'changed': [describe(n, items[n], old_items[n]) for n in changed][:200]},
        'blog': {'posts': posts, 'new': [f'{BLOG_URL}/{p}' for p in new_posts]},
        'schema_items': items,
    }
    report['news'] = bool(added or changed or new_posts or (
        report['telethon']['upgrade_available'] and
        latest != (previous or {}).get('telethon', {}).get('latest')))
    history = (previous or {}).get('history', [])
    if report['news']:
        history.append({'at': now, 'layer': schema['layer'], 'added': len(added), 'changed': len(changed),
                        'blog_new': report['blog']['new'], 'telethon_latest': latest})
    report['history'] = history[-50:]
    return report


def summary(report):
    lines = [f"Telegram: новое в API (слой {report['telegram_layer']})"]
    t = report['telethon']
    if t['upgrade_available']:
        lines.append(f"Вышел Telethon {t['latest']} (у нас {t['installed']}).")
    new = report['new_since_last_check']
    if new['added'] or new['changed']:
        lines.append(f"Новых функций и типов: {len(new['added'])}, изменённых: {len(new['changed'])}.")
        lines += ['• ' + x for x in (new['added'] + new['changed'])[:8]]
    if report['blog']['new']:
        lines.append('В блоге Telegram:')
        lines += ['• ' + x for x in report['blog']['new'][:5]]
    lines.append('Еженедельный агент разберёт это и внедрит полезное; подробности — get_telegram_updates.')
    return '\n'.join(lines)


def public_report(report):
    """What the connector serves: everything except the full schema snapshot."""
    return {k: v for k, v in report.items() if k != 'schema_items'}


def notify(report, here):
    text = summary(report)
    request_id = 'feature-watch-' + hashlib.sha256(text.encode()).hexdigest()[:24]
    status = subprocess.run([sys.executable, str(here / 'rpc.py'), 'get_status'], input=b'{}',
                            check=True, cwd=here, capture_output=True)
    owner = json.loads(status.stdout)['result']['account_name']
    payload = json.dumps({'chat_id': 'me', 'expected_chat': owner, 'text': text, 'request_id': request_id})
    subprocess.run([sys.executable, str(here / 'rpc.py'), 'send_message'], input=payload.encode(),
                   check=True, cwd=here, capture_output=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--notify', action='store_true', help='send a note to Saved Messages when something is new')
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from instance import PRIVATE
    path = PRIVATE / REPORT
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = None
    report = check(PRIVATE, previous)
    path.write_text(json.dumps(report, ensure_ascii=False))
    print(json.dumps({k: report[k] for k in ('checked_at', 'telegram_layer', 'telethon', 'news')}, ensure_ascii=False))
    print(f"missing in Telethon: {report['missing_in_telethon']['count']}")
    if args.notify and report['news'] and previous is not None:
        notify(report, here)


def register_feature_watch_tool(mcp, private, read):

    @mcp.tool(annotations=read)
    async def get_telegram_updates(include_missing: bool = False) -> dict:
        """What is new in Telegram compared with this connector: the newest Telegram API layer, the
        Telethon version installed and available, API functions/types added or changed since the
        previous daily check, and new posts on the Telegram blog. include_missing lists the API
        names Telegram has that the installed library cannot call yet. Read-only; the report is
        refreshed daily on the server."""
        try:
            report = json.loads((private / REPORT).read_text())
        except (OSError, ValueError):
            return {'available': False, 'note': 'The daily check has not run yet on this server'}
        result = public_report(report)
        if not include_missing:
            result['missing_in_telethon'] = {'count': report['missing_in_telethon']['count']}
        return {'available': True, **result}

    return get_telegram_updates


if __name__ == '__main__':
    main()
