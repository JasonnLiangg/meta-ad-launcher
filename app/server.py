#!/usr/bin/env python3
"""Local launcher UI. Builds a batch CSV and runs launch_ads.py on it, so the screen and
the CSV path share one engine. Listens on 127.0.0.1 only.

    .venv/bin/python app/server.py      then open http://127.0.0.1:8790
"""
import csv
import hashlib
import json
import os
import re
import time
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

APP = Path(__file__).resolve().parent
ROOT = APP.parent
sys.path.insert(0, str(ROOT))
import launch_ads as L  # noqa: E402

PORT = int(os.environ.get("AD_LAUNCHER_PORT") or 8790)
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
BATCHES = ROOT / "batches"
DROPS = BATCHES / "media"
THUMBS = ROOT / ".thumbs"
LIBRARY = ROOT / "copy_library.json"   # testing / proven / failed copy lines; launches file their copy as testing (see README, Copy library)
HANDOFF = ROOT / "handoff.json"   # a launch prepared outside the screen (README, Hand-off); loaded once into the screen as the current draft
FFMPEG = shutil.which("ffmpeg") or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.path.exists(p)), None)   # video thumbnails only
MEDIA_EXT = L.IMAGE_EXT | L.VIDEO_EXT

CSV_FIELDS = ["ad_set_id", "ad_set_name", "page", "campaign_id", "campaign_name", "campaign_daily_budget",
              "countries", "age_min", "age_max", "genders", "daily_budget", "optimization_event", "start_time",
              "ad_name", "media_path", "media_path_9x16", "thumbnail_path", "primary_text", "primary_text_options",
              "headline", "headline_options", "description", "link_url", "cta", "url_tags"]

known = {}   # file id -> Path; only registered files are ever served back
jobs = {}
sessions = {}
lock = threading.Lock()


# --- Meta -----------------------------------------------------------------------
def meta(profile):
    key = profile or "default"
    if key not in sessions:
        try:
            cfg = L.Config(None if key == "default" else key)
        except SystemExit as ex:
            raise RuntimeError(str(ex))
        if not cfg.has_token:
            raise RuntimeError(f"No META_ACCESS_TOKEN in .env{'' if key == 'default' else '.' + key}")
        m = L.Meta(cfg)
        L.load_account(m, cfg)   # name, currency and time zone come from the ad account itself
        sessions[key] = (cfg, m)
    return sessions[key]


def paged(m, path, params):
    out, after = [], None
    while True:
        p = dict(params, limit=200)
        if after:
            p["after"] = after
        page = m.get(path, p)
        out += page.get("data", [])
        paging = page.get("paging", {})
        after = paging.get("cursors", {}).get("after") if paging.get("next") else None
        if not after:
            return out


LIVE = json.dumps([{"field": "effective_status", "operator": "NOT_IN", "value": ["DELETED", "ARCHIVED"]}])


def account_label(prof):
    """The ad account's real name for the Account menu; the profile key if it can't be reached."""
    try:
        cfg, _ = meta(prof)
        return f"{cfg.name} · {cfg.account[4:]}" if cfg.name else cfg.account
    except Exception:
        return prof


def pick(kind="folders"):
    """Opens the Finder picker on this Mac and returns the chosen paths ([] if cancelled).
    kind "folders": one or more folders (⌘-click for several). kind "files": one or more videos/images."""
    if kind == "files":
        ask = ('choose file with prompt "Choose the videos or images (⌘-click to pick several)" '
               'of type {"public.movie", "public.image"} with multiple selections allowed')
    else:
        ask = 'choose folder with prompt "Choose the folders with your ad creatives (⌘-click to pick several)" with multiple selections allowed'
    script = f"activate\nset picked to ({ask})\nset out to \"\"\nrepeat with f in picked\nset out to out & POSIX path of f & linefeed\nend repeat\nreturn out"
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    return [l for l in r.stdout.splitlines() if l.strip()] if r.returncode == 0 else []


def list_campaigns(cfg, m):
    rows = paged(m, f"{cfg.account}/campaigns",
                 {"fields": "id,name,effective_status,daily_budget,lifetime_budget", "filtering": LIVE})
    return [{"id": c["id"], "name": c["name"], "status": c.get("effective_status", ""),
             "cbo": bool(c.get("daily_budget") or c.get("lifetime_budget")),
             "budget": int(c["daily_budget"]) / cfg.unit if c.get("daily_budget") else None} for c in rows]


