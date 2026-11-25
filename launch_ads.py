#!/usr/bin/env python3
"""Bulk Meta ad launcher. One CSV row = one ad. Ad sets and ads are created PAUSED, or ACTIVE with --active (new campaigns stay PAUSED).

    python launch_ads.py ads.csv --dry-run     # validate + preview, creates nothing
    python launch_ads.py ads.csv               # launch

Order of work: validate -> upload media -> create creatives -> create ad sets -> create ads.
If validation finds any problem, nothing is launched. If any creative is rejected by Meta,
no ad sets or ads are created (fix and rerun; finished work is reused from the results log).
"""
import argparse
import csv
import hashlib
import struct
import json
import os
import sys
import threading
import time
import urllib.parse
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")  # urllib3/LibreSSL noise on macOS system Python
import logging
logging.getLogger().setLevel(logging.ERROR)  # SDK deprecation chatter

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
API_VERSION = "v26.0"
BATCH_SIZE = 50  # Meta's max calls per batch request

# --- Text limits -------------------------------------------------------------
# HARD = blocks the launch. RECOMMENDED = warning only (text past this gets truncated).
HARD = {"primary_text": 63000,  # long-form copy runs 10k–35k chars; Meta's own check at the creative step is the real gate
        "headline": 255, "description": 255, "ad_name": 400}
RECOMMENDED = {"primary_text": 125, "headline": 40, "description": 25}

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
CTAS = {"SHOP_NOW", "LEARN_MORE", "ORDER_NOW", "BUY_NOW", "GET_OFFER", "SIGN_UP", "SUBSCRIBE",
        "CONTACT_US", "DOWNLOAD", "SEE_MORE", "GET_QUOTE", "APPLY_NOW", "BOOK_TRAVEL"}
EVENTS = {"PURCHASE", "ADD_TO_CART", "INITIATED_CHECKOUT", "LEAD", "COMPLETE_REGISTRATION",
          "VIEW_CONTENT", "ADD_PAYMENT_INFO", "SEARCH"}

# Advantage+ creative features that change copy or visuals. Opted OUT by default so the ad
# runs exactly as written (nothing reworded or restyled without approval). Set CREATIVE_ENHANCEMENTS=meta_default to skip.
OPT_OUT_FEATURES = [
    "text_optimizations", "description_automation", "enhance_cta", "add_text_overlay",
    "image_touchups", "image_templates", "image_brightness_and_contrast", "image_animation",
    "image_background_gen", "image_uncrop", "image_text_translation", "text_translation",
    "translate_voiceover", "inline_comment", "creative_stickers", "reveal_details_over_time",
    "video_filtering", "video_uncrop", "product_extensions",
]

STATUS = "PAUSED"   # ad sets and ads; set to ACTIVE by --active. New campaigns are always created PAUSED.

THROTTLE_CODES = {4, 17, 32, 613} | set(range(80000, 80015))
TRANSIENT_CODES = {1, 2}
MAX_WAIT_TOTAL = 30 * 60  # give up backing off after 30 min; remaining rows marked "rerun later"
CHECK_WAIT_TOTAL = 90        # a Review (dry run) gives up after 90 s instead: retrying against a blocked account only keeps
                             # the block on, and the screen looks frozen for 10 minutes
RATE_LIMIT_MSG = ("the ad account has hit Meta's request limit (too many calls in the last hour). Your batch is fine and "
                  "nothing was sent. Wait about 15 minutes, then Re-check once: every extra check while it is blocked "
                  "keeps the limit on")
UPLOAD_WORKERS = 5  # files uploaded at the same time; each upload waits on Meta per piece, so one alone uses a fraction of the connection

RESULT_FIELDS = ["row", "key", "ad_name", "ad_set", "status", "ad_id", "creative_id",
                 "ad_set_id", "error", "updated"]

AD_SET_SETTING_COLS = ["campaign_id", "campaign_name", "campaign_daily_budget", "countries",
                       "age_min", "age_max", "genders", "daily_budget", "optimization_event",
                       "bid_strategy", "advantage_audience", "start_time"]


# --- Helpers -----------------------------------------------------------------
def say(msg=""):
    print(msg, flush=True)


def clean(v):
    return (v or "").strip()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def row_key(r):
    parts = [r["ad_set_id"] or r["ad_set_name"], r["page"], r["ad_name"], r["media_path"], r["thumbnail_path"],
             r["primary_text"], r["headline"], r["description"], r["link_url"], r["cta"], r["url_tags"],
             r.get("primary_text_options", ""), r.get("headline_options", "")]
    if r.get("media_path_9x16"):
        parts.append(r["media_path_9x16"])
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:16]


def short(s, n):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def media_kind(path):
    ext = Path(path).suffix.lower()
    return "video" if ext in VIDEO_EXT else "image" if ext in IMAGE_EXT else None


def image_size(path):
    """(width, height) of a png / gif / jpg read from its header, or None if it can't be read."""
    try:
        with open(path, "rb") as f:
            head = f.read(26)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", head[16:24])
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", head[6:10])
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    b = f.read(1)
                    while b and b != b"\xff":
                        b = f.read(1)
                    while b == b"\xff":
                        b = f.read(1)
                    if not b:
                        return None
                    m = b[0]
                    if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                        continue
                    seg = struct.unpack(">H", f.read(2))[0]
                    if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                        h, w = struct.unpack(">xHH", f.read(5))
                        return w, h
                    f.seek(seg - 2, 1)
    except Exception:
        pass
    return None


def image_shape(size):
    """feed = square up to 4:5 (Feed placements) · tall = 9:16-ish (Stories and Reels) · other."""
    if not size or not size[0]:
        return None
    ratio = size[1] / size[0]
    return "tall" if ratio >= 1.5 else "feed" if 0.95 <= ratio <= 1.3 else "other"


def resolve_path(p, base):
    p = Path(os.path.expanduser(p))
    return p if p.is_absolute() else (base / p)


def parse_text_options(row, name):
    """Parse a _options column (JSON list of texts). Returns the list, or None if not set."""
    raw = row.get(name + "_options", "").strip()
    if not raw:
        return None
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) < 2:
        raise ValueError("must be a JSON list with at least 2 texts")
    if len(values) > 5:
        raise ValueError(f"has {len(values)} texts (max 5)")
    if not all(isinstance(v, str) and v.strip() for v in values):
        raise ValueError("contains empty or non-text entries")
    if len(set(values)) != len(values):
        raise ValueError("has duplicate texts")
    if values[0] != row[name]:
        raise ValueError(f"first entry must match {name}")
    for v in values:
        if name in HARD and len(v) > HARD[name]:
            raise ValueError(f"a text exceeds {HARD[name]} chars")
    return values


# --- Config ------------------------------------------------------------------
class Config:
    def __init__(self, profile=None):
        env = HERE / (f".env.{profile}" if profile else ".env")
        if not env.is_file():
            sys.exit(f"No settings file {env.name} in {HERE}. Run ./setup.sh, or copy .env.example to {env.name} and fill it in (README, Setup)")
        self.profile = profile or "default"
        load_dotenv(env, override=True)
        self.token = clean(os.getenv("META_ACCESS_TOKEN"))
        acct = clean(os.getenv("META_AD_ACCOUNT_ID"))
        self.account = acct if acct.startswith("act_") else f"act_{acct}"
        self.page_id = clean(os.getenv("META_PAGE_ID"))
        self.pixel_id = clean(os.getenv("META_PIXEL_ID"))
        self.ig_user_id = clean(os.getenv("META_INSTAGRAM_USER_ID"))
        # Advertiser + payer shown in Meta's Ad Library (required for AU, EU, TW, SG and other regions).
        self.advertiser_id = clean(os.getenv("META_ADVERTISER_ID"))
        self.app_secret = clean(os.getenv("META_APP_SECRET"))
        self.enhancements = clean(os.getenv("CREATIVE_ENHANCEMENTS")) or "off"
        # Filled in from Meta by load_account(): the ad account's own name, currency and time zone
        self.name, self.currency, self.timezone, self.unit = "", "", "", 100

    @staticmethod
    def is_placeholder(v):
        return not v or "PASTE" in v.upper() or set(v.replace("act_", "")) <= {"X"}

    @property
    def has_token(self):
        return not self.is_placeholder(self.token)

    def problems(self):
        out = []
        if not self.has_token:
            out.append("META_ACCESS_TOKEN is missing in .env")
        if self.is_placeholder(self.account):
            out.append("META_AD_ACCOUNT_ID is missing in .env")
        return out

    @property
    def ads_manager_url(self):
        return f"https://adsmanager.facebook.com/adsmanager/manage/ads?act={self.account[4:]}"


