# AICounter Reddit Comment Monitor

A personal script that watches one Reddit post for new comments and posts
replies to them. It exists so the author of the post can answer
commenters without keeping a browser tab open.

- Post monitored: https://www.reddit.com/r/iOSAppsMarketing/comments/1xydkmc/
  (announcement post for [AICounter](https://www.connectionscomputer.com/aicounter/count-pipes/),
  an iPhone app that counts pipe ends from a photo)
- It only ever touches that one post. It does not vote, does not create
  submissions, does not send direct messages, and does not collect data
  about other users.
- Reply text is drafted by the operator (the account owner, or Muse
  acting for them) and passed in with `--text`. The script only handles
  API plumbing, de-duplication, and guardrails.
- Deployment waits for Reddit's approval of the Data API access request
  (Responsible Builder Policy); the script has never run against the
  live API yet.

## Setup

1. Create a Reddit app at https://www.reddit.com/prefs/apps
   (type **web app**). Note its client ID and secret.
2. Get a permanent refresh token with the authorization-code flow
   (`duration=permanent`, scopes `read submit identity`):
   - Open in a browser (replace `CLIENT_ID`):
     `https://www.reddit.com/api/v1/authorize?client_id=CLIENT_ID&response_type=code&state=x&redirect_uri=http://localhost:8080&duration=permanent&scope=read%20submit%20identity`
   - Approve, then copy the `code` from the redirect URL.
   - Exchange it (within ~10 minutes):
     `curl -X POST https://www.reddit.com/api/v1/access_token -u CLIENT_ID:CLIENT_SECRET -d grant_type=authorization_code -d code=CODE -d redirect_uri=http://localhost:8080`
   - The response contains `refresh_token`. Keep it secret.
3. Set the environment:

```sh
export REDDIT_CLIENT_ID=...
export REDDIT_CLIENT_SECRET=...
export REDDIT_REFRESH_TOKEN=...
export REDDIT_USERNAME=Such_Huckleberry_565
```

## Usage

`--state` takes an absolute path and goes after the subcommand:

```sh
python reddit_monitor.py check
python reddit_monitor.py fetch --state /abs/path/seen_comments.json
python reddit_monitor.py reply --comment-id abc123 --text "..." --state /abs/path/seen_comments.json
python reddit_monitor.py reply --comment-id abc123 --text "..." --state /abs/path/seen_comments.json --dry-run
python reddit_monitor.py mark --comment-id abc123 --status skipped:spam --state /abs/path/seen_comments.json
```

State lives in the seen-list JSON file (one reply per comment, ever).
Run it hourly, e.g. from cron with `flock -n` so runs never overlap,
and keep the credentials in a restricted file the cron job reads. It
uses a handful of API calls per check, far below Reddit's free tier
of 100 queries per minute.

## Reconciling a pending record

`reply` writes `pending` to the seen-list before posting, so a crash
or timeout can never silently produce a duplicate reply. If a comment
stays `pending`, check the post on Reddit: if the reply is visible,
edit the seen-list and set that comment to `posted` with its reply
ID; if it is not, delete the `pending` entry and retry.

## Guardrails

- One reply per comment, enforced by the seen-list (plus the pending
  record and a file lock around the whole read-check-post
  transaction).
- Never replies to the operator's own comments or to AutoModerator.
- Refuses reply text containing banned marketing claims
  (accuracy guarantees, audit/inventory language, "save hours", and
  similar). Accuracy is never guaranteed; when a reply discusses
  accuracy, it says markers can be checked, added, or removed
  before use.
- A corrupt or unreadable state file fails closed instead of
  starting from an empty seen-list. A collapsed ("more") comment
  tree fails the fetch loudly instead of silently missing comments.
- Python 3.10+ on Linux, standard library only, no dependencies.
