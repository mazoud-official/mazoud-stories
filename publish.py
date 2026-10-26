#!/usr/bin/env python3
"""Publish one Mazoud photo per day as an Instagram Story.

Standard library only. Safe by default: nothing is published unless
LIVE_PUBLISHING=true *and* --live is passed. See README.md.
"""
import argparse
import json
import os
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
PHOTOS_DIR = os.path.join(ROOT, "photos")
STATE_FILE = os.path.join(ROOT, "state", "history.json")

TZ = ZoneInfo("Asia/Dubai")
PUBLISH_AT = (9, 0)          # 09:00 Dubai time
LATEST = (18, 0)             # a delayed/retried run will not post after this
MAX_EARLY_WAIT = 45 * 60     # an early run waits for 09:00 at most this long
MAX_BYTES = 8 * 1024 * 1024  # Instagram's image limit

PHOTO_RE = re.compile(r"^Mazoud_(\d+)\.jpe?g$", re.IGNORECASE)


class Abort(Exception):
    """Stop without publishing anything."""


# ---------------------------------------------------------------- photos

def list_photos(directory=PHOTOS_DIR):
    """JPGs named Mazoud_<n>.jpg directly inside `directory`, in numeric order."""
    found = []
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        m = PHOTO_RE.match(name)
        if m and os.path.isfile(path) and not os.path.islink(path):
            found.append((int(m.group(1)), name))
    found.sort()
    return found


def jpeg_size(path):
    """(width, height) from the JPEG header; raises Abort if not a JPEG."""
    with open(path, "rb") as f:
        if f.read(2) != b"\xff\xd8":
            raise Abort(f"{os.path.basename(path)} is not a JPEG")
        while True:
            marker = f.read(2)
            if len(marker) < 2 or marker[0] != 0xFF:
                break
            (length,) = struct.unpack(">H", f.read(2))
            if marker[1] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                _, h, w = struct.unpack(">BHH", f.read(5))
                return w, h
            f.seek(length - 2, 1)
    raise Abort(f"{os.path.basename(path)}: could not read JPEG dimensions")


def check_photo(path):
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        raise Abort(f"{os.path.basename(path)} is {size / 1048576:.1f} MB (Instagram limit 8 MB)")
    return jpeg_size(path)


# ---------------------------------------------------------------- state

def new_state():
    return {"version": 1, "cycle": 1, "last": None, "pending": None, "history": []}


def load_state(path=STATE_FILE):
    if not os.path.exists(path):
        return new_state()
    with open(path) as f:
        return json.load(f)


