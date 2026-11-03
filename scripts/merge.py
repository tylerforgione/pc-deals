#!/usr/bin/env python3
"""Merge one day's finds into data/deals.json, score them, and export CSV.

Usage:
    python3 scripts/merge.py finds.json [--date YYYY-MM-DD]

finds.json is a JSON list. Each item (fields marked * are required):
  url*            listing URL (used as the unique id)
  title*          listing title
  category*       one of CATEGORIES below
  total_cad*      price incl. shipping, CAD (number)
  price_cad       price before shipping
  shipping_cad    shipping cost
  typical_cad     estimated typical current used/refurb price in Canada for the same item
  source          e.g. "eBay.ca", "Best Buy marketplace"
  location        seller / city
  condition       "Used", "Refurbished (Good)", "New", ...
  specs           short free-text spec line
  cpu             CPU model, e.g. "i5-8500T" (mini PCs, laptops, desktops)
  ram_gb          installed RAM in GB
  storage_gb      SSD capacity in GB
  capacity_gb     for memory / storage items: total GB in the listing
  sodimm_slots    number of DDR4/DDR5 SO-DIMM slots (mini PCs)
  m2_and_sata     true if it has both an M.2 slot and a 2.5" bay
  adapter         true / false / null  (power adapter included?)
  thin_client     true if it is a thin client
  fits_my_gear    true if it works with the owner's existing parts
  fit_note        why it fits / how it pairs with existing parts
  why             one line on why this is a good deal
  caveats         anything to watch out for
  available       false if a previously-listed item was checked and is sold/removed

Pass --no-age when the re-check of tracked listings failed (blocked pages); the script also
detects this automatically when fewer than 10% of tracked listings were re-checked.

Statuses: NEW, SEEN, PRICE DROP, PRICE UP, NOT SEEN (missing from today's sweep),
GONE (confirmed sold/removed, or missing for GONE_AFTER_DAYS days).
"""
import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "deals.json"
CSV_OUT = ROOT / "data" / "deals.csv"

CATEGORIES = [
    "Mini PC", "Laptop", "Desktop", "CPU", "GPU", "Motherboard", "Memory",
    "Storage", "Case", "Power supply", "Networking", "Server / Workstation", "Other",
]
GONE_AFTER_DAYS = 4      # missing this many days in a row -> GONE
DROP_GONE_AFTER_DAYS = 45  # remove GONE listings from the file after this long
MIN_DEAL_SCORE = 1.20     # non-mini-PC items must be >= ~17% below typical price
MINI_PC_SOFT_CAP = 250
MINI_PC_STRETCH_CAP = 300

# Mini PC CPU points (homelab value). Unknown CPUs fall back to cpu_points in the find.
CPU_POINTS = {
    r"i3-[67]\d{3}T?": 12, r"i5-[67]\d{3}T?": 20, r"i7-[67]\d{3}T?": 28,
    r"i3-[89]\d{3}T?": 25, r"i3-10\d{3}T?": 30,
    r"i5-[89]\d{3}T?": 45, r"i7-8\d{3}T?": 55, r"i7-9\d{3}T?": 60, r"i9-9\d{3}T?": 65,
    r"i5-10\d{3}T?": 60, r"i7-10\d{3}T?": 72, r"i9-10\d{3}T?": 75,
    r"i5-1[12]\d{3}T?": 75, r"i7-1[12]\d{3}T?": 85, r"i5-1[34]\d{3}T?": 85, r"i7-1[34]\d{3}T?": 95,
    r"ryzen \d pro 2\d{3}": 40, r"ryzen \d pro 3\d{3}": 48, r"ryzen \d pro 4\d{3}": 75,
    r"ryzen \d pro 5\d{3}": 82, r"ryzen \d 5\d{3}": 82, r"ryzen \d 7\d{3}": 90,
    r"n100|n150|n97": 35, r"n305|n300": 45,
}


def cpu_points(item):
    if item.get("cpu_points") is not None:
        return float(item["cpu_points"])
    cpu = (item.get("cpu") or "").lower()
    for pat, pts in CPU_POINTS.items():
        if re.search(pat, cpu, re.I):
            return float(pts)
    return 30.0


def mini_pc_points(item):
    pts = cpu_points(item)
    pts += 2.5 * min(float(item.get("ram_gb") or 0), 64)
    pts += min(0.04 * float(item.get("storage_gb") or 0), 40)
    if (item.get("sodimm_slots") or 0) >= 2:
        pts += 10
    if item.get("m2_and_sata"):
        pts += 5
    if item.get("adapter") is True:
        pts += 5
    elif item.get("adapter") is False:
        pts -= 15
    if item.get("thin_client"):
        pts -= 25
    return pts


def score(item):
    total = float(item["total_cad"])
    typical = item.get("typical_cad")
    item["deal_score"] = round(float(typical) / total, 2) if typical and total > 0 else None
    item["discount_pct"] = round((1 - total / float(typical)) * 100) if typical and total > 0 else None
    item["value_score"] = None
    if item["category"] == "Mini PC":
        item["value_score"] = round(mini_pc_points(item) / total * 100, 1)
    cap = item.get("capacity_gb")
    if item["category"] == "Memory" and cap:
        item["unit_price"] = f"${total / float(cap):.2f}/GB"
    elif item["category"] == "Storage" and cap:
        item["unit_price"] = f"${total / (float(cap) / 1000):.0f}/TB"
    # A single 0-100 rank number so the table can sort across categories.
    if item["category"] == "Mini PC":
        rank = (item["value_score"] or 0) * 1.3
        if item["deal_score"]:
            rank = rank * 0.7 + min(item["deal_score"], 2.5) * 40 * 0.3
    else:
        rank = min(item["deal_score"] or 1.0, 2.5) * 40
    if item.get("fits_my_gear"):
        rank += 5
    item["rank_score"] = round(min(rank, 100), 1)


