# Mazoud daily Instagram Story

Posts one photo a day as an **Instagram Story** at **09:00 Asia/Dubai**, in filename order
(`Mazoud_001.jpg`, `Mazoud_002.jpg`, …), and starts again from the first photo after the last.
It runs on GitHub Actions, so your Mac can be off.

## How it works

- **Photos:** `photos/` holds byte-identical copies of the JPGs directly inside
  `~/Desktop/Mazoud/Photos`. Subfolders (such as `Cutouts`) and other files are ignored, and
  the originals are never touched. Only names like `Mazoud_<number>.jpg` / `.jpeg` count, sorted
  by number.
- **Stories only:** the API call always sends `media_type=STORIES` (never a feed post or reel).
  This is checked by a test.
- **Schedule:** Dubai is UTC+4 all year, so 09:00 Dubai is 05:00 UTC. GitHub sometimes starts
  scheduled jobs late, so the main run starts at 08:40 Dubai and waits until 09:00. Retry runs
  at 09:20, 10:00 and 12:00 do nothing if today's story is already out. Nothing is posted after
  18:00 Dubai. A day that GitHub misses completely is skipped, and the next day continues the
  rotation; the missed photo is not lost.
- **State:** `state/history.json` records every story (date, file, cycle, Instagram IDs) and the
  current position. The workflow commits it back to the repository, so it survives restarts.
- **No duplicates:** at most one story per Dubai calendar day. Before publishing, the run saves
  the container ID it is about to publish. If a run dies after publishing but before recording
  it, the next run asks Instagram for that container's status, sees `PUBLISHED`, records it, and
  does not post again. If the state can't be saved, the run stops before posting.
- **New photos:** add them to `~/Desktop/Mazoud/Photos`, run `scripts/sync_photos.sh`, then
  commit and push. Each one slots into the rotation by its number.
- **Checks:** each photo is checked to be a JPEG of at most 8 MB before it's sent.

## Setup (one time)

1. **Instagram:** the account must be a professional (Business or Creator) account, connected
   to a Facebook Page, with a Meta developer app that has `instagram_basic` and
   `instagram_content_publish`. For a token that doesn't expire, use a **system user** access
   token from Meta Business Settings. Ordinary user tokens expire after 60 days and would stop
   the posts.
   - If you use *Instagram Login* instead (`graph.instagram.com`), set the repository variable
     `GRAPH_HOST=graph.instagram.com`. Those tokens must be refreshed every 60 days.
2. **GitHub:** create a **public** repository and push this folder to it. Meta downloads each
   photo from its public `raw.githubusercontent.com` address, so the photos are publicly visible.
3. **Secrets** (Settings → Secrets and variables → Actions):
   - `IG_USER_ID`: the Instagram professional account ID.
   - `IG_ACCESS_TOKEN`: the access token.

   Optional variable: `GRAPH_VERSION` (default `v23.0`).

## Dry run (posts nothing)

- **Locally:** `python3 publish.py --days 14` shows the next photo and the coming days.
- **On GitHub:** Actions → *Mazoud daily story* → *Run workflow* → `dry-run`. This also checks
  that the photo URL is reachable and that the credentials can read the account. It only reads;
  it changes no state.
- **Tests:** `python3 -m unittest discover -s tests`.

Until the next step is done, scheduled runs are dry runs too.

## Going live (only after the owner approves)

Set the repository variable **`LIVE_PUBLISHING`** to `true`. From then on, the 09:00 runs
publish. To stop at any time, delete that variable or set it to anything else, or disable the
workflow.

## Notes

- Stories are 9:16 (portrait). Landscape photos (for example 004, 006, 013 and 025) appear
  smaller, with a background that Instagram adds.
- GitHub disables scheduled workflows in a repository with no activity for 60 days. Once live,
  the daily state commits keep it active. During a long dry-run period, re-enable it if GitHub
  asks.
