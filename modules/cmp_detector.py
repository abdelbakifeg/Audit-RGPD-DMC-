"""
Module 1 v2 — Détecteur de CMP
================================
Version robuste avec extraction UUID améliorée
et logs de debug intégrés.
"""

import re
import logging
import httpx
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class CMPType(str, Enum):
    DIDOMI       = "didomi"
    ONETRUST     = "onetrust"
    AXEPTIO      = "axeptio"
    TARTEAUCITRON= "tarteaucitron"
    COOKIEBOT    = "cookiebot"
    UNKNOWN      = "unknown"
    NONE         = "none"


@dataclass
class CMPResult:
    cmp_type:     CMPType
    api_key:      Optional[str]
    api_endpoint: Optional[str]
    confidence:   str
    raw_signal:   str


CMP_SIGNATURES = {
    CMPType.DIDOMI: [
        (r'sdk\.privacy-center\.org', "high"),
        (r'didomi\.io',               "high"),
        (r'window\.didomiConfig',     "high"),
        (r'didomi-notice',            "medium"),
        (r'didomi',                   "low"),
    ],
    CMPType.ONETRUST: [
        (r'cdn\.cookielaw\.org',      "high"),
        (r'optanon',                  "high"),
        (r'onetrust',                 "high"),
    ],
    CMPType.AXEPTIO: [
        (r'axept\.io',                "high"),
        (r'axeptio',                  "high"),
    ],
    CMPType.TARTEAUCITRON: [
        (r'tarteaucitron\.js',        "high"),
        (r'tarteaucitron',            "medium"),
    ],
    CMPType.COOKIEBOT: [
        (r'cookiebot\.com',           "high"),
        (r'CookieConsent',            "medium"),
    ],
}

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

# Pattern UUID standard 8-4-4-4-12
UUID_PATTERN = r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'


def _extract_didomi_key(html: str) -> Optional[str]:
    """
    Extrait la clé API Didomi avec plusieurs stratégies.
    """
    # Stratégie 1 : dans l'URL du script sdk.privacy-center.org
    match = re.search(
        r'sdk\.privacy-center\.org/(' + UUID_PATTERN + r')',
        html, re.IGNORECASE
    )
    if match:
        logging.warning(f"[CMP] Clé Didomi trouvée via sdk URL : {match.group(1)}")
        return match.group(1)

    # Stratégie 2 : dans window.didomiConfig
    match2 = re.search(
        r'didomiConfig\s*[=:][^{]*{[^}]*apiKey["\s:\']+(' + UUID_PATTERN + r')',
        html, re.IGNORECASE | re.DOTALL
    )
    if match2:
        logging.warning(f"[CMP] Clé Didomi trouvée via didomiConfig : {match2.group(1)}")
        return match2.group(1)

    # Stratégie 3 : apiKey générique
    match3 = re.search(
        r'["\']?apiKey["\']?\s*[:=]\s*["\'](' + UUID_PATTERN + r')["\']',
        html, re.IGNORECASE
    )
    if match3:
        logging.warning(f"[CMP] Clé Didomi trouvée via apiKey : {match3.group(1)}")
        return match3.group(1)

    # Stratégie 4 : chercher tous les UUIDs proches de "didomi"
    didomi_section = re.search(
        r'didomi.{0,500}(' + UUID_PATTERN + r')',
        html, re.IGNORECASE | re.DOTALL
    )
    if didomi_section:
        logging.warning(f"[CMP] Clé Didomi trouvée via proximité : {didomi_section.group(1)}")
        return didomi_section.group(1)

    logging.warning("[CMP] Aucune clé Didomi trouvée")
    return None


def _extract_onetrust_key(html: str) -> Optional[str]:
    match = re.search(
        r'cdn\.cookielaw\.org/consent/(' + UUID_PATTERN + r')',
        html, re.IGNORECASE
    )
    return match.group(1) if match else None


def _build_api_endpoint(cmp_type: CMPType, api_key: Optional[str]) -> Optional[str]:
    if cmp_type == CMPType.DIDOMI and api_key:
        return f"https://sdk.privacy-center.org/config/production/{api_key}/latest.json"
    if cmp_type == CMPType.ONETRUST and api_key:
        return f"https://cdn.cookielaw.org/consent/{api_key}/v2/en.json"
    return None


def detect_cmp(html: str) -> CMPResult:
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
                    best_signal = f"pattern '{pattern}' : '{match.group(0)}'"
                break

    if best_cmp == CMPType.NONE and len(html) > 500:
        best_cmp    = CMPType.UNKNOWN
        best_conf   = "high"
        best_signal = "page chargée, aucun CMP reconnu"

    api_key      = None
    api_endpoint = None

    if best_cmp == CMPType.DIDOMI:
        api_key      = _extract_didomi_key(html)
        api_endpoint = _build_api_endpoint(CMPType.DIDOMI, api_key)
        logging.warning(f"[CMP] Didomi détecté | clé={api_key} | endpoint={api_endpoint}")

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
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

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

    logging.warning(f"[CMP] HTML chargé : {len(html):,} chars pour {url}")
    result = detect_cmp(html)
    logging.warning(f"[CMP] Résultat détection : {result.cmp_type.value} | confiance={result.confidence}")
    return result, html