def save_state(state, path=STATE_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def next_photo(photos, state):
    """The photo after the last published one, wrapping to the first.

    Returns (number, name, cycle). New files slot into the rotation by number.
    """
    if not photos:
        raise Abort("no Mazoud_<n>.jpg files in photos/")
    cycle = state.get("cycle", 1)
    last = state.get("last")
    if last:
        key = (last["number"], last["file"])
        for num, name in photos:
            if (num, name) > key:
                return num, name, cycle
        return photos[0][0], photos[0][1], cycle + 1
    return photos[0][0], photos[0][1], cycle


def published_on(state, day):
    return [h for h in state["history"] if h["date"] == day and h["status"] == "published"]


# ---------------------------------------------------------------- Graph API

class GraphAPI:
    """Instagram API with Instagram Login (graph.instagram.com) by default."""

    def __init__(self, user_id, token, host="graph.instagram.com", version="v23.0"):
        self.user_id, self.token = user_id, token
        self.base = f"https://{host}/{version}"

    def _call(self, method, path, params):
        params = dict(params, access_token=self.token)
        data = urllib.parse.urlencode(params).encode()
        url = f"{self.base}/{path}"
        if method == "GET":
            req = urllib.request.Request(f"{url}?{data.decode()}")
        else:
            req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(self._redact(f"Graph API {method} {path}: HTTP {e.code}: {body}")) from None
        except urllib.error.URLError as e:
            raise RuntimeError(self._redact(f"Graph API {method} {path}: {e.reason}")) from None

    def _redact(self, text):
        # Neither the token nor the account ID ever reaches the log.
        return text.replace(self.token, "<IG_ACCESS_TOKEN>").replace(self.user_id, "<IG_USER_ID>")

    def me(self):
        """The account the token belongs to (read-only)."""
        return self._call("GET", "me", {"fields": "user_id,username,account_type"})

    def publishing_limit(self):
        """Read-only; needs the content publishing permission, so it also checks that."""
        data = self._call("GET", f"{self.user_id}/content_publishing_limit", {"fields": "quota_usage,config"})
        return (data.get("data") or [{}])[0]

    def create_story(self, image_url):
        # media_type=STORIES: never a feed post or reel
        return self._call("POST", f"{self.user_id}/media",
                          {"media_type": "STORIES", "image_url": image_url})["id"]

    def status(self, container_id):
        return self._call("GET", container_id, {"fields": "status_code"}).get("status_code")

    def publish(self, container_id):
        return self._call("POST", f"{self.user_id}/media_publish", {"creation_id": container_id})["id"]


# ---------------------------------------------------------------- persistence

def git_persist(message):
    """Commit and push state/history.json. Raises if the push fails."""
    def git(*args):
        subprocess.run(["git", *args], cwd=ROOT, check=True)
    git("add", "state/history.json")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    git("commit", "-m", message)
    git("pull", "--rebase", "--quiet")
    git("push")


# ---------------------------------------------------------------- run

class Publisher:
    def __init__(self, api, image_url_for, persist, now=None, sleep=time.sleep,
                 photos_dir=PHOTOS_DIR, state_file=STATE_FILE, log=print, poll_seconds=10):
        self.api, self.image_url_for, self._persist = api, image_url_for, persist
        self.now = now or (lambda: datetime.now(TZ))
        self.sleep, self.photos_dir, self.state_file = sleep, photos_dir, state_file
        self.log, self.poll_seconds = log, poll_seconds

    def persist(self, state, message):
        save_state(state, self.state_file)
        self._persist(message)

    def record(self, state, pending, media_id, note=None):
        entry = {"date": pending["date"], "file": pending["file"], "number": pending["number"],
                 "cycle": pending["cycle"], "status": "published", "container_id": pending.get("container_id"),
                 "media_id": media_id, "published_at": self.now().isoformat(timespec="seconds")}
        if note:
            entry["note"] = note
        state["history"].append(entry)
        state["last"] = {"file": pending["file"], "number": pending["number"], "date": pending["date"]}
        state["cycle"] = pending["cycle"]
        state["pending"] = None
        self.persist(state, f"Story {pending['date']}: {pending['file']}")
        self.log(f"Recorded {pending['file']} for {pending['date']} (media {media_id}).")

    def wait_until_finished(self, container_id):
        for _ in range(60):
            code = self.api.status(container_id)
            if code in ("FINISHED", "PUBLISHED"):
                return code
            if code in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"container {container_id} status {code}")
            self.sleep(self.poll_seconds)
        raise RuntimeError(f"container {container_id} still not ready")

    def resolve_pending(self, state):
        """Finish or discard a run that stopped part-way. Returns pending to reuse, or None."""
        p = state.get("pending")
        if not p:
            return None
        if not p.get("container_id"):
            self.log("Earlier run stopped before creating a story container; nothing was posted.")
            return p
        code = self.api.status(p["container_id"])
        self.log(f"Earlier run left container {p['container_id']} in state {code}.")
        if code == "PUBLISHED":
            # It went out; the earlier run just didn't get to record it. Never re-post.
            self.record(state, p, None, note="recovered: container already published")
            return "done"
        if code in ("FINISHED", "IN_PROGRESS") and p["date"] == self.today():
            return p  # not yet published: publish this same container
        p = dict(p, container_id=None)  # expired/errored: nothing was posted, start again
        return p

    def today(self):
        return self.now().date().isoformat()

    def run(self):
        now = self.now()
        today = now.date().isoformat()
        state = load_state(self.state_file)

        if published_on(state, today):
            self.log(f"Already published today ({today}); nothing to do.")
            return "skipped"

        pending = self.resolve_pending(state)
        if pending == "done":
            if published_on(state, today):
                return "recovered"
            pending = None
            state = load_state(self.state_file)

        start = now.replace(hour=PUBLISH_AT[0], minute=PUBLISH_AT[1], second=0, microsecond=0)
        latest = now.replace(hour=LATEST[0], minute=LATEST[1], second=0, microsecond=0)
        if now < start:
            wait = (start - now).total_seconds()
            if wait > MAX_EARLY_WAIT:
                self.log(f"Too early ({now:%H:%M} Dubai); not publishing.")
                return "too-early"
            self.log(f"Waiting {int(wait)} s for 09:00 Dubai.")
            self.sleep(wait)
        elif now > latest:
            self.log(f"Too late ({now:%H:%M} Dubai, cutoff {LATEST[0]:02d}:{LATEST[1]:02d}); skipping today.")
            return "too-late"

        if pending and pending.get("file") and os.path.isfile(os.path.join(self.photos_dir, pending["file"])):
            pending = dict(pending, date=today)
        else:
            num, name, cycle = next_photo(list_photos(self.photos_dir), state)
            pending = {"date": today, "file": name, "number": num, "cycle": cycle, "container_id": None}
        check_photo(os.path.join(self.photos_dir, pending["file"]))

        # 1. Note the intent before touching Instagram.
        if not pending.get("container_id"):
            state["pending"] = pending
            self.persist(state, f"Pending story {today}: {pending['file']}")
            # 2. Create the container; save its id before publishing so a retry can check it.
            pending["container_id"] = self.api.create_story(self.image_url_for(pending["file"]))
            state["pending"] = pending
            self.persist(state, f"Container for {today}: {pending['file']}")

        # 3. Publish once the container is ready.
        if self.wait_until_finished(pending["container_id"]) == "PUBLISHED":
            self.record(state, pending, None, note="container already published")
            return "recovered"
        media_id = self.api.publish(pending["container_id"])
        self.record(state, pending, media_id)
        return "published"


