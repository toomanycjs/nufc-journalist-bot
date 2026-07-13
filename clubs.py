"""Per-club config: which X journalists feed which Bluesky account.

Each key is a club; its value is the list of X screen names (without the @) to
mirror to that club's Bluesky account.

Bluesky credentials per club are resolved from the environment (see
``load_bsky_accounts`` in bot.py):
  - ``nufc`` uses the ``BSKY_HANDLE`` / ``BSKY_PASSWORD`` env vars/secrets.
  - every other club comes from the ``BSKY_ACCOUNTS`` JSON secret, keyed by the
    same club name used here.

A club is skipped when its journalist list is empty OR it has no Bluesky
credentials configured — so you can add journalists and accounts incrementally
without breaking the clubs that are already live.
"""

CLUBS = {
    "nufc": [
        "SkySports_Keith",   # Keith Downie (Sky Sports)
        "CraigHope_DM",      # Craig Hope (Daily Mail)
        "MsiDouglas",        # Mark Douglas
        "LukeEdwardsTele",   # Luke Edwards (Telegraph)
        "lee_ryder",         # Lee Ryder (Newcastle Chronicle)
        "JoelBlandSport",    # Joel Bland
    ],
    "chelsea": [],
    "manutd": [],
    "mancity": [],
    "liverpool": [],
    "arsenal": [],
    "spurs": [],
}
