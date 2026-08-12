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
import subprocess
from pathlib import Path

import httpx
from atproto import Client as BskyClient, client_utils, models
from atproto_client.request import Request
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

# Video guardrails. If a clip is longer/bigger than these (or Bluesky rejects
# it, e.g. daily video cap), we fall back to posting its thumbnail + text.
MAX_VIDEO_SECONDS = 180
MAX_VIDEO_BYTES = 50 * 1024 * 1024
PREFERRED_MAX_BITRATE = 2_800_000  # prefer ~720p to keep uploads fast/reliable
# Video blobs upload slowly; the default atproto timeout is too short for them.
BSKY_TIMEOUT = httpx.Timeout(180.0)

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


def persist_state(state: dict) -> None:
    """Save state to disk and, in CI (PERSIST_GIT=1), immediately commit + push.

    Pushing after every account that posts — rather than once at the end —
    means a run that is cancelled or that overlaps another can't cause the next
    run to repost: the newest state is already in the repo. The ``git pull``
    also pulls in any other run's/manual pushes so they converge.
    """
    save_state(state)
    if os.environ.get("PERSIST_GIT") != "1":
        return
    try:
        subprocess.run(["git", "add", "state.json"], cwd=ROOT, check=True)
        if subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=ROOT).returncode == 0:
            return  # nothing changed
        subprocess.run(["git", "commit", "-q", "-m", "Update state [skip ci]"], cwd=ROOT, check=True)
        subprocess.run(["git", "pull", "-q", "--rebase", "--autostash"], cwd=ROOT, check=False)
        subprocess.run(["git", "push", "-q"], cwd=ROOT, check=False)
    except Exception as exc:
        print(f"  state persist failed: {exc!r}")


def load_bsky_accounts() -> dict:
    """Return {club: {"handle": ..., "password": ...}}.

    Credentials are resolved from the environment in increasing priority:
      1. NUFC's original BSKY_HANDLE / BSKY_PASSWORD.
      2. A combined BSKY_ACCOUNTS JSON secret: {club: {handle, password}}.
      3. Per-club secrets BSKY_<CLUB> (e.g. BSKY_MANUTD) holding
         {"handle": ..., "password": ...} — these OVERRIDE the combined blob.

    Per-club secrets are the preferred way to add/fix a club, because a GitHub
    secret is write-only: editing the combined blob means re-entering every
    club, whereas a per-club secret only ever touches that one club.
    """
    accounts: dict = {}

    handle = os.environ.get("BSKY_HANDLE")
    password = os.environ.get("BSKY_PASSWORD")
    if handle and password:
        accounts["nufc"] = {"handle": handle, "password": password}

    raw = os.environ.get("BSKY_ACCOUNTS")
    if raw:
        accounts.update(json.loads(raw))

    for club in CLUBS:
        per_club = os.environ.get(f"BSKY_{club.upper()}")
        if per_club:
            accounts[club] = json.loads(per_club)

    return accounts


def clean(text: str) -> str:
    """Unescape HTML entities (X returns &amp; etc.) and trim."""
    return html.unescape(text or "").strip()


def tweet_text(tweet) -> str:
    """Full tweet text, preferring the long-form note text over the legacy
    280-char version. X Premium 'note tweets' truncate ``.text`` and end it with
    a t.co self-link; ``.full_text`` carries the complete content."""
    return getattr(tweet, "full_text", None) or getattr(tweet, "text", None) or ""


def _try_fetch(url: str, timeout: float = 20.0) -> bytes | None:
    """Download a URL to bytes; log and return None on failure."""
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        print(f"  media fetch failed ({url}): {exc!r}")
        return None


def media_source(tweet):
    """The tweet carrying the media.

    Prefers the retweeted tweet for retweets, and falls back to the quoted
    tweet when a quote tweet has no media of its own (on X the visible image
    is usually the quoted post's).
    """
    source = getattr(tweet, "retweeted_tweet", None) or tweet
    if not (getattr(source, "media", None) or []):
        quote = getattr(source, "quote", None)
        if quote is not None and (getattr(quote, "media", None) or []):
            return quote
    return source


