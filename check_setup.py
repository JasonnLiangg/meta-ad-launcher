#!/usr/bin/env python3
"""Checks your .env against Meta and lists the IDs you need for it. Read-only: creates and changes nothing.

    .venv/bin/python check_setup.py              # checks .env
    .venv/bin/python check_setup.py --profile x  # checks .env.x
"""
import argparse
import sys

import launch_ads as L

ap = argparse.ArgumentParser()
ap.add_argument("--profile")
cfg = L.Config(ap.parse_args().profile)
env = ".env" if cfg.profile == "default" else f".env.{cfg.profile}"

problems = cfg.problems()
if problems:
    print("🛑 Fill these in first:")
    for p in problems:
        print(f"   {p}")
    sys.exit(1)

meta = L.Meta(cfg)
meta.max_wait = L.CHECK_WAIT_TOTAL
ok = True


def section(title):
    print(f"\n{title}")


def rows(path, fields):
    return meta.get(path, {"fields": fields, "limit": 200}).get("data", [])


try:
    L.load_account(meta, cfg)
except RuntimeError as ex:
    print(f"🛑 Meta refused the token or the ad account: {ex}")
    print("   Check META_ACCESS_TOKEN and META_AD_ACCOUNT_ID, and that the system user is assigned to the ad account.")
    sys.exit(1)
print(f"✅ Ad account: {cfg.name} ({cfg.account}) · currency {cfg.currency} · time zone {cfg.timezone}")

section("Pages you can advertise from (META_PAGE_ID):")
pages = {}
try:
    for pg in rows("me/accounts", "id,name,tasks"):
        if "ADVERTISE" in (pg.get("tasks") or []):
            pages[pg["id"]] = pg["name"]
    try:
        for pg in rows(f"{cfg.account}/promote_pages", "id,name"):
            pages.setdefault(pg["id"], pg["name"])
    except RuntimeError:
        pass
except RuntimeError as ex:
    print(f"   couldn't read Pages: {ex}")
for pid, name in sorted(pages.items(), key=lambda kv: kv[1].lower()):
    print(f"   {pid}  {name}{'   ← in ' + env if pid == cfg.page_id else ''}")
if not pages:
    ok = False
    print("   🛑 none. Assign your Page to the system user with the 'Create ads' permission (README, step 2).")
elif cfg.is_placeholder(cfg.page_id):
    print(f"   ⚠️  META_PAGE_ID is blank in {env}. Copy one of the IDs above into it.")
elif cfg.page_id not in pages:
    ok = False
    print(f"   🛑 META_PAGE_ID {cfg.page_id} isn't in this list.")

section("Pixels / datasets (META_PIXEL_ID):")
try:
    pixels = rows(f"{cfg.account}/adspixels", "id,name")
    for px in pixels:
        print(f"   {px['id']}  {px.get('name', '')}{'   ← in ' + env if px['id'] == cfg.pixel_id else ''}")
    if not pixels:
        print("   none found. Create one in Events Manager and connect it to your store.")
    if cfg.is_placeholder(cfg.pixel_id):
        print(f"   ⚠️  META_PIXEL_ID is blank in {env}. The launcher needs it to create ad sets.")
    elif pixels and cfg.pixel_id not in {px["id"] for px in pixels}:
        print(f"   ⚠️  META_PIXEL_ID {cfg.pixel_id} isn't in this list. Check it.")
except RuntimeError as ex:
    print(f"   couldn't read pixels: {ex}")

section("Instagram accounts (META_INSTAGRAM_USER_ID, optional):")
try:
    igs = rows(f"{cfg.account}/instagram_accounts", "id,username")
    for ig in igs:
        print(f"   {ig['id']}  @{ig.get('username', '')}{'   ← in ' + env if ig['id'] == cfg.ig_user_id else ''}")
    if not igs:
        print("   none connected to this ad account (fine: ads use the Page's own Instagram setup).")
except RuntimeError as ex:
    print(f"   couldn't read Instagram accounts (fine to skip): {ex}")

print("\n" + ("✅ Setup looks good. Next: .venv/bin/python launch_ads.py test/test_ad.csv --dry-run" if ok
              else "🛑 Fix the lines marked 🛑, then run this again."))
sys.exit(0 if ok else 1)
