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

1. **Instagram:** the account must be a professional (Business or Creator) account. The Meta app
   uses **Instagram Login** (`graph.instagram.com`) with the `instagram_business_basic` and
   `instagram_business_content_publish` permissions. Its long-lived tokens last 60 days and must
   be refreshed before they expire, or the stories stop. The repository variable
   `GRAPH_HOST=graph.instagram.com` (also the default) selects these endpoints.
2. **GitHub:** create a **public** repository and push this folder to it. Meta downloads each
   photo from its public `raw.githubusercontent.com` address, so the photos are publicly visible.
3. **Secrets** (Settings → Secrets and variables → Actions):
   - `IG_USER_ID`: the Instagram professional account ID.
   - `IG_ACCESS_TOKEN`: the access token.

   Optional variable: `GRAPH_VERSION` (default `v23.0`).

## Dry run (posts nothing)

- **Locally:** `python3 publish.py --days 14` shows the next photo and the coming days.
- **On GitHub:** Actions → *Mazoud daily story* → *Run workflow* → `dry-run`. This also checks
  that the next photo's URL is reachable, that the token is valid, that `IG_USER_ID` belongs to
  it, and that it has publishing permission (without printing either value). It only reads;
  it changes no state.
- **Tests:** `python3 -m unittest discover -s tests`.

Until the next step is done, scheduled runs are dry runs too.

## Token renewal

Instagram Login long-lived tokens last 60 days. The workflow *Instagram token renewal*
(`refresh_token.py`) renews `IG_ACCESS_TOKEN` on the 1st of each month (06:00 Dubai) through
Meta's `refresh_access_token` endpoint (`grant_type=ig_refresh_token`).

- **Checks before renewing:** the token looks like an Instagram Login token (starts with `IG`,
  with no stray spaces or quotes), Instagram accepts it, and it belongs to `IG_USER_ID`.
  Instagram itself refuses a renewal if the token is short-lived, less than 24 hours old, or
  expired.
- **Checks before storing:** the renewed token must work for the same account and be valid for
  at least 30 days. If not, nothing is stored and the old secret stays as it is.
- **Saving the secret:** GitHub's built-in workflow token can't write secrets, so this uses a
  second secret, `SECRETS_ADMIN_TOKEN`. That is a fine-grained personal access token limited to
  this repository, with only **Secrets: Read and write**. The new value is passed to
  `gh secret set` on standard input and is never printed, written to disk or committed.
- **Expired or revoked token:** the run fails with an explanation (for example, that a new token
  has to be generated in the Meta app dashboard). GitHub emails you about failed runs.
- **Test safely:** *Run workflow* → `check` checks everything and never calls the renewal
  endpoint, so it can't invalidate anything.
- **Switching it on:** a renewal only happens when the repository variable
  `TOKEN_REFRESH_ENABLED` is `true` (and the workflow is enabled).

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
