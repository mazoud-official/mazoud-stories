import io, json, os, sys, unittest, urllib.error
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import refresh_token as R

OLD, NEW = "IGAAold" + "x" * 40, "IGAAnew" + "y" * 40
ENV = {"IG_ACCESS_TOKEN": OLD, "IG_USER_ID": "17841400000000000", "SECRETS_ADMIN_TOKEN": "github_pat_zzz",
       "GITHUB_REPOSITORY": "o/r", "TOKEN_REFRESH_ENABLED": "true"}


class Harness:
    def __init__(self, new=NEW, expires=5184000, valid=(OLD, NEW), set_fails=0):
        self.secret, self.updated, self.calls, self.lines = OLD, "2026-10-26T11:25:36Z", [], []
        self.new, self.expires, self.valid, self.set_fails = new, expires, valid, set_fails
    def refresh(self, token):
        self.calls.append("refresh")
        return self.new, self.expires
    def account(self, token, uid):
        if token not in self.valid:
            raise R.RefreshError("the token does not belong to IG_USER_ID")
        return "mazoud.ae"
    def secret_set(self, repo, value, gh):
        self.calls.append("set")
        if self.set_fails:
            self.set_fails -= 1
            raise R.RefreshError("gh failed")
        self.secret, self.updated = value, "2026-11-01T02:00:05Z"
    def updated_at(self, repo, gh):
        return self.updated
    def run(self, mode, env=ENV):
        return R.run(mode, env, refresh=self.refresh, secret_set=self.secret_set,
                     secret_updated_at=self.updated_at, account=self.account, log=self.lines.append)
    def output(self):
        return "\n".join(self.lines)


class Tests(unittest.TestCase):
    def assertNoTokens(self, h):
        for t in (OLD, NEW, "github_pat_zzz", "17841400000000000"):
            self.assertNotIn(t, h.output())

    def test_check_never_refreshes_or_writes(self):
        h = Harness()
        self.assertEqual(h.run("check"), "checked")
        self.assertEqual(h.calls, [])
        self.assertEqual(h.secret, OLD)
        self.assertNoTokens(h)

    def test_refresh_disabled_by_default(self):
        h = Harness()
        env = dict(ENV); env.pop("TOKEN_REFRESH_ENABLED")
        self.assertEqual(h.run("refresh", env), "disabled")
        self.assertEqual(h.calls, [])

    def test_refresh_updates_secret(self):
        h = Harness()
        self.assertEqual(h.run("refresh"), "refreshed")
        self.assertEqual(h.secret, NEW)
        self.assertNoTokens(h)

    def test_retries_secret_write(self):
        h = Harness(set_fails=2)
        self.assertEqual(h.run("refresh"), "refreshed")
        self.assertEqual(h.calls.count("set"), 3)

    def test_new_token_that_does_not_work_is_not_stored(self):
        h = Harness(valid=(OLD,))
        with self.assertRaises(R.RefreshError):
            h.run("refresh")
        self.assertEqual(h.secret, OLD)
        self.assertNotIn("set", h.calls)

    def test_short_lived_reply_is_not_stored(self):
        h = Harness(expires=3600)
        with self.assertRaises(R.RefreshError):
            h.run("refresh")
        self.assertEqual(h.secret, OLD)

    def test_wrong_token_types_refused_before_any_call(self):
        for bad in ("EAAB" + "x" * 40, " IGAA" + "x" * 40, '"IGAA' + 'x' * 40 + '"', "", "abc"):
            h = Harness()
            with self.assertRaises(R.RefreshError):
                h.run("check", dict(ENV, IG_ACCESS_TOKEN=bad))
            self.assertEqual(h.calls, [])

    def test_missing_secrets_credential(self):
        h = Harness()
        env = dict(ENV); env.pop("SECRETS_ADMIN_TOKEN")
        with self.assertRaisesRegex(R.RefreshError, "SECRETS_ADMIN_TOKEN"):
            h.run("refresh", env)
        self.assertEqual(h.calls, [])

    def _http_error(self, body):
        return urllib.error.HTTPError(R.REFRESH_URL, 400, "Bad", {}, io.BytesIO(json.dumps(body).encode()))

    def test_expired_or_revoked_token_explained_without_value(self):
        err = self._http_error({"error": {"code": 190, "message": f"Error validating access token {OLD}"}})
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(R.RefreshError) as cm:
                R.http_refresh(OLD)
        self.assertIn("expired, revoked", str(cm.exception))
        self.assertNotIn(OLD, str(cm.exception))

    def test_official_endpoint_and_grant_type(self):
        seen = {}
        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass
        def fake(url, timeout):
            seen["url"] = url
            return Resp(json.dumps({"access_token": NEW, "token_type": "bearer", "expires_in": 5184000}).encode())
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertEqual(R.http_refresh(OLD), (NEW, 5184000))
        self.assertTrue(seen["url"].startswith("https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&"))

    def test_secret_value_goes_via_stdin_not_arguments(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            R.gh_secret_set("o/r", NEW, "github_pat_zzz")
        args, kwargs = run.call_args
        self.assertNotIn(NEW, " ".join(args[0]))
        self.assertEqual(kwargs["input"], NEW)


if __name__ == "__main__":
    unittest.main()
