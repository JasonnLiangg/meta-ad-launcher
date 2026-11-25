# Meta Ad Launcher

Launch a whole batch of Meta (Facebook and Instagram) ads in one go, from your own computer, into your own ad account.

You load your images and videos, sort them into ad sets, add your copy, and review. The launcher then uploads everything and creates the campaign, ad sets and ads for you. Everything is created **paused** unless you choose otherwise, so nothing spends until you switch it on in Ads Manager.

There are two ways to use it, and both run the same engine:

- **The screen:** a window on your Mac where you drag creatives into ad sets and click **Review & launch**.
- **A spreadsheet (CSV):** one row per ad, run from Terminal. Handy for very large batches or if you use an AI assistant to prepare them.

It runs only on your computer. Your token and your ads go straight from your Mac to Meta and nowhere else.

**What you need**

- A Mac (the screen uses Finder and Mac shortcuts; the CSV path also works on Windows and Linux).
- Python 3.9 or newer. Check with `python3 --version` in Terminal. If it's missing, install it from [python.org/downloads](https://www.python.org/downloads/).
- Google Chrome (optional: the screen opens in its own window with Chrome, otherwise in your default browser).
- A Meta business portfolio with an ad account, a Facebook Page and a pixel (dataset).
- About 30 minutes for the one-time setup. Most of it is on Meta's side.

---

## Setup (once)

### 1. Download and install

1. On this repo's GitHub page, click **Code → Download ZIP**, unzip it, and move the folder somewhere permanent (for example `Documents/meta-ad-launcher`). Don't move it later: the app shortcut points at this folder.
2. Open **Terminal**, type `cd ` (with a space), drag the folder onto the Terminal window, and press Enter.
3. Run:

   ```
   ./setup.sh
   ```

   This installs what the launcher needs inside the folder (in `.venv/`) and creates your settings file, `.env`. Nothing is installed anywhere else on your Mac.

### 2. Get your Meta token

The launcher talks to Meta with a **system user token**: a permanent key that belongs to your business, not to your personal login. You create it once. Meta renames its menus now and then, so a label may differ slightly from what's written here.

**a. Create a Meta app** (the token has to belong to an app)

