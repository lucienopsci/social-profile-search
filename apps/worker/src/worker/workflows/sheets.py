"""Google Sheets: lecture des inputs, écriture des résultats et du dashboard.

Utilise l'API REST Google Sheets avec un compte de service (GOOGLE_SERVICE_ACCOUNT_JSON)
- fichier de clé JSON stocké dans le secret du même nom.
"""
import json
import os

import httpx

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_BASE = "https://www.googleapis.com/drive/v3"

from .models import CSV_HEADERS  # noqa: F401 (constants partagées)


async def _get_access_token(client: httpx.AsyncClient) -> str:
    """Échange un JWT fait depuis le compte de service contre un access token."""
    import time  # noqa: local import
    from pathlib import Path

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


class SheetsClient:
    def __init__(self, spreadsheet_id: str):
        self.id = spreadsheet_id
        self._token: str | None = None

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

    async def read_inputs(self, tab: str) -> list[list]:
        """Lit l'onglet Inputs: colonnes target_id, name, description."""
        async with httpx.AsyncClient(timeout=60) as client:
            r = await self._req(
                client, "GET",
                f"{SHEETS_BASE}/{self.id}/values/{tab}!A2:C",
            )
            r.raise_for_status()
            return r.json().get("values", [])

    async def append_results(self, client: httpx.AsyncClient, tab: str, rows: list[list]):
        """Ajoute les comptes trouvés (colonnes CSV_HEADERS + TRUE/FALSE) à l'onglet résultats."""
        await self._req(
            client, "POST",
            f"{SHEETS_BASE}/{self.id}/values/{tab}!A1:append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"values": rows},
        )

    async def upsert_dashboard(self, client: httpx.AsyncClient, tab: str, row: list):
        """Ajoute ou met à jour une ligne du dashboard (todo: map target_id+platform -> ligne)."""
        await self._req(
            client, "POST",
            f"{SHEETS_BASE}/{self.id}/values/{tab}!A1:append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"values": [row]},
        )


async def find_spreadsheet_id(name: str) -> str:
    """Résout le nom du Sheet en ID via Drive."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _get_access_token(client)
        r = await client.get(
            f"{DRIVE_BASE}/files",  # drive list endpoint
            params={"q": f"name = '{name}' and mimeType = 'application/vnd.google-apps.spreadsheet'",
                    "fields": "files(id,name)"},
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        files = r.json().get("files", [])
        if not files:
            raise FileNotFoundError(f"Aucun Google Sheet nommé '{name}' n'est accessible")
        return files[0]["id"]