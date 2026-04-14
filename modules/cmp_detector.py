"""
Module 1 — Détecteur de CMP
============================
Identifie quel Consent Management Platform est présent sur une page.
Stratégie : analyse du HTML brut + signatures connues.
Pas de navigateur headless ici — juste un fetch HTTP rapide.
Retourne le type de CMP et les métadonnées nécessaires pour la suite.
"""

import re
import httpx
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class CMPType(str, Enum):
    DIDOMI    = "didomi"
    ONETRUST  = "onetrust"
    AXEPTIO   = "axeptio"
    TARTEAUCITRON = "tarteaucitron"
    COOKIEBOT = "cookiebot"
    UNKNOWN   = "unknown"
    NONE      = "none"


@dataclass
class CMPResult:
    cmp_type: CMPType
    api_key: Optional[str]       # clé/ID pour appeler l'API directe du CMP
    api_endpoint: Optional[str]  # URL de l'API JSON directe
    confidence: str              # "high" | "medium" | "low"
    raw_signal: str              # ce qui a permis la détection (pour logs/debug)


# Signatures de détection par CMP
# Chaque entrée : (pattern à chercher dans le HTML, niveau de confiance)
CMP_SIGNATURES = {
    CMPType.DIDOMI: [
        (r'sdk\.privacy-center\.org/([a-f0-9-]{36})', "high"),
        (r'didomi\.io',                                 "high"),
        (r'window\.didomiConfig',                       "high"),
        (r'didomi-notice',                              "medium"),
        (r'didomi',                                     "low"),
    ],
    CMPType.ONETRUST: [
        (r'cdn\.cookielaw\.org/consent/([a-f0-9-]+)',  "high"),
        (r'optanon',                                    "high"),
        (r'onetrust',                                   "high"),
        (r'OneTrust',                                   "medium"),
    ],
    CMPType.AXEPTIO: [
        (r'axept\.io',                                  "high"),
        (r'axeptio',                                    "high"),
        (r'window\._axcb',                              "high"),
    ],
    CMPType.TARTEAUCITRON: [
        (r'tarteaucitron\.js',                          "high"),
        (r'tarteaucitron',                              "medium"),
    ],
    CMPType.COOKIEBOT: [
        (r'cookiebot\.com',                             "high"),
        (r'CookieConsent',                              "medium"),
    ],
}

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _extract_didomi_key(html: str) -> Optional[str]:
    """
    Extrait la clé API Didomi depuis le HTML.
    Format attendu dans le src du script :
    https://sdk.privacy-center.org/{UUID-36-chars}/loader.js
    """
    match = re.search(
        r'sdk\.privacy-center\.org/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})',
        html
    )
    if match:
        return match.group(1)

    # Fallback : clé dans window.didomiConfig
    match2 = re.search(r'apiKey["\s:]+["\']([a-f0-9-]{36})["\']', html)
    if match2:
        return match2.group(1)

    return None


def _extract_onetrust_key(html: str) -> Optional[str]:
    """
    Extrait l'ID OneTrust depuis le script src.
    Format : cdn.cookielaw.org/consent/{UUID}/OtAutoBlock.js
    """
    match = re.search(
        r'cdn\.cookielaw\.org/consent/([a-f0-9-]{36})',
        html
    )
    return match.group(1) if match else None


def _build_api_endpoint(cmp_type: CMPType, api_key: Optional[str]) -> Optional[str]:
    """Construit l'URL de l'API JSON directe selon le CMP détecté."""
    if cmp_type == CMPType.DIDOMI and api_key:
        return f"https://sdk.privacy-center.org/config/production/{api_key}/latest.json"
    if cmp_type == CMPType.ONETRUST and api_key:
        return f"https://cdn.cookielaw.org/consent/{api_key}/v2/en.json"
    return None


def detect_cmp(html: str) -> CMPResult:
    """
    Analyse le HTML d'une page et retourne le CMP détecté.
    Logique : on cherche toutes les signatures, on garde celle
    avec le niveau de confiance le plus élevé.
    """
    best_cmp    = CMPType.NONE
    best_conf   = "low"
    best_signal = "aucun signal trouvé"

    for cmp_type, signatures in CMP_SIGNATURES.items():
        for pattern, confidence in signatures:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                if CONFIDENCE_RANK.get(confidence, 0) > CONFIDENCE_RANK.get(best_conf, 0):
                    best_cmp    = cmp_type
                    best_conf   = confidence
                    best_signal = f"pattern '{pattern}' trouvé : '{match.group(0)}'"
                break  # on prend la meilleure signature par CMP, on passe au suivant

    # Si rien de connu → UNKNOWN (page existe mais CMP non reconnu)
    if best_cmp == CMPType.NONE and len(html) > 500:
        best_cmp    = CMPType.UNKNOWN
        best_conf   = "high"
        best_signal = "page chargée mais aucun CMP reconnu"

    # Extraction des clés API selon le CMP
    api_key      = None
    api_endpoint = None

    if best_cmp == CMPType.DIDOMI:
        api_key      = _extract_didomi_key(html)
        api_endpoint = _build_api_endpoint(CMPType.DIDOMI, api_key)

    elif best_cmp == CMPType.ONETRUST:
        api_key      = _extract_onetrust_key(html)
        api_endpoint = _build_api_endpoint(CMPType.ONETRUST, api_key)

    return CMPResult(
        cmp_type     = best_cmp,
        api_key      = api_key,
        api_endpoint = api_endpoint,
        confidence   = best_conf,
        raw_signal   = best_signal,
    )


async def fetch_and_detect(url: str, timeout: int = 15) -> tuple[CMPResult, str]:
    """
    Fetch HTTP de l'URL puis détection du CMP.
    Retourne (CMPResult, html_brut).
    Gère les redirections et les erreurs proprement.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    # On s'assure que l'URL a un schéma
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers=headers
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        html = response.text

    result = detect_cmp(html)
    return result, html
