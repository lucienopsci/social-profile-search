"""Workflows de recherche et vérification de comptes sociaux par lots.

- social-profile-search (parent): lit les cibles dans le Google Sheet; YouTube et
  TikTok sont recherchés PAR LOT (respectivement apidojo et clockworks, ~100
  mots-clés par run, attribution par search_query) avec repli individuel par cible
  si le lot échoue; les autres plateformes sont traitées par le workflow enfant;
  dashboard de suivi dans le Sheet.
  Plateformes vides = mode préparation: crée les onglets results/dashboard, aucune recherche Apify.
- process-target (enfant): recherche sur chaque plateforme via Apify (sauf lignes
  pré-cherchées par le parent), vérifie la pertinence, écrit les résultats et l'état.
"""
from datetime import datetime, timedelta

import mistralai.workflows as workflows
from pydantic import BaseModel, ConfigDict

from .models import (
    BATCHED_PLATFORMS,
    CSV_HEADERS,
    DASHBOARD_HEADERS,
    VALID_PLATFORMS,
    BatchSearchParams,
    ProcessTargetParams,
    TargetInput,
    PerPlatformStatus,
)


# --------------------------------------------------------------------------
# Activités
# --------------------------------------------------------------------------

@workflows.activity(
    start_to_close_timeout=timedelta(minutes=3),
    retry_policy_max_attempts=3,
)
async def fetch_targets(sheet_name: str, inputs_tab: str) -> list[TargetInput]:
    """Lit l'onglet Inputs du Google Sheet et le résout en TargetInput."""
    from .sheets import find_spreadsheet_id, SheetsClient

    spreadsheet_id = await find_spreadsheet_id(sheet_name)
    sheets = SheetsClient(spreadsheet_id)
    rows = await sheets.read_inputs(inputs_tab)
    targets = []
    for i, row in enumerate(rows, start=2):
        if not row or not row[0].strip():
            continue
        target_id = row[0].strip() or f"row-{i}"
        name = row[1].strip() if len(row) > 1 else ""
        desc = row[2].strip() if len(row) > 2 else ""
        if name:
            targets.append(TargetInput(target_id=target_id, name=name, description=desc))
    return targets


@workflows.activity(
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy_max_attempts=3,
)
async def prepare_sheet(sheet_name: str) -> dict:
    """Crée les onglets results/dashboard avec en-têtes s'ils manquent (aucune recherche)."""
    import httpx
    from .sheets import SheetsClient, find_spreadsheet_id

    spreadsheet_id = await find_spreadsheet_id(sheet_name)
    sheets = SheetsClient(spreadsheet_id)
    async with httpx.AsyncClient(timeout=300) as client:
        results_tab = await sheets.ensure_tab(client, "Results", list(CSV_HEADERS))
        dashboard_tab = await sheets.ensure_tab(client, "Dashboard", list(DASHBOARD_HEADERS))
    return {"results_tab": results_tab, "dashboard_tab": dashboard_tab}


@workflows.activity(
    start_to_close_timeout=timedelta(minutes=2),
    retry_policy_max_attempts=3,
)
async def fetch_usage_usd() -> float:
    """Coût Apify total engagé ce mois-ci."""
    from .apify_client import get_usage_usd

    return await get_usage_usd()


@workflows.activity(
    start_to_close_timeout=timedelta(minutes=4),
    retry_policy_max_attempts=3,
)
async def search_platform(platform: str, query: str) -> dict:
    """Une recherche de profils sur une plateforme (max 10 résultats, 1 page)."""
    from .apify_client import search_profiles

    return await search_profiles(platform, query)


@workflows.activity(
    start_to_close_timeout=timedelta(minutes=40),
    retry_policy_max_attempts=2,
)
async def search_platform_batch(params: BatchSearchParams) -> dict:
    """Recherche par lot pour une plateforme batchée (mise par le parent).

    Retourne {rows:[{search_query...}], unattributed:int, missing_keywords:[...]}.
    Un lot en échec ne fait PAS échouer le run: il retourne failed=<erreur> et
    les cibles concernées basculent en repli individuel côté parent.
    """
    from .apify_client import search_profiles_batch

    try:
        return await search_profiles_batch(params.platform, params.keywords)
    except Exception as e:  # lot échoué -> repli individuel par cible
        return {
            "rows": [],
            "unattributed": 0,
            "missing_keywords": list(params.keywords),
            "failed": str(e)[:300],
        }


