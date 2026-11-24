"""Clients Apify: recherches de profils sociaux uniquement, 10 résultats max.

Règles issues de la skill apify-api-usage:
- acteurs autorisés uniquement, aucun autre acteur
- aucune donnée fabriquée: uniquement ce qu'Apify retourne

Recherches PAR LOT (attribution de chaque résultat à son mot-clé d'origine):
- youtube: apidojo~youtube-channel-information-scraper (keywords array)
  ~0.001$/recherche + ~0.0005$/chaîne, pas de frais de lancement
- tiktok: clockworks~tiktok-scraper (searchQueries + searchSection "/user")
  ~0.003$/profil + 0.001$/run, batch de recherche de comptes natif
Repli individuel du même acteur côté enfant si attribution/lot en échec.
"""
import os
import unicodedata

import httpx

from .models import BATCHED_PLATFORMS, VALID_PLATFORMS

APIFY_BASE = "https://api.apify.com/v2"

# Recherches par lot: 1 run pour N mots-clés, attribution par search_query.
BATCH_SEARCH_ACTORS = {
    "youtube": {
        "actor_id": "apidojo~youtube-channel-information-scraper",
        # ~0.00099$/recherche + ~0.000495$/chaîne retournée, pas de frais de lancement
        "input": lambda kws: {"keywords": kws, "maxItems": len(kws) * 10},
    },
    "tiktok": {
        "actor_id": "clockworks~tiktok-scraper",
        # ~0.003$/profil + 0.001$/run (Bronze); "/user" = recherche de comptes
        "input": lambda kws: {"searchQueries": kws, "searchSection": "/user", "maxProfilesPerQuery": 10},
    },
}

# Recherche individuelle (1 cible): repli des plateformes par lot + autres plateformes.
ACTORS = {
    "linkedin": {
        "actor_id": "harvestapi~linkedin-profile-search-by-name",
        "input": lambda q: {
            "searchTerm": q,
            "profileScraperMode": "Short",
            "maxItems": 10,
            "maxPages": 1,
        },
    },
    "x": {
        "actor_id": "xquik~x-user-search-scraper",
        "input": lambda q: {
            "searchTerms": [q],
            "maxItemsPerTarget": 10,
            "maxResultsPerSearch": 10,
        },
    },
    "bluesky": {
        "actor_id": "indomitably_doctor~bluesky-search-scraper",
        "input": lambda q: {"query": q, "mode": "searchUsers", "maxItems": 10},
    },
    "instagram": {
        "actor_id": "apify~instagram-search-scraper",
        "input": lambda q: {"search": q, "searchType": "user", "searchLimit": 10},
    },
    "tiktok": {
        # repli individuel du même acteur que le lot
        "actor_id": BATCH_SEARCH_ACTORS["tiktok"]["actor_id"],
        "input": lambda q: {"searchQueries": [q], "searchSection": "/user", "maxProfilesPerQuery": 10},
    },
    "facebook": {
        "actor_id": "patient_discovery~facebook-search-people",
        "input": lambda q: {"searchQuery": q, "maxPages": 1},
    },
    "youtube": {
        # repli individuel du même acteur que le lot
        "actor_id": BATCH_SEARCH_ACTORS["youtube"]["actor_id"],
        "input": lambda q: {"keywords": [q], "maxItems": 10},
    },
}


def _auth_headers() -> dict:
    """Token Apify en header (jamais en query param: il fuiterait dans les logs)."""
    return {"Authorization": "Bearer " + os.environ["APIFY_TOKEN"]}


def _props(item: dict) -> dict:
    """Apify renvoie parfois {item: {...}} ou l'objet directement."""
    if isinstance(item, dict) and isinstance(item.get("item"), dict):
        return item["item"]
    return item if isinstance(item, dict) else {}


def _str(props: dict, *keys: str) -> str:
    for k in keys:
        v = props.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def _norm_name(s: str) -> str:
    """Normalise un nom pour l'attribution: minuscules, sans accents, alphanumérique."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c.lower() for c in s if c.isalnum())


def _raise_detail(r: httpx.Response, platform: str) -> None:
    """Erreur explicite incluant le corps de réponse Apify."""
    raise httpx.HTTPStatusError(
        f"Apify {platform}: HTTP {r.status_code} - {r.text[:300]}", request=r.request, response=r
    )


def _row_from_search(platform: str, query: str, p: dict) -> dict:
    """Ligne CSV normalisée depuis les propriétés brutes d'un item Apify."""
    # TikTok clockworks: les items de recherche sont des VIDÉOS; le profil
    # de l'auteur est dans authorMeta (id, name=handle, nickName, signature, fans,
    # profileUrl). Le mot-clé d'origine est dans searchQuery (attribution).
    if platform == "tiktok" and isinstance(p.get("authorMeta"), dict):
        am = p["authorMeta"]
        handle = _str(am, "name", "uniqueId", "username")
        return {
            "search_query": query,
            "platform": platform,
            "account_id": _str(am, "id", "userId", "uid"),
            "account_username": handle,
            "account_name": _str(am, "nickName", "fullName"),
            "account_description": _str(am, "signature", "bio"),
            "followers": am.get("fans") or am.get("followerCount") or "",
            "url": _str(am, "profileUrl", "url") or (
                "https://www.tiktok.com/@" + handle if platform == "tiktok" and handle else ""
            ),
            "relevant": "",
        }
    return {
        "search_query": query,
        "platform": platform,
        "account_id": _str(p, "id", "userId", "uid", "did", "channelId"),
        "account_username": _str(p, "username", "uniqueId", "handle", "user", "userName"),
        "account_name": _str(p, "nickName", "fullName", "fullNameDecoded", "displayName", "name", "title", "channelTitle"),
        "account_description": _str(p, "signature", "bio", "description", "about", "headline", "summary"),
        "followers": p.get("followersCount") or p.get("followerCount") or p.get("followerCount") or p.get("subscriberCount") or p.get("subscribers") or "",
        "url": _str(p, "url", "webUrl", "profileUrl", "profile_url", "channelUrl") or (
            "https://www.tiktok.com/@" + _str(p, "uniqueId", "username") if platform == "tiktok" and (_str(p, "uniqueId", "username")) else ""
        ),
        "relevant": "",
    }


