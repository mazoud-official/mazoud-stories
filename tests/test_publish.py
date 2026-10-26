import os, shutil, sys, tempfile, unittest
from datetime import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import publish as P

JPEG = b"\xff\xd8\xff\xc0\x00\x11\x08\x07\x80\x04\x38\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xd9"


class FakeAPI:
    def __init__(self):
        self.containers, self.published, self.fail_publish_after = {}, [], False
    def create_story(self, url):
        cid = f"c{len(self.containers) + 1}"
        self.containers[cid] = {"url": url, "status": "FINISHED"}
        return cid
    def status(self, cid):
        return self.containers[cid]["status"]
    def publish(self, cid):
        assert self.containers[cid]["status"] == "FINISHED", "published a container twice"
        self.containers[cid]["status"] = "PUBLISHED"
        self.published.append(self.containers[cid]["url"])
        if self.fail_publish_after:
            raise RuntimeError("network dropped after publishing")
        return f"m{len(self.published)}"


class Clock:
    def __init__(self, *a):
        self.t = datetime(*a, tzinfo=P.TZ)
    def __call__(self):
        return self.t
    def sleep(self, s):
        from datetime import timedelta
        self.t += timedelta(seconds=s)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.photos = os.path.join(self.dir, "photos"); os.mkdir(self.photos)
        os.mkdir(os.path.join(self.photos, "Cutouts"))
        self.state = os.path.join(self.dir, "history.json")
        for n in (1, 2, 3):
            self.add(f"Mazoud_{n:03d}.jpg")
        self.add("Cutouts/Mazoud_cutout_001.jpg"); self.add("notes.txt"); self.add("Mazoud_009.png")
        self.api, self.persisted = FakeAPI(), []
    def tearDown(self):
        shutil.rmtree(self.dir)
    def add(self, name):
        with open(os.path.join(self.photos, name), "wb") as f:
            f.write(JPEG)
    def pub(self, clock, persist=None):
        return P.Publisher(self.api, lambda f: "https://x/" + f, persist or self.persisted.append,
                           now=clock, sleep=clock.sleep, photos_dir=self.photos,
                           state_file=self.state, log=lambda *a: None, poll_seconds=0)
    def files(self):
        return [u.rsplit("/", 1)[1] for u in self.api.published]


