"""Modèles d'entrée/sortie du workflow de recherche de comptes sociaux."""
from pydantic import BaseModel, ConfigDict


class TargetInput(BaseModel):
    """Une cible: une ligne de l'onglet Inputs du Google Sheet."""
    model_config = ConfigDict(extra="forbid")

    target_id: str          # identifiant unique (ex: ligne du sheet)
    name: str               # nom de la personne/organisation
    description: str = ""   # description (peut être vide)


class BatchParams(BaseModel):
    """Entrée du workflow parent social-profile-search."""
    model_config = ConfigDict(extra="forbid")

    sheet_name: str                 # nom du Google Sheet de sortie + dashboard
    inputs_tab: str = "Inputs"      # onglet où lire les cibles
    platforms: list[str]            # subset de: linkedin, x, bluesky, instagram, tiktok, facebook
    max_concurrency: int = 20        # cibles traitées en parallèle
    platforms_concurrency: int = 6  # plateformes appelées en parallèle par cible


class TargetStatus(BaseModel):
    """Ligne du dashboard de suivi, une par cible."""
    target_id: str
    name: str
    platform_name: str = ""
    searched: bool = False          # recherche effectuée sur la plateforme
    accounts_returned: int = 0      # comptes retournés par Apify
    accounts_checked: int = 0        # comptes vérifiés (règle exact/fuzzy/approx)
    accounts_relevant: int = 0      # comptes jugés pertinents (relevant=TRUE)
    cost_usd: float = 0.0           # coût Apify engagé pour cette cible
    error: str = ""

class PerPlatformStatus(BaseModel):
    platform: str
    searched: bool
    accounts_returned: int
    accounts_checked: int
    accounts_relevant: int
    cost_usd: float
    error: str = ""


class ProcessTargetParams(BaseModel):
    """Entrée du workflow enfant (une cible)."""
    model_config = ConfigDict(extra="forbid")

    target: TargetInput
    sheet_name: str
    platforms: list[str]


# Constantes pures (définies ici car le sandbox de workflow interdit d'importer
# les modules d'E/S, y compris les constantes qui y vivent).
VALID_PLATFORMS = frozenset({"linkedin", "x", "bluesky", "instagram", "tiktok", "facebook"})

CSV_HEADERS = [
    "search_query", "platform", "account_id", "account_username",
    "account_name", "account_description", "followers", "url", "relevant",
]
DASHBOARD_HEADERS = [
    "target_id", "name", "platform", "searched", "accounts_returned",
    "accounts_checked", "accounts_relevant", "cost_usd", "error", "updated_at",
]
