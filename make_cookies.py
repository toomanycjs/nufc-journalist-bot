"""Build cookies.json for twikit from a real browser session.

This bypasses twikit's automated login (which X blocks via Cloudflare). Instead
you log into the throwaway X account in your normal browser, then copy two
cookie values into .env:

    X_AUTH_TOKEN   (cookie name: auth_token)
    X_CT0          (cookie name: ct0)

To find them: log into x.com in Chrome/Edge -> F12 (DevTools) -> Application tab
-> Cookies -> https://x.com -> copy the Value of `auth_token` and `ct0`.

Then run:  python make_cookies.py
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

auth_token = os.environ["X_AUTH_TOKEN"].strip()
ct0 = os.environ["X_CT0"].strip()

if not auth_token or not ct0:
    raise SystemExit("X_AUTH_TOKEN and X_CT0 must both be set in .env")

cookies = {"auth_token": auth_token, "ct0": ct0}
Path(__file__).with_name("cookies.json").write_text(
    json.dumps(cookies), encoding="utf-8"
)
print("Wrote cookies.json (auth_token + ct0).")
print("Next: paste the full contents of cookies.json into GitHub secret X_COOKIES_JSON.")
