"""Football journalist bot.

Reads the latest tweets from sets of X accounts (via twikit, using a cached
cookie session) and mirrors new, non-reply tweets to per-club Bluesky accounts
(via the atproto AT Protocol client).

Which journalists map to which club live in clubs.py; the Bluesky credentials
per club come from the environment (see load_bsky_accounts).

State lives in state.json, namespaced per club, so we never double-post. On an
account's very first run we *seed* — record the current newest tweet and post
nothing — so we don't flood a fresh Bluesky account with backfill. A shared
"_profiles" cache stores each screen name's numeric id + display name so we only
resolve a profile once (halving X requests on later runs).
"""

import asyncio
import html
import json
import os
import re
from pathlib import Path

from atproto import Client as BskyClient, client_utils, models
from dotenv import load_dotenv
from twikit import Client as XClient

import twikit_patch  # noqa: F401  # fixes twikit issue #408; remove when fixed upstream

from clubs import CLUBS

load_dotenv()

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
COOKIES_FILE = ROOT / "cookies.json"

# Bluesky post limit is 300 graphemes. Tweets longer than this are split across
# a reply thread rather than truncated.
BSKY_LIMIT = 300
# Room reserved for the " (i/n)" counter suffix added to each threaded post.
COUNTER_RESERVE = 9
# How many tweets to fetch per account each run.
FETCH_COUNT = 20
# Safety cap so a long outage can't dump dozens of posts in one burst.
# Overflow is NOT lost — state only advances to the last tweet we actually
# posted, so the rest are picked up on the next run.
MAX_POSTS_PER_ACCOUNT_PER_RUN = 8

URL_RE = re.compile(r"https?://\S+")


def load_state() -> dict:
    """Load state.json, migrating the old flat NUFC layout if present.

    Old layout was ``{screen_name: last_id}`` (all string values). New layout is
    ``{"_profiles": {...}, "<club>": {screen_name: last_id}, ...}``.
    """
    if not STATE_FILE.exists():
        return {}
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if data and all(isinstance(v, str) for v in data.values()):
        data = {"nufc": data}  # migrate legacy flat state
    return data


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def load_bsky_accounts() -> dict:
    """Return {club: {"handle": ..., "password": ...}}.

    NUFC keeps its original BSKY_HANDLE/BSKY_PASSWORD env vars; all other clubs
    come from the BSKY_ACCOUNTS JSON secret (which may also override nufc).
    """
    accounts: dict = {}
    handle = os.environ.get("BSKY_HANDLE")
    password = os.environ.get("BSKY_PASSWORD")
    if handle and password:
        accounts["nufc"] = {"handle": handle, "password": password}
    raw = os.environ.get("BSKY_ACCOUNTS")
    if raw:
        accounts.update(json.loads(raw))
    return accounts


def clean(text: str) -> str:
    """Unescape HTML entities (X returns &amp; etc.) and trim."""
    return html.unescape(text or "").strip()


def tweet_text(tweet) -> str:
    """Full tweet text, preferring the long-form note text over the legacy
    280-char version. X Premium 'note tweets' truncate ``.text`` and end it with
    a t.co self-link; ``.full_text`` carries the complete content."""
    return getattr(tweet, "full_text", None) or getattr(tweet, "text", None) or ""


def compose(display_name: str, screen_name: str, tweet) -> str:
    """Build the full Bluesky post text: attribution header + tweet body.

    No truncation here — length is handled by splitting into a thread at post
    time (see :func:`split_into_chunks` / :func:`post_thread`).
    """
    retweeted = getattr(tweet, "retweeted_tweet", None)
    if retweeted is not None:
        body = f"RT @{retweeted.user.screen_name}: {clean(tweet_text(retweeted))}"
    else:
        body = clean(tweet_text(tweet))

    return f"{display_name} (@{screen_name})\n\n{body}"


def split_into_chunks(text: str, limit: int) -> list[str]:
    """Split text into <= limit pieces, preferring word (whitespace) boundaries.

    Uses len() (code points) as a conservative proxy for Bluesky's grapheme
    count — multi-codepoint emoji count as more here, so we only ever split
    earlier than strictly necessary, never past the real 300-grapheme cap.
    """
    chunks: list[str] = []
    current = ""
    for word in text.split(" "):
        while len(word) > limit:  # a single token longer than a whole post
            if current:
                chunks.append(current)
                current = ""
            chunks.append(word[:limit])
            word = word[limit:]
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks


