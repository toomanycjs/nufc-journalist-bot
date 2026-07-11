"""Runtime patch for twikit's X client-transaction breakage.

Since ~2026-03-18, X changed its webpack manifest / ondemand.s.js structure, so
twikit 2.3.3's regexes in ``x_client_transaction/transaction.py`` no longer
match and every request dies with ``Couldn't get KEY_BYTE indices``
(see twikit issue #408).

This reapplies the fix from twikit PRs #410 / #411 at runtime, so we don't have
to edit site-packages — it works identically locally and on GitHub Actions
(which does a fresh ``pip install`` of the still-broken release).

Import this module once, before creating a twikit ``Client``. Delete it and the
corresponding imports once a fixed twikit release is available.
"""

import re

from twikit.x_client_transaction import transaction as _t
from twikit.user import User as _User

# New X manifest maps chunk-id -> name and chunk-id -> hash separately, e.g.
#   ...,158:"ondemand.s",...   and   ...,158:"<hexhash>",...
_ON_DEMAND_FILE_REGEX = re.compile(r',(\d+):["\']ondemand\.s["\']')
_HASH_TEMPLATE = r',{}:"([0-9a-f]+)"'
_INDICES_REGEX = re.compile(r'\[(\d+)\],\s*16')


async def _get_indices(self, home_page_response, session, headers):
    key_byte_indices = []
    response = self.validate_response(home_page_response) or self.home_page_response
    response_str = str(response)

    on_demand_file = _ON_DEMAND_FILE_REGEX.search(response_str)
    if on_demand_file:
        chunk_index = on_demand_file.group(1)
        hash_match = re.search(_HASH_TEMPLATE.format(chunk_index), response_str)
        if hash_match:
            file_hash = hash_match.group(1)
            url = (
                "https://abs.twimg.com/responsive-web/client-web/"
                f"ondemand.s.{file_hash}a.js"
            )
            resp = await session.request(method="GET", url=url, headers=headers)
            for item in _INDICES_REGEX.finditer(str(resp.text)):
                key_byte_indices.append(item.group(1))

    if not key_byte_indices:
        raise Exception("Couldn't get KEY_BYTE indices")

    key_byte_indices = list(map(int, key_byte_indices))
    return key_byte_indices[0], key_byte_indices[1:]


# Apply the transaction patch.
_t.ON_DEMAND_FILE_REGEX = _ON_DEMAND_FILE_REGEX
_t.INDICES_REGEX = _INDICES_REGEX
_t.ClientTransaction.get_indices = _get_indices


# --- User parser fix -------------------------------------------------------
# X has dropped/relocated several legacy.* fields, so twikit's strict
# User.__init__ raises KeyError on whatever happens to be missing
# (description urls, withheld_in_countries, ...). Replace it with a fully
# defensive parser that .get()s everything. We only rely on name/screen_name,
# so missing extras degrade gracefully to defaults.
def _safe_user_init(self, client, data):
    self._client = client
    legacy = data.get("legacy", {}) or {}
    core = data.get("core", {}) or {}
    entities = legacy.get("entities", {}) or {}

    self.id = data.get("rest_id")
    self.created_at = legacy.get("created_at") or core.get("created_at")
    self.name = legacy.get("name") or core.get("name")
    self.screen_name = legacy.get("screen_name") or core.get("screen_name")
    self.profile_image_url = legacy.get("profile_image_url_https")
    self.profile_banner_url = legacy.get("profile_banner_url")
    self.url = legacy.get("url")
    self.location = legacy.get("location")
    self.description = legacy.get("description")
    self.description_urls = entities.get("description", {}).get("urls", [])
    self.urls = entities.get("url", {}).get("urls")
    self.pinned_tweet_ids = legacy.get("pinned_tweet_ids_str", [])
    self.is_blue_verified = data.get("is_blue_verified", False)
    self.verified = legacy.get("verified", False)
    self.possibly_sensitive = legacy.get("possibly_sensitive", False)
    self.can_dm = legacy.get("can_dm", False)
    self.can_media_tag = legacy.get("can_media_tag", False)
    self.want_retweets = legacy.get("want_retweets", False)
    self.default_profile = legacy.get("default_profile", False)
    self.default_profile_image = legacy.get("default_profile_image", False)
    self.has_custom_timelines = legacy.get("has_custom_timelines", False)
    self.followers_count = legacy.get("followers_count", 0)
    self.fast_followers_count = legacy.get("fast_followers_count", 0)
    self.normal_followers_count = legacy.get("normal_followers_count", 0)
    self.following_count = legacy.get("friends_count", 0)
    self.favourites_count = legacy.get("favourites_count", 0)
    self.listed_count = legacy.get("listed_count", 0)
    self.media_count = legacy.get("media_count", 0)
    self.statuses_count = legacy.get("statuses_count", 0)
    self.is_translator = legacy.get("is_translator", False)
    self.translator_type = legacy.get("translator_type")
    self.withheld_in_countries = legacy.get("withheld_in_countries", [])
    self.protected = legacy.get("protected", False)


_User.__init__ = _safe_user_init