@workflows.activity(
    start_to_close_timeout=timedelta(minutes=2),
)
async def match_accounts(rows: list[dict], target: TargetInput) -> list[dict]:
    """Vérifie la pertinence de chaque compte et remplit la colonne relevant."""
    from .matching import is_relevant

    out = []
    for account in rows:
        account["relevant"] = "TRUE" if is_relevant(account, target) else "FALSE"
        out.append(account)
    return out


@workflows.activity(
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy_max_attempts=4,
)
async def append_results_rows(sheet_name: str, results_tab: str, rows: list[list]) -> None:
    """Écrit les résultats dans l'onglet résultats du Sheet."""
    import httpx
    from .sheets import SheetsClient, find_spreadsheet_id

    spreadsheet_id = await find_spreadsheet_id(sheet_name)
    sheets = SheetsClient(spreadsheet_id)
    async with httpx.AsyncClient(timeout=300) as client:
        await sheets.append_results(client, results_tab, rows)


@workflows.activity(
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy_max_attempts=4,
)
async def upsert_status(sheet_name: str, dashboard_tab: str, row: list) -> None:
    """Écrit une ligne d'état dans le dashboard du Sheet."""
    import httpx
    from .sheets import SheetsClient, find_spreadsheet_id

    spreadsheet_id = await find_spreadsheet_id(sheet_name)
    sheets = SheetsClient(spreadsheet_id)
    async with httpx.AsyncClient(timeout=300) as client:
        await sheets.upsert_dashboard(client, dashboard_tab, row)


# --------------------------------------------------------------------------
# Workflow enfant: traite une cible
# --------------------------------------------------------------------------

@workflows.workflow.define(
    name="process-target",
    workflow_display_name="Traiter une cible",
    workflow_description="Recherche, vérifie et enregistre les comptes sociaux d'une cible.",
)
class ProcessTargetWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, params: ProcessTargetParams) -> dict:
        t = params.target
        statuses: list[PerPlatformStatus] = []
        all_rows: list[list] = []

        for platform in params.platforms:
            # lignes pré-cherchées par le parent (lot youtube/tiktok): pas de recherche
            pre = None
            if params.precomputed_rows:
                pre = params.precomputed_rows.get(platform)
            if pre is not None:
                matched = await match_accounts(rows=pre, target=t)
                for acc in matched:
                    all_rows.append([acc.get(k, "") for k in CSV_HEADERS])
                n_rel = sum(1 for a in matched if a["relevant"] == "TRUE")
                statuses.append(PerPlatformStatus(
                    platform=platform, searched=True,
                    accounts_returned=len(matched), accounts_checked=len(matched),
                    accounts_relevant=n_rel, cost_usd=0.0,
                ))
                continue
            try:
                result = await search_platform(platform=platform, query=t.name)
                matched = await match_accounts(rows=result["rows"], target=t)
                for acc in matched:
                    all_rows.append([acc.get(k, "") for k in CSV_HEADERS])
                n_rel = sum(1 for a in matched if a["relevant"] == "TRUE")
                statuses.append(PerPlatformStatus(
                    platform=platform, searched=True,
                    accounts_returned=len(matched), accounts_checked=len(matched),
                    accounts_relevant=n_rel, cost_usd=0.0,
                ))
            except Exception as e:  # une plateforme en erreur ne bloque pas la cible
                statuses.append(PerPlatformStatus(
                    platform=platform, searched=False, error=str(e)[:200],
                ))

        if all_rows:
            await append_results_rows(
                sheet_name=params.sheet_name,
                results_tab="Results",
                rows=all_rows,
            )
        for st in statuses:
            await upsert_status(
                sheet_name=params.sheet_name,
                dashboard_tab="Dashboard",
                row=[
                    t.target_id, t.name, st.platform, st.searched,
                    st.accounts_returned, st.accounts_checked,
                    st.accounts_relevant, round(st.cost_usd, 4), st.error,
                    datetime.utcnow().isoformat(timespec="seconds") + "Z",
                ],
            )
        return {
            "target_id": t.target_id,
            "platforms": [s.model_dump() for s in statuses],
            "total_relevant": sum(s.accounts_relevant for s in statuses),
        }


