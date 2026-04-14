"""
Module 4 — Orchestrateur
==========================
Point d'entrée unique pour auditer une URL.
Assemble M1 + M2 + M3 et décide automatiquement
quelle route prendre selon le CMP détecté.

Pipeline de décision :
  URL → M1 détection CMP
       ├─ Didomi/OneTrust + clé API → M2 extraction directe
       ├─ Autre CMP connu           → M3 Playwright
       ├─ Aucun CMP                 → M3 Playwright
       └─ URL morte / erreur        → marqué ERREUR

Sortie : AuditResult unifié, indépendant de la route empruntée.
"""

import asyncio
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
    CONFORME      = "conforme"       # Dékuple trouvé au bon endroit
    NON_CONFORME  = "non_conforme"   # URL valide mais Dékuple absent
    A_VERIFIER    = "a_verifier"     # Résultat ambigu, vérification manuelle
    ERREUR        = "erreur"         # URL morte ou inaccessible
    TIMEOUT       = "timeout"        # Délai dépassé


class AuditRoute(str, Enum):
    API_DIDOMI   = "api_didomi"      # M2 — API JSON directe
    API_ONETRUST = "api_onetrust"    # M2 variante OneTrust
    PLAYWRIGHT   = "playwright"      # M3 — navigateur headless
    FETCH_ONLY   = "fetch_only"      # fetch statique suffit (HTML simple)
    ECHEC        = "echec"           # impossible d'accéder à l'URL


# ─── Résultat unifié ──────────────────────────────────────────────────────────

@dataclass
class AuditResult:
    # Identification
    url: str
    audited_at: str

    # Statut global
    status: AuditStatus
    route: AuditRoute

    # Dékuple DMC
    dekuple_found: bool = False
    dekuple_locations: list[str] = field(default_factory=list)
    dekuple_name: Optional[str] = None          # nom exact trouvé
    dekuple_policy_url: Optional[str] = None    # URL politique Dékuple

    # Partenaires (si CMP avec liste)
    partners_total: int = 0
    partners_list: list[str] = field(default_factory=list)

    # CMP détecté
    cmp_type: str = "unknown"
    cmp_api_key: Optional[str] = None

    # Preuves
    screenshots: list[str] = field(default_factory=list)
    links_analyzed: list[str] = field(default_factory=list)
    text_extract: str = ""            # extrait du texte analysé (500 chars)

    # Méta
    duration_seconds: float = 0.0
    error: Optional[str] = None
    notes: str = ""                   # observations pour vérification manuelle


# ─── Logique de décision statut ───────────────────────────────────────────────

def _determine_status(
    dekuple_found: bool,
    route: AuditRoute,
    error: Optional[str],
    cmp_type: CMPType,
) -> AuditStatus:
    """
    Règle métier pour le statut final.
    On est strict : si on ne peut pas conclure avec certitude → A_VERIFIER.
    """
    if error or route == AuditRoute.ECHEC:
        return AuditStatus.ERREUR

    if dekuple_found:
        return AuditStatus.CONFORME

    # Pas trouvé mais on a eu accès complet à la liste partenaires via API
    if route in (AuditRoute.API_DIDOMI, AuditRoute.API_ONETRUST):
        return AuditStatus.NON_CONFORME  # certitude : liste complète analysée

    # Pas trouvé via Playwright — possible que le contenu soit inaccessible
    if route == AuditRoute.PLAYWRIGHT:
        return AuditStatus.NON_CONFORME

    return AuditStatus.A_VERIFIER


# ─── Routes ───────────────────────────────────────────────────────────────────

async def _route_api_didomi(
    cmp: CMPResult,
    url: str,
) -> tuple[AuditRoute, dict]:
    """Route M2 : extraction via API JSON Didomi."""
    result: DidomiResult = await extract_didomi(cmp.api_endpoint)

    if not result.success:
        # Fallback vers Playwright si l'API échoue
        return AuditRoute.PLAYWRIGHT, {"fallback_reason": result.error}

    partners_names = [p.name for p in result.partners]

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
    """Route M3 : navigation Playwright complète."""
    result: PlaywrightResult = await navigate_and_extract(url)

    if not result.success:
        return AuditRoute.ECHEC, {
            "error": result.error,
            "dekuple_found": False,
        }

    screenshots = [s.path for s in result.screenshots]
    text_preview = result.all_text[:500] if result.all_text else ""

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
    """
    Audite une URL complète.
    Orchestre M1 → M2 ou M3 selon le CMP détecté.
    Retourne un AuditResult unifié.
    """
    start = time.time()
    audited_at = datetime.now().isoformat()

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    result = AuditResult(
        url=url,
        audited_at=audited_at,
        status=AuditStatus.A_VERIFIER,
        route=AuditRoute.ECHEC,
    )

    try:
        # ── Étape 1 : détection CMP (M1) ──────────────────────────────────
        cmp, html = await fetch_and_detect(url)
        result.cmp_type    = cmp.cmp_type.value
        result.cmp_api_key = cmp.api_key

        # ── Étape 2 : choix de la route ────────────────────────────────────
        data   = {}
        route  = AuditRoute.ECHEC

        if cmp.cmp_type == CMPType.DIDOMI and cmp.api_endpoint:
            route, data = await _route_api_didomi(cmp, url)
            # Si l'API a échoué → fallback Playwright
            if route == AuditRoute.PLAYWRIGHT:
                route, data = await _route_playwright(url)

        elif cmp.cmp_type == CMPType.ONETRUST and cmp.api_endpoint:
            # OneTrust — même logique que Didomi pour l'instant
            # TODO : implémenter extracteur OneTrust dédié (Module 2b)
            route, data = await _route_playwright(url)
            result.notes = "OneTrust détecté — analyse via Playwright (API OneTrust à implémenter)"

        else:
            # Tous les autres cas : Playwright
            route, data = await _route_playwright(url)

        # ── Étape 3 : remplissage du résultat unifié ───────────────────────
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

    except Exception as e:
        result.status = AuditStatus.ERREUR
        result.route  = AuditRoute.ECHEC
        result.error  = f"{type(e).__name__} — {e}"

    result.duration_seconds = round(time.time() - start, 2)
    return result


# ─── Audit en batch ───────────────────────────────────────────────────────────

async def audit_batch(
    urls: list[str],
    concurrency: int = 5,
    on_progress=None,       # callback(index, total, result) pour la progression
) -> list[AuditResult]:
    """
    Audite une liste d'URLs en parallèle avec contrôle de concurrence.
    concurrency : nombre d'URLs traitées simultanément (défaut 5).
    on_progress : callback appelé après chaque URL terminée.
    """
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
    if r.screenshots:
        lines.append(f"   Screenshots : {len(r.screenshots)} captures")
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

    if non_conformes:
        lines.append("\nURLs NON CONFORMES — action requise :")
        for r in results:
            if r.status == AuditStatus.NON_CONFORME:
                lines.append(f"  ❌ {r.url}")

    if a_verifier:
        lines.append("\nURLs À VÉRIFIER MANUELLEMENT :")
        for r in results:
            if r.status == AuditStatus.A_VERIFIER:
                lines.append(f"  ⚠️  {r.url} — {r.notes or r.error or ''}")

    return "\n".join(lines)
