"""NUFC journalist bot.

Reads the latest tweets from a set of X accounts (via twikit, using a cached
login session) and mirrors new, non-reply tweets to Bluesky (via the atproto
AT Protocol client).

State (the last tweet id posted per account) lives in state.json so we never
double-post. On an account's very first run we *seed* — record the current
newest tweet and post nothing — so we don't flood Bluesky with backfill.
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

from accounts import ACCOUNTS

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
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


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


async def collect_new_tweets(x: XClient, screen_name: str, last_id: int):
    """Return (user, list of new non-reply tweets sorted oldest-first)."""
    user = await x.get_user_by_screen_name(screen_name)
    tweets = await user.get_tweets("Tweets", count=FETCH_COUNT)

    fresh = []
    for t in tweets:
        if int(t.id) <= last_id:
            continue
        if getattr(t, "in_reply_to", None):  # skip replies
            continue
        fresh.append(t)

    fresh.sort(key=lambda t: int(t.id))  # oldest first
    return user, fresh


async def run() -> None:
    state = load_state()

    x = XClient("en-US")
    x.load_cookies(str(COOKIES_FILE))

    bsky = BskyClient()
    bsky.login(os.environ["BSKY_HANDLE"], os.environ["BSKY_PASSWORD"])

    for screen_name in ACCOUNTS:
        seeding = screen_name not in state
        last_id = int(state.get(screen_name, 0))

        try:
            user, fresh = await collect_new_tweets(x, screen_name, last_id)
        except Exception as exc:  # keep going if one account fails
            print(f"[{screen_name}] fetch failed: {exc!r}")
            continue

        if not fresh:
            print(f"[{screen_name}] no new tweets")
            continue

        if seeding:
            newest = int(fresh[-1].id)
            state[screen_name] = str(newest)
            print(f"[{screen_name}] seeded at {newest} (skipped {len(fresh)} existing)")
            continue

        to_post = fresh[:MAX_POSTS_PER_ACCOUNT_PER_RUN]
        posted_up_to = last_id
        for t in to_post:
            text = compose(user.name, screen_name, t)
            try:
                n = post_thread(bsky, text)
                posted_up_to = int(t.id)
                suffix = f" ({n}-post thread)" if n > 1 else ""
                print(f"[{screen_name}] posted {t.id}{suffix}")
            except Exception as exc:
                print(f"[{screen_name}] post failed for {t.id}: {exc!r}")
                break  # stop so we retry this + later tweets next run

        state[screen_name] = str(posted_up_to)
        if len(fresh) > len(to_post):
            print(f"[{screen_name}] {len(fresh) - len(to_post)} more queued for next run")

    save_state(state)


if __name__ == "__main__":
    asyncio.run(run())