# Meta takes budgets in the currency's smallest unit: cents for most currencies (x100), whole units for these
# (Meta Marketing API, "Currencies": offset 1).
WHOLE_UNIT_CURRENCIES = {"CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "VND"}


def load_account(meta, cfg):
    """Reads the ad account's name, currency and time zone, so budgets and start times are never assumed."""
    info = meta.get(cfg.account, {"fields": "name,currency,timezone_name"})
    cfg.name = info.get("name") or ""
    cfg.currency = (info.get("currency") or "").upper()
    cfg.timezone = info.get("timezone_name") or ""
    cfg.unit = 1 if cfg.currency in WHOLE_UNIT_CURRENCIES else 100


# --- Meta API wrapper with backoff ------------------------------------------
class Meta:
    def __init__(self, cfg):
        from facebook_business.api import FacebookAdsApi
        self.cfg = cfg
        # (connect, read) seconds. Without a timeout a dropped reply from Meta hangs the launch forever.
        self.api = FacebookAdsApi.init(access_token=cfg.token, app_secret=cfg.app_secret or None,
                                       api_version=API_VERSION, crash_log=False, timeout=(20, 120))
        self.waited = 0

    # Errors ------------------------------------------------------------------
    @staticmethod
    def parse_error(body):
        try:
            err = (json.loads(body) if isinstance(body, str) else body).get("error", {})
        except Exception:
            return {"code": None, "msg": short(str(body), 200), "transient": False}
        msg = err.get("error_user_msg") or err.get("message") or "unknown error"
        title = err.get("error_user_title")
        return {"code": err.get("code"), "sub": err.get("error_subcode"),
                "msg": f"{title}: {msg}" if title and title not in msg else msg,
                "transient": bool(err.get("is_transient"))}

    @staticmethod
    def wait_hint(headers):
        """Seconds Meta says to wait, from usage headers (dict or batch list form)."""
        if isinstance(headers, list):
            headers = {h.get("name", ""): h.get("value", "") for h in headers}
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        best = 0
        try:
            for bucket in json.loads(headers.get("x-business-use-case-usage", "{}")).values():
                for u in bucket:
                    best = max(best, int(u.get("estimated_time_to_regain_access", 0)) * 60)
        except Exception:
            pass
        try:
            best = max(best, int(json.loads(headers.get("x-ad-account-usage", "{}"))
                                 .get("reset_time_duration", 0)))
        except Exception:
            pass
        return best

    def retryable(self, e):
        return e["code"] in THROTTLE_CODES or e["code"] in TRANSIENT_CODES or e["transient"]

    def backoff(self, attempt, hint=0, why="Meta rate limit"):
        wait = max(hint, min(2 ** attempt * 5, 300))
        if self.waited + wait > getattr(self, "max_wait", MAX_WAIT_TOTAL):
            return False
        say(f"  ⏳ {why} — waiting {wait}s, then retrying")
        time.sleep(wait)
        self.waited += wait
        return True

    def call(self, fn, what, retry_blank=False):
        """Run a single SDK call with automatic backoff. Returns result or raises RuntimeError.
        retry_blank: also retry when Meta answers with a server error and no error code (safe only
        for calls that can be repeated, like sending a piece of a video)."""
        from facebook_business.exceptions import FacebookRequestError
        attempt = 0
        while True:
            try:
                return fn()
            except FacebookRequestError as ex:
                e = self.parse_error(ex.body())
                if self.retryable(e) and self.backoff(attempt, self.wait_hint(ex.http_headers())):
                    attempt += 1
                    continue
                if e["code"] in THROTTLE_CODES:   # still blocked after waiting: say so plainly instead of Meta's wording
                    raise RuntimeError(RATE_LIMIT_MSG)
                if e["code"] is None:
                    status = ex.http_status()
                    if (retry_blank and attempt < 6 and (status or 500) >= 500
                            and self.backoff(attempt, why=f"{what}: Meta server error (HTTP {status})")):
                        attempt += 1
                        continue
                    e["msg"] = e["msg"] or f"Meta gave an empty reply (HTTP {status})"
                raise RuntimeError(f"{what}: {e['msg']} (code {e['code']})")
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as ex:
                if self.backoff(attempt, why=f"{what}: no reply from Meta (network)"):
                    attempt += 1
                    continue
                raise RuntimeError(f"{what}: network error {ex}")

    def get(self, path, params=None):
        parts = tuple(x for x in path.split("/") if x)  # SDK treats a str path as a full URL
        return self.call(lambda: self.api.call("GET", parts, params=params or {}).json(), f"GET {path}")

    def post(self, path, params=None):
        parts = tuple(x for x in path.split("/") if x)
        return self.call(lambda: self.api.call("POST", parts, params=params or {}).json(), f"POST {path}")

    def batch(self, jobs):
        """jobs: list of dicts {method, path, params, ok(json), fail(msg)}. Throttled calls retry."""
        from facebook_business.exceptions import FacebookRequestError
        pending, attempt, total = list(jobs), 0, len(jobs)
        while pending:
            retry, hint = [], 0
            for i in range(0, len(pending), BATCH_SIZE):
                chunk = pending[i:i + BATCH_SIZE]
                answered = set()
                b = self.api.new_batch()
                for j in chunk:
                    def ok(resp, j=j):
                        answered.add(id(j))
                        j["ok"](resp.json())

                    def fail(resp, j=j):
                        nonlocal hint
                        answered.add(id(j))
                        e = self.parse_error(resp.body())
                        if self.retryable(e):
                            hint = max(hint, self.wait_hint(resp.headers()))
                            retry.append(j)
                        else:
                            j["fail"](f"{e['msg']} (code {e['code']})")
                    b.add(j["method"], j["path"], params=j.get("params"), success=ok, failure=fail)
                try:
                    b.execute()
                    retry.extend(j for j in chunk if id(j) not in answered)  # Meta gave no answer
                except FacebookRequestError as ex:
                    e = self.parse_error(ex.body())
                    if self.retryable(e):
                        hint = max(hint, self.wait_hint(ex.http_headers()))
                        retry.extend(chunk)
                    else:
                        for j in chunk:
                            j["fail"](f"{e['msg']} (code {e['code']})")
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                    retry.extend(chunk)
                say(f"  ✓ {total - len(pending) + i + len(chunk) - len(retry)}/{total} sent to Meta")
            pending = retry
            if pending and not self.backoff(attempt, hint):
                for j in pending:
                    j["fail"]("Meta rate limit didn't clear in 30 min — rerun later (finished rows are skipped)")
                return
            attempt += 1


# --- Loading + validation ----------------------------------------------------
def load_rows(csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = [clean(h) for h in (reader.fieldnames or [])]
        rows = []
        for i, raw in enumerate(reader, start=2):  # row 1 is the header
            r = {clean(k): clean(v) for k, v in raw.items() if k}
            if not any(r.values()):
                continue
            for col in ["ad_set_id", "ad_set_name", "page", "ad_name", "media_path", "thumbnail_path",
                        "primary_text", "headline", "description", "link_url", "cta",
                        "url_tags", "primary_text_options", "headline_options", "media_path_9x16"] + AD_SET_SETTING_COLS:
                r.setdefault(col, "")
            r["ad_set_id"] = r["ad_set_id"].replace("'", "")
            r["cta"] = (r["cta"] or "SHOP_NOW").upper().replace(" ", "_")
            r["line"] = i
            r["key"] = row_key(r)
            rows.append(r)
    return headers, rows


LINK_WARN = "warn: "   # check_link prefix for "couldn't confirm, but not broken"


LINK_OK_FILE = HERE / ".link_ok.json"   # landing pages that loaded recently: {url: unix time}
LINK_OK_HOURS = 12


def link_ok_recently(url):
    """True if this exact link loaded fine in the last LINK_OK_HOURS. Review, Re-check and Launch each check the link,
    and a few checks in a row is what makes Shopify throttle (HTTP 429), so a link that just passed isn't loaded again."""
    try:
        return time.time() - float(json.loads(LINK_OK_FILE.read_text()).get(url, 0)) < LINK_OK_HOURS * 3600
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def remember_link_ok(url):
    try:
        try:
            seen = json.loads(LINK_OK_FILE.read_text())
        except (OSError, ValueError):
            seen = {}
        if not isinstance(seen, dict):
            seen = {}
        now = time.time()
        seen = {u: t for u, t in seen.items() if isinstance(t, (int, float)) and now - t < LINK_OK_HOURS * 3600}
        seen[url] = now
        LINK_OK_FILE.write_text(json.dumps(seen, indent=1))
    except OSError:
        pass   # remembering is a convenience; never fail a check over it


def check_link(url, cache):
    """None if the page loads, an error message if it's broken, or a LINK_WARN message if the
    store only throttled the check (429/503 means the page is there).

    Shopify throttles a request that claims to be a browser but doesn't behave like one, and answers
    a plain, honest request. So a throttled check is retried plainly, and a product link is then
    confirmed from the product's small data file (<link>.js), which says for certain whether it's live."""
    if url in cache:
        return cache[url]
    if link_ok_recently(url):
        cache[url] = None
        return None
    browser = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 (ad-launcher link check)",
               "Accept": "text/html,application/xhtml+xml"}
    status = None
    for wait, headers in ((0, browser), (0, None), (4, None)):
        time.sleep(wait)
        try:
            status = requests.get(url, timeout=12, allow_redirects=True, headers=headers).status_code
        except Exception as ex:
            cache[url] = f"landing page didn't load ({type(ex).__name__})"
            return cache[url]
        if status not in (429, 503):
            break
    throttled = status
    if status in (429, 503) and "/products/" in url:
        try:
            alt = requests.get(url.split("?")[0].split("#")[0].rstrip("/") + ".js", timeout=12)
            if alt.status_code == 200 and alt.json().get("handle"):
                status = 200
            elif alt.status_code == 404:
                status = 404
        except Exception:
            pass
    if status < 400:
        cache[url] = None
        remember_link_ok(url)
    elif status in (429, 503):
        cache[url] = (f"{LINK_WARN}{url} — the store rate-limited the link check (HTTP {status}). "
                      "The page exists; open it once in your browser to be sure")
    elif status != throttled:
        cache[url] = "landing page returned HTTP 404 (the store has no live product at this link)"
    else:
        cache[url] = f"landing page returned HTTP {status}"
    return cache[url]


def validate(rows, base, check_links=True):
    errors, warns = [], []

    def err(r, msg):
        errors.append(f"row {r['line']} ({r['ad_name'] or 'no name'}): {msg}")

    if not rows:
        errors.append("CSV has no ad rows")
    link_cache, seen_keys, seen_names, long_text, link_warned = {}, {}, {}, {}, set()
    for r in rows:
        if not (r["ad_set_id"] or r["ad_set_name"]):
            err(r, "needs ad_set_id or ad_set_name")
        if r["ad_set_id"] and not r["ad_set_id"].isdigit():
            err(r, f"ad_set_id '{r['ad_set_id']}' isn't a numeric ID")
        for col in ["ad_name", "media_path", "primary_text", "headline", "link_url"]:
            if not r[col]:
                err(r, f"{col} is empty")
        for col, lim in HARD.items():
            if len(r[col]) > lim:
                err(r, f"{col} is {len(r[col])} chars (Meta max {lim})")
        for col, lim in RECOMMENDED.items():
            if len(r[col]) > lim:
                long_text.setdefault((col, r[col], lim), []).append(r["line"])
        if r["cta"] not in CTAS:
            err(r, f"cta '{r['cta']}' isn't a Meta CTA ({', '.join(sorted(CTAS))})")

        # Text options (multiple primary texts / headlines per ad)
        for opt_col in ["primary_text", "headline"]:
            try:
                parse_text_options(r, opt_col)
            except (json.JSONDecodeError, ValueError, TypeError) as ex:
                err(r, f"{opt_col}_options: {ex}")

        # Media
        if r["media_path"]:
            p = resolve_path(r["media_path"], base)
            kind = media_kind(p)
            if not p.is_file():
                err(r, f"file not found: {p}")
            elif kind is None:
                err(r, f"unsupported file type {p.suffix} (images: jpg/png/gif · video: mp4/mov/m4v)")
            r["_media"], r["_kind"] = p, kind
        if r["thumbnail_path"]:
            t = resolve_path(r["thumbnail_path"], base)
            if not t.is_file():
                err(r, f"thumbnail not found: {t}")
            elif media_kind(t) != "image":
                err(r, "thumbnail_path must be an image")
            r["_thumb"] = t
        # Second image for Stories and Reels: one ad, the media_path image in feeds and this one full-screen
        if r["media_path_9x16"]:
            a = resolve_path(r["media_path_9x16"], base)
            if not a.is_file():
                err(r, f"9:16 image not found: {a}")
            elif media_kind(a) != "image" or r.get("_kind") != "image":
                err(r, "media_path_9x16 pairs two images (a feed image plus a 9:16 image); videos can't be paired")
            else:
                r["_alt"] = a
                main, alt = image_shape(image_size(r["_media"])), image_shape(image_size(a))
                if alt and alt != "tall":
                    err(r, f"{a.name} is in the 9:16 slot but isn't a tall image (it would be cropped in Stories and Reels)")
                elif main == "tall":
                    err(r, f"{r['_media'].name} is in the feed slot but is a tall image. Put the square one in media_path and the tall one in media_path_9x16")
                elif main == "other":
                    warns.append(f"row {r['line']}: {r['_media'].name} isn't square or 4:5, so feeds may crop it")

        # URL
        u = urllib.parse.urlparse(r["link_url"])
        if r["link_url"] and (u.scheme not in ("http", "https") or "." not in u.netloc):
            err(r, f"link_url isn't a valid URL: {r['link_url']}")
        elif r["link_url"] and check_links:
            problem = check_link(r["link_url"], link_cache)
            if problem and problem.startswith(LINK_WARN):
                if problem not in link_warned:
                    link_warned.add(problem)
                    warns.append(problem[len(LINK_WARN):])
            elif problem:
                err(r, problem)
        if r["url_tags"] and ("?" in r["url_tags"] or " " in r["url_tags"]):
            err(r, "url_tags should look like utm_source=fb&utm_campaign=x (no '?' or spaces)")

        # Duplicates
        nk = (r["ad_set_id"] or r["ad_set_name"], r["ad_name"])
        if r["key"] in seen_keys:
            err(r, f"exact duplicate of row {seen_keys[r['key']]}")
        elif nk in seen_names:
            err(r, f"same ad name in same ad set as row {seen_names[nk]}")
        seen_keys.setdefault(r["key"], r["line"])
        seen_names.setdefault(nk, r["line"])

    # One line per long text, not one per row: long copy is normal, Meta just adds "See more"
    for (col, text, lim), lines in long_text.items():
        where = f"rows {lines[0]}–{lines[-1]}" if len(lines) > 1 else f"row {lines[0]}"
        what = "primary text" if col == "primary_text" else col.replace("_", " ")
        snippet = text[:40].replace("\n", " ")
        warns.append(f"{what} “{snippet}…” is {len(text)} chars ({where}). Meta shows the first ~{lim}, "
                     f"then “See more”. Fine for long copy")

    # Ad set settings must agree across rows that create the same new ad set
    groups = {}
    for r in rows:
        if not r["ad_set_id"] and r["ad_set_name"]:
            groups.setdefault(r["ad_set_name"], []).append(r)
    for name, rs in groups.items():
        settings = {tuple(r[c] for c in AD_SET_SETTING_COLS) for r in rs if any(r[c] for c in AD_SET_SETTING_COLS)}
        if len(settings) > 1:
            errors.append(f"ad set '{name}': rows disagree on ad set settings — make them identical")
        for r in rs:
            for c in ["age_min", "age_max", "daily_budget", "advantage_audience"]:
                if r[c]:
                    try:
                        float(r[c])
                    except ValueError:
                        err(r, f"{c} '{r[c]}' isn't a number")
            if r["optimization_event"] and r["optimization_event"].upper() not in EVENTS:
                err(r, f"optimization_event '{r['optimization_event']}' not one of {', '.join(sorted(EVENTS))}")
            if r["start_time"]:
                try:
                    datetime.fromisoformat(r["start_time"])
                except ValueError:
                    err(r, f"start_time '{r['start_time']}' should look like 2026-10-10T00:00:00-04:00")
            if r["genders"] and r["genders"].lower() not in ("all", "female", "male", "women", "men"):
                err(r, "genders must be all, female or male")
            for cc in (r["countries"] or "US").split(","):
                cc = cc.strip()
                if cc.upper() == "UK":
                    err(r, "country 'UK' isn't a Meta code. Use GB for the United Kingdom")
                elif len(cc) != 2 or not cc.isalpha():
                    err(r, f"country '{cc}' should be a 2-letter code (US, CA)")
    # Campaign settings for new CBO campaigns
    for r in rows:
        if r["campaign_daily_budget"] and not r["campaign_name"]:
            err(r, "campaign_daily_budget without campaign_name — add campaign_name or remove the budget")
    camp_groups = {}
    for r in rows:
        if r["campaign_name"]:
            camp_groups.setdefault(r["campaign_name"], []).append(r)
    for name, rs in camp_groups.items():
        # campaign_daily_budget filled = CBO; blank on every row = ABO (each ad set carries its own daily_budget)
        budgets = {r["campaign_daily_budget"] for r in rs if r["campaign_daily_budget"]}
        if len(budgets) > 1:
            errors.append(f"campaign '{name}': rows disagree on campaign_daily_budget")
        if budgets and any(not r["campaign_daily_budget"] for r in rs):
            errors.append(f"campaign '{name}': some rows have campaign_daily_budget and some don't — "
                          f"fill it on every row (CBO) or leave it blank on every row (ABO)")
        abo = not budgets
        for r in rs:
            if r["campaign_id"]:
                err(r, "has both campaign_id and campaign_name — use one or the other")
            if abo:
                if not r["daily_budget"] and not r["ad_set_id"]:
                    err(r, f"campaign_name '{name}' has no campaign_daily_budget, so it's created as ABO — "
                           f"fill this ad set's daily_budget (or add campaign_daily_budget for a CBO)")
                continue
            try:
                v = float(r["campaign_daily_budget"])
                if v <= 0:
                    err(r, "campaign_daily_budget must be positive")
            except ValueError:
                err(r, f"campaign_daily_budget '{r['campaign_daily_budget']}' isn't a number")
            if r["daily_budget"]:
                err(r, "campaign_daily_budget makes this a CBO — leave ad set daily_budget blank (or clear campaign_daily_budget for an ABO)")
    for r in rows:
        if r["daily_budget"]:
            try:
                if float(r["daily_budget"]) <= 0:
                    err(r, "daily_budget must be positive")
            except ValueError:
                pass  # already reported above
    return errors, warns


def resolve_campaigns(meta, cfg, rows):
    """Resolve campaign_name to campaign_id. Existing campaigns are reused; new ones are marked for creation."""
    errors = []
    names = sorted({r["campaign_name"] for r in rows if r["campaign_name"]})
    if not names:
        return errors
    existing, after = {}, None
    while True:
        params = {"fields": "id,name,daily_budget,lifetime_budget,effective_status", "limit": 200}
        if after:
            params["after"] = after
        page = meta.get(f"{cfg.account}/campaigns", params)
        for c in page.get("data", []):
            if c.get("effective_status") not in ("DELETED", "ARCHIVED"):
                existing.setdefault(c["name"], []).append(c)
        paging = page.get("paging", {})
        after = paging.get("cursors", {}).get("after") if paging.get("next") else None
        if not after:
            break
    for name in names:
        rs = [r for r in rows if r["campaign_name"] == name]
        matches = existing.get(name, [])
        want_cbo = any(r["campaign_daily_budget"] for r in rs)
        if len(matches) == 1:
            camp = matches[0]
            cbo = bool(camp.get("daily_budget") or camp.get("lifetime_budget"))
            if cbo != want_cbo:
                have, want = ("CBO", "ABO") if cbo else ("ABO", "CBO")
                errors.append(f"campaign '{name}' already exists as {have}, but these rows set it up as {want} — "
                              f"{'clear the ad set daily_budget and add campaign_daily_budget' if cbo else 'clear campaign_daily_budget and give each ad set a daily_budget'}, or use a new campaign name")
            else:
                say(f"  campaign '{name}' exists ({camp['id']}) — reusing")
                for r in rs:
                    r["campaign_id"] = camp["id"]
                    r["_campaign_label"] = name
        elif len(matches) > 1:
            errors.append(f"campaign name '{name}' matches {len(matches)} campaigns — use campaign_id instead")
        else:
            if want_cbo:
                budget = next(r["campaign_daily_budget"] for r in rs if r["campaign_daily_budget"])
                say(f"  campaign '{name}' is new — will create as CBO ({budget} {cfg.currency}/day)")
            else:
                n_sets = len({r["ad_set_name"] for r in rs})
                total = sum(float(b) for b in {r["ad_set_name"]: r["daily_budget"] for r in rs}.values() if b)
                say(f"  campaign '{name}' is new — will create as ABO ({n_sets} ad set(s), {total:g} {cfg.currency}/day in total)")
            for r in rs:
                r["_create_campaign"] = True
                r["_campaign_label"] = f"{name} (NEW)"
    return sorted(set(errors), key=errors.index)


def resolve_ad_sets(meta, cfg, rows):
    """Confirm ad_set_ids exist in this account; map ad_set_names to existing ad sets.
    Names that don't exist become 'to create' (needs campaign_id). Read-only."""
    errors = []
    ids = sorted({r["ad_set_id"] for r in rows if r["ad_set_id"]})
    found = {}  # one batched GET per ad set (the ?ids= lookup was removed in v26.0)
    meta.batch([{"method": "GET", "path": f"{sid}?fields=id,name,account_id,effective_status",
                 "ok": lambda j, sid=sid: found.__setitem__(sid, j), "fail": lambda m: None} for sid in ids])
    for sid in ids:
        info = found.get(sid)
        if not info:
            errors.append(f"ad set {sid} not found (or token can't see it)")
        elif f"act_{info.get('account_id')}" != cfg.account:
            errors.append(f"ad set {sid} belongs to another ad account (act_{info.get('account_id')})")
        elif info.get("effective_status") in ("DELETED", "ARCHIVED"):
            errors.append(f"ad set {sid} '{info.get('name')}' is {info['effective_status'].lower()}")
        else:
            for r in rows:
                if r["ad_set_id"] == sid:
                    r["_ad_set_label"] = info.get("name", sid)

    names = sorted({r["ad_set_name"] for r in rows if not r["ad_set_id"] and r["ad_set_name"]})
    if names:
        existing, after = {}, None
        while True:
            params = {"fields": "id,name,campaign_id,effective_status", "limit": 500,
                      "filtering": json.dumps([{"field": "effective_status", "operator": "NOT_IN",
                                                "value": ["DELETED", "ARCHIVED"]}])}
            if after:
                params["after"] = after
            page = meta.get(f"{cfg.account}/adsets", params)
            for a in page.get("data", []):
                existing.setdefault(a["name"], []).append(a)
            after = page.get("paging", {}).get("cursors", {}).get("after") if page.get("paging", {}).get("next") else None
            if not after:
                break
        for name in names:
            rs = [r for r in rows if r["ad_set_name"] == name and not r["ad_set_id"]]
            if any(r.get("_create_campaign") for r in rs):
                # A campaign that doesn't exist yet has no ad sets; never match same-named ones elsewhere.
                for r in rs:
                    r["_create_ad_set"], r["_ad_set_label"] = True, f"{name} (NEW)"
                continue
            camp = next((r["campaign_id"] for r in rs if r["campaign_id"]), "")
            matches = [a for a in existing.get(name, []) if not camp or a["campaign_id"] == camp]
            if len(matches) == 1:
                for r in rs:
                    r["ad_set_id"], r["_ad_set_label"] = matches[0]["id"], name
            elif len(matches) > 1:
                errors.append(f"ad set name '{name}' matches {len(matches)} ad sets — use ad_set_id or add campaign_id")
            elif not camp:
                errors.append(f"ad set '{name}' doesn't exist; to create it, fill campaign_id or campaign_name on its rows")
            else:
                for r in rs:
                    r["_create_ad_set"], r["_ad_set_label"] = True, f"{name} (NEW)"
        camps = sorted({r["campaign_id"] for r in rows if r.get("_create_ad_set") and r["campaign_id"]})
        for c in camps:
            try:
                info = meta.get(c, {"fields": "id,name,account_id,daily_budget,lifetime_budget"})
                if f"act_{info.get('account_id')}" != cfg.account:
                    errors.append(f"campaign {c} belongs to another ad account")
                cbo = bool(info.get("daily_budget") or info.get("lifetime_budget"))
                for r in rows:
                    if r.get("_create_ad_set") and r["campaign_id"] == c:
                        if cbo and r["daily_budget"]:
                            errors.append(f"row {r['line']}: campaign '{info.get('name')}' uses campaign budget (CBO) — leave daily_budget blank")
                        if not cbo and not r["daily_budget"]:
                            errors.append(f"row {r['line']}: campaign '{info.get('name')}' has no campaign budget — fill daily_budget")
            except RuntimeError as ex:
                errors.append(f"campaign {c}: {ex}")
    return sorted(set(errors), key=errors.index)


# Countries where Meta requires an advertiser + payer (Meta help: "About advertiser and payer
# transparency for ads", checked 2026-11-17): the EU and its territories, AU, IN, MY, SG, TH, TW.
TRANSPARENCY_COUNTRIES = set("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE "
                             "AX GF GP MQ RE YT MF AU IN MY SG TH TW".split())


def ad_set_countries(r):
    return [c.strip().upper() for c in (r["countries"] or "US").split(",") if c.strip()]


def needs_advertiser(r):
    return sorted(set(ad_set_countries(r)) & TRANSPARENCY_COUNTRIES)


def resolve_advertiser(meta, cfg):
    """Advertiser/payer identity for new ad sets. Without it, ad sets targeting AU, the EU and other
    regulated regions run with 'Location limited' and an 'Select the advertiser and payer' error.
    Uses META_ADVERTISER_ID, else copies it from the account's newest ad set that has one."""
    if not cfg.is_placeholder(cfg.advertiser_id):
        return []
    page = meta.get(f"{cfg.account}/adsets", {"fields": "regional_regulation_identities", "limit": 100})
    for a in page.get("data", []):
        ids = a.get("regional_regulation_identities") or {}
        if ids.get("universal_beneficiary"):
            cfg.advertiser_id = ids["universal_beneficiary"]
            cfg.payer_id = ids.get("universal_payer") or ids["universal_beneficiary"]
            return []
    return ["config: no advertiser/payer found — set it once on any ad set in Ads Manager "
            "(Ad transparency), or put META_ADVERTISER_ID in .env"]


def resolve_pages(meta, cfg, rows):
    """Each row's `page` (a name or ID, blank = META_PAGE_ID) -> a Page this account can advertise from."""
    errors = []
    pages = {}
    if meta:
        # Pages the token's system user is assigned with ADVERTISE. promote_pages alone flaps between calls,
        # so it only adds to this list.
        for pg in meta.get("me/accounts", {"fields": "id,name,tasks", "limit": 200}).get("data", []):
            if "ADVERTISE" in (pg.get("tasks") or []):
                pages[pg["id"]] = pg["name"]
        try:
            for pg in meta.get(f"{cfg.account}/promote_pages", {"fields": "id,name", "limit": 200}).get("data", []):
                pages.setdefault(pg["id"], pg["name"])
        except RuntimeError:
            pass
    by_name = {}
    for pid, name in pages.items():
        by_name.setdefault(name.strip().lower(), []).append(pid)
    for r in rows:
        want = r["page"] or ("" if cfg.is_placeholder(cfg.page_id) else cfg.page_id)
        if not want:
            errors.append(f"row {r['line']}: no page — fill the page column or META_PAGE_ID in .env")
            continue
        if not meta:  # offline dry run: trust IDs, can't look up names
            if want.isdigit():
                r["_page_id"], r["_page_label"] = want, want
            else:
                errors.append(f"row {r['line']}: page '{want}' can only be looked up with a token in .env")
            continue
        ids = [want] if want in pages else by_name.get(want.lower(), [])
        if len(ids) == 1:
            r["_page_id"], r["_page_label"] = ids[0], pages[ids[0]]
        elif len(ids) > 1:
            errors.append(f"row {r['line']}: {len(ids)} pages are named '{want}' — use the page ID")
        else:
            errors.append(f"row {r['line']}: page '{want}' isn't one this ad account can advertise from "
                          f"({', '.join(pages.values()) or 'none found'})")
    return sorted(set(errors), key=errors.index)


# --- Results log -------------------------------------------------------------
class Log:
    def __init__(self, path):
        self.path = path
        self.rows = {}
        if path.exists():
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    self.rows[r["key"]] = r

    def done(self, key):
        r = self.rows.get(key)
        return r if r and r.get("status") == "created" and r.get("ad_id") else None

    def set(self, row, **kw):
        cur = self.rows.get(row["key"], {})
        cur.update({"row": row["line"], "key": row["key"], "ad_name": row["ad_name"],
                    "ad_set": row.get("_ad_set_label") or row["ad_set_id"] or row["ad_set_name"],
                    "updated": datetime.now().isoformat(timespec="seconds")})
        cur.update({k: v for k, v in kw.items() if v is not None})
        self.rows[row["key"]] = cur

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=RESULT_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in sorted(self.rows.values(), key=lambda r: int(r.get("row") or 0)):
                w.writerow(r)
        tmp.replace(self.path)


# --- Media -------------------------------------------------------------------
class MediaCache:
    """sha256 of file -> uploaded hash / video id, per ad account. Survives reruns."""

    def __init__(self, account):
        self.path, self.account = HERE / ".media_cache.json", account
        try:
            self.data = json.loads(self.path.read_text())
        except Exception:
            self.data = {}
        self.data.setdefault(account, {})

    def get(self, digest):
        return self.data[self.account].get(digest)

    def put(self, digest, value):
        self.data[self.account][digest] = value
        self.path.write_text(json.dumps(self.data, indent=1))


def upload_video(meta, cfg, path):
    """Sends one video to Meta in the pieces Meta asks for and returns the video id.
    A rate limit, server error or dropped reply retries that one piece, so what's already sent
    is never sent again (the SDK's own upload restarts the whole file)."""
    from facebook_business.exceptions import FacebookRequestError
    what = f"upload {path.name}"

    def send(params, files=None):
        def go():
            try:
                return meta.api.call("POST", (cfg.account, "advideos"), params=params, files=files,
                                     url_override="https://graph-video.facebook.com").json()
            except FacebookRequestError as ex:
                # Meta: "wrong position, continue from here" — it already has part of this piece
                body = ex.body()
                data = body.get("error", {}).get("error_data") if isinstance(body, dict) else None
                if (params.get("upload_phase") == "transfer" and isinstance(data, dict)
                        and "start_offset" in data and "end_offset" in data):
                    return data
                raise
        return meta.call(go, what, retry_blank=True)

    s = send({"upload_phase": "start", "file_size": path.stat().st_size})
    session, vid = s["upload_session_id"], s["video_id"]
    start, end = int(s["start_offset"]), int(s["end_offset"])
    with open(path, "rb") as f:
        stuck = 0
        while start != end:
            f.seek(start)
            piece = f.read(end - start)
            r = send({"upload_phase": "transfer", "upload_session_id": session, "start_offset": start},
                     {"video_file_chunk": (path.name, piece, "multipart/form-data")})
            was = start
            start, end = int(r["start_offset"]), int(r["end_offset"])
            stuck = stuck + 1 if start <= was else 0
            if stuck > 5:
                raise RuntimeError(f"{what}: Meta stopped accepting the file partway — rerun to try again")
    send({"upload_phase": "finish", "upload_session_id": session, "title": path.name})
    return vid


def upload_media(meta, cfg, rows):
    """Uploads each distinct file once. Returns {path: {...}} and {path: error}."""
    from facebook_business.adobjects.adimage import AdImage
    cache = MediaCache(cfg.account)
    files = {}
    for r in rows:
        files.setdefault(r["_media"], r["_kind"])
        if r.get("_thumb"):
            files.setdefault(r["_thumb"], "image")
        if r.get("_alt"):
            files.setdefault(r["_alt"], "image")
    out, errs, videos = {}, {}, []
    lock = threading.Lock()   # guards out/errs/videos and the cache file across upload threads

    def one(i, path, kind):
        digest = sha256(path)
        with lock:
            hit = cache.get(digest)
        if hit and (kind == "image" or hit.get("ready")):
            with lock:
                out[path] = hit
            say(f"  [{i}/{len(files)}] {path.name} — already uploaded, reusing")
            return
        say(f"  [{i}/{len(files)}] uploading {path.name}")
        try:
            if kind == "image":
                img = AdImage(parent_id=cfg.account)
                img[AdImage.Field.filename] = str(path)
                meta.call(img.remote_create, f"upload {path.name}")
                with lock:
                    out[path] = {"kind": "image", "hash": img[AdImage.Field.hash]}
                    cache.put(digest, out[path])
                say(f"  ✓ {path.name} uploaded")
            else:
                if hit:  # uploaded before but processing never confirmed
                    vid = hit["video_id"]
                else:
                    vid = upload_video(meta, cfg, path)
                    with lock:
                        cache.put(digest, {"kind": "video", "video_id": vid, "ready": False})
                say(f"  ✓ {path.name} uploaded")
                with lock:
                    videos.append((path, digest, vid))
        except Exception as ex:
            with lock:
                errs[path] = str(ex)

    # A few files at once: one upload rarely fills the connection, and Meta's per-file
    # handshakes overlap instead of queueing
    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
        for f in [pool.submit(one, i, path, kind) for i, (path, kind) in enumerate(files.items(), 1)]:
            f.result()

    if videos:
        say(f"  waiting for Meta to process {len(videos)} video(s)…")
        deadline = time.time() + 45 * 60
        waiting = list(videos)
        while waiting and time.time() < deadline:
            still = []
            for path, digest, vid in waiting:
                try:
                    st = meta.get(vid, {"fields": "status"}).get("status", {})
                except RuntimeError as ex:
                    errs[path] = str(ex)
                    continue
                s = st.get("video_status")
                if s == "ready":
                    thumb = None
                    try:
                        th = meta.get(f"{vid}/thumbnails", {"fields": "uri,is_preferred"}).get("data", [])
                        th = sorted(th, key=lambda t: not t.get("is_preferred"))
                        thumb = th[0]["uri"] if th else None
                    except RuntimeError:
                        pass
                    out[path] = {"kind": "video", "video_id": vid, "thumb_url": thumb, "ready": True}
                    cache.put(digest, out[path])
                    say(f"  ✓ {path.name} processed")
                elif s == "error":
                    errs[path] = f"Meta couldn't process {path.name}: {json.dumps(st.get('processing_phase', {}).get('error', ''))[:150]}"
                else:
                    still.append((path, digest, vid))
            waiting = still
            if waiting:
                time.sleep(15)
        for path, _, _ in waiting:
            errs[path] = f"{path.name} still processing after 45 min — rerun later to continue"
    return out, errs


# --- Build payloads ----------------------------------------------------------
# Placements that show an ad full-screen. A paired ad shows its 9:16 image here and its feed image everywhere else.
TALL_PLACEMENTS = {"publisher_platforms": ["facebook", "instagram", "messenger"],
                   "facebook_positions": ["story", "facebook_reels"],
                   "instagram_positions": ["story", "reels"],
                   "messenger_positions": ["story"]}


def paired_creative_params(cfg, r, media):
    """One ad, two images (Meta's placement asset customization): the 9:16 in Stories and Reels,
    the feed image in every other placement. All of the ad lives in asset_feed_spec."""
    bodies = parse_text_options(r, "primary_text") or [r["primary_text"]]
    titles = parse_text_options(r, "headline") or [r["headline"]]
    afs = {"ad_formats": ["SINGLE_IMAGE"],
           "images": [{"hash": media[r["_media"]]["hash"], "adlabels": [{"name": "feed"}]},
                      {"hash": media[r["_alt"]]["hash"], "adlabels": [{"name": "tall"}]}],
           "bodies": [{"text": t} for t in bodies],
           "titles": [{"text": t} for t in titles],
           "link_urls": [{"website_url": r["link_url"]}],
           "call_to_action_types": [r["cta"]],
           "asset_customization_rules": [
               {"customization_spec": TALL_PLACEMENTS, "image_label": {"name": "tall"}, "priority": 1},
               {"customization_spec": {}, "image_label": {"name": "feed"}, "priority": 2}]}
    if r["description"]:
        afs["descriptions"] = [{"text": r["description"]}]
    spec = {"page_id": r["_page_id"]}
    if cfg.ig_user_id and r["_page_id"] == cfg.page_id:
        spec["instagram_user_id"] = cfg.ig_user_id
    params = {"name": r["ad_name"], "object_story_spec": spec, "asset_feed_spec": afs,
              "contextual_multi_ads": {"enroll_status": "OPT_OUT"}}
    if r["url_tags"]:
        params["url_tags"] = r["url_tags"]
    if cfg.enhancements != "meta_default":
        params["degrees_of_freedom_spec"] = {"creative_features_spec": {
            f: {"enroll_status": "OPT_OUT"} for f in OPT_OUT_FEATURES}}
    return params


def creative_params(cfg, r, media):
    if r.get("_alt"):
        return paired_creative_params(cfg, r, media)
    m = media[r["_media"]]
    cta = {"type": r["cta"], "value": {"link": r["link_url"]}}
    if m["kind"] == "image":
        data = {"link": r["link_url"], "message": r["primary_text"], "name": r["headline"],
                "image_hash": m["hash"]}
        if r["description"]:
            data["description"] = r["description"]
        data["call_to_action"] = cta
        spec = {"page_id": r["_page_id"], "link_data": data}
    else:
        data = {"video_id": m["video_id"], "message": r["primary_text"], "title": r["headline"]}
        if r.get("_thumb"):
            data["image_hash"] = media[r["_thumb"]]["hash"]
        elif m.get("thumb_url"):
            data["image_url"] = m["thumb_url"]
        if r["description"]:
            data["link_description"] = r["description"]
        data["call_to_action"] = cta
        spec = {"page_id": r["_page_id"], "video_data": data}
    if cfg.ig_user_id and r["_page_id"] == cfg.page_id:  # the IG account belongs to the default page
        spec["instagram_user_id"] = cfg.ig_user_id
    params = {"name": r["ad_name"], "object_story_spec": spec,
              # Multi-advertiser ads off: the ad never shows in a carousel next to other brands' ads
              "contextual_multi_ads": {"enroll_status": "OPT_OUT"}}
    if r["url_tags"]:
        params["url_tags"] = r["url_tags"]
    bodies = parse_text_options(r, "primary_text")
    titles = parse_text_options(r, "headline")
    if bodies or titles:
        afs = {"optimization_type": "DEGREES_OF_FREEDOM",
               "bodies": [{"text": t} for t in (bodies or [r["primary_text"]])],
               "titles": [{"text": t} for t in (titles or [r["headline"]])]}
        if r["description"]:
            afs["descriptions"] = [{"text": r["description"]}]
        params["asset_feed_spec"] = afs
    if cfg.enhancements != "meta_default":
        params["degrees_of_freedom_spec"] = {"creative_features_spec": {
            f: {"enroll_status": "OPT_OUT"} for f in OPT_OUT_FEATURES}}
    return params


def ad_set_params(cfg, r):
    gender = {"female": [2], "women": [2], "male": [1], "men": [1]}.get(r["genders"].lower())
    targeting = {"geo_locations": {"countries": ad_set_countries(r)},
                 "age_min": int(float(r["age_min"] or 18)), "age_max": int(float(r["age_max"] or 65)),
                 "targeting_automation": {"advantage_audience": int(float(r["advantage_audience"] or 1))}}
    if gender:
        targeting["genders"] = gender
    p = {"name": r["ad_set_name"], "campaign_id": r["campaign_id"], "status": STATUS,
         "billing_event": "IMPRESSIONS", "optimization_goal": "OFFSITE_CONVERSIONS",
         "promoted_object": {"pixel_id": cfg.pixel_id,
                             "custom_event_type": (r["optimization_event"] or "PURCHASE").upper()},
         "targeting": targeting,
         # Attribution: 7-day click, 1-day engaged view, 1-day view. Meta can't change this after creation.
         "attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 7},
                              {"event_type": "ENGAGED_VIDEO_VIEW", "window_days": 1},
                              {"event_type": "VIEW_THROUGH", "window_days": 1}]}
    if needs_advertiser(r) and not Config.is_placeholder(cfg.advertiser_id):  # Ad transparency
        payer = getattr(cfg, "payer_id", "") or cfg.advertiser_id
        p["regional_regulation_identities"] = {"universal_beneficiary": cfg.advertiser_id,
                                               "universal_payer": payer}
    if r["daily_budget"]:  # ABO: the ad set owns its budget, so it also owns the bid strategy
        p["daily_budget"] = int(round(float(r["daily_budget"]) * cfg.unit))  # account currency, in its smallest unit
        p["bid_strategy"] = "LOWEST_COST_WITHOUT_CAP"
    if r["bid_strategy"]:
        p["bid_strategy"] = r["bid_strategy"].upper()
    if r["start_time"]:
        p["start_time"] = r["start_time"]  # ISO 8601 with offset; ad set stays PAUSED until switched on
    return p


