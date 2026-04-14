"""
Module 4 — Orchestrateur
==========================
Point d'entrée unique pour auditer une URL.
Assemble M1 + M2 + M3 et décide automatiquement
quelle route prendre selon le CMP détecté.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
from enum import Enum

from modules.cmp_detector   import fetch_and_detect, CMPType, CMPResult
from modules.didomi_extractor import extract_didomi, DidomiResult
from modules.playwright_navigator import navigate_and_extract, PlaywrightResult


# ─── Statuts possibles pour une URL ──────────────────────────────────────────

class AuditStatus(str, Enum):
    CONFORME      = "conforme"
    NON_CONFORME  = "non_conforme"
    A_VERIFIER    = "a_verifier"
    ERREUR        = "erreur"
    TIMEOUT       = "timeout"


class AuditRoute(str, Enum):
    API_DIDOMI   = "api_didomi"
    API_ONETRUST = "api_onetrust"
    PLAYWRIGHT   = "playwright"
    FETCH_ONLY   = "fetch_only"
    ECHEC        = "echec"


# ─── Résultat unifié ──────────────────────────────────────────────────────────

@dataclass
class AuditResult:
    url: str
    audited_at: str
    status: AuditStatus
    route: AuditRoute
    dekuple_found: bool = False
    dekuple_locations: list[str] = field(default_factory=list)
    dekuple_name: Optional[str] = None
    dekuple_policy_url: Optional[str] = None
    partners_total: int = 0
    partners_list: list[str] = field(default_factory=list)
    cmp_type: str = "unknown"
    cmp_api_key: Optional[str] = None
    screenshots: list[str] = field(default_factory=list)
    links_analyzed: list[str] = field(default_factory=list)
    text_extract: str = ""
    duration_seconds: float = 0.0
    error: Optional[str] = None
    notes: str = ""


# ─── Logique de décision statut ───────────────────────────────────────────────

def _determine_status(
    dekuple_found: bool,
    route: AuditRoute,
    error: Optional[str],
    cmp_type: CMPType,
) -> AuditStatus:
    if error or route == AuditRoute.ECHEC:
        return AuditStatus.ERREUR
    if dekuple_found:
        return AuditStatus.CONFORME
    if route in (AuditRoute.API_DIDOMI, AuditRoute.API_ONETRUST):
        return AuditStatus.NON_CONFORME
    if route == AuditRoute.PLAYWRIGHT:
        return AuditStatus.NON_CONFORME
    return AuditStatus.A_VERIFIER


# ─── Routes ───────────────────────────────────────────────────────────────────

async def _route_api_didomi(cmp: CMPResult, url: str) -> tuple[AuditRoute, dict]:
    logging.warning(f"[AUDIT] Route Didomi API pour {url}")
    result: DidomiResult = await extract_didomi(cmp.api_endpoint)

    if not result.success:
        logging.warning(f"[AUDIT] Didomi API échouée : {result.error} — fallback Playwright")
        return AuditRoute.PLAYWRIGHT, {"fallback_reason": result.error}

    partners_names = [p.name for p in result.partners]
    logging.warning(f"[AUDIT] Didomi OK : {result.total_partners} partenaires | Dékuple: {result.dekuple_found}")

    return AuditRoute.API_DIDOMI, {
        "dekuple_found":      result.dekuple_found,
        "dekuple_name":       result.dekuple_partner.name if result.dekuple_partner else None,
        "dekuple_policy_url": result.dekuple_partner.privacy_policy_url if result.dekuple_partner else None,
        "dekuple_locations":  ["liste_partenaires_cmp"] if result.dekuple_found else [],
        "partners_total":     result.total_partners,
        "partners_list":      partners_names,
        "text_extract":       f"Partenaires CMP : {', '.join(partners_names[:10])}",
        "error":              None,
        "notes":              f"API Didomi — {result.total_partners} partenaires analysés",
    }


async def _route_playwright(url: str) -> tuple[AuditRoute, dict]:
    logging.warning(f"[AUDIT] Route Playwright/fetch pour {url}")
    result: PlaywrightResult = await navigate_and_extract(url)

    if not result.success:
        logging.warning(f"[AUDIT] Navigation échouée : {result.error}")
        return AuditRoute.ECHEC, {
            "error": result.error,
            "dekuple_found": False,
        }

    screenshots = [s.path for s in result.screenshots]
    text_preview = result.all_text[:500] if result.all_text else ""
    logging.warning(f"[AUDIT] Navigation OK : Dékuple={result.dekuple_found} | texte={len(result.all_text)} chars | liens={len(result.links_followed)}")

    notes = ""
    if not result.dekuple_found:
        zones = [c.source for c in result.contents]
        notes = f"Zones analysées : {', '.join(set(zones))}. Liens suivis : {len(result.links_followed)}."

    return AuditRoute.PLAYWRIGHT, {
        "dekuple_found":      result.dekuple_found,
        "dekuple_name":       "Dékuple DMC" if result.dekuple_found else None,
        "dekuple_policy_url": None,
        "dekuple_locations":  result.dekuple_locations,
        "partners_total":     0,
        "partners_list":      [],
        "screenshots":        screenshots,
        "links_analyzed":     result.links_followed,
        "text_extract":       text_preview,
        "error":              None,
        "notes":              notes,
    }


# ─── Point d'entrée principal ─────────────────────────────────────────────────

async def audit_url(url: str) -> AuditResult:
    start      = time.time()
    audited_at = datetime.now().isoformat()

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    logging.warning(f"[AUDIT] ━━━ Démarrage audit : {url}")

    result = AuditResult(
        url=url,
        audited_at=audited_at,
        status=AuditStatus.A_VERIFIER,
        route=AuditRoute.ECHEC,
    )

    try:
        # ── Étape 1 : détection CMP ────────────────────────────────────────
        logging.warning(f"[AUDIT] Étape 1 — Détection CMP : {url}")
        cmp, html = await fetch_and_detect(url)
        logging.warning(f"[AUDIT] CMP détecté : {cmp.cmp_type.value} | clé API : {cmp.api_key or 'none'}")

        result.cmp_type    = cmp.cmp_type.value
        result.cmp_api_key = cmp.api_key

        # ── Étape 2 : choix de la route ────────────────────────────────────
        logging.warning(f"[AUDIT] Étape 2 — Choix de route")
        data  = {}
        route = AuditRoute.ECHEC

        if cmp.cmp_type == CMPType.DIDOMI and cmp.api_endpoint:
            route, data = await _route_api_didomi(cmp, url)
            if route == AuditRoute.PLAYWRIGHT:
                route, data = await _route_playwright(url)

        elif cmp.cmp_type == CMPType.ONETRUST and cmp.api_endpoint:
            route, data = await _route_playwright(url)
            result.notes = "OneTrust détecté — analyse via fetch"

        else:
            route, data = await _route_playwright(url)

        # ── Étape 3 : remplissage résultat ─────────────────────────────────
        result.route             = route
        result.dekuple_found     = data.get("dekuple_found", False)
        result.dekuple_name      = data.get("dekuple_name")
        result.dekuple_policy_url= data.get("dekuple_policy_url")
        result.dekuple_locations = data.get("dekuple_locations", [])
        result.partners_total    = data.get("partners_total", 0)
        result.partners_list     = data.get("partners_list", [])
        result.screenshots       = data.get("screenshots", [])
        result.links_analyzed    = data.get("links_analyzed", [])
        result.text_extract      = data.get("text_extract", "")
        result.error             = data.get("error")
        if data.get("notes"):
            result.notes = data["notes"]

        # ── Étape 4 : statut final ─────────────────────────────────────────
        result.status = _determine_status(
            result.dekuple_found,
            result.route,
            result.error,
            cmp.cmp_type,
        )
        logging.warning(f"[AUDIT] ✅ Terminé : {url} → {result.status.value} | Dékuple={result.dekuple_found}")

    except Exception as e:
        logging.warning(f"[AUDIT] ❌ EXCEPTION sur {url} : {type(e).__name__} — {e}")
        import traceback
        logging.warning(f"[AUDIT] Traceback : {traceback.format_exc()}")
        result.status = AuditStatus.ERREUR
        result.route  = AuditRoute.ECHEC
        result.error  = f"{type(e).__name__} — {e}"

    result.duration_seconds = round(time.time() - start, 2)
    logging.warning(f"[AUDIT] Durée : {result.duration_seconds}s")
    return result


# ─── Audit en batch ───────────────────────────────────────────────────────────

async def audit_batch(
    urls: list[str],
    concurrency: int = 5,
    on_progress=None,
) -> list[AuditResult]:
    semaphore = asyncio.Semaphore(concurrency)
    results   = [None] * len(urls)
    total     = len(urls)

    async def _audit_one(index: int, url: str):
        async with semaphore:
            result = await audit_url(url)
            results[index] = result
            if on_progress:
                await on_progress(index + 1, total, result)
            return result

    await asyncio.gather(*[
        _audit_one(i, url) for i, url in enumerate(urls)
    ])
    return results


# ─── Formatage ────────────────────────────────────────────────────────────────

STATUS_ICONS = {
    AuditStatus.CONFORME:     "✅",
    AuditStatus.NON_CONFORME: "❌",
    AuditStatus.A_VERIFIER:   "⚠️",
    AuditStatus.ERREUR:       "🔴",
    AuditStatus.TIMEOUT:      "⏱️",
}

def format_result(r: AuditResult) -> str:
    icon = STATUS_ICONS.get(r.status, "?")
    lines = [
        f"{icon} {r.url}",
        f"   Statut    : {r.status.value}",
        f"   Route     : {r.route.value}",
        f"   CMP       : {r.cmp_type}",
        f"   Dékuple   : {'TROUVÉ' if r.dekuple_found else 'ABSENT'}",
    ]
    if r.dekuple_found:
        lines += [
            f"   Nom exact : {r.dekuple_name}",
            f"   Localisé  : {', '.join(r.dekuple_locations)}",
        ]
    if r.partners_total:
        lines.append(f"   Partenaires : {r.partners_total} analysés")
    if r.notes:
        lines.append(f"   Notes       : {r.notes}")
    if r.error:
        lines.append(f"   Erreur      : {r.error}")
    lines.append(f"   Durée       : {r.duration_seconds}s")
    return "\n".join(lines)


def format_batch_summary(results: list[AuditResult]) -> str:
    total         = len(results)
    conformes     = sum(1 for r in results if r.status == AuditStatus.CONFORME)
    non_conformes = sum(1 for r in results if r.status == AuditStatus.NON_CONFORME)
    a_verifier    = sum(1 for r in results if r.status == AuditStatus.A_VERIFIER)
    erreurs       = sum(1 for r in results if r.status == AuditStatus.ERREUR)

    lines = [
        "=" * 55,
        "SYNTHÈSE AUDIT RGPD — DÉKUPLE DMC",
        "=" * 55,
        f"Total URLs analysées : {total}",
        f"✅ Conformes          : {conformes} ({conformes*100//total if total else 0}%)",
        f"❌ Non conformes      : {non_conformes} ({non_conformes*100//total if total else 0}%)",
        f"⚠️  À vérifier        : {a_verifier}",
        f"🔴 Erreurs            : {erreurs}",
        "=" * 55,
    ]
    return "\n".join(lines)