def list_adsets(cfg, m, campaign_id):
    if not campaign_id.isdigit():
        raise ValueError("bad campaign id")
    rows = paged(m, f"{campaign_id}/adsets", {"fields": "id,name,effective_status", "filtering": LIVE})
    return [{"id": a["id"], "name": a["name"], "status": a.get("effective_status", "")} for a in rows]


def list_pages(cfg, m):
    pages = {}
    for pg in m.get("me/accounts", {"fields": "id,name,tasks", "limit": 200}).get("data", []):
        if "ADVERTISE" in (pg.get("tasks") or []):
            pages[pg["id"]] = pg["name"]
    try:
        for pg in m.get(f"{cfg.account}/promote_pages", {"fields": "id,name", "limit": 200}).get("data", []):
            pages.setdefault(pg["id"], pg["name"])
    except RuntimeError:
        pass
    return [{"id": k, "name": v} for k, v in sorted(pages.items(), key=lambda kv: kv[1].lower())]


# --- Files ----------------------------------------------------------------------
def natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def file_info(p):
    fid = hashlib.sha1(str(p).encode()).hexdigest()[:12]
    known[fid] = p
    kind = L.media_kind(p)
    w, h = (L.image_size(p) or (0, 0)) if kind == "image" else (0, 0)
    # shape (feed / tall / other) comes from the image itself, so pairing a 1:1 with its 9:16 doesn't rest on file names
    return {"id": fid, "name": p.name, "stem": p.stem, "path": str(p), "size": p.stat().st_size,
            "kind": kind, "w": w, "h": h, "shape": L.image_shape((w, h)) or ""}


def scan(folder):
    d = Path(os.path.expanduser(folder.strip().strip("'\""))).resolve()
    if d.is_file():
        if d.suffix.lower() not in MEDIA_EXT:
            raise ValueError(f"Not an image or video: {d.name}")
        return [file_info(d)]
    if not d.is_dir():
        raise ValueError(f"Folder not found: {d}")
    files = [p for p in d.iterdir() if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in MEDIA_EXT]
    if not files:
        raise ValueError(f"No images or videos in {d.name}")
    return [file_info(p) for p in sorted(files, key=lambda p: natural(p.name))]


def scan_many(paths):
    """Any mix of folders (their images/videos, not subfolders) and single files, in the order picked, no repeats.
    Returns (files, problems): an empty folder among several is a note, not a stop."""
    out, seen, problems = [], set(), []
    for path in paths:
        try:
            for f in scan(path):
                if f["id"] not in seen:
                    seen.add(f["id"]); out.append(f)
        except ValueError as e:
            problems.append(str(e))
    if not out and problems:
        raise ValueError("; ".join(problems))
    return out, problems


MEDIA_CT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
            ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime"}


def thumb(fid):
    p = known.get(fid)
    if not p or not p.is_file():
        return None, None
    if L.media_kind(p) == "image":
        ct = "image/png" if p.suffix.lower() == ".png" else "image/gif" if p.suffix.lower() == ".gif" else "image/jpeg"
        return p.read_bytes(), ct
    THUMBS.mkdir(exist_ok=True)
    out = THUMBS / f"{fid}.jpg"
    if not out.exists() and FFMPEG:
        subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", "1", "-i", str(p), "-frames:v", "1",
                        "-vf", "scale=240:-1", str(out)], timeout=30)
    return (out.read_bytes(), "image/jpeg") if out.exists() else (None, None)