# --- Output ------------------------------------------------------------------
def warn_level(w):
    """fyi = fine to launch as is; check = worth a look, doesn't block the launch."""
    return "fyi" if "Fine for long copy" in w or w.startswith("Advertiser & payer added") else "check"


def write_report(path, cfg, rows, skipped, warns, errors, would=""):
    if not path:
        return
    out = {"account": cfg.account, "status": STATUS, "would": would, "errors": errors,
           "warnings": [{"level": warn_level(w), "text": w} for w in warns],
           "links": sorted({r["link_url"] for r in rows}),
           "rows": [{"line": r["line"], "skipped": r["key"] in skipped, "page": r.get("_page_label", ""),
                     "ad_set": r.get("_ad_set_label") or r["ad_set_id"] or r["ad_set_name"],
                     "new_ad_set": bool(r.get("_create_ad_set")), "ad_name": r["ad_name"],
                     "media": Path(r["media_path"]).name,
                     "media_9x16": Path(r["media_path_9x16"]).name if r.get("media_path_9x16") else "",
                     "primary_texts": len(parse_text_options(r, "primary_text") or [r["primary_text"]]),
                     "headlines": len(parse_text_options(r, "headline") or [r["headline"]]),
                     "hook": r["primary_text"], "headline": r["headline"], "description": r["description"],
                     "cta": r["cta"], "start_time": r.get("start_time", "")} for r in rows]}
    Path(path).write_text(json.dumps(out, ensure_ascii=False))