# --------------------------------------------------------------------------
# Workflow parent: batch complet
# --------------------------------------------------------------------------

class ParentParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet_name: str
    inputs_tab: str = "Inputs"
    platforms: list[str]  # vide = mode préparation (création des onglets, aucune recherche)
    max_concurrent_targets: int = 5   # cibles (child workflows) en parallèle
    max_concurrent_batches: int = 3  # lots (youtube/tiktok) en parallèle
    batch_size: int = 100            # mots-clés par run de lot (youtube/tiktok)
    cleanup_platforms: list[str] = []  # plateformes dont on purge les lignes Results/Dashboard avant tout (ex: results erronés)
    skip_existing: bool = False      # ignorer les cibles déjà présentes dans le Dashboard
    max_targets: int = 0             # limite du nombre de cibles traitées (0 = toutes)


@workflows.activity(
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy_max_attempts=2,
)
async def cleanup_platform_rows(sheet_name: str, platforms: list[str]) -> dict:
    """Purge des lignes par plateforme dans Results et Dashboard (aucun appel Apify)."""
    import httpx
    from .sheets import SheetsClient, find_spreadsheet_id

    spreadsheet_id = await find_spreadsheet_id(sheet_name)
    sheets = SheetsClient(spreadsheet_id)
    summary: dict = {}
    async with httpx.AsyncClient(timeout=300) as client:
        for p in platforms:
            summary[f"results_{p}"] = await sheets.purge_platform_rows(
                client, "Results", list(CSV_HEADERS), p
            )
            summary[f"dashboard_{p}"] = await sheets.purge_platform_rows(
                client, "Dashboard", list(DASHBOARD_HEADERS), p
            )
    return summary


@workflows.activity(
    start_to_close_timeout=timedelta(minutes=3),
    retry_policy_max_attempts=3,
)
async def fetch_done_target_ids(sheet_name: str) -> list[str]:
    """target_id déjà présents dans le Dashboard (cibles déjà tentées)."""
    import httpx
    from .sheets import SheetsClient, find_spreadsheet_id

    spreadsheet_id = await find_spreadsheet_id(sheet_name)
    sheets = SheetsClient(spreadsheet_id)
    async with httpx.AsyncClient(timeout=300) as client:
        return await sheets.read_done_target_ids(client, "Dashboard")


