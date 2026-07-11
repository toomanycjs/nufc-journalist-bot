# NUFC journalist bot

Mirrors new tweets from a set of Newcastle United journalists on X to a Bluesky
account, for free, on a schedule.

- **Scrape:** [twikit](https://github.com/d60/twikit) reads each journalist's
  timeline using a browser cookie session from a throwaway X account.
- **Post:** the [atproto](https://github.com/MarshalX/atproto) client posts to
  Bluesky over the AT Protocol.
- **Run:** GitHub Actions on a 15-minute cron. No server needed.

Accounts mirrored are listed in [`accounts.py`](accounts.py).

## How it decides what to post

- Skips **replies**; keeps original tweets, retweets (shown as `RT @user:`),
  and quotes.
- Posts as `Display Name (@handle)` followed by the tweet text. No link back to X.
- Tracks the last tweet posted per account in `state.json`, so nothing is
  double-posted. The **first** time it sees an account it seeds silently (records
  the newest tweet, posts nothing) so it doesn't dump backfill.

## Files

| File | Purpose |
|------|---------|
| `bot.py` | Main loop: scrape → filter → post → save state |
| `twikit_patch.py` | Runtime fixes for current twikit breakage (see below) |
| `make_cookies.py` | Builds `cookies.json` from browser cookie values in `.env` |
| `test_fetch.py` | Read-only smoke test (prints latest tweets) |
| `accounts.py` | The list of X accounts to mirror |
| `.github/workflows/bot.yml` | GitHub Actions cron |

## ⚠️ Read this first — the honest caveats

- **Free X scraping is fragile.** twikit breaks whenever X changes its internals.
  As of this writing twikit 2.3.3 is broken two ways, both worked around in
  `twikit_patch.py`: the `Couldn't get KEY_BYTE indices` transaction bug
  ([issue #408](https://github.com/d60/twikit/issues/408)) and a `User` parser
  `KeyError`. If posts stop, the fix is usually updating twikit and/or adjusting
  `twikit_patch.py`. **Delete `twikit_patch.py` and its imports once a fixed
  twikit release lands.**
- **Login is done with browser cookies, not a password.** X's Cloudflare blocks
  twikit's automated login from this network, so instead you log into the
  throwaway account in a real browser and copy its `auth_token` + `ct0` cookies.
  These expire eventually (weeks–months, or if you log out of that browser
  session); when they do, redo *Setup step 2–3* to refresh them.
- **Datacenter-IP risk on GitHub Actions.** Everything is proven working from a
  home connection. GitHub Actions runs from a datacenter IP that X trusts less —
  the cookie session may still work, but if the Actions logs show Cloudflare 403s
  or empty fetches, use the **local fallback** below instead.
- **Use a throwaway X account, never your personal one.** The cookies grant full
  access to whatever account is logged in.

## Setup

### 1. Create the two accounts

- A **throwaway X account** (new email, handle, password).
- The **Bluesky account** that posts. In Bluesky: *Settings → Privacy and
  security → App Passwords* → create one and use that **app password**.

### 2. Get the X cookies (locally, in your browser)

1. Log into the throwaway X account at https://x.com in Chrome/Edge.
2. F12 → **Application** tab → **Cookies** → `https://x.com`.
3. Copy the **Value** of `auth_token` and `ct0`.

### 3. Build the cookie file and test (locally)

Requires Python 3.10+.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env      # then edit .env: paste X_AUTH_TOKEN, X_CT0, BSKY_*
.venv/Scripts/python make_cookies.py   # writes cookies.json
.venv/Scripts/python test_fetch.py     # should print recent tweets
```

`cookies.json` is gitignored and will not be committed. Copy its full contents
for the next step.

### 4. Put the code on GitHub

Create a **private** repo, then:

```bash
git init
git add .
git commit -m "NUFC journalist bot"
git branch -M main
git remote add origin https://github.com/<you>/nufc-journalist-bot.git
git push -u origin main
```

### 5. Add GitHub secrets

Repo → *Settings → Secrets and variables → Actions → New repository secret*:

| Secret name       | Value                                              |
|-------------------|----------------------------------------------------|
| `X_COOKIES_JSON`  | the full contents of `cookies.json`                |
| `BSKY_HANDLE`     | e.g. `nufcjournalistbot.bsky.social`               |
| `BSKY_PASSWORD`   | the Bluesky **app password**                       |

### 6. Turn it on

Repo → *Actions* → enable workflows → **NUFC journalist bot** → **Run workflow**.
The first cloud run **seeds** (posts nothing); after that it posts new tweets
every 15 minutes.

## Local fallback (Windows Task Scheduler)

If GitHub Actions gets IP-blocked, run it from this machine instead (the IP that
already works). Create a task that runs every 15 minutes:

- Program: `C:\Users\Craig\Documents\nufc-journalist-bot\.venv\Scripts\python.exe`
- Arguments: `bot.py`
- Start in: `C:\Users\Craig\Documents\nufc-journalist-bot`

It reads `.env` and `cookies.json` locally — no GitHub secrets needed.

## Tuning

Constants at the top of [`bot.py`](bot.py): `MAX_POSTS_PER_ACCOUNT_PER_RUN`,
`FETCH_COUNT`. Cron frequency is in
[`.github/workflows/bot.yml`](.github/workflows/bot.yml).