def dry_run(args, env):
    """Read-only check: nothing is posted and no state is written."""
    state = load_state()
    photos = list_photos()
    now = datetime.now(TZ)
    today = now.date().isoformat()
    print(f"Dubai time now: {now:%Y-%m-%d %H:%M} | photos in rotation: {len(photos)} | cycle {state['cycle']}")
    if published_on(state, today):
        print(f"Already published today: {published_on(state, today)[0]['file']}")
    _, next_name, cycle = next_photo(photos, state)
    w, h = check_photo(os.path.join(PHOTOS_DIR, next_name))
    print(f"Next story: {next_name} ({w}x{h}), cycle {cycle}")
    if state.get("pending"):
        print(f"Pending from an earlier run: {state['pending']}")

    # Show the coming days of the rotation.
    sim = json.loads(json.dumps(state))
    print(f"Next {args.days} days:")
    for i in range(args.days):
        num, name, cycle = next_photo(photos, sim)
        day = (now + timedelta(days=i)).date().isoformat()
        print(f"  {day} 09:00  {name}  (cycle {cycle})")
        sim["last"], sim["cycle"] = {"file": name, "number": num, "date": day}, cycle

    base = image_base(env)
    if base:
        url = f"{base}/{urllib.parse.quote(next_name)}"
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            ctype = r.headers.get("Content-Type", "")
        print(f"Image URL reachable: {url} ({ctype})")
        if not ctype.startswith("image/"):
            raise Abort(f"image URL does not serve an image: {ctype}")
    if env.get("IG_USER_ID") and env.get("IG_ACCESS_TOKEN"):
        api = api_from(env)
        print(f"API host: {api.base}")
        me = api.me()
        print(f"Token valid: yes (account @{me.get('username')}, type {me.get('account_type')})")
        if env["IG_USER_ID"].strip() not in (str(me.get("user_id")), str(me.get("id"))):
            raise Abort("IG_USER_ID does not belong to the account of IG_ACCESS_TOKEN")
        print("IG_USER_ID matches the token's account: yes")
        limit = api.publishing_limit()
        quota = (limit.get("config") or {}).get("quota_total")
        print(f"Content publishing permission: yes (posts in last 24 h: {limit.get('quota_usage')}"
              + (f" of {quota})" if quota else ")"))
    else:
        print("No Instagram credentials set; account check skipped.")
    if env.get("GITHUB_ACTIONS") == "true":
        # Proves the run may push state/history.json (authenticates, sends nothing).
        r = subprocess.run(["git", "push", "--dry-run", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            raise Abort("this run could not push to the repository, so the history could not be saved")
        print("History can be saved to GitHub (git push --dry-run): yes")
    print("DRY RUN: nothing was published and no state was changed.")


def image_base(env):
    if env.get("IMAGE_BASE_URL"):
        return env["IMAGE_BASE_URL"].rstrip("/")
    if env.get("GITHUB_REPOSITORY") and env.get("GITHUB_SHA"):
        return f"https://raw.githubusercontent.com/{env['GITHUB_REPOSITORY']}/{env['GITHUB_SHA']}/photos"
    return None


def api_from(env):
    return GraphAPI(env["IG_USER_ID"], env["IG_ACCESS_TOKEN"],
                    env.get("GRAPH_HOST") or "graph.instagram.com", env.get("GRAPH_VERSION") or "v23.0")


def is_mpo(path):
    with open(path, "rb") as f:
        head = f.read(65536)
    return b"MPF\x00" in head


def test_story(env, name, action, log=print):
    """One manual Story test, outside the daily rotation (state is not touched).

    prepare: checks the photo, the account and the URL, creates a STORIES container and
             waits until Instagram has processed it, but never publishes it
             (an unpublished container expires after 24 hours and is never shown).
    publish: the same, then publishes that container once.
    """
    if not PHOTO_RE.match(name) or not os.path.isfile(os.path.join(PHOTOS_DIR, name)):
        raise Abort(f"{name} is not a photo in photos/")
    path = os.path.join(PHOTOS_DIR, name)
    w, h = check_photo(path)
    if is_mpo(path):
        raise Abort(f"{name} is an MPO (extended JPEG), which Instagram does not accept")
    log(f"Photo: {name}, JPEG, {w}x{h}, {os.path.getsize(path) / 1048576:.2f} MB, aspect {w / h:.3f} "
        f"(Stories recommend 9:16 = 0.5625)")
    base = image_base(env)
    if not base:
        raise Abort("no image base URL")
    url = f"{base}/{urllib.parse.quote(name)}"
    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as r:
        ctype, length = r.headers.get("Content-Type", ""), r.headers.get("Content-Length")
    if not ctype.startswith("image/jpeg") or (length and int(length) != os.path.getsize(path)):
        raise Abort(f"public URL serves {ctype}, {length} bytes; expected the same JPEG")
    log(f"Public URL serves the same file: yes ({ctype}, {length} bytes)")
    api = api_from(env)
    me = api.me()
    if env["IG_USER_ID"].strip() not in (str(me.get("user_id")), str(me.get("id"))):
        raise Abort("IG_USER_ID does not belong to the account of IG_ACCESS_TOKEN")
    log(f"Account: @{me.get('username')} ({me.get('account_type')}); IG_USER_ID matches: yes")
    limit = api.publishing_limit()
    log(f"Publishing quota used in last 24 h: {limit.get('quota_usage')}")

    container = api.create_story(url)  # media_type=STORIES
    log("Instagram accepted a STORIES container for this image: yes")
    status = None
    for _ in range(30):
        status = api.status(container)
        if status in ("FINISHED", "PUBLISHED", "ERROR", "EXPIRED"):
            break
        time.sleep(10)
    log(f"Container status: {status}")
    if status != "FINISHED":
        raise Abort(f"container is {status}; not publishing")
    if action != "publish":
        log("PREPARE ONLY: the container was not published; it expires unseen within 24 hours.")
        return "prepared"
    media_id = api.publish(container)
    log(f"PUBLISHED one test Story (media id {media_id}). The daily rotation and its state were not changed.")
    return "published"


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--test-story":
        ap = argparse.ArgumentParser()
        ap.add_argument("--test-story", required=True, metavar="FILE")
        ap.add_argument("--action", choices=["prepare", "publish"], default="prepare")
        args = ap.parse_args()
        env = os.environ
        try:
            if args.action == "publish" and env.get("TEST_CONFIRM") != f"PUBLISH {args.test_story}":
                raise Abort(f"publishing a test Story needs the confirmation text 'PUBLISH {args.test_story}'")
            test_story(env, args.test_story, args.action)
            return 0
        except Abort as e:
            print(f"Stopped: {e}", file=sys.stderr)
            return 2
        except (RuntimeError, OSError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="publish for real (also needs LIVE_PUBLISHING=true)")
    ap.add_argument("--days", type=int, default=7, help="dry run: how many days of the rotation to show")
    args = ap.parse_args()
    env = os.environ
    try:
        if not args.live:
            dry_run(args, env)
            return 0
        if env.get("LIVE_PUBLISHING") != "true":
            print("Live publishing is not switched on (LIVE_PUBLISHING != 'true'). Running a dry run instead.")
            dry_run(args, env)
            return 0
        base = image_base(env)
        if not base:
            raise Abort("no image base URL")
        pub = Publisher(api_from(env), lambda f: f"{base}/{urllib.parse.quote(f)}", git_persist)
        print(f"Result: {pub.run()}")
        return 0
    except Abort as e:
        print(f"Stopped without publishing: {e}", file=sys.stderr)
        return 2
    except (RuntimeError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
