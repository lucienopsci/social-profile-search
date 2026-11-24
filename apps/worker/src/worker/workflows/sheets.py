"""Google Sheets: lecture des inputs, écriture des résultats et du dashboard.

Utilise l'API REST Google Sheets avec un compte de service (GOOGLE_SERVICE_ACCOUNT_JSON)
- fichier de clé JSON stocké dans le secret du même nom.
Les onglets results/dashboard manquants sont créés automatiquement (avec en-têtes).
Toutes les requêtes retentent avec backoff sur 429 (quota écritures ~60/min par user).
"""
import asyncio
import json
import os
import re
import urllib.parse

import httpx

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

from .models import CSV_HEADERS, DASHBOARD_HEADERS

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{25,}$")


async def _get_access_token(client: httpx.AsyncClient) -> str:
    """Échange un JWT fait depuis le compte de service contre un access token."""
    import time  # noqa: local import
    import base64

    creds = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])

    def b64(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    now = int(time.time())
    header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = b64(json.dumps({
        "iss": creds["client_email"],
        "scope": "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }).encode())
    # signature RS256 via cryptography
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(creds["private_key"].encode(), password=None)
    signer_input = f"{header}.{claims}".encode()
    sig = key.sign(signer_input, padding.PKCS1v15(), hashes.SHA256())
    jwt = f"{header}.{claims}.{b64(sig)}"
    r = await client.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt,
    })
    r.raise_for_status()
    return r.json()["access_token"]


def _range(tab: str, a1: str) -> str:
    """Construit un range A1 encodé pour l'URL, nom d'onglet toujours entre guillemets."""
    quoted = "'" + tab.replace("'", "''") + "'"
    return urllib.parse.quote(f"{quoted}!{a1}")


class SheetsClient:
    def __init__(self, spreadsheet_id: str):
        self.id = spreadsheet_id
        self._token: str | None = None
        self._tab_map: dict[str, str] | None = None

    async def _headers(self, client: httpx.AsyncClient) -> dict:
        if not self._token:
            self._token = await _get_access_token(client)
        return {"Authorization": f"Bearer {self._token}"}

    async def _req(self, client: httpx.AsyncClient, method: str, url: str, **kw):
        """Requête Sheets avec retry/backoff sur 429 (quota) et 503.

        Respecte le header Retry-After de Google; sinon backoff exponentiel
        10s, 20s, 40s, 60s, 60s (capé), jusqu'à 6 tentatives.
        """
        last_r = None
        r = None
        for attempt in range(6):
            headers = await self._headers(client)
            r = await client.request(method, url, headers=headers, **kw)
            if r.status_code == 401:  # token expiré: refresh une fois
                self._token = None
                headers = await self._headers(client)
                r = await client.request(method, url, headers=headers, **kw)
            if r.status_code in (429, 503) and attempt < 5:
                retry_after = r.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else min(10 * (2 ** attempt), 60)
                except ValueError:
                    wait = min(10 * (2 ** attempt), 60)
                last_r = r
                await asyncio.sleep(wait)
                continue
            return r
        return last_r if last_r is not None else r

    @staticmethod
    def _norm(s: str) -> str:
        return (s or "").strip().lower().replace(" ", "")

    async def _list_tabs(self, client: httpx.AsyncClient) -> list[str]:
        r = await self._req(
            client, "GET", f"{SHEETS_BASE}/{self.id}", params={"fields": "sheets.properties.title"}
        )
        if r.status_code != 200:
            # NE JAMAIS déduire "onglet absent" d'une erreur réseau/quota
            raise RuntimeError(
                f"Impossible de lister les onglets du spreadsheet (HTTP {r.status_code}). "
                f"Réponse Google: {r.text[:300]}"
            )
        data = r.json().get("sheets", [])
        return [s.get("properties", {}).get("title", "?") for s in data]

    async def _resolve_tab(self, client: httpx.AsyncClient, tab: str) -> str:
        """Titre réel de l'onglet, insensible casse/espaces, tolérant aux préfixes."""
        if self._tab_map is None:
            titles = await self._list_tabs(client)
            self._tab_map = {self._norm(t): t for t in titles}
        key = self._norm(tab)
        if key in self._tab_map:
            return self._tab_map[key]
        for k, t in self._tab_map.items():
            if k.startswith(key) or key.startswith(k):
                return t
        raise RuntimeError(
            f"Onglet '{tab}' introuvable. Onglets disponibles: {list(self._tab_map.values())}"
        )

    async def _create_tab(self, client: httpx.AsyncClient, tab: str, headers: list[str]) -> str:
        """Crée l'onglet via batchUpdate et écrit sa ligne d'en-têtes."""
        r = await self._req(
            client, "POST", f"{SHEETS_BASE}/{self.id}:batchUpdate",
            json={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
        )
        if r.status_code >= 400:
            # déjà créé entre-temps par un autre child: ignorer cette erreur précise
            if r.status_code == 400 and "already exists" in r.text:
                if self._tab_map is not None:
                    self._tab_map[self._norm(tab)] = tab
                return tab
            raise RuntimeError(
                f"Impossible de créer l'onglet '{tab}' (HTTP {r.status_code}). "
                f"Réponse Google: {r.text[:300]}"
            )
        rw = await self._req(
            client, "PUT",
            f"{SHEETS_BASE}/{self.id}/values/{_range(tab, 'A1')}",
            params={"valueInputOption": "USER_ENTERED"},
            json={"values": [headers]},
        )
        if rw.status_code >= 400:
            raise RuntimeError(
                f"Impossible d'écrire les en-têtes de '{tab}' (HTTP {rw.status_code}). "
                f"Réponse Google: {rw.text[:300]}"
            )
        if self._tab_map is not None:
            self._tab_map[self._norm(tab)] = tab
        return tab

    async def ensure_tab(self, client: httpx.AsyncClient, tab: str, headers: list[str]) -> str:
        """Retourne le titre réel de l'onglet, en le créant avec en-têtes s'il est absent."""
        try:
            return await self._resolve_tab(client, tab)
        except RuntimeError as e:
            # ne créer que si l'erreur vient de l'absence réelle d'onglet
            if "introuvable" in str(e):
                return await self._create_tab(client, tab, headers)
            raise

    async def read_inputs(self, tab: str) -> list[list]:
        """Lit l'onglet inputs: colonnes target_id, name, description."""
        async with httpx.AsyncClient(timeout=120) as client:
            real_tab = await self.ensure_tab(client, tab, ["target_id", "name", "description"])
            r = await self._req(
                client, "GET",
                f"{SHEETS_BASE}/{self.id}/values/{_range(real_tab, 'A2:C')}",
            )
            if r.status_code != 200:
                raise RuntimeError(
                    f"Impossible de lire l'onglet '{real_tab}' (HTTP {r.status_code}). "
                    f"Réponse Google: {r.text[:300]}"
                )
            return r.json().get("values", [])

    async def append_results(self, client: httpx.AsyncClient, tab: str, rows: list[list]):
        """Ajoute les comptes trouvés (colonnes CSV_HEADERS + TRUE/FALSE) à l'onglet résultats."""
        real_tab = await self.ensure_tab(client, tab, CSV_HEADERS)
        r = await self._req(
            client, "POST",
            f"{SHEETS_BASE}/{self.id}/values/{_range(real_tab, 'A1')}:append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"values": rows},
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"Impossible d'écrire dans l'onglet '{real_tab}' (HTTP {r.status_code}). "
                f"Réponse Google: {r.text[:300]}"
            )

    async def upsert_dashboard(self, client: httpx.AsyncClient, tab: str, row: list):
        """Upsert réel d'une ligne dashboard, clé = (target_id, platform).

        - si une ligne existe déjà pour (target_id, platform), elle est mise à jour en place
        - sinon, la ligne est ajoutée à la fin de l'onglet
        """
        real_tab = await self.ensure_tab(client, tab, DASHBOARD_HEADERS)
        target_id = str(row[0]).strip() if len(row) > 0 else ""
        platform = str(row[2]).strip() if len(row) > 2 else ""

        # 1) lire les lignes existantes du dashboard
        r = await self._req(
            client, "GET",
            f"{SHEETS_BASE}/{self.id}/values/{_range(real_tab, 'A2:J')}",
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"Impossible de lire le dashboard '{real_tab}' (HTTP {r.status_code}). "
                f"Réponse Google: {r.text[:300]}"
            )
        existing = r.json().get("values", [])

        # 2) chercher la ligne correspondante (clé: colonnes A et C)
        match_idx = None
        for i, erow in enumerate(existing):
            a = str(erow[0]).strip() if len(erow) > 0 else ""
            c = str(erow[2]).strip() if len(erow) > 2 else ""
            if a == target_id and c == platform:
                match_idx = i
                break

        pad = lambda values: values + [""] * (len(DASHBOARD_HEADERS) - len(values))

        if match_idx is not None:
            # mise à jour en place: ligne sheet = index (0-based dans A2:J) + 2
            sheet_row = match_idx + 2
            r = await self._req(
                client, "PUT",
                f"{SHEETS_BASE}/{self.id}/values/{_range(real_tab, f'A{sheet_row}:J{sheet_row}')}",
                params={"valueInputOption": "USER_ENTERED"},
                json={"values": [pad(row)]},
            )
        else:
            # ajout en fin d'onglet
            r = await self._req(
                client, "POST",
                f"{SHEETS_BASE}/{self.id}/values/{_range(real_tab, 'A1')}:append",
                params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                json={"values": [pad(row)]},
            )
        if r.status_code != 200:
            raise RuntimeError(
                f"Impossible d'écrire dans le dashboard '{real_tab}' (HTTP {r.status_code}). "
                f"Réponse Google: {r.text[:300]}"
            )


async def find_spreadsheet_id(name_or_id: str) -> str:
    """Résout un nom OU un ID de spreadsheet en ID.

    Si la valeur ressemble à un ID (>= 25 chars alphanumériques/_-), l'utilise directement.
    Sinon, recherche par nom via Drive.
    """
    if _ID_RE.match(name_or_id or ""):
        return name_or_id
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _get_access_token(client)
        r = await client.get(
            "https://www.googleapis.com/drive/v3/files",
            params={"q": f"name = '{name_or_id}' and mimeType = 'application/vnd.google-apps.spreadsheet'",
                    "fields": "files(id,name)"},
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        files = r.json().get("files", [])
        if not files:
            raise FileNotFoundError(f"Aucun Google Sheet nommé ou accessible via '{name_or_id}'")
        return files[0]["id"]
