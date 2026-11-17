#!/usr/bin/env python3
"""AICounter Reddit comment monitor -- personal use.

Watches one Reddit post for new comments and posts replies to them.

Design: this script is only the API plumbing. Reply text is drafted by
the operator (a human, or Muse acting for the account owner) and passed
in with --text. The script fetches comments, keeps the seen-list, enforces
the guardrails below, and posts.

Authentication: Reddit OAuth2, authorization-code flow with a permanent
refresh token. Credentials come from the environment:

    REDDIT_CLIENT_ID       client ID of the Reddit app
    REDDIT_CLIENT_SECRET   client secret of the Reddit app
    REDDIT_REFRESH_TOKEN   permanent refresh token (duration=permanent)
    REDDIT_USERNAME        e.g. Such_Huckleberry_565 (User-Agent + identity check)

Commands (--state goes after the subcommand; use an absolute path):
    python reddit_monitor.py check
        Verify the token and print the authenticated username.
    python reddit_monitor.py fetch --state /abs/path/seen_comments.json
        Print new (not yet seen) comments as JSON.
    python reddit_monitor.py reply --comment-id ID --text "..." --state /abs/path/seen_comments.json [--dry-run]
        Post one reply. Refuses banned claims, replies to the operator's
        own comments or AutoModerator, and second replies to the same
        comment. Marks the comment seen as posted.
    python reddit_monitor.py mark --comment-id ID --status REASON --state /abs/path/seen_comments.json
        Record a comment as seen without replying, e.g. skipped:spam,
        skipped:own-comment, skipped:automoderator.

Guardrails:
  - One reply per comment, ever (enforced by the seen-list, with a
    pending record written before the POST and a file lock around the
    whole read -> check -> post transaction).
  - Never replies to the operator's own comments or to AutoModerator.
  - Refuses reply text containing banned marketing claims (see
    BANNED_PHRASES). Accuracy is never guaranteed; when a reply
    discusses accuracy, the operator states that markers can be
    checked, added, or removed before use. The script enforces the
    ban; the operator owns the wording -- a canned disclaimer in
    every reply would read as automated, which the reply style
    forbids.
  - A corrupt or unreadable state file fails closed: the script
    refuses to run rather than starting from an empty seen-list.
  - A "more" node in the comment tree fails the fetch loudly instead
    of silently missing comments.
  - Only ever touches POST_ID in SUBREDDIT. Nothing else is read or
    written. No voting, no submissions, no direct messages.

Rate limits: Reddit's free tier allows 100 queries per minute per OAuth
client ID. An hourly check uses a handful of calls.

Requires Python 3.10+ on Linux; standard library only.
"""

import argparse
import base64
import contextlib
import fcntl
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SUBREDDIT = "iOSAppsMarketing"
POST_ID = "1xydkmc"
POST_URL = "https://www.reddit.com/r/iOSAppsMarketing/comments/1xydkmc/"
REQUIRED_SCOPES = {"read", "submit", "identity"}
COMMENT_ID_RE = re.compile(r"[a-z0-9]+")

# Reply text must never contain these (case-insensitive substring match).
BANNED_PHRASES = [
    "guaranteed accurate",
    "100% accurate",
    "eliminate counting errors",
    "audit record",
    "inventory verification",
    "replace manual inspections",
    "save hours",
    "trusted by factories worldwide",
    "certified",
    "validated",
]

SELF_REPLY_REFUSAL = "refusing to reply to our own comment or AutoModerator"


def _env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def _user_agent():
    username = os.environ.get("REDDIT_USERNAME", "Such_Huckleberry_565").strip()
    return f"linux:aicounter-reddit-monitor:1.0 (by /u/{username})"


def _http(method, url, headers, data=None):
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        if e.code == 429:
            delay = (e.headers.get("Retry-After")
                     or e.headers.get("X-Ratelimit-Reset") or "unknown")
            raise RuntimeError(
                f"Reddit rate limited (429); retry after {delay} seconds; {detail}"
            ) from e
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"{method} {url} network failure: {e}") from e