@workflows.workflow.define(
    name="social-profile-search",
    workflow_display_name="Recherche de comptes sociaux (batch)",
    workflow_description=("Lit les cibles dans un Google Sheet, recherche les comptes "
                          "sur les plateformes via Apify (youtube/tiktok par lot), vérifie "
                          "la pertinence, écrit les résultats et le dashboard de suivi."),
)
class SocialProfileSearchWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, params: ParentParams) -> dict:
        cleanup_summary: dict | None = None
        if params.cleanup_platforms:
            cleanup_summary = await cleanup_platform_rows(
                sheet_name=params.sheet_name, platforms=params.cleanup_platforms
            )
        if not params.platforms:
            # Mode préparation: crée les onglets results/dashboard, aucun appel Apify.
            prepared = await prepare_sheet(sheet_name=params.sheet_name)
            return {
                "prepared": True,
                "results_tab": prepared["results_tab"],
                "dashboard_tab": prepared["dashboard_tab"],
                "cleanup": cleanup_summary,
                "message": "Onglets results/dashboard prêts. Aucune recherche lancée.",
            }
        invalid = [p for p in params.platforms if p not in VALID_PLATFORMS]
        if invalid:
            raise ValueError(f"Plateformes non supportées: {invalid}. "
                             f"Valides: {sorted(VALID_PLATFORMS)}")

        usage_start = await fetch_usage_usd()

        targets = await fetch_targets(
            sheet_name=params.sheet_name, inputs_tab=params.inputs_tab
        )
        if not targets:
            return {"processed": 0, "cleanup": cleanup_summary,
                    "message": "Aucune cible trouvée dans l'onglet Inputs"}

        # Onglets de sortie garantis présents avant le batch.
        await prepare_sheet(sheet_name=params.sheet_name)

        skipped_existing = 0
        if params.skip_existing:
            done = set(await fetch_done_target_ids(sheet_name=params.sheet_name))
            before = len(targets)
            targets = [t for t in targets if t.target_id not in done]
            skipped_existing = before - len(targets)
        if params.max_targets and params.max_targets > 0:
            if len(targets) > params.max_targets:
                targets = targets[:params.max_targets]
        if not targets:
            return {"processed": 0, "cleanup": cleanup_summary,
                    "skipped_existing": skipped_existing,
                    "message": "Toutes les cibles sont déjà traitées ou aucune restante."}

        # ------------------------------------------------------------------
        # Plateformes batchées (youtube, tiktok): recherche par lot + attribution
        # ------------------------------------------------------------------
        batched = [p for p in params.platforms if p in BATCHED_PLATFORMS]
        size = max(1, params.batch_size)
        batches = [targets[i:i + size] for i in range(0, len(targets), size)]

        # rows par plateforme, puis par nom de cible (search_query)
        precomputed: dict[str, dict[str, list[dict]]] = {p: {} for p in batched}
        batch_stats: dict[str, dict] = {}
        for platform in batched:
            items = [
                BatchSearchParams(platform=platform, keywords=[t.name for t in b])
                for b in batches
            ]
            results = await workflows.execute_activities_in_parallel(
                activity=search_platform_batch,
                items=items,
                max_concurrent_scheduled_tasks=max(1, params.max_concurrent_batches),
            )
            stats = {"batches": len(batches), "batch_failed": 0,
                     "unattributed": 0, "fallback_targets": 0}
            for b, res in zip(batches, results):
                if res is None or res.get("failed"):
                    # lot échoué: ses cibles seront comptées en repli par la boucle finale
                    stats["batch_failed"] += 1
                    continue
                stats["unattributed"] += res.get("unattributed", 0)
                for row in res.get("rows", []):
                    precomputed[platform].setdefault(row["search_query"], []).append(row)
            # cibles sans aucune ligne attribuée (lot échoué inclus) -> repli individuel
            for t in targets:
                if t.name not in precomputed[platform]:
                    stats["fallback_targets"] += 1
            batch_stats[platform] = stats

        # ------------------------------------------------------------------
        # Plateformes: enfant par cible (lignes de lot transmises si dispo)
        # ------------------------------------------------------------------
        child_items = [
            ProcessTargetParams(
                target=t,
                sheet_name=params.sheet_name,
                platforms=params.platforms,
                precomputed_rows={
                    p: precomputed[p][t.name]
                    for p in batched
                    if t.name in precomputed[p]
                } or None,
            )
            for t in targets
        ]
        results = await workflows.execute_activities_in_parallel(
            activity=run_target_child,
            items=child_items,
            max_concurrent_scheduled_tasks=max(1, params.max_concurrent_targets),
        )

        usage_end = await fetch_usage_usd()
        total_relevant = sum(
            (r or {}).get("total_relevant", 0) for r in results if r
        )
        failures = sum(1 for r in results if r is None)
        return {
            "targets": len(targets),
            "processed": len(results) - failures,
            "failed": failures,
            "accounts_relevant_total": total_relevant,
            "apify_usage_usd": round(usage_end - usage_start, 4),
            "batch_stats": batch_stats,
            "cleanup": cleanup_summary,
            "skipped_existing": skipped_existing,
        }


@workflows.activity(
    start_to_close_timeout=timedelta(hours=2),
    retry_policy_max_attempts=2,
)
async def run_target_child(params: ProcessTargetParams) -> dict:
    """Adaptateur (activité): exécute le workflow enfant pour une cible."""
    return await workflows.execute_workflow(
        ProcessTargetWorkflow, params=params
    )