def image_urls(tweet) -> list[str]:
    """Up to 4 photo URLs at 'medium' size (keeps blobs under Bluesky's limit)."""
    urls: list[str] = []
    for m in getattr(media_source(tweet), "media", None) or []:
        if type(m).__name__ != "Photo":
            continue
        url = getattr(m, "media_url", None)
        if not url:
            continue
        base = url.split("?")[0]
        head, _, ext = base.rpartition(".")
        urls.append(f"{head}?format={ext}&name=medium" if head else url)
    return urls[:4]


def fetch_images(tweet) -> list[bytes]:
    """Download a tweet's photos. Failures are skipped, not fatal."""
    return [b for b in (_try_fetch(u) for u in image_urls(tweet)) if b]


def pick_video_stream(media) -> str | None:
    """Choose an MP4 stream URL that should fit Bluesky's limits, or None.

    Prefers the highest bitrate at or below ~720p; if a clip is too long or the
    estimated size is too big, returns None so we fall back to the thumbnail.
    """
    duration = (getattr(media, "duration_millis", 0) or 0) / 1000
    if duration and duration > MAX_VIDEO_SECONDS:
        return None
    streams = sorted(
        (
            (getattr(s, "bitrate", 0) or 0, getattr(s, "url", None))
            for s in getattr(media, "streams", None) or []
            if getattr(s, "url", None) and getattr(s, "bitrate", None)
        )
    )  # ascending by bitrate
    if not streams:
        return None
    chosen = None
    for bitrate, url in streams:
        if bitrate <= PREFERRED_MAX_BITRATE:
            chosen = (bitrate, url)
    chosen = chosen or streams[0]  # else the smallest available
    bitrate, url = chosen
    if duration and (bitrate * duration / 8) > MAX_VIDEO_BYTES:
        return None
    return url


def fetch_media(tweet) -> dict:
    """Return {"images": [...], "video": bytes|None, "thumb": bytes|None}.

    A tweet has either photos or one video/GIF (X doesn't mix them). For video
    we download a size-capped MP4 plus the thumbnail (used as a fallback image
    if the video can't be sent).
    """
    for m in getattr(media_source(tweet), "media", None) or []:
        if type(m).__name__ in ("Video", "AnimatedGif"):
            thumb_url = getattr(m, "media_url", None)
            video_url = pick_video_stream(m)
            return {
                "images": [],
                "video": _try_fetch(video_url, timeout=90.0) if video_url else None,
                "thumb": _try_fetch(thumb_url) if thumb_url else None,
            }
    return {"images": fetch_images(tweet), "video": None, "thumb": None}


def send_media_post(bsky: BskyClient, body: str, media: dict, reply):
    """Send a post with its media, degrading gracefully: video -> thumbnail
    image -> text-only."""
    video = media.get("video")
    thumb = media.get("thumb")
    images = list(media.get("images") or [])

    if video:
        try:
            return bsky.send_video(build_richtext(body), video=video, reply_to=reply)
        except Exception as exc:
            print(f"  video attach failed, falling back to thumbnail: {exc!r}")
            images = [thumb] if thumb else []
    elif thumb and not images:
        images = [thumb]  # video existed but wasn't usable -> post its thumbnail

    if images:
        try:
            return bsky.send_images(build_richtext(body), images=images, reply_to=reply)
        except Exception as exc:
            print(f"  image attach failed, posting text only: {exc!r}")
    return bsky.send_post(build_richtext(body), reply_to=reply)


def quoted_tweet(tweet):
    """The tweet this one quotes, if any (checks the retweeted tweet too)."""
    source = getattr(tweet, "retweeted_tweet", None) or tweet
    return getattr(source, "quote", None)


def quoted_permalink(tweet) -> str | None:
    """Link to the quoted tweet, when its content wasn't returned by X."""
    source = getattr(tweet, "retweeted_tweet", None) or tweet
    legacy = getattr(source, "_data", None) or {}
    permalink = (legacy.get("legacy") or {}).get("quoted_status_permalink") or {}
    return permalink.get("expanded")