def get_access_token():
    client_id = _env("REDDIT_CLIENT_ID")
    client_secret = _env("REDDIT_CLIENT_SECRET")
    refresh_token = _env("REDDIT_REFRESH_TOKEN")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    status, text = _http(
        "POST",
        TOKEN_URL,
        {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _user_agent(),
        },
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    payload = json.loads(text)
    if payload.get("token_type", "").lower() != "bearer":
        raise RuntimeError(f"unexpected token_type: {payload.get('token_type')!r}")
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"token exchange failed (HTTP {status}): {text[:300]}")
    granted = set((payload.get("scope") or "").split())
    if granted:
        missing = REQUIRED_SCOPES - granted
        if missing:
            raise RuntimeError(f"token is missing required scopes: {sorted(missing)}")
    return token


def api_get(token, path, params=None):
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return json.loads(
        _http(
            "GET",
            url,
            {"Authorization": f"Bearer {token}", "User-Agent": _user_agent()},
        )[1]
    )


def api_post(token, path, data):
    return json.loads(
        _http(
            "POST",
            API_BASE + path,
            {"Authorization": f"Bearer {token}", "User-Agent": _user_agent()},
            data,
        )[1]
    )


@contextlib.contextmanager
def _state_lock(state_path):
    """Exclusive lock for the read -> check -> post transaction."""
    lock_path = state_path + ".lock"
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _validate_comment_id(value):
    if not COMMENT_ID_RE.fullmatch(value or ""):
        raise ValueError(f"invalid Reddit comment ID: {value!r}")


def _flatten(children, out):
    for child in children or []:
        kind = child.get("kind")
        data = child.get("data") or {}
        if kind == "t1":
            author = data.get("author")
            body = data.get("body")
            if not isinstance(author, str) or not isinstance(body, str):
                continue
            if author.casefold() == "[deleted]":
                continue
            if body.strip() in ("", "[deleted]", "[removed]"):
                continue
            out.append(
                {
                    "id": data.get("id"),
                    "author": author,
                    "body": body,
                    "permalink": "https://www.reddit.com" + (data.get("permalink") or ""),
                    "created_utc": data.get("created_utc"),
                }
            )
            replies = data.get("replies")
            if isinstance(replies, dict):
                _flatten((replies.get("data") or {}).get("children"), out)
        elif kind == "more":
            raise RuntimeError(
                "incomplete comment tree: 'more' node encountered; implement "
                "/api/morechildren before treating this fetch as complete"
            )


def fetch_all_comments(token):
    """Return every comment currently visible on the monitored post."""
    listings = api_get(
        token, f"/r/{SUBREDDIT}/comments/{POST_ID}", {"limit": 100, "sort": "new"}
    )
    if not isinstance(listings, list) or len(listings) < 2:
        raise RuntimeError(f"unexpected comments response: {str(listings)[:300]}")
    comments = []
    _flatten((listings[1].get("data") or {}).get("children"), comments)
    return [c for c in comments if c.get("id")]


