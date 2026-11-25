---
name: launch-ads
description: Bulk-launch Meta ads (PAUSED by default) from a folder of images and/or videos plus copy, using launch_ads.py in this repo. Use for "/launch-ads", "launch these ads", "upload these 10 videos into Broad US", "push this batch to Meta".
---

# /launch-ads

Tool: `launch_ads.py` in the repo root. README.md has the CSV columns. Run it from the repo root with `.venv/bin/python`. Ads are created PAUSED unless the user says to launch this batch Active: then add `--active` to the launch command (never otherwise). New campaigns are always PAUSED, so Active ads in one won't spend until the user turns the campaign on.

Never read, print or ask for the contents of `.env`. If the token or an ID is missing, tell the user which line to fill in themselves and point them to README "Setup".

## 0. If the user wants to see it in the Ad Launcher screen first
Build the launch as a hand-off instead of a CSV: write `handoff.json` in the repo root (format in README, "Hand-off"), one ad set per angle with its own copy (creative IDs = `sha1(<absolute path>)[:12]`, or the screen drops them), then run `pkill -f app/server.py` and have the user open the app. They check it and click **Review & launch**, which writes `batches/<date>-<campaign>.csv` and dry-runs it. Continue from step 2 on that CSV.

## 1. Build the CSV
- Write it to `batches/<MM-DD>-<slug>.csv`, starting from the `template.csv` headers.
- **Media:** list the folder the user names and use one row per file × copy variant ("3 hooks each" means 3 rows per file).
- A static that comes as a feed image plus a 9:16 (`A05-1x1.png` + `A05-9x16.png`) is ONE row: the feed image in `media_path`, the tall one in `media_path_9x16`. Never two rows.
- **Ad set:**
  - "into Broad US" → `ad_set_name` = `Broad US`; the launcher finds it.
  - An ID → `ad_set_id`.
  - A new ad set → also fill `campaign_id` or `campaign_name`, `countries`, and the budget columns. Ask only for what's missing.
- **Page:** set `page` to the Page name the user gives. If they don't name one and `META_PAGE_ID` may be blank, ask once per batch.
- **Copy:** use the copy the user gives. If they point to a file or an earlier launch, reuse it verbatim. Never invent copy. If copy is missing, ask.
- **Names:** ad set `Batch//Concept//Format//LP//Country`, joined with `//`, blank parts left out; ads `Batch//Concept//<file name>`. Write the batch out (`Batch 10`, never `B10`). If the user has their own naming convention, use theirs.
- **Link:** ask if none is given.
- **Account:** `.env` by default. If the user names another account, add `--profile <name>` to both commands (`.env.<name>`; list them with `ls -a | grep .env.`). If the profile doesn't exist, stop and ask.

## 2. Dry run → wait for OK
- Run `.venv/bin/python launch_ads.py <csv> --dry-run 2>&1 | tail -60`.
- Problems listed: fix what's yours (paths, typos) and rerun. Ask the user about the rest.
- Clean: show the ad account line, the table and the "Would create" line as-is, plus any warnings, then **stop and wait for the user's OK.**

## 3. Policy check (new copy only)
Skip when the copy is reused verbatim from an earlier launch. Otherwise read the unique copy lines and flag any that risk Meta ad policy: health or income claims and guaranteed results, before/after or implied body change, personal attributes ("Do you have…?"), unrealistic timeframes or numbers. Show `line → risk → safer rewrite`, one line each, alongside the dry-run table, and get the user's OK on both in one reply.

## 4. Launch
- Run `.venv/bin/python launch_ads.py <csv> 2>&1 | tail -25`.
- Report only the summary block: created, failed with reasons, Ads Manager link. Never print the results CSV or raw API output.
- On failures, fix the rows and rerun the same command. Finished rows are skipped.
- Not done until the user confirms Ads Manager shows each ad's destination link and button.