def recent_urls():
    urls = []
    for f in sorted(BATCHES.glob("*.csv"), key=lambda f: f.stat().st_mtime, reverse=True):
        if f.name.endswith(".results.csv"):
            continue
        try:
            with open(f, newline="", encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    u = (r.get("link_url") or "").strip()
                    if u and u not in urls:
                        urls.append(u)
        except Exception:
            pass
    return urls[:20]


def past_copy(limit=12):
    """Copy used in recent launches (batches with a results log, not marked -deleted), newest first.
    Each launch lists its distinct copy sets (primary texts, headlines, description) and how many ads used each."""
    out = []
    for f in sorted(BATCHES.glob("*.csv"), key=lambda f: f.stat().st_mtime, reverse=True):
        if f.name.endswith(".results.csv") or "-deleted" in f.stem or not f.with_suffix(".results.csv").exists():
            continue
        sets = {}
        try:
            with open(f, newline="", encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    def opts(col, one):
                        try:
                            v = json.loads(r.get(col) or "[]")
                        except Exception:
                            v = []
                        return [x.strip() for x in (v or [r.get(one) or ""]) if x and x.strip()]
                    p, h, d = opts("primary_text_options", "primary_text"), opts("headline_options", "headline"), (r.get("description") or "").strip()
                    key = json.dumps([p, h, d])
                    s = sets.setdefault(key, {"p": p, "h": h, "d": d, "ads": 0, "adsets": []})
                    s["ads"] += 1
                    a = (r.get("ad_set_name") or r.get("ad_set_id") or "").strip()
                    if a and a not in s["adsets"]:
                        s["adsets"].append(a)
        except Exception:
            continue
        if sets:
            out.append({"name": f.stem, "date": datetime.fromtimestamp(f.stat().st_mtime).strftime("%b %-d"),
                        "sets": sorted(sets.values(), key=lambda s: -s["ads"])})
        if len(out) >= limit:
            break
    return out


def load_library():
    try:
        return json.loads(LIBRARY.read_text()).get("lines", [])
    except Exception:
        return []


def save_library(lines):
    LIBRARY.write_text(json.dumps({"lines": lines}, indent=1, ensure_ascii=False))


STATUSES = ("testing", "proven", "failed")
norm = lambda t: " ".join((t or "").split())


def find_line(lines, kind, product, text):
    return next((x for x in lines if x["kind"] == kind and x["product"] == product and norm(x["text"]) == norm(text)), None)


def library_action(d):
    """status: move a line to testing / proven / failed; delete: remove a line for good."""
    lines, act = load_library(), d.get("action")
    if act == "status":
        line = next((x for x in lines if x["id"] == d.get("id")), None)
        if not line or d.get("status") not in STATUSES:
            raise ValueError("Unknown line or status")
        line["status"] = d["status"]
    elif act == "delete":
        before = len(lines)
        lines = [x for x in lines if x["id"] != d.get("id")]
        if len(lines) == before:
            raise ValueError("Unknown line")
    else:
        raise ValueError("Unknown action")
    save_library(lines)
    return lines


def record_launch(csv_path, product):
    """Files every primary text and headline of a launched batch under its product. New lines start in Testing;
    lines already in the library keep their status and get this launch added to their history."""
    product = (product or "").strip()
    if not product:
        return
    lines, today = load_library(), datetime.fromtimestamp(Path(csv_path).stat().st_mtime).strftime("%Y-%m-%d")
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for kind, many, one in (("p", "primary_text_options", "primary_text"), ("h", "headline_options", "headline")):
            try:
                texts = json.loads(r.get(many) or "[]") or [r.get(one) or ""]
            except Exception:
                texts = [r.get(one) or ""]
            for text in (t.strip() for t in texts if t and t.strip()):
                line = find_line(lines, kind, product, text)
                if not line:
                    line = {"id": hashlib.sha1(f"{product}|{kind}|{text}".encode()).hexdigest()[:10], "kind": kind,
                            "product": product, "text": text, "status": "testing", "angle": "", "result": "", "added": today}
                    lines.append(line)
                if csv_path.stem not in line.setdefault("launches", []):
                    line["launches"].append(csv_path.stem)
                line["last_used"] = max(line.get("last_used") or "", today)
    save_library(lines)


# --- Batch building --------------------------------------------------------------
def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s or "").strip("-.")


def iso_start(s, tz):
    """The start time typed on the screen is in the ad account's time zone (this computer's if Meta gave none)."""
    if not s:
        return ""
    d = datetime.fromisoformat(s)
    try:
        return d.replace(tzinfo=ZoneInfo(tz)).isoformat(timespec="seconds")
    except Exception:
        return d.astimezone().isoformat(timespec="seconds")


def clean_texts(xs):
    return [x.strip() for x in (xs or []) if x and x.strip()]


def build_rows(d):
    camp = d.get("campaign") or {}
    copy = d.get("copy") or {}
    url = (d.get("link_url") or "").strip()
    start = iso_start(d.get("start_time"), meta(d.get("profile"))[0].timezone) if d.get("start_time") else ""
    rows, problems = [], []
    abo = camp.get("mode") == "new" and camp.get("type") == "abo"
    if camp.get("mode") == "new" and not camp.get("name"):
        problems.append("New campaign needs a name")
    elif camp.get("mode") == "new" and not abo and not camp.get("budget"):
        problems.append("New CBO campaign needs a daily budget (or switch it to ABO)")
    if camp.get("mode") == "existing" and not str(camp.get("id", "")).isdigit():
        problems.append("Pick a campaign")
    seen_names = set()
    for s in d.get("adsets") or []:
        if not s.get("creatives"):
            continue
        existing = s.get("mode") == "existing"
        if existing and not str(s.get("id", "")).isdigit():
            problems.append(f"Ad set '{s.get('name') or '?'}': pick which existing ad set")
            continue
        if not existing and not s.get("name"):
            problems.append("An ad set has no name")
            continue
        if not existing:
            if s["name"].strip() in seen_names:
                problems.append(f"Two ad sets are both named '{s['name'].strip()}' (they'd merge into one). Change one of them")
            seen_names.add(s["name"].strip())
        base = {"page": s.get("page") or d.get("page", ""), "link_url": (s.get("link_url") or "").strip() or url,
                "cta": s.get("cta") or d.get("cta") or "SHOP_NOW", "url_tags": (d.get("url_tags") or "").strip()}
        if existing:
            base["ad_set_id"] = s["id"]
        else:
            if not str(s.get("countries") or "").strip():
                problems.append(f"Ad set '{s['name']}' needs at least one country")
            if (abo or camp.get("abo")) and not str(s.get("daily_budget") or "").strip():
                problems.append(f"Ad set '{s['name']}' needs a daily budget (ABO)")
            base.update({"ad_set_name": s["name"].strip(), "countries": s.get("countries"),
                         "age_min": str(s.get("age_min") or 18), "age_max": str(s.get("age_max") or 65),
                         "genders": s.get("gender") or "all", "optimization_event": "PURCHASE",
                         "start_time": start, "daily_budget": str(s.get("daily_budget") or "")})
            if camp.get("mode") == "new":
                base.update({"campaign_name": camp["name"].strip(),
                             "campaign_daily_budget": "" if abo else str(camp["budget"]),
                             "daily_budget": base["daily_budget"] if abo else ""})
            else:
                base["campaign_id"] = str(camp.get("id", ""))
        for c in s["creatives"]:
            p = known.get(c.get("id"))
            if not p:
                problems.append(f"Creative {c.get('name')} isn't loaded any more — reload the folder")
                continue
            alt = known.get(c.get("alt")) if c.get("alt") else None   # the 9:16 partner of a paired static
            if c.get("alt") and not alt:
                problems.append(f"{p.name}: its 9:16 image isn't loaded any more — reload the folder")
                continue
            cc = c.get("copy") or s.get("copy") or copy
            pts, hls = clean_texts(cc.get("primary_texts")), clean_texts(cc.get("headlines"))
            if not pts or not hls:
                problems.append(f"{p.name}: needs at least one primary text and one headline")
                continue
            rows.append(dict(base, ad_name=(c.get("ad_name") or "").strip() or p.stem, media_path=str(p),
                             media_path_9x16=str(alt) if alt else "",
                             primary_text=pts[0], primary_text_options=json.dumps(pts) if len(pts) > 1 else "",
                             headline=hls[0], headline_options=json.dumps(hls) if len(hls) > 1 else "",
                             description=(cc.get("description") or "").strip()))
    if not rows and not problems:
        problems.append("No creatives are in an ad set yet")
    return rows, problems


DRY = {"lines": [], "running": False}
CHECKING = threading.Lock()   # one Review check at a time: two at once doubled the calls to Meta and tripped its rate limit


def run_dry(csv_path, profile, skip_links):
    report = csv_path.with_name(csv_path.stem + ".check.json")
    report.unlink(missing_ok=True)
    args = [sys.executable, str(ROOT / "launch_ads.py"), str(csv_path), "--dry-run", "--report", str(report)]
    if skip_links:
        args.append("--no-link-check")
    if profile and profile != "default":
        args += ["--profile", profile]
    # lines are kept in DRY as they arrive, so the Review screen can show what the check is doing
    DRY["lines"], DRY["running"] = [], True
    p = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         env=dict(os.environ, PYTHONUNBUFFERED="1"))
    timer = threading.Timer(600, p.kill)
    timer.start()
    try:
        for line in p.stdout:
            DRY["lines"].append(line.rstrip("\n"))
        p.wait()
    finally:
        timer.cancel()
        DRY["running"] = False
    try:
        rep = json.loads(report.read_text())
    except Exception:
        rep = None   # the launcher stopped before it could check (e.g. a bad CSV): the screen shows the raw output
    return p.returncode == 0, "\n".join(DRY["lines"]).strip(), rep