def load_seen(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise RuntimeError(
            f"cannot read state file {path}: {e}; refusing to continue"
        ) from e
    if not isinstance(data, dict):
        raise RuntimeError(f"state file {path} must contain a JSON object")
    return data


def save_seen(path, seen):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _check_text(text):
    if not text or not text.strip():
        raise ValueError("reply text is empty")
    if len(text) > 10000:
        raise ValueError("reply text exceeds Reddit's 10,000 character limit")
    lowered = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            raise ValueError(f"reply text contains banned phrase: {phrase!r}")


def _verify_identity(token):
    """Return the authenticated username, refusing a mismatch."""
    own = _env("REDDIT_USERNAME")
    me = api_get(token, "/api/v1/me")
    actual = me.get("name")
    if not isinstance(actual, str) or actual.casefold() != own.casefold():
        raise RuntimeError(
            "authenticated Reddit account does not match REDDIT_USERNAME"
        )
    return actual


def _validate_post_result(result):
    """Return the created comment's ID, or raise if Reddit did not confirm."""
    if not isinstance(result, dict):
        raise RuntimeError("unexpected reply response: not a JSON object")
    payload = result.get("json")
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected reply response: missing 'json' object")
    errors = payload.get("errors") or []
    if errors:
        raise RuntimeError(f"Reddit rejected the reply: {errors}")
    things = (payload.get("data") or {}).get("things")
    if not isinstance(things, list):
        raise RuntimeError("Reddit did not confirm creation of a comment")
    created = [
        t["data"]["id"]
        for t in things
        if isinstance(t, dict)
        and t.get("kind") == "t1"
        and isinstance(t.get("data"), dict)
        and t["data"].get("id")
    ]
    if not created:
        raise RuntimeError("Reddit did not confirm creation of a comment")
    return created[0]


def cmd_check(args):
    token = get_access_token()
    actual = _verify_identity(token)
    print(f"authenticated as u/{actual}")
    return 0


def cmd_fetch(args):
    token = get_access_token()
    seen = load_seen(args.state)
    new = [c for c in fetch_all_comments(token) if c["id"] not in seen]
    print(json.dumps(new, indent=2, ensure_ascii=False))
    return 0


def cmd_reply(args):
    _validate_comment_id(args.comment_id)
    _check_text(args.text)
    token = get_access_token()
    own = _verify_identity(token)
    with _state_lock(args.state):
        seen = load_seen(args.state)
        comment_id = args.comment_id
        if comment_id in seen:
            print(
                f"already handled {comment_id}: {seen[comment_id].get('status')}",
                file=sys.stderr,
            )
            return 2
        comments = {c["id"]: c for c in fetch_all_comments(token)}
        comment = comments.get(comment_id)
        if comment is None:
            print(f"comment {comment_id} not found on the monitored post",
                  file=sys.stderr)
            return 2
        author = comment.get("author")
        if not isinstance(author, str) or author.casefold() in (
            own.casefold(),
            "automoderator",
            "[deleted]",
        ):
            print(f"{SELF_REPLY_REFUSAL} (author u/{author})", file=sys.stderr)
            return 2
        if args.dry_run:
            print(f"dry run: would reply to {comment_id} by u/{author}")
            return 0
        # Record "pending" before the POST so a crash or timeout can
        # never silently produce a duplicate reply. A pending record
        # needs manual reconciliation (see README).
        seen[comment_id] = {"status": "pending", "at": int(time.time())}
        save_seen(args.state, seen)
        result = api_post(
            token,
            "/api/comment",
            {"thing_id": f"t1_{comment_id}", "text": args.text, "api_type": "json"},
        )
        reply_id = _validate_post_result(result)
        seen[comment_id] = {
            "status": "posted",
            "reply_id": reply_id,
            "at": int(time.time()),
        }
        save_seen(args.state, seen)
    print(f"replied to {comment_id} (reply id {reply_id})")
    return 0


def cmd_mark(args):
    _validate_comment_id(args.comment_id)
    with _state_lock(args.state):
        seen = load_seen(args.state)
        if args.comment_id in seen:
            raise ValueError(
                f"comment {args.comment_id} already has a state record: "
                f"{seen[args.comment_id].get('status')}"
            )
        seen[args.comment_id] = {"status": args.status, "at": int(time.time())}
        save_seen(args.state, seen)
    print(f"marked {args.comment_id} as {args.status}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="AICounter Reddit comment monitor")
    parser.add_argument("--state", default="seen_comments.json",
                        help="path to the seen-list JSON file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="verify credentials and print the username")

    p_fetch = sub.add_parser("fetch", help="print new (unseen) comments as JSON")
    p_fetch.add_argument("--state", default=argparse.SUPPRESS)

    p_reply = sub.add_parser("reply", help="post one reply to a comment")
    p_reply.add_argument("--comment-id", required=True)
    p_reply.add_argument("--text", required=True)
    p_reply.add_argument("--dry-run", action="store_true")
    p_reply.add_argument("--state", default=argparse.SUPPRESS)

    p_mark = sub.add_parser("mark", help="record a comment as seen without replying")
    p_mark.add_argument("--comment-id", required=True)
    p_mark.add_argument("--status", required=True,
                        help="e.g. skipped:spam, skipped:own-comment")
    p_mark.add_argument("--state", default=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return cmd_check(args)
        if args.command == "fetch":
            return cmd_fetch(args)
        if args.command == "reply":
            return cmd_reply(args)
        if args.command == "mark":
            return cmd_mark(args)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON response: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