class Tests(Base):
    def test_only_top_level_numbered_jpgs(self):
        self.add("Mazoud_10.JPEG")
        self.assertEqual([n for _, n in P.list_photos(self.photos)],
                         ["Mazoud_001.jpg", "Mazoud_002.jpg", "Mazoud_003.jpg", "Mazoud_10.JPEG"])

    def test_daily_order_and_wrap(self):
        for day in range(1, 8):
            self.assertEqual(self.pub(Clock(2026, 11, day, 9, 0)).run(), "published")
        self.assertEqual(self.files(), ["Mazoud_001.jpg", "Mazoud_002.jpg", "Mazoud_003.jpg"] * 2 + ["Mazoud_001.jpg"])
        self.assertEqual(P.load_state(self.state)["cycle"], 3)

    def test_once_per_day(self):
        c = Clock(2026, 11, 1, 9, 0)
        self.pub(c).run()
        self.assertEqual(self.pub(c).run(), "skipped")
        self.assertEqual(self.pub(Clock(2026, 11, 1, 12, 0)).run(), "skipped")
        self.assertEqual(len(self.api.published), 1)

    def test_early_run_waits_until_nine(self):
        c = Clock(2026, 11, 1, 8, 40)
        self.pub(c).run()
        self.assertEqual((c.t.hour, c.t.minute), (9, 0))

    def test_too_late_skips(self):
        self.assertEqual(self.pub(Clock(2026, 11, 1, 19, 0)).run(), "too-late")
        self.assertEqual(self.api.published, [])

    def test_new_photo_joins_rotation(self):
        self.pub(Clock(2026, 11, 1, 9)).run()
        self.add("Mazoud_002b.jpg")  # not matching: ignored
        self.add("Mazoud_004.jpg")
        for d in (2, 3, 4, 5):
            self.pub(Clock(2026, 11, d, 9)).run()
        self.assertEqual(self.files(), ["Mazoud_001.jpg", "Mazoud_002.jpg", "Mazoud_003.jpg", "Mazoud_004.jpg", "Mazoud_001.jpg"])

    def test_crash_after_publish_is_not_reposted(self):
        self.api.fail_publish_after = True
        with self.assertRaises(RuntimeError):
            self.pub(Clock(2026, 11, 1, 9)).run()
        self.api.fail_publish_after = False
        self.assertEqual(self.pub(Clock(2026, 11, 1, 9, 20)).run(), "recovered")
        self.assertEqual(self.pub(Clock(2026, 11, 1, 10)).run(), "skipped")
        self.assertEqual(len(self.api.published), 1)
        self.pub(Clock(2026, 11, 2, 9)).run()
        self.assertEqual(self.files(), ["Mazoud_001.jpg", "Mazoud_002.jpg"])

    def test_failed_save_before_publish_posts_nothing(self):
        def broken(msg):
            raise RuntimeError("push rejected")
        with self.assertRaises(RuntimeError):
            self.pub(Clock(2026, 11, 1, 9), persist=broken).run()
        self.assertEqual(self.api.published, [])
        self.assertEqual(self.pub(Clock(2026, 11, 1, 9, 20)).run(), "published")
        self.assertEqual(self.files(), ["Mazoud_001.jpg"])

    def test_failure_on_create_retries_same_photo(self):
        orig = self.api.create_story
        self.api.create_story = lambda url: (_ for _ in ()).throw(RuntimeError("API down"))
        with self.assertRaises(RuntimeError):
            self.pub(Clock(2026, 11, 1, 9)).run()
        self.api.create_story = orig
        self.pub(Clock(2026, 11, 1, 10)).run()
        self.assertEqual(self.files(), ["Mazoud_001.jpg"])

    def test_expired_container_from_yesterday_is_redone_today(self):
        self.api.fail_publish_after = False
        p = self.pub(Clock(2026, 11, 1, 9))
        p.api.publish = lambda cid: (_ for _ in ()).throw(RuntimeError("publish refused"))
        with self.assertRaises(RuntimeError):
            p.run()
        self.api = FakeAPI(); self.api.containers["c1"] = {"url": "", "status": "EXPIRED"}
        self.pub(Clock(2026, 11, 2, 9)).run()
        self.assertEqual(self.files(), ["Mazoud_001.jpg"])

    def test_stories_only(self):
        import inspect
        src = inspect.getsource(P.GraphAPI.create_story)
        self.assertIn('"media_type": "STORIES"', src)
        self.assertNotIn("REELS", inspect.getsource(P))

    def test_oversize_refused(self):
        with open(os.path.join(self.photos, "Mazoud_001.jpg"), "ab") as f:
            f.write(b"\0" * (P.MAX_BYTES + 1))
        with self.assertRaises(P.Abort):
            self.pub(Clock(2026, 11, 1, 9)).run()
        self.assertEqual(self.api.published, [])


class TestStoryTests(Base):
    def run_test(self, action, confirm=""):
        import io, contextlib
        from unittest import mock
        class Head:
            headers = {"Content-Type": "image/jpeg", "Content-Length": str(len(JPEG))}
            def __enter__(s): return s
            def __exit__(s, *a): pass
        api = self.api
        api.me = lambda: {"user_id": "123", "username": "mazoud.ae", "account_type": "MEDIA_CREATOR"}
        api.publishing_limit = lambda: {"quota_usage": 0}
        env = {"IG_USER_ID": "123", "IG_ACCESS_TOKEN": "IGAAx", "IMAGE_BASE_URL": "https://x",
               "TEST_CONFIRM": confirm}
        argv = ["publish.py", "--test-story", "Mazoud_001.jpg", "--action", action]
        with mock.patch.object(P, "PHOTOS_DIR", self.photos), mock.patch.object(P, "api_from", lambda e: api), \
             mock.patch("urllib.request.urlopen", lambda *a, **k: Head()), mock.patch.dict(os.environ, env), \
             mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            return P.main()

    def test_prepare_never_publishes(self):
        self.assertEqual(self.run_test("prepare"), 0)
        self.assertEqual(len(self.api.containers), 1)
        self.assertEqual(self.api.published, [])

    def test_publish_needs_exact_confirmation(self):
        self.assertEqual(self.run_test("publish", "PUBLISH"), 2)
        self.assertEqual(self.run_test("publish", "publish Mazoud_001.jpg"), 2)
        self.assertEqual(self.api.containers, {})
        self.assertEqual(self.run_test("publish", "PUBLISH Mazoud_001.jpg"), 0)
        self.assertEqual(self.files(), ["Mazoud_001.jpg"])
        self.assertFalse(os.path.exists(self.state))


if __name__ == "__main__":
    unittest.main()
