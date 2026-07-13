"""Quick read-only smoke test: can we fetch tweets with the saved cookies?"""

import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")  # Windows console prints emoji safely

from twikit import Client

import twikit_patch  # noqa: F401  # fixes twikit issue #408

from clubs import CLUBS


async def main() -> None:
    client = Client("en-US")
    client.load_cookies("cookies.json")

    screen_name = CLUBS["nufc"][0]
    user = await client.get_user_by_screen_name(screen_name)
    print(f"OK: {user.name} (@{user.screen_name}) — {user.followers_count} followers")

    tweets = await user.get_tweets("Tweets", count=3)
    for t in tweets:
        text = (t.text or "").replace("\n", " ")
        print(f"  {t.id}  {text[:80]}")


if __name__ == "__main__":
    asyncio.run(main())
