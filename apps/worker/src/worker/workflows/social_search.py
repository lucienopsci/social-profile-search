"""Workflows de recherche et vérification de comptes sociaux par lots.

- social-profile-search (parent): lit les cibles dans le Google Sheet, découpe en lots,
  traite chaque cible via le workflow enfant, tient le dashboard de suivi.
- process-target (enfant): recherche sur chaque plateforme via Apify, vérifie la
  pertinence, écrit les résultats et l'état par plateforme.
"""
from datetime import datetime, timedelta

import mistralai.workflows as workflows
from pydantic import BaseModel, ConfigDict

from .models import CSV_HEADERS, VALID_PLATFORMS, ProcessTargetParams, TargetInput, PerPlatformStatus

# Les modules réseau (httpx, apify_client, sheets) sont importés SOUS les activités:
# le sandbox de workflow interdit tout import de code d'E/S dans le code de workflow.


# --------------------------------------------------------------------------
# Activités
# --------------------------------------------------------------------------

@workflows.activity(
    start_to_close_timeout=timedelta(minutes=2),
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
    start_to_close_timeout=timedelta(minutes=2),
    retry_policy_max_attempts=3,
)
async def fetch_usage_usd() -> float:
    """Coût Apify total engagé ce mois-ci."""
    from .apify_client import get_usage_usd

    return await get_usage_usd()


@workflows.activity(
    start_to_close_timeout=timedelta(minutes=1),
    retry_policy_max_attempts=3,
)
async def search_platform(platform: str, query: str) -> dict:
    """Une recherche de profils sur une plateforme (max 10 résultats, 1 page)."""
    from .apify_client import search_profiles

    return await search_profiles(platform, query)


@workflows.activity(
    start_to_close_timeout=timedelta(minutes=1),
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
    start_to_close_timeout=timedelta(minutes=2),
    retry_policy_max_attempts=3,
)
async def append_results_rows(sheet_name: str, results_tab: str, rows: list[list]) -> None:
    """Écrit les résultats dans l'onglet résultats du Sheet."""
    import httpx
    from .sheets import SheetsClient, find_spreadsheet_id

    spreadsheet_id = await find_spreadsheet_id(sheet_name)
    sheets = SheetsClient(spreadsheet_id)
    async with httpx.AsyncClient(timeout=60) as client:
        await sheets.append_results(client, results_tab, rows)


@workflows.activity(
    start_to_close_timeout=timedelta(minutes=2),
    retry_policy_max_attempts=3,
)
async def upsert_status(sheet_name: str, dashboard_tab: str, row: list) -> None:
    """Écrit une ligne d'état dans le dashboard du Sheet."""
    import httpx
    from .sheets import SheetsClient, find_spreadsheet_id

    spreadsheet_id = await find_spreadsheet_id(sheet_name)
    sheets = SheetsClient(spreadsheet_id)
    async with httpx.AsyncClient(timeout=60) as client:
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
    platforms: list[str]
    max_concurrent_targets: int = 20


@workflows.workflow.define(
    name="social-profile-search",
    workflow_display_name="Recherche de comptes sociaux (batch)",
    workflow_description=("Lit les cibles dans un Google Sheet, recherche les comptes "
                          "sur les plateformes via Apify, vérifie la pertinence, "
                          "écrit les résultats et le dashboard de suivi."),
)
class SocialProfileSearchWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, params: ParentParams) -> dict:
        invalid = [p for p in params.platforms if p not in VALID_PLATFORMS]
        if invalid:
            raise ValueError(f"Plateformes non supportées: {invalid}. "
                             f"Valides: {sorted(VALID_PLATFORMS)}")
        if not params.platforms:
            raise ValueError("Aucune plateforme fournie: préciser au moins une plateforme.")

        usage_start = await fetch_usage_usd()

        targets = await fetch_targets(
            sheet_name=params.sheet_name, inputs_tab=params.inputs_tab
        )
        if not targets:
            return {"processed": 0, "message": "Aucune cible trouvée dans l'onglet Inputs"}

        # Exécution parallèle des enfants (auto continue-as-new au-delà des limites d'historique)
        child_items = [
            ProcessTargetParams(
                target=t,
                sheet_name=params.sheet_name,
                platforms=params.platforms,
            )
            for t in targets
        ]

        results = await workflows.execute_activities_in_parallel(
            activity=run_target_child,
            items=child_items,
            max_concurrent_scheduled_tasks=params.max_concurrent_targets,
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
        }


@workflows.activity(
    start_to_close_timeout=timedelta(hours=1),
    retry_policy_max_attempts=2,
)
async def run_target_child(params: ProcessTargetParams) -> dict:
    """Adaptateur (activité): exécute le workflow enfant pour une cible."""
    return await workflows.execute_workflow(
        ProcessTargetWorkflow, params=params
    )