def preview(rows, skipped):
    say(f"\n{'#':>3}  {'page':<16} {'ad set':<22} {'ad name':<28} {'media':<18} {'hook':<38} {'headline':<24} cta")
    for r in rows:
        mark = "↺" if r["key"] in skipped else " "
        say(f"{r['line']:>3}{mark} {short(r.get('_page_label', ''), 16):<16} {short(r.get('_ad_set_label') or r['ad_set_id'] or r['ad_set_name'], 22):<22} "
            f"{short(r['ad_name'], 28):<28} {short(Path(r['media_path']).name + (' +9:16' if r.get('media_path_9x16') else ''), 18):<18} "
            f"{short(r['primary_text'], 38):<38} {short(r['headline'], 24):<24} {r['cta']}")
    links = sorted({r["link_url"] for r in rows})
    say(f"\nlinks: {' · '.join(short(l, 60) for l in links[:4])}{' …' if len(links) > 4 else ''}")
    if skipped:
        say(f"↺ = already launched in a previous run, will be skipped ({len(skipped)})")


def summary(cfg, log, rows, skipped, results_path):
    by = {"created": [], "failed": [], "skipped": []}
    for r in rows:
        e = log.rows.get(r["key"], {})
        status = "skipped" if r["key"] in skipped else e.get("status") or "failed"
        if status == "failed" and not e.get("error"):
            e = dict(e, row=r["line"], ad_name=r["ad_name"], error="not reached (an earlier step stopped the run)")
        by[status].append(e)
    say("\n" + "=" * 60)
    say(f"✅ {len(by['created'])} created ({STATUS})   ❌ {len(by['failed'])} failed   ↺ {len(by['skipped'])} skipped (done before)")
    grouped = {}
    for e in by["failed"]:
        grouped.setdefault(e.get("error", ""), []).append(str(e.get("row")))
    for err, rws in grouped.items():  # identical errors print once
        label = f"row {rws[0]}" if len(rws) == 1 else f"{len(rws)} rows ({', '.join(rws[:6])}{'…' if len(rws) > 6 else ''})"
        say(f"   {label}: {short(err, 200)}")
    say(f"Ads Manager: {cfg.ads_manager_url}")
    say(f"Results log: {results_path}")


