#!/usr/bin/env python3
"""Track Fortnite Item Shop changes against a local cache."""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
except ImportError as _exc:
    sys.exit(f"missing dependency '{_exc.name}'. run: pip install -r requirements.txt")

API_URL = "https://fortnite-api.com/v2/shop/br/combined"
DEFAULT_DB = Path.home() / ".cache" / "shop_tracker" / "shop.db"

def _init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            price INTEGER,
            rarity TEXT,
            category TEXT,
            image_url TEXT,
            first_seen TEXT,
            last_seen TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
            checked_at TEXT PRIMARY KEY,
            item_count INTEGER
        );
        """
    )
    conn.commit()
    conn.close()

def _fetch_shop() -> list[dict]:
    r = httpx.get(API_URL, timeout=30, headers={
        "User-Agent": "shop_tracker/0.1 (personal cron tool)",
    })
    r.raise_for_status()
    data = r.json()
    items = []
    seen_ids = set()
    featured = data.get("data", {}).get("featured", {}).get("entries", [])
    daily = data.get("data", {}).get("daily", {}).get("entries", [])
    for entry in featured + daily:
        for item in entry.get("items", []):
            iid = item.get("id")
            if not iid or iid in seen_ids:
                continue
            seen_ids.add(iid)
            items.append({
                "id": iid,
                "name": item.get("name"),
                "price": entry.get("finalPrice") or entry.get("regularPrice"),
                "rarity": item.get("rarity", {}).get("displayValue"),
                "category": item.get("type", {}).get("displayValue"),
                "image_url": item.get("images", {}).get("icon"),
            })
    return items

def _update_cache(db_path: Path, items: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    cur.execute("SELECT id, name, price, rarity, category, image_url FROM items")
    old = {row[0]: {
        "id": row[0], "name": row[1], "price": row[2],
        "rarity": row[3], "category": row[4], "image_url": row[5]
    } for row in cur.fetchall()}

    new_ids = set()
    current = {}
    for it in items:
        if not it.get("id"):
            continue
        new_ids.add(it["id"])
        current[it["id"]] = it
        cur.execute(
            """INSERT INTO items (id, name, price, rarity, category, image_url, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name,
                 price=excluded.price,
                 rarity=excluded.rarity,
                 category=excluded.category,
                 image_url=excluded.image_url,
                 last_seen=excluded.last_seen""",
            (it["id"], it["name"], it.get("price"), it.get("rarity"), it.get("category"), it.get("image_url"), now, now)
        )

    removed_ids = set(old.keys()) - new_ids
    removed = [old[i] for i in removed_ids]
    added = [current[i] for i in (new_ids - set(old.keys()))]
    changed = []
    for iid in new_ids & set(old.keys()):
        if current[iid].get("price") != old[iid].get("price"):
            changed.append({"old": old[iid], "new": current[iid]})

    cur.execute("INSERT OR IGNORE INTO runs (checked_at, item_count) VALUES (?, ?)", (now, len(items)))
    conn.commit()
    conn.close()
    return added, removed, changed

def _fmt_item(it: dict) -> str:
    return f"{it.get('name', '???')} ({it.get('rarity', '?')}) - {it.get('price', '?')} V-Bucks"

def _load_snapshot(db_path: Path, since: str) -> dict[str, dict]:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, price, rarity, category, image_url FROM items WHERE first_seen <= ?",
        (since,)
    )
    rows = cur.fetchall()
    conn.close()
    return {row[0]: {
        "id": row[0], "name": row[1], "price": row[2],
        "rarity": row[3], "category": row[4], "image_url": row[5]
    } for row in rows}

def main() -> int:
    p = argparse.ArgumentParser(description="Diff today's Fortnite Item Shop against local cache.")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="sqlite db path")
    p.add_argument("--json", action="store_true", help="output raw json")
    p.add_argument("--quiet", "-q", action="store_true", help="only print changes, skip summary")
    p.add_argument("--since", type=str, default="", help="date YYYY-MM-DD to diff against instead of last run")
    args = p.parse_args()

    _init_db(args.db)

    try:
        items = _fetch_shop()
    except httpx.HTTPStatusError as e:
        print(f"fetch failed: {e.response.status_code}", file=sys.stderr)
        return 1
    except httpx.RequestError as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1

    if not items:
        print("empty shop response, nothing to compare", file=sys.stderr)
        return 1

    if args.since:
        old = _load_snapshot(args.db, args.since)
        new_ids = {it["id"] for it in items if it.get("id")}
        current = {it["id"]: it for it in items if it.get("id")}
        added = [current[i] for i in (new_ids - set(old.keys()))]
        removed = [old[i] for i in (set(old.keys()) - new_ids)]
        changed = []
        for iid in new_ids & set(old.keys()):
            if current[iid].get("price") != old[iid].get("price"):
                changed.append({"old": old[iid], "new": current[iid]})
    else:
        added, removed, changed = _update_cache(args.db, items)

    if args.json:
        print(json.dumps({"added": added, "removed": removed, "changed": changed}, indent=2))
        return 0

    if not added and not removed and not changed:
        if not args.quiet:
            print("no changes since last check")
        return 0

    if added:
        print("NEW:")
        for it in added:
            print(f"  + {_fmt_item(it)}")
    if removed:
        print("GONE:")
        for it in removed:
            print(f"  - {_fmt_item(it)}")
    if changed:
        print("PRICE CHANGE:")
        for diff in changed:
            print(f"  ~ {_fmt_item(diff['old'])} -> {diff['new'].get('price', '?')} V-Bucks")

    return 1

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