def qualifies(item):
    total = float(item["total_cad"])
    if item["category"] == "Mini PC":
        if total <= MINI_PC_SOFT_CAP:
            return True
        # stretch: only standout machines (8th gen+ class CPU and 32GB+ or a big discount)
        strong = cpu_points(item) >= 45 and float(item.get("ram_gb") or 0) >= 32
        big_discount = (item.get("deal_score") or 0) >= 1.35
        return total <= MINI_PC_STRETCH_CAP and (strong or big_discount)
    return (item.get("deal_score") or 0) >= MIN_DEAL_SCORE


def norm_id(url):
    url = url.strip()
    url = re.sub(r"[?#].*$", "", url)
    url = re.sub(r"/ref=.*$", "", url)  # Amazon tracking suffix
    return url.rstrip("/").lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("finds")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--no-age", action="store_true",
                    help="re-checks failed today: don't count unseen listings toward GONE")
    args = ap.parse_args()
    today = args.date

    finds = json.loads(Path(args.finds).read_text())
    db = json.loads(DATA.read_text()) if DATA.exists() else {"listings": []}
    by_id = {l["id"]: l for l in db["listings"]}
    tracked_before = {lid for lid, l in by_id.items() if l.get("status") != "GONE"}
    seen_today = set()
    added = rejected = 0

    for f in finds:
        missing = [k for k in ("url", "title", "category", "total_cad") if f.get(k) in (None, "")]
        if missing:
            print(f"skip (missing {missing}): {f.get('title') or f.get('url')}", file=sys.stderr)
            continue
        if f["category"] not in CATEGORIES:
            f["category"] = "Other"
        lid = norm_id(f["url"])
        old = by_id.get(lid)

        if f.get("available") is False:
            if old:
                old["status"] = "GONE"
                old["gone_since"] = old.get("gone_since") or today
            continue

        score(f)
        if not old and not qualifies(f):
            rejected += 1
            continue
        seen_today.add(lid)

        if old:
            prev_total = float(old["total_cad"])
            history = old.get("price_history", [])
            if abs(float(f["total_cad"]) - prev_total) >= 0.5:
                history.append({"date": today, "total_cad": float(f["total_cad"])})
                status = "PRICE DROP" if float(f["total_cad"]) < prev_total else "PRICE UP"
            else:
                status = "SEEN"
            first_seen = old.get("first_seen", today)
            old.update(f)  # today's fields win; fields missing today are kept
            old["first_seen"] = first_seen
            old.update(id=lid, status=status, last_seen=today, price_history=history, missed_days=0)
            old.pop("gone_since", None)
        else:
            f.update(id=lid, status="NEW", first_seen=today, last_seen=today, missed_days=0,
                     price_history=[{"date": today, "total_cad": float(f["total_cad"])}])
            by_id[lid] = f
            added += 1

    # Safety: if almost nothing that was already tracked got re-checked today, the
    # re-check step most likely failed (blocked fetches), not the listings. Don't age them.
    rechecked = len(tracked_before & seen_today) + sum(
        1 for f in finds if f.get("available") is False and norm_id(f.get("url", "")) in tracked_before)
    no_age = args.no_age
    if not no_age and len(tracked_before) >= 10 and rechecked < 0.1 * len(tracked_before):
        no_age = True
        print(f"WARNING: only {rechecked}/{len(tracked_before)} tracked listings were re-checked; "
              "assuming the re-check failed and NOT aging unseen listings today.", file=sys.stderr)

    # Listings not seen today
    for lid, l in list(by_id.items()):
        if lid in seen_today or l.get("last_seen") == today and l["status"] != "GONE":
            continue
        if l["status"] == "GONE":
            since = dt.date.fromisoformat(l.get("gone_since", today))
            if (dt.date.fromisoformat(today) - since).days > DROP_GONE_AFTER_DAYS:
                del by_id[lid]
            continue
        if no_age:
            l["status"] = "NOT SEEN"  # not confirmed today, but don't count it toward GONE
            continue
        l["missed_days"] = l.get("missed_days", 0) + 1
        if l["missed_days"] >= GONE_AFTER_DAYS:
            l["status"] = "GONE"
            l["gone_since"] = today
        else:
            l["status"] = "NOT SEEN"

    listings = sorted(by_id.values(), key=lambda l: (l["status"] == "GONE", -(l.get("rank_score") or 0)))
    db = {"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
          "date": today, "listings": listings}
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(db, indent=1, ensure_ascii=False) + "\n")

    cols = ["status", "rank_score", "category", "title", "total_cad", "typical_cad", "discount_pct",
            "value_score", "unit_price", "fits_my_gear", "source", "location", "condition", "specs",
            "why", "fit_note", "caveats", "first_seen", "last_seen", "url"]
    with CSV_OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for l in listings:
            if l["status"] != "GONE":
                w.writerow(l)

    active = sum(l["status"] != "GONE" for l in listings)
    print(f"{today}: {added} new, {rejected} rejected (below the bar), {active} active, "
          f"{len(listings) - active} gone")


if __name__ == "__main__":
    main()