# --- Main --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Bulk-launch Meta ads from a CSV (PAUSED unless --active).")
    ap.add_argument("csv", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="validate and preview; create nothing")
    ap.add_argument("--no-link-check", action="store_true", help="skip loading each landing page")
    ap.add_argument("--results", type=Path, help="results log path (default: <csv>.results.csv)")
    ap.add_argument("--active", action="store_true", help="create the ad sets and ads ACTIVE (they can start spending once Meta approves them)")
    ap.add_argument("--report", type=Path, help="also write the check results as JSON here (used by the Ad Launcher screen)")
    ap.add_argument("--profile", help="use .env.<profile> instead of .env (another ad account / business)")
    a = ap.parse_args()

    cfg = Config(a.profile)
    global STATUS
    STATUS = "ACTIVE" if a.active else "PAUSED"
    csv_path = a.csv.expanduser().resolve()
    if not csv_path.is_file():
        sys.exit(f"CSV not found: {csv_path}")
    results_path = a.results or csv_path.with_name(csv_path.stem + ".results.csv")
    log = Log(results_path)

    _, rows = load_rows(csv_path)
    say(f"{len(rows)} rows in {csv_path.name} → ad account {cfg.account} ({cfg.profile})")
    keys = {r["key"] for r in rows}  # forget failures for rows that have since been edited
    log.rows = {k: v for k, v in log.rows.items() if k in keys or v.get("status") == "created"}
    say("⏳ Checking the files" + ("" if a.no_link_check else " and landing pages") + "…")
    errors, warns = validate(rows, csv_path.parent, check_links=not a.no_link_check)
    errors = [f"config: {p}" for p in cfg.problems() if not (a.dry_run and "TOKEN" in p)] + errors

    meta = None
    if cfg.has_token:
        say("⏳ Checking the campaign, ad sets and Page with Meta…")
        try:
            meta = Meta(cfg)
            if a.dry_run:
                meta.max_wait = CHECK_WAIT_TOTAL
            load_account(meta, cfg)
            say(f"  ad account: {cfg.name or cfg.account} · budgets in {cfg.currency or '?'} · time zone {cfg.timezone or '?'}")
            errors += resolve_campaigns(meta, cfg, rows)
            errors += resolve_ad_sets(meta, cfg, rows)
            errors += resolve_pages(meta, cfg, rows)
            if any(r.get("_create_ad_set") for r in rows):
                if cfg.is_placeholder(cfg.pixel_id):
                    errors.append("config: META_PIXEL_ID is missing in .env (needed to create ad sets)")
                need = {r["ad_set_name"]: needs_advertiser(r) for r in rows
                        if r.get("_create_ad_set") and needs_advertiser(r)}
                if need:
                    errors += resolve_advertiser(meta, cfg)
                    for name, cc in sorted(need.items()):
                        warns.append(f"Advertiser & payer added to '{name}' (required for {', '.join(cc)})")
        except RuntimeError as ex:
            errors.append(f"Meta: {ex}")
    else:
        warns.append("no token in .env — ad sets weren't checked against Meta")
        errors += resolve_pages(None, cfg, rows)

    skipped = {r["key"] for r in rows if log.done(r["key"])}
    if warns:
        say(f"\n⚠️  {len(warns)} warning(s):")
        for w in warns[:15]:
            say(f"   {w}")
        if len(warns) > 15:
            say(f"   … {len(warns) - 15} more")
    if errors:
        say(f"\n🛑 {len(errors)} problem(s) — nothing launched:")
        for e in errors:
            say(f"   {e}")
        write_report(a.report, cfg, rows, skipped, warns, errors)
        sys.exit(1)

    preview(rows, skipped)
    todo = [r for r in rows if r["key"] not in skipped]
    new_camps = sorted({r["campaign_name"] for r in todo if r.get("_create_campaign")})
    new_sets = sorted({r["ad_set_name"] for r in todo if r.get("_create_ad_set")})
    n_media = len({r["_media"] for r in todo} | {r["_alt"] for r in todo if r.get("_alt")})
    parts = [f"{len(todo)} ads", f"{len(todo)} creatives"]
    if new_camps:
        parts.append(f"{len(new_camps)} new campaign{'s' if len(new_camps) != 1 else ''} ({', '.join(new_camps)})")
    parts.append(f"{len(new_sets)} new ad sets" + (f" ({', '.join(new_sets)})" if new_sets else ""))
    parts.append(f"{n_media} media files")
    say(f"\nWould create: {' · '.join(parts)} — all {STATUS}")
    write_report(a.report, cfg, rows, skipped, warns, [], " · ".join(parts))
    if a.dry_run:
        say("Dry run — nothing sent to Meta.")
        return
    if not todo:
        say("Everything in this CSV was already launched.")
        summary(cfg, log, rows, skipped, results_path)
        return

    try:
        run(meta, cfg, todo, log)
    except KeyboardInterrupt:
        say("\nStopped. Progress saved — rerun the same command to continue.")
    finally:
        log.save()
    summary(cfg, log, rows, skipped, results_path)
    if any((log.rows.get(r["key"]) or {}).get("status") == "failed" for r in todo):
        sys.exit(1)   # so the screen shows "Stopped" and allows a rerun instead of "Launched"


