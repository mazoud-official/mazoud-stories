#!/usr/bin/env python3
"""Renew the Instagram Login long-lived access token and store it in the
IG_ACCESS_TOKEN repository secret. Standard library only (plus the `gh` CLI
that GitHub's runners include, used to write the secret).

    --check    read-only: checks the current token and the secrets credential;
               never calls the refresh endpoint and never writes anything
    --refresh  renews the token (needs TOKEN_REFRESH_ENABLED=true)

No token value is ever printed, logged or written to disk.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from publish import GraphAPI

REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
SECRET_NAME = "IG_ACCESS_TOKEN"
MIN_NEW_LIFETIME = 30 * 24 * 3600   # a renewed token must be good for at least 30 days


class RefreshError(Exception):
    pass


def check_format(token):
    """Instagram Login tokens start with "IG"; Facebook Login tokens start with "EAA"."""
    if not token or token != token.strip() or any(c in token for c in " \"'\n\r\t"):
        raise RefreshError("IG_ACCESS_TOKEN is empty or contains spaces, quotes or line breaks")
    if token.startswith("EAA"):
        raise RefreshError("IG_ACCESS_TOKEN is a Facebook Login token; this refresh is only for Instagram Login tokens")
    if not token.startswith("IG"):
        raise RefreshError("IG_ACCESS_TOKEN does not look like an Instagram Login token")


def check_account(token, user_id):
    """The token is accepted by Instagram and belongs to IG_USER_ID. Returns the username."""
    me = GraphAPI(user_id, token).me()
    if user_id.strip() not in (str(me.get("user_id")), str(me.get("id"))):
        raise RefreshError("the token does not belong to IG_USER_ID")
    return me.get("username")


def explain(code, subcode, message):
    if code == 190:
        return ("Instagram rejected the token (expired, revoked, or the password changed). "
                "It can't be renewed; a new long-lived token has to be generated in the Meta app dashboard.")
    if "24 hours" in message or "24 hour" in message:
        return "the token is less than 24 hours old; Instagram only renews tokens that are at least a day old"
    if code in (4, 17, 32, 613):
        return "Instagram rate limit; try again later"
    return "Instagram refused the renewal"


def http_refresh(token):
    """Calls Meta's official endpoint. Returns (new_token, expires_in_seconds)."""
    query = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": token})
    try:
        with urllib.request.urlopen(f"{REFRESH_URL}?{query}", timeout=60) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e).get("error", {})
        except ValueError:
            err = {}
        code, sub, msg = err.get("code"), err.get("error_subcode"), str(err.get("message", ""))
        raise RefreshError(f"HTTP {e.code}, code {code}/{sub}: {explain(code, sub, msg)}") from None
    except urllib.error.URLError as e:
        raise RefreshError(f"could not reach Instagram: {e.reason}") from None
    new, expires = data.get("access_token"), data.get("expires_in")
    if not new or not isinstance(expires, int):
        raise RefreshError("Instagram's reply had no token or expiry")
    if str(data.get("token_type", "bearer")).lower() != "bearer":
        raise RefreshError("unexpected token type in Instagram's reply")
    return new, expires


def gh_secret_set(repo, value, gh_token):
    """Encrypts and stores the secret through `gh` (value on stdin, never in argv)."""
    env = dict(os.environ, GH_TOKEN=gh_token)
    r = subprocess.run(["gh", "secret", "set", SECRET_NAME, "--repo", repo], input=value,
                       text=True, capture_output=True, env=env)
    if r.returncode != 0:
        raise RefreshError(f"could not update the {SECRET_NAME} secret (gh exit {r.returncode})")


def gh_secret_updated_at(repo, gh_token):
    env = dict(os.environ, GH_TOKEN=gh_token)
    r = subprocess.run(["gh", "api", f"repos/{repo}/actions/secrets/{SECRET_NAME}", "--jq", ".updated_at"],
                       text=True, capture_output=True, env=env)
    if r.returncode != 0:
        raise RefreshError("the secrets credential can't read this repository's secrets "
                           "(missing, expired, or lacking the Secrets permission)")
    return r.stdout.strip()


def mask(value):
    # GitHub hides this value anywhere it might appear in the log from now on.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{value}")


def run(mode, env, refresh=http_refresh, secret_set=gh_secret_set,
        secret_updated_at=gh_secret_updated_at, account=check_account, now=None, log=print):
    token, user_id = env.get("IG_ACCESS_TOKEN", ""), env.get("IG_USER_ID", "")
    gh_token, repo = env.get("SECRETS_ADMIN_TOKEN", ""), env.get("GITHUB_REPOSITORY", "")
    now = now or datetime.now(timezone.utc)
    if not user_id:
        raise RefreshError("IG_USER_ID is not set")

    check_format(token)
    log("Token type: Instagram Login: yes")
    username = account(token, user_id)
    log(f"Current token valid: yes (@{username}); IG_USER_ID matches: yes")

    if not gh_token:
        raise RefreshError("SECRETS_ADMIN_TOKEN is not set, so a renewed token could not be saved")
    before = secret_updated_at(repo, gh_token)
    log(f"Secrets credential can read {SECRET_NAME}: yes (last updated {before})")

    if mode == "check":
        log("CHECK ONLY: the refresh endpoint was not called and nothing was changed.")
        return "checked"

    if env.get("TOKEN_REFRESH_ENABLED") != "true":
        log("Token renewal is not switched on (TOKEN_REFRESH_ENABLED != 'true'); nothing was changed.")
        return "disabled"

    new, expires_in = refresh(token)
    mask(new)
    check_format(new)
    if expires_in < MIN_NEW_LIFETIME:
        raise RefreshError(f"the renewed token expires in {expires_in // 86400} days; expected about 60")
    account(new, user_id)  # never store a token that doesn't work for this account
    log("Renewed token valid for the same account: yes")

    for attempt in range(3):
        try:
            secret_set(repo, new, gh_token)
            break
        except RefreshError:
            if attempt == 2:
                raise
    after = secret_updated_at(repo, gh_token)
    if after == before:
        raise RefreshError(f"{SECRET_NAME} did not change after the update")
    expires = (now + timedelta(seconds=expires_in)).date().isoformat()
    log(f"{SECRET_NAME} updated at {after}. New token expires about {expires}.")
    return "refreshed"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_const", dest="mode", const="check")
    g.add_argument("--refresh", action="store_const", dest="mode", const="refresh")
    args = ap.parse_args()
    try:
        run(args.mode, os.environ)
        return 0
    except RefreshError as e:
        print(f"ERROR: token renewal failed: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:  # from GraphAPI, already redacted
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