def compose(display_name: str, screen_name: str, tweet) -> str:
    """Build the full Bluesky post text: attribution header + tweet body.

    Quote tweets get the quoted tweet's author and text appended, since
    Bluesky can't embed an X post — without this the post loses the context it
    was replying to and reads as a non-sequitur.

    No truncation here — length is handled by splitting into a thread at post
    time (see :func:`split_into_chunks` / :func:`post_thread`).
    """
    retweeted = getattr(tweet, "retweeted_tweet", None)
    if retweeted is not None:
        body = f"RT @{retweeted.user.screen_name}: {clean(tweet_text(retweeted))}"
    else:
        body = clean(tweet_text(tweet))

    quote = quoted_tweet(tweet)
    if quote is not None:
        q_handle = getattr(getattr(quote, "user", None), "screen_name", None)
        q_text = clean(tweet_text(quote))
        if q_text or q_handle:
            attribution = f"@{q_handle}" if q_handle else "a post"
            body = f"{body}\n\n[Quoting {attribution}: {q_text}]"
    else:
        # X sometimes withholds the quoted tweet entirely (deleted, or from a
        # suspended/protected account). Fall back to its permalink so the post
        # still shows it was quoting something, rather than reading oddly.
        link = quoted_permalink(tweet)
        if link:
            body = f"{body}\n\n[Quoting: {link}]"

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


def post_thread(bsky: BskyClient, full_text: str, media: dict | None = None) -> int:
    """Post text to Bluesky, splitting into a reply thread if it's too long.

    Any media (video or images) is attached to the first post, degrading
    gracefully to a thumbnail and then to text-only. Returns the post count.
    """
    media = media or {}
    has_media = bool(media.get("video") or media.get("thumb") or media.get("images"))
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

        if i == 1 and has_media:
            response = send_media_post(bsky, body, media, reply)
        else:
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


async def process_account(x, bsky, club_state, profiles, label, screen_name) -> bool:
    """Post any new tweets for one account. Returns True if state changed
    (seeded or posted), so the caller can persist immediately."""
    seeding = screen_name not in club_state
    last_id = int(club_state.get(screen_name, 0))

    try:
        user_id, name = await resolve_profile(x, screen_name, profiles)
        fresh = await collect_new_tweets(x, user_id, last_id)
    except Exception as exc:  # keep going if one account fails
        print(f"[{label}] fetch failed: {exc!r}")
        return False

    if not fresh:
        print(f"[{label}] no new tweets")
        return False

    if seeding:
        newest = int(fresh[-1].id)
        club_state[screen_name] = str(newest)
        print(f"[{label}] seeded at {newest} (skipped {len(fresh)} existing)")
        return True

    to_post = fresh[:MAX_POSTS_PER_ACCOUNT_PER_RUN]
    posted_up_to = last_id
    for t in to_post:
        text = compose(name, screen_name, t)
        media = fetch_media(t)
        try:
            n = post_thread(bsky, text, media)
            posted_up_to = int(t.id)
            bits = []
            if n > 1:
                bits.append(f"{n}-post thread")
            if media.get("video"):
                bits.append("video")
            n_img = len(media.get("images") or [])
            if n_img:
                bits.append(f"{n_img} image{'s' if n_img > 1 else ''}")
            elif media.get("thumb") and not media.get("video"):
                bits.append("thumbnail")
            suffix = f" ({', '.join(bits)})" if bits else ""
            print(f"[{label}] posted {t.id}{suffix}")
        except Exception as exc:
            print(f"[{label}] post failed for {t.id}: {exc!r}")
            break  # stop so we retry this + later tweets next run

    changed = posted_up_to != last_id
    if changed:
        club_state[screen_name] = str(posted_up_to)
    if len(fresh) > len(to_post):
        print(f"[{label}] {len(fresh) - len(to_post)} more queued for next run")
    return changed


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
            bsky = BskyClient(request=Request(timeout=BSKY_TIMEOUT))
            bsky.login(creds["handle"], creds["password"])
        except Exception as exc:
            print(f"[{club}] Bluesky login failed: {exc!r}")
            continue

        club_state = state.setdefault(club, {})
        for screen_name in screen_names:
            changed = await process_account(
                x, bsky, club_state, profiles, f"{club}/{screen_name}", screen_name
            )
            if changed:
                persist_state(state)  # push immediately so overlaps can't repost

    persist_state(state)


if __name__ == "__main__":
    asyncio.run(run())