def build_richtext(text: str):
    """Turn any URLs in the text into clickable Bluesky link facets."""
    builder = client_utils.TextBuilder()
    pos = 0
    for match in URL_RE.finditer(text):
        if match.start() > pos:
            builder.text(text[pos:match.start()])
        url = match.group()
        builder.link(url, url)
        pos = match.end()
    if pos < len(text):
        builder.text(text[pos:])
    return builder


def post_thread(bsky: BskyClient, full_text: str) -> int:
    """Post text to Bluesky, splitting into a reply thread if it's too long.

    Returns the number of posts created.
    """
    if len(full_text) <= BSKY_LIMIT:
        chunks = [full_text]
    else:
        chunks = split_into_chunks(full_text, BSKY_LIMIT - COUNTER_RESERVE)

    total = len(chunks)
    root = parent = None
    for i, chunk in enumerate(chunks, 1):
        body = chunk if total == 1 else f"{chunk} ({i}/{total})"
        reply = None
        if parent is not None:
            reply = models.AppBskyFeedPost.ReplyRef(parent=parent, root=root)
        response = bsky.send_post(build_richtext(body), reply_to=reply)
        ref = models.create_strong_ref(response)
        root = root or ref
        parent = ref
    return total


async def resolve_profile(x: XClient, screen_name: str, profiles: dict):
    """Return (user_id, display_name), resolving via X and caching on first use."""
    cached = profiles.get(screen_name)
    if cached and cached.get("id"):
        return cached["id"], cached.get("name") or screen_name
    user = await x.get_user_by_screen_name(screen_name)
    profiles[screen_name] = {"id": user.id, "name": user.name}
    return user.id, user.name


async def collect_new_tweets(x: XClient, user_id: str, last_id: int):
    """Return new non-reply tweets (oldest-first) for a user id."""
    tweets = await x.get_user_tweets(user_id, "Tweets", count=FETCH_COUNT)
    fresh = []
    for t in tweets:
        if int(t.id) <= last_id:
            continue
        if getattr(t, "in_reply_to", None):  # skip replies
            continue
        fresh.append(t)
    fresh.sort(key=lambda t: int(t.id))  # oldest first
    return fresh


async def process_account(x, bsky, club_state, profiles, label, screen_name) -> None:
    seeding = screen_name not in club_state
    last_id = int(club_state.get(screen_name, 0))

    try:
        user_id, name = await resolve_profile(x, screen_name, profiles)
        fresh = await collect_new_tweets(x, user_id, last_id)
    except Exception as exc:  # keep going if one account fails
        print(f"[{label}] fetch failed: {exc!r}")
        return

    if not fresh:
        print(f"[{label}] no new tweets")
        return

    if seeding:
        newest = int(fresh[-1].id)
        club_state[screen_name] = str(newest)
        print(f"[{label}] seeded at {newest} (skipped {len(fresh)} existing)")
        return

    to_post = fresh[:MAX_POSTS_PER_ACCOUNT_PER_RUN]
    posted_up_to = last_id
    for t in to_post:
        text = compose(name, screen_name, t)
        try:
            n = post_thread(bsky, text)
            posted_up_to = int(t.id)
            suffix = f" ({n}-post thread)" if n > 1 else ""
            print(f"[{label}] posted {t.id}{suffix}")
        except Exception as exc:
            print(f"[{label}] post failed for {t.id}: {exc!r}")
            break  # stop so we retry this + later tweets next run

    club_state[screen_name] = str(posted_up_to)
    if len(fresh) > len(to_post):
        print(f"[{label}] {len(fresh) - len(to_post)} more queued for next run")


async def run() -> None:
    state = load_state()
    profiles = state.setdefault("_profiles", {})

    x = XClient("en-US")
    x.load_cookies(str(COOKIES_FILE))

    bsky_accounts = load_bsky_accounts()

    for club, screen_names in CLUBS.items():
        creds = bsky_accounts.get(club)
        if not creds or not screen_names:
            continue  # club not configured yet — skip

        try:
            bsky = BskyClient()
            bsky.login(creds["handle"], creds["password"])
        except Exception as exc:
            print(f"[{club}] Bluesky login failed: {exc!r}")
            continue

        club_state = state.setdefault(club, {})
        for screen_name in screen_names:
            await process_account(
                x, bsky, club_state, profiles, f"{club}/{screen_name}", screen_name
            )

    save_state(state)


if __name__ == "__main__":
    asyncio.run(run())