def start_job(csv_path, profile, skip_links, active=False, product=""):
    with lock:
        if any(not j["done"] for j in jobs.values()):
            raise RuntimeError("A launch is already running")
        jid = uuid.uuid4().hex[:8]
        jobs[jid] = {"lines": [], "done": False, "code": None}
    args = [sys.executable, str(ROOT / "launch_ads.py"), str(csv_path)]
    if active:
        args.append("--active")
    if skip_links:
        args.append("--no-link-check")
    if profile and profile != "default":
        args += ["--profile", profile]

    def work():
        j = jobs[jid]
        try:
            p = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 bufsize=1, env=dict(os.environ, PYTHONUNBUFFERED="1"))
            for line in p.stdout:
                j["lines"].append(line.rstrip("\n"))
            j["code"] = p.wait()
            if j["code"] == 0:
                try:
                    record_launch(csv_path, product)
                except Exception as ex:
                    j["lines"].append(f"⚠ Launched, but the copy wasn't filed in the copy library: {ex}")
        except Exception as ex:
            j["lines"].append(f"🛑 {ex}")
            j["code"] = 1
        j["done"] = True
    threading.Thread(target=work, daemon=True).start()
    return jid


def batch_csv(name):
    p = (BATCHES / Path(name).name).resolve()
    if p.parent != BATCHES.resolve() or p.suffix != ".csv" or not p.is_file():
        raise ValueError("Unknown batch file")
    return p


# --- HTTP -------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body, code=200, ct="application/json"):
        data = json.dumps(body).encode() if ct == "application/json" else body
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_media(self, fid):
        """The full file for the preview window. Videos are sent in ranges so the player can seek without loading it all."""
        p = known.get(fid)
        if not p or not p.is_file():
            return self.reply({"error": "no preview"}, 404)
        size, start, end = p.stat().st_size, 0, None
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start, end = int(m.group(1)), int(m.group(2)) if m.group(2) else None
            else:
                start = max(0, size - int(m.group(2)))
            end = size - 1 if end is None else min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                return self.end_headers()
        else:
            m, end = None, size - 1
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", MEDIA_CT.get(p.suffix.lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        left = end - start + 1
        try:
            with open(p, "rb") as f:
                f.seek(start)
                while left > 0:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass   # the player dropped this request to seek elsewhere

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def guard(self, write):
        # Only this machine's browser, and only our own page, may call the API.
        if self.headers.get("Host") not in HOSTS:
            return False
        return not write or self.headers.get("X-Launcher") == "1"

    def handle_any(self, method):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if not self.guard(method != "GET"):
            return self.reply({"error": "forbidden"}, 403)
        try:
            return self.route(method, url.path, q)
        except Exception as ex:
            return self.reply({"error": str(ex)}, 400)

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    def route(self, method, path, q):
        prof = q.get("profile") or "default"
        if method == "GET" and path in ("/", "/index.html"):
            return self.reply((APP / "index.html").read_bytes(), ct="text/html; charset=utf-8")
        if method == "GET" and path == "/api/handoff":   # one-time: returns the prepared draft, then renames the file
            if not HANDOFF.is_file():
                return self.reply({})
            d = json.loads(HANDOFF.read_text())
            if (d.get("profile") or "default") != prof:
                return self.reply({})
            HANDOFF.rename(BATCHES / f"handoff-loaded-{datetime.now():%Y%m%d-%H%M%S}.json")
            return self.reply(d)
        if method == "GET" and path == "/api/init":
            keys = ["default"] + sorted(p.name[5:] for p in ROOT.glob(".env.*") if p.name != ".env.example")
            profiles = [{"key": k, "label": account_label(k)} for k in keys]
            base = {"profiles": profiles, "profile": prof, "recent_urls": recent_urls(), "copy_library": load_library()}
            cfg, m = meta(prof)
            return self.reply(dict(base, account=cfg.account, account_label=account_label(prof), default_page=cfg.page_id,
                                   currency=cfg.currency, timezone=cfg.timezone,
                                   pages=list_pages(cfg, m), campaigns=list_campaigns(cfg, m)))
        if method == "GET" and path == "/api/copy-library-list":
            return self.reply(load_library())
        if method == "GET" and path == "/api/past-copy":
            return self.reply(past_copy())
        if method == "GET" and path == "/api/adsets":
            cfg, m = meta(prof)
            return self.reply(list_adsets(cfg, m, q.get("campaign", "")))
        if method == "GET" and path == "/api/thumb":
            data, ct = thumb(q.get("id", ""))
            return self.reply(data, ct=ct) if data else self.reply({"error": "no preview"}, 404)
        if method == "GET" and path == "/api/media":
            return self.send_media(q.get("id", ""))
        if method == "GET" and path == "/api/dry-progress":
            return self.reply({"running": DRY["running"], "lines": DRY["lines"][-40:]})
        if method == "GET" and path == "/api/job":
            j = jobs.get(q.get("id", ""))
            if not j:
                return self.reply({"error": "unknown job"}, 404)
            since = int(q.get("since") or 0)
            return self.reply({"lines": j["lines"][since:], "done": j["done"], "code": j["code"]})
        if method == "POST" and path == "/api/pick":   # Finder picker: several folders, or several single files
            paths = pick(self.body().get("kind", "folders"))
            files, problems = scan_many(paths) if paths else ([], [])
            return self.reply({"paths": paths, "files": files, "problems": problems})
        if method == "POST" and path == "/api/scan":   # a pasted folder or file path
            return self.reply(scan(self.body().get("folder", "")))
        if method == "POST" and path == "/api/register":   # re-registers a restored draft's files; missing ones are left out
            files = [Path(p) for p in self.body().get("paths", [])]
            return self.reply([file_info(p) for p in files if p.is_file() and p.suffix.lower() in MEDIA_EXT])
        if method == "PUT" and path == "/api/drop":
            session, name = q.get("session", ""), Path(q.get("name", "")).name
            if not re.fullmatch(r"[A-Za-z0-9-]{4,40}", session) or Path(name).suffix.lower() not in MEDIA_EXT:
                raise ValueError("Only jpg/png/gif/mp4/mov/m4v files")
            dest = DROPS / session
            dest.mkdir(parents=True, exist_ok=True)
            out, left = dest / name, int(self.headers.get("Content-Length") or 0)
            with open(out, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            return self.reply(file_info(out))
        if method == "POST" and path == "/api/copy-library":
            return self.reply(library_action(self.body()))
        if method == "POST" and path == "/api/build":
            d = self.body()
            if not CHECKING.acquire(blocking=False):
                return self.reply({"error": "A check is already running. Give it a minute to finish; a second one at the "
                                            "same time only makes Meta slow both down."}, 409)
            try:
                DRY["lines"], DRY["running"] = [], True
                rows, problems = build_rows(d)
                if problems:
                    return self.reply({"ok": False, "output": "🛑 Fix these first:\n" + "\n".join(f"   {p}" for p in problems)})
                name = safe_name(d.get("batch_name")) or datetime.now().strftime("%m-%d-launch")
                out = BATCHES / f"{name}.csv"
                with open(out, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
                    w.writeheader()
                    for r in rows:
                        w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
                ok, output, report = run_dry(out, d.get("profile"), d.get("skip_links"))
                return self.reply({"ok": ok, "csv": out.name, "output": output, "report": report})
            finally:
                DRY["running"] = False
                CHECKING.release()
        if method == "POST" and path == "/api/launch":
            d = self.body()
            return self.reply({"job": start_job(batch_csv(d.get("csv", "")), d.get("profile"), d.get("skip_links"), d.get("active") is True, d.get("product") or "")})
        return self.reply({"error": "not found"}, 404)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Ad Launcher running at http://127.0.0.1:{PORT}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