1. Go to [developers.facebook.com/apps](https://developers.facebook.com/apps) and click **Create app**.
2. Choose the use case **Other**, then the type **Business**. Name it anything (for example "Ad Launcher") and connect it to your business portfolio.
3. In the app's dashboard, add the **Marketing API** product.
4. Switch the app from **Development** to **Live** (the toggle at the top; Meta asks for a privacy policy URL first, and your store's privacy page works). If the app stays in Development mode, every ad fails with "created by an app that is in development mode".

**b. Create a system user**

1. Go to [business.facebook.com/settings](https://business.facebook.com/settings) → **Users → System users** → **Add**.
2. Name it (for example "Launch Bot") and give it the **Admin** role.

**c. Give the system user your assets**

Select the system user, click **Assign assets**, and add:

- your **ad account**, with **Manage campaigns** (full control);
- every **Page** you run ads from, with **Create ads** at least;
- your **pixel / dataset**.

**d. Generate the token**

1. Still on the system user, click **Generate token**.
2. Pick the app from step a, and set the expiry to **Never**.
3. Tick these permissions: `ads_management`, `ads_read`, `business_management`, `pages_show_list`, `pages_read_engagement`.
4. Copy the token. Meta shows it once.

Treat the token like a password. Anyone who has it can create ads on your account. Never paste it into a chat, an email or a shared document. It only ever goes in your `.env` file, which stays on your computer.

### 3. Fill in `.env`

Open the `.env` file in the folder with a text editor. (Files starting with a dot are hidden in Finder: press **⌘ ⇧ .** to show them, or run `open -e .env` in Terminal.)

Fill in the first two lines:

- `META_ACCESS_TOKEN`: the token from step 2.
- `META_AD_ACCOUNT_ID`: your ad account's number. It's in your Ads Manager address bar after `act=`.

Then run the checker, which reads your account and lists the other IDs for you:

```
.venv/bin/python check_setup.py
```

It only reads; it never creates or changes anything. It prints your ad account's name, currency and time zone, the Pages you can advertise from, and your pixels, each with its ID. Copy the ones you want into `.env`:

| Setting | What it is |
|---|---|
| `META_PAGE_ID` | The Page ads run from when you don't pick one. |
| `META_PIXEL_ID` | Your pixel / dataset. Needed whenever the launcher creates an ad set. |
| `META_INSTAGRAM_USER_ID` | Optional. The Instagram account for the default Page. Leave blank and Meta uses the Page's own Instagram setup. |
| `META_APP_SECRET` | Optional. Only if your app has "Require App Secret" switched on. |
| `META_ADVERTISER_ID` | Optional. The advertiser and payer shown under Ad transparency, required for ad sets targeting the EU, Australia, India, Malaysia, Singapore, Thailand or Taiwan. Leave blank and it's copied from the newest ad set in your account that has one. If none has one yet, set it once on any ad set in Ads Manager. |
| `CREATIVE_ENHANCEMENTS` | `off` (the default) keeps your ads exactly as you made them. See "Advantage+ creative enhancements" below. |

Run `check_setup.py` again until it ends with "Setup looks good".

### 4. Test it

A dry run checks everything and creates nothing:

```
.venv/bin/python launch_ads.py test/test_ad.csv --dry-run
```

If it ends with "Would create: 1 ads…", run it for real by removing `--dry-run`. That creates one **paused** test campaign called "LAUNCHER TEST (delete me)" with one ad set and one ad. Open Ads Manager, check the ad shows its link and button, then delete the campaign. (If Meta says the 10/day test budget is below your currency's minimum, raise `daily_budget` in `test/test_ad.csv`.)

### 5. Open the screen

```
./start.sh
```

To get a proper app with an icon in `~/Applications` and a shortcut on your Desktop, run this once:

```
./install_app.sh
```

After that, open **Ad Launcher** like any other app. Double-clicking `Open Launcher.command` also works (the first time, macOS may ask you to right-click it and choose **Open**).

---

## The screen

**Layout.** The left side has three tabs (**Setup**, **Creatives**, **Copy**) and scrolls separately from the ad sets on the right, where the header with **Review & launch** stays pinned, ungrouped creatives sit above the ad sets, and each ad set folds to one line (▾, or **Collapse all**). **Light / Dark / Auto** in the header switches the look.

**Account and campaign.** Pick a campaign or make a new one with **Budget** set to:

- **Campaign (CBO):** one daily budget for the campaign.
- **Per ad set (ABO):** a **Daily budget per ad set** box flows down to every new ad set, each ad set can change its own under **Targeting & page**, and the hint shows the total per day.

Budgets are in your ad account's own currency, and the start time is in your ad account's own time zone. Both are read from Meta and shown on the screen.

**Creatives.** Each pick adds to what's already loaded, in any mix:

- **Choose folders…:** pick one folder, or ⌘-click several, and every image and video inside each one is added (not subfolders).
- **Choose videos…:** ⌘-click several videos or images inside a folder (⇧-click for a range).
- **Drag files in**, or **paste a folder or file path**.

Drag creatives into ad sets, or select several (click, shift-click a range, or **Select all**) and use **Move selected to…**. Click a thumbnail to preview it full size; **←** / **→** step through.

Images: jpg, png, gif. Videos: mp4, mov, m4v. Video thumbnails on the screen need `ffmpeg` (optional: `brew install ffmpeg`); without it videos still launch, they just show no thumbnail.

**Link, targeting and timing.**

- **Landing page:** **Open ↗** opens the link with your UTM tags added, exactly as the ad will send people.
- **Countries** are picked from a list and must be chosen for every new ad set (Review stops an ad set with none).
- **Ages** are dropdowns 18–65+.
- **UTM:** the box tidies a pasted "?", spaces or a full URL. **Use standard** fills `utm_source=facebook&utm_medium=paid`.
- **Start date and time** (or none): a calendar with a time row under it.

**Names.**

- **Ad set:** five boxes (**Batch**, **Concept**, **Format**, **LP**, **Country**) join into `Batch//Concept//Format//LP//Country`. Blank boxes are left out. **Country** fills itself from the countries you picked, and **LP** becomes `PDP` when the link is a `/products/` page; typing in either overrides it. `b10` or `batch 10` becomes `Batch 10`. Review stops two ad sets with the same name.
- **Ads** in a new ad set are named `Batch//Concept//<file name>`. Click an ad's name to type your own.

**Copy.** Enter up to 5 primary texts and 5 headlines; Meta rotates them. Page, CTA and copy are set for the whole launch and can be changed per ad set (**Targeting & page** and **✎ Copy** tabs) or per ad on its row. The most specific one wins.

**Copy library.** Every primary text and headline you launch is filed per product (the campaign name before `//`, so name campaigns like `My Product//CBO`) as **Testing**, **Proven** or **Failed**. Mark lines as you learn what works, and reuse proven ones with **+ Use**. **↺ Reuse last launch** fills in the copy from your most recent launch. The library lives in `copy_library.json` in this folder.

**Statics in two shapes (1:1 + 9:16).** A static made as a feed image and a tall image launches as **one ad with two images**: feeds show the feed image (1:1 or 4:5), Stories and Reels show the 9:16. Load both files and the screen pairs them into one row tagged **1:1 + 9:16**. Which image is which comes from the file's real pixel shape. Names decide who belongs with whom: size words (`1x1`, `9x16`, `square`, `story`…) are dropped and the rest must match (`A05-1x1` + `A05-9x16`). To pair by hand, tick two images and click **Pair selected**; **⇄** splits a pair. *This feature has been checked by dry run only. The first time you use it, launch one paused paired ad and confirm in Ads Manager that the link, the button and both images show.*

**Review & launch.**

- **Review** writes the batch to `batches/<date>-<campaign>.csv` and dry-runs it. You get a banner (ready / fix first), notes split into **Must fix**, **Worth a look** and **FYI**, and a table of every ad.
- **Paused / Active** switch: resets to Paused on every review and asks before an Active launch. New campaigns are always created paused, so Active ads in a new campaign start only once you turn the campaign on.
- **Launch** shows live progress as a checklist, with each file's upload status.
- **When it finishes,** a green card shows what was created, with **Open in Ads Manager** and **Start a new launch**.
- **If it stops,** a red card says what went wrong in plain words, and the batch stays open to fix and **Re-check**. Ads that already launched are never created twice.

**Saved as you go.** The launch in progress is saved in the browser and comes back after a refresh or restart.

**Running it.** The screen only listens on your own Mac (`http://127.0.0.1:8790`). The server keeps running quietly after the window closes, so reopening is instant (log: `~/.local/share/ad-launcher/launcher.log`). To stop it: `pkill -f app/server.py`. To use another port, set `AD_LAUNCHER_PORT` before `./start.sh`.

---

## The CSV path

Copy `template.csv`. Each row is one ad.

```
.venv/bin/python launch_ads.py batches/my-batch.csv --dry-run   # check + preview, creates nothing
.venv/bin/python launch_ads.py batches/my-batch.csv             # launch (PAUSED)
.venv/bin/python launch_ads.py batches/my-batch.csv --active    # launch ad sets + ads ACTIVE
```

- `--no-link-check` skips loading each landing page.
- `--results path.csv` writes the results log somewhere other than next to the CSV.
- `--profile <name>` uses `.env.<name>` instead of `.env` (see "More than one ad account").

| Column | Required | Notes |
|---|---|---|
| `ad_set_id` | one of these two | An existing ad set. |
| `ad_set_name` | one of these two | An exact name. An existing ad set with that name is used; if none exists, one is **created** (needs `campaign_id` or `campaign_name`). |
| `page` | no | The Page the ad runs from, by name or ID. If blank, `META_PAGE_ID` is used. Each row can use a different Page. |
| `ad_name` | yes | |
| `media_path` | yes | jpg/png/gif or mp4/mov/m4v. `~` and paths relative to the CSV work. |
| `media_path_9x16` | no | A second, tall image for the same ad (images only). Shown in Stories and Reels. |
| `thumbnail_path` | no | Video cover image. If blank, Meta's auto thumbnail is used. |
| `primary_text` | yes | Line breaks are fine; wrap the text in quotes. |
| `primary_text_options` | no | JSON list of 2–5 primary texts for Meta to rotate (`["text A","text B"]`). The first entry must match `primary_text`. |
| `headline` | yes | |
| `headline_options` | no | Same, for headlines. |
| `description` | no | |
| `link_url` | yes | Must load (not a 404). |
| `cta` | no | Defaults to SHOP_NOW. Also LEARN_MORE, ORDER_NOW, BUY_NOW, GET_OFFER, SIGN_UP, SUBSCRIBE and others. |
| `url_tags` | no | e.g. `utm_source=facebook&utm_medium=paid` |

**Creating a new campaign.** Use `campaign_name` instead of `campaign_id`. If a campaign with that name already exists and is the same type, it's reused, so reruns are safe.

| Column | Notes |
|---|---|
| `campaign_name` | Rows that share the name go into the same campaign. |
| `campaign_daily_budget` | Daily budget in your account's currency. Filled on every row = CBO. Blank on every row = ABO (each ad set needs its own `daily_budget`). |

New campaigns use the Sales objective (`OUTCOME_SALES`) and lowest-cost bidding.

**Creating a new ad set.** Every row going into the same new ad set must have identical values here.

| Column | Default | Notes |
|---|---|---|
| `campaign_id` | (required unless `campaign_name` is set) | |
| `countries` | US | 2-letter codes, comma-separated: `US,CA`. The UK is `GB`. |
| `age_min` / `age_max` | 18 / 65 | |
| `genders` | all | Or `female` / `male` |
| `daily_budget` | blank | Your account's currency, per day. **Required for ABO, blank for CBO.** |
| `optimization_event` | PURCHASE | On your pixel. Also ADD_TO_CART, INITIATED_CHECKOUT, LEAD and others. |
| `bid_strategy` | Meta default | e.g. `COST_CAP` (then set the cap in Ads Manager) |
| `advantage_audience` | 1 | 0 to turn Advantage+ audience off |
| `start_time` | now | ISO with your account's offset, e.g. `2026-10-10T00:00:00-04:00` |

New ad sets are created with attribution **7-day click, 1-day engaged view, 1-day view**. Meta can't change attribution after an ad set exists, so if you want something else, change `attribution_spec` in `ad_set_params` in `launch_ads.py`.

---

## What it does, in order

1. **Validation.** Nothing is sent to Meta until this passes: files exist, required text is present, links load, ad sets belong to your account, budgets make sense, no duplicate rows. If anything fails, it lists **every** problem and launches nothing.
2. **Media.** Each file is uploaded once, even if 10 ads use it, and is remembered across runs (`.media_cache.json`). Five files upload at a time. It waits for videos to finish processing.
3. **Creatives.** If Meta rejects any creative, it stops here: no campaigns, ad sets or ads are created. Fix the row and rerun.
4. **New campaigns**, always paused.
5. **New ad sets**, paused (or Active).
6. **Ads**, paused (or Active).

**Results log.** It writes `<csv name>.results.csv` next to your CSV, with each row's status, ad ID and error. Rerunning the same CSV skips rows that already succeeded, so a rerun after a failure is always safe. If you edit a row's copy or media, it counts as a new ad.

**Rate limits.** When Meta says "slow down", the launcher waits the time Meta suggests and retries. A launch stops cleanly after 30 minutes of waiting in total and tells you to rerun; a Review gives up after 90 seconds with "wait about 15 minutes, then Re-check once".

A brand-new Meta app is on Meta's **development access** tier, which allows far fewer calls per hour than standard access. Opening the screen, each Review and each launch all spend from the same allowance, so clicking Re-check several times in a row can block the account's reads for a while. If that happens, wait 15 minutes. To raise the limit for good, apply for **Advanced Access** to `ads_management` in your app's dashboard (App Review).

### Text limits

| Field | Blocks the launch | Warning (gets cut off with "See more") |
|---|---|---|
| Primary text | over 63,000 chars | over 125 |
| Headline | over 255 | over 40 |
| Description | over 255 | over 25 |

### Advantage+ creative enhancements

These are **off by default** (`CREATIVE_ENHANCEMENTS=off`). Each creative is sent with an opt-out for every Meta feature that changes copy or visuals: text rewrites, auto descriptions, CTA changes, text overlays, image touch-ups, templates, animation, translation, generated backgrounds and others. Your ad runs exactly as you made it. Set it to `meta_default` to let Meta decide. Multi-advertiser ads are also switched off on every creative.

### Budgets and currency

Budgets you type are in your ad account's currency, in whole units (50 means 50 dollars, euros, pounds…). The launcher converts to what Meta expects, including currencies with no cents (JPY, KRW, HUF, IDR, CLP, COP, CRC, ISK, PYG, TWD, VND). On your first launch, glance at the budget in Ads Manager before switching anything on.

---

## More than one ad account

Copy `.env` to `.env.<name>` (for example `.env.brand2`) and change the IDs, and the token if that account sits in another business portfolio. The screen's account menu then lists both. On the CSV path, add `--profile brand2`. Check a profile with `.venv/bin/python check_setup.py --profile brand2`.

## Hand-off: prepare a launch outside the screen

A script or an AI assistant can prepare a whole launch (creatives, ad sets, copy, campaign) as `handoff.json` in this folder. The next time the screen opens, it loads that as the current launch, shows a banner, and renames the file to `batches/handoff-loaded-<time>.json` so it loads only once. You check it and click **Review & launch**.

The file is `{profile, note, draft:{fields, cr, ung, sets, copy, nextKey}}`, the same shape the screen saves in the browser. Creative IDs must be `sha1(<absolute file path>)[:12]`, used both as the `cr` key and in each set's `items`; any other ID is dropped on load. A paired static is one `cr` entry (the feed image) with `alt: {id, name, stem, path}` for its 9:16 file.

## Using it with Claude Code

If you use Claude Code, open it in this folder and type `/launch-ads`. The skill in `.claude/skills/launch-ads/` has Claude build the CSV from a folder and your copy, dry-run it, wait for your OK, then launch.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| "created by an app that is in development mode" | Switch your Meta app to **Live** (Setup, step 2a). |
| "page '…' isn't one this ad account can advertise from" | Assign that Page to the system user with **Create ads** (step 2c), then run `check_setup.py`. |
| "META_PIXEL_ID is missing" | Fill it in `.env`; `check_setup.py` lists your pixels. |
| "Can't reach Meta" on the screen | Run `.venv/bin/python check_setup.py`; it says what's wrong with the token or account. After changing `.env`, restart: `pkill -f app/server.py`, then reopen. |
| "the ad account has hit Meta's request limit" | Wait 15 minutes, then Re-check **once** (see Rate limits). |
| Ad set shows "Location limited" / "Select the advertiser and payer" | Set the advertiser and payer once on any ad set in Ads Manager, or fill `META_ADVERTISER_ID`. |
| "landing page returned HTTP 404" | The link is wrong or the product isn't live. A page that passed is remembered for 12 hours (`.link_ok.json`; delete it to force a fresh check). |
| The token stopped working | It was revoked or the system user lost an asset. Generate a new token (step 2d) and paste it into `.env`. |
| macOS won't open `Open Launcher.command` | Right-click it → **Open**, or use `./start.sh` in Terminal. |

Never call a launch done until Ads Manager shows the ad with its destination link and button.

## What's in the folder

| File | What |
|---|---|
| `launch_ads.py` | The engine: checks a CSV and creates the ads. |
| `app/server.py`, `app/index.html` | The screen. |
| `check_setup.py` | Read-only check of your settings; lists your Page, pixel and Instagram IDs. |
| `setup.sh`, `start.sh`, `install_app.sh`, `Open Launcher.command` | Install, open the screen, build the Mac app. |
| `.env.example` | Template for your settings. Your real settings live in `.env`, which is never uploaded to GitHub. |
| `template.csv`, `test/` | A CSV to copy, and a one-ad test. |
| `batches/` | Every batch you review or launch, with its results log. Yours; not uploaded to GitHub. |

Built on Meta Marketing API **v26.0** with the `facebook_business` SDK 26.0.2. Meta retires API versions after about two years; when it does, update `API_VERSION` in `launch_ads.py` and the SDK version in `requirements.txt`.
