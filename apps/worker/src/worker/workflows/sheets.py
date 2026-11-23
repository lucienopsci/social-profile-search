"""Google Sheets: lecture des inputs, écriture des résultats et du dashboard.

Utilise l'API REST Google Sheets avec un compte de service (GOOGLE_SERVICE_ACCOUNT_JSON)
- fichier de clé JSON stocké dans le secret du même nom.
"""
import json
import os
import re
import urllib.parse

import httpx

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_BASE = "https://www.googleapis.com/drive/v3"

from .models import CSV_HEADERS  # noqa: F401 (constants partagées)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{25,}$")


async def _get_access_token(client: httpx.AsyncClient) -> str:
    """Échange un JWT fait depuis le compte de service contre un access token."""
    import time  # noqa: local import

    creds = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    # build JWT
    import base64

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
    # sign with the private key (RS256) via cryptography
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
        headers = await self._headers(client)
        r = await client.request(method, url, headers=headers, **kw)
        if r.status_code == 401:  # token expired
            self._token = None
            headers = await self._headers(client)
            r = await client.request(method, url, headers=headers, **kw)
        return r

    @staticmethod
    def _norm(s: str) -> str:
        return (s or "").strip().lower().replace(" ", "")

    async def _list_tabs(self, client: httpx.AsyncClient) -> list[str]:
        """Liste les titres d'onglets, pour des messages d'erreur exploitables."""
        r = await self._req(
            client, "GET", f"{SHEETS_BASE}/{self.id}", params={"fields": "sheets.properties.title"}
        )
        if r.status_code == 200:
            data = r.json().get("sheets", [])
            return [s.get("properties", {}).get("title", "?") for s in data]
        return []

    async def _resolve_tab(self, client: httpx.AsyncClient, tab: str) -> str:
        """Retourne le titre réel de l'onglet, insensible à la casse et aux espaces.

        Tolère aussi les variantes de nombre (Input/Inputs, Dashboard/Dashboard).
        """
        if self._tab_map is None:
            self._tab_map = {}
            for t in await self._list_tabs(client):
                self._tab_map[self._norm(t)] = t
        key = self._norm(tab)
        if key in self._tab_map:
            return self._tab_map[key]
        for k, t in self._tab_map.items():
            if k.startswith(key) or key.startswith(k):
                return t
        raise RuntimeError(
            f"Onglet '{tab}' introuvable. Onglets disponibles: {list(self._tab_map.values())}"
        )

    async def read_inputs(self, tab: str) -> list[list]:
        """Lit l'onglet inputs: colonnes target_id, name, description."""
        async with httpx.AsyncClient(timeout=60) as client:
            real_tab = await self._resolve_tab(client, tab)
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
        real_tab = await self._resolve_tab(client, tab)
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
        """Ajoute une ligne au dashboard (append; une ligne par target_id x plateforme)."""
        real_tab = await self._resolve_tab(client, tab)
        r = await self._req(
            client, "POST",
            f"{SHEETS_BASE}/{self.id}/values/{_range(real_tab, 'A1')}:append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"values": [row]},
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"Impossible d'écrire dans l'onglet '{real_tab}' (HTTP {r.status_code}). "
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
            f"{DRIVE_BASE}/files",  # drive list endpoint
            params={"q": f"name = '{name_or_id}' and mimeType = 'application/vnd.google-apps.spreadsheet'",
                    "fields": "files(id,name)"},
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        files = r.json().get("files", [])
        if not files:
            raise FileNotFoundError(f"Aucun Google Sheet nommé ou accessible via '{name_or_id}'")
        return files[0]["id"]
