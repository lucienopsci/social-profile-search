"""Modèles d'entrée/sortie du workflow de recherche de comptes sociaux."""
from pydantic import BaseModel, ConfigDict


class TargetInput(BaseModel):
    """Une cible: une ligne de l'onglet Inputs du Google Sheet."""
    model_config = ConfigDict(extra="forbid")

    target_id: str          # identifiant unique (ex: ligne du sheet)
    name: str               # nom de la personne/organisation
    description: str = ""   # description (peut être vide)


class BatchSearchParams(BaseModel):
    """Lot de mots-clés pour une recherche par lot (youtube/tiktok)."""
    model_config = ConfigDict(extra="forbid")

    platform: str
    keywords: list[str]


class TargetStatus(BaseModel):
    """Ligne du dashboard de suivi, une par cible."""
    target_id: str
    name: str
    platform_name: str = ""
    searched: bool = False
    accounts_returned: int = 0
    accounts_checked: int = 0
    accounts_relevant: int = 0
    cost_usd: float = 0.0
    error: str = ""


class PerPlatformStatus(BaseModel):
    """État d'une cible sur une plateforme (valeurs par défaut: branchage erreur)."""
    platform: str
    searched: bool = False
    accounts_returned: int = 0
    accounts_checked: int = 0
    accounts_relevant: int = 0
    cost_usd: float = 0.0
    error: str = ""


class ProcessTargetParams(BaseModel):
    """Entrée du workflow enfant (une cible)."""
    model_config = ConfigDict(extra="forbid")

    target: TargetInput
    sheet_name: str
    platforms: list[str]
    # résultats pré-cherchés en lot par le parent, clé = plateforme ("youtube", "tiktok"),
    # valeur = lignes attribuées à cette cible (search_query == target.name).
    # None = le child fait lui-même la recherche individuelle (repli).
    precomputed_rows: "dict[str, list[dict]] | None" = None


# Constantes pures (définies ici car le sandbox de workflow interdit d'importer
# les modules d'E/S, y compris les constantes qui y vivent).
VALID_PLATFORMS = frozenset({"linkedin", "x", "bluesky", "instagram", "tiktok", "facebook", "youtube"})
BATCHED_PLATFORMS = frozenset({"tiktok"})

CSV_HEADERS = [
    "search_query", "platform", "account_id", "account_username",
    "account_name", "account_description", "followers", "url", "relevant",
]
DASHBOARD_HEADERS = [
    "target_id", "name", "platform", "searched", "accounts_returned",
    "accounts_checked", "accounts_relevant", "cost_usd", "error", "updated_at",
]