def run(meta, cfg, todo, log):
    def fail_rows(rs, msg):
        for r in rs:
            log.set(r, status="failed", error=msg)
        log.save()

    has_new_camps = any(r.get("_create_campaign") for r in todo)
    n = 5 if has_new_camps else 4
    s = 0

    # Media
    s += 1
    say(f"\n{s}/{n} Media")
    media, merrs = upload_media(meta, cfg, todo)
    bad = [r for r in todo if r["_media"] in merrs or r.get("_thumb") in merrs or r.get("_alt") in merrs]
    for r in bad:
        log.set(r, status="failed", error=merrs.get(r["_media"]) or merrs.get(r.get("_thumb")) or merrs.get(r.get("_alt")))
    if bad:
        log.save()
        say(f"🛑 {len(bad)} row(s) have media problems — stopping before creating anything. Fix and rerun.")
        return

    # Creatives (reuse creative_id from an earlier partial run)
    s += 1
    say(f"{s}/{n} Creatives")
    jobs = []
    for r in todo:
        prev = log.rows.get(r["key"], {}).get("creative_id")
        if prev:
            r["_creative"] = prev
            continue
        jobs.append({"method": "POST", "path": f"{cfg.account}/adcreatives",
                     "params": creative_params(cfg, r, media),
                     "ok": lambda j, r=r: (r.__setitem__("_creative", j["id"]), log.set(r, creative_id=j["id"])),
                     "fail": lambda m, r=r: log.set(r, status="failed", error=f"creative: {m}")})
    meta.batch(jobs)
    log.save()
    bad = [r for r in todo if not r.get("_creative")]
    if bad:
        say(f"🛑 Meta rejected {len(bad)} creative(s) — no ad sets or ads created. Fix and rerun.")
        return

    # New campaigns: CBO when campaign_daily_budget is set, otherwise ABO
    if has_new_camps:
        s += 1
        say(f"{s}/{n} Campaigns")
        new_camps = {}
        for r in todo:
            if r.get("_create_campaign") and r["campaign_name"]:
                new_camps.setdefault(r["campaign_name"], []).append(r)
        camp_ok = True
        jobs = []
        for name, rs in new_camps.items():
            params = {"name": name, "objective": "OUTCOME_SALES", "status": "PAUSED", "special_ad_categories": []}
            if rs[0]["campaign_daily_budget"]:  # CBO
                params.update({"daily_budget": int(round(float(rs[0]["campaign_daily_budget"]) * cfg.unit)),
                               "bid_strategy": "LOWEST_COST_WITHOUT_CAP"})
            else:  # ABO: budgets live on the ad sets. Meta requires this flag on campaigns without a budget;
                # False keeps each ad set's budget its own (no sharing between ad sets).
                params["is_adset_budget_sharing_enabled"] = False
            def _ok(j, rs=rs, name=name):
                say(f"  ✓ '{name}' created ({j['id']})")
                for r in rs:
                    r["campaign_id"] = j["id"]
            def _fail(m, rs=rs, name=name):
                nonlocal camp_ok
                camp_ok = False
                fail_rows(rs, f"campaign '{name}': {m}")
            jobs.append({"method": "POST", "path": f"{cfg.account}/campaigns",
                         "params": params, "ok": _ok, "fail": _fail})
        meta.batch(jobs)
        log.save()
        if not camp_ok:
            say("🛑 Campaign creation failed — no ad sets or ads created. Fix and rerun.")
            return

    # New ad sets (one per name)
    s += 1
    say(f"{s}/{n} Ad sets")
    new = {}
    for r in todo:
        if r.get("_create_ad_set") and not r["ad_set_id"]:
            new.setdefault(r["ad_set_name"], []).append(r)
    jobs = []
    for name, rs in new.items():
        def ok(j, rs=rs):
            for r in rs:
                r["ad_set_id"] = j["id"]
                log.set(r, ad_set_id=j["id"])
        jobs.append({"method": "POST", "path": f"{cfg.account}/adsets", "params": ad_set_params(cfg, rs[0]),
                     "ok": ok, "fail": lambda m, rs=rs: fail_rows(rs, f"ad set: {m}")})
    meta.batch(jobs)
    log.save()
    if new:
        say(f"  {sum(1 for rs in new.values() if rs[0]['ad_set_id'])}/{len(new)} new ad set(s) created")

    # Ads
    s += 1
    say(f"{s}/{n} Ads")
    jobs = []
    for r in todo:
        if not r["ad_set_id"]:
            continue
        jobs.append({"method": "POST", "path": f"{cfg.account}/ads",
                     "params": {"name": r["ad_name"], "adset_id": r["ad_set_id"],
                                "creative": {"creative_id": r["_creative"]}, "status": STATUS},
                     "ok": lambda j, r=r: log.set(r, status="created", ad_id=j["id"], ad_set_id=r["ad_set_id"], error=""),
                     "fail": lambda m, r=r: log.set(r, status="failed", error=f"ad: {m}")})
    meta.batch(jobs)
    log.save()


if __name__ == "__main__":
    main()