async def _run_actor(client: httpx.AsyncClient, platform: str, query: str) -> dict:
    """Exécute un acteur de recherche de profils et normalise les résultats."""
    if platform not in ACTORS:
        raise ValueError(f"Plateforme non supportée: {platform}")
    spec = ACTORS[platform]
    run_input = spec["input"](query)

    r = await client.post(
        f"{APIFY_BASE}/acts/{spec['actor_id']}/run-sync-get-dataset-items",
        params={"memory": 1024, "timeout": 120},
        headers=_auth_headers(),
        json=run_input,
    )
    if r.status_code >= 400:
        _raise_detail(r, platform)
    items = r.json() or []

    rows = [_row_from_search(platform, query, _props(it)) for it in items]
    return {"platform": platform, "rows": rows[:10]}


async def search_profiles(platform: str, query: str) -> dict:
    """Exécute une recherche de profils (max 10 résultats) et retourne les lignes CSV normalisées."""
    if platform not in VALID_PLATFORMS:
        raise ValueError(f"Plateforme non supportée: {platform}")
    async with httpx.AsyncClient(timeout=180) as client:
        return await _run_actor(client, platform, query)


# --------------------------------------------------------------------------
# Recherches par lot (youtube/tiktok): 1 run pour N mots-clés
# --------------------------------------------------------------------------

def _attributed_keyword(p: dict, keywords_norm: dict[str, str]) -> str | None:
    """Retrouve le mot-clé d'origine d'un résultat (clé normalisée)."""
    for k in ("searchKeyword", "keyword", "searchQuery", "query", "searchTerm", "inputQuery"):
        v = _norm_name(str(p.get(k) or ""))
        if v and v in keywords_norm:
            return keywords_norm[v]
    return None


async def search_profiles_batch(platform: str, keywords: list[str]) -> dict:
    """Recherche par lot pour une plateforme batchée (youtube/tiktok).

    - 1 run pour len(keywords) recherches, ~10 résultats par mot-clé
    - chaque résultat est attribué à son mot-clé d'origine (search_query)
    - résultats non attribuables rejetés et comptés; l'appelant décide du repli
    """
    if platform not in BATCHED_PLATFORMS:
        raise ValueError(f"Plateforme non batchée: {platform}. Batchées: {sorted(BATCHED_PLATFORMS)}")
    if not keywords:
        return {"rows": [], "unattributed": 0, "missing_keywords": []}
    norm_map = {_norm_name(k): k for k in keywords}
    spec = BATCH_SEARCH_ACTORS[platform]
    run_input = spec["input"](keywords)

    async with httpx.AsyncClient(timeout=1200) as client:
        r = await client.post(
            f"{APIFY_BASE}/acts/{spec['actor_id']}/run-sync-get-dataset-items",
            params={"memory": 2048, "timeout": 900},
            headers=_auth_headers(),
            json=run_input,
        )
        if r.status_code >= 400:
            _raise_detail(r, platform)
        items = r.json() or []

    rows = []
    unattributed = 0
    seen = set()
    seen_accounts = set()
    for it in items:
        p = _props(it)
        kw = _attributed_keyword(p, norm_map)
        if kw is None:
            unattributed += 1
            continue
        seen.add(_norm_name(kw))
        row = _row_from_search(platform, kw, p)
        # déduplication par compte (clockworks renvoie plusieurs vidéos du même auteur)
        dedup_key = (platform, row.get("account_id") or row.get("account_username"))
        if dedup_key in seen_accounts:
            continue
        seen_accounts.add(dedup_key)
        rows.append(row)

    missing = [k for k in keywords if _norm_name(k) not in seen]
    return {"rows": rows, "unattributed": unattributed, "missing_keywords": missing}


async def get_usage_usd() -> float:
    """Coût Apify engagé ce mois (USD) via l'API usage de l'utilisateur."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://api.apify.com/v2/users/me/usage/monthly", headers=_auth_headers()
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        usage = data.get("currentMonth", data)
        return float(usage.get("totalAmount", 0)) / 100


async def actor_price_usd(platform: str) -> float:
    """Prix de lancement d'un acteur (estimation du coût par exécution)."""
    spec = ACTORS[platform]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{APIFY_BASE}/acts/{spec['actor_id']}", headers=_auth_headers()
        )
        r.raise_for_status()
        pricing = r.json().get("data", {}).get("pricing", [])
        if pricing:
            return float(pricing[0].get("pricePerUnitUsd", 0) or 0)
        return 0.0
