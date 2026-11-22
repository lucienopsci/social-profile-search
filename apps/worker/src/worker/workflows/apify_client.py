"""Clients Apify: recherches de profils sociaux uniquement, 1 page / 10 résultats max.

Règles issues de la skill apify-api-usage:
- acteurs autorisés uniquement, aucun autre acteur
- searchTerms / maxItems / maxPages / searchType configurés pour des profils et 10 résultats max
- aucune donnée fabriquée: uniquement ce qu'Apify retourne
"""
import os
import time
import urllib.parse

import httpx

from .models import VALID_PLATFORMS

APIFY_BASE = "https://api.apify.com/v2"
ACTORS = {
    "linkedin": {
        "actor_id": "harvestapi~linkedin-profile-search-by-name",
        "input": lambda q: {
            "searchTerm": q,
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
        "actor_id": "igview-owner~tiktok-search-user-scraper",
        "input": lambda q: {"searchQuery": q, "maxResults": 10, "maxPages": 1},
    },
    "facebook": {
        "actor_id": "patient_discovery~facebook-search-people",
        "input": lambda q: {"searchQuery": q, "maxPages": 1},
    },
}


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


async def _run_actor(client: httpx.AsyncClient, platform: str, query: str) -> dict:
    """Exécute un acteur de recherche de profils et normalise les résultats."""
    if platform not in ACTORS:
        raise ValueError(f"Plateforme non supportée: {platform}")
    spec = ACTORS[platform]
    run_input = spec["input"](query)
    token = os.environ["APIFY_TOKEN"]

    r = await client.post(
        f"{APIFY_BASE}/acts/{spec['actor_id']}/run-sync-get-dataset-items",
        params={"token": token, "memory": 1024, "timeout": 120},
        json=run_input,
    )
    r.raise_for_status()
    items = r.json() or []

    rows = []
    for it in items:
        p = _props(it)
        rows.append({
            "search_query": query,
            "platform": platform,
            "account_id": _str(p, "id", "userId", "uid", "did"),
            "account_username": _str(p, "username", "handle", "user", "userName"),
            "account_name": _str(p, "fullName", "fullNameDecoded", "displayName", "name", "title"),
            "account_description": _str(p, "bio", "description", "about", "headline", "summary"),
            "followers": p.get("followersCount") or p.get("followers") or p.get("followerCount") or "",
            "url": _str(p, "url", "profileUrl", "profile_url"),
            "relevant": "",
        })
    return {"platform": platform, "rows": rows[:10]}


async def search_profiles(platform: str, query: str) -> dict:
    """Exécute une recherche de profils (max 10 résultats) et retourne les lignes CSV normalisées."""
    if platform not in VALID_PLATFORMS:
        raise ValueError(f"Plateforme non supportée: {platform}")
    async with httpx.AsyncClient(timeout=180) as client:
        return await _run_actor(client, platform, query)


async def get_usage_usd() -> float:
    """Coût Apify engagé ce mois (USD) via l'API usage de l'utilisateur."""
    token = os.environ["APIFY_TOKEN"]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://api.apify.com/v2/users/me/usage/monthly", params={"token": token}
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        usage = data.get("currentMonth", data)
        return float(usage.get("totalAmount", 0)) / 100


async def actor_price_usd(platform: str) -> float:
    """Prix de lancement d'un acteur (estimation du coût par exécution)."""
    spec = ACTORS[platform]
    token = os.environ["APIFY_TOKEN"]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{APIFY_BASE}/acts/{spec['actor_id']}", params={"token": token}
        )
        r.raise_for_status()
        pricing = r.json().get("data", {}).get("pricing", [])
        if pricing:
            return float(pricing[0].get("pricePerUnitUsd", 0) or 0)
        return 0.0
