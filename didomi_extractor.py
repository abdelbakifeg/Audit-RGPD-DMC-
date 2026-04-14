"""
Module 2 — Extracteur API Didomi
==================================
Interroge directement l'API JSON de Didomi pour récupérer
la liste complète des partenaires, sans iframe, sans scroll,
sans navigateur headless.

Entrée  : api_endpoint (fourni par Module 1)
Sortie  : DidomiResult avec liste complète des partenaires,
          présence Dékuple DMC, et données brutes pour audit.
"""

import httpx
import asyncio
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


# Toutes les variantes de noms à chercher pour Dékuple DMC
DEKUPLE_KEYWORDS = [
    "dékuple",
    "dekuple",
    "dékuple dmc",
    "dekuple dmc",
    "DMC",
    "d\u00e9kuple",   # unicode é
    "groupe d\u00e9kuple",
    "groupe dekuple",
]


@dataclass
class Partner:
    id: str
    name: str
    privacy_policy_url: Optional[str] = None
    is_dekuple: bool = False


@dataclass
class DidomiResult:
    success: bool
    api_endpoint: str
    partners: list[Partner] = field(default_factory=list)
    dekuple_found: bool = False
    dekuple_partner: Optional[Partner] = None
    total_partners: int = 0
    vendor_count: int = 0          # partenaires IAB TCF
    custom_vendor_count: int = 0   # partenaires custom (hors IAB)
    raw_purposes: list[str] = field(default_factory=list)
    fetched_at: str = ""
    error: Optional[str] = None


def _is_dekuple(name: str) -> bool:
    """Vérifie si un nom de partenaire correspond à Dékuple DMC."""
    name_lower = name.lower().strip()
    for keyword in DEKUPLE_KEYWORDS:
        if keyword.lower() in name_lower:
            return True
    return False


def _parse_partners(data: dict) -> list[Partner]:
    """
    Extrait tous les partenaires depuis la réponse JSON Didomi.
    Didomi structure les partenaires en deux listes :
    - vendors         : partenaires IAB TCF standard
    - custom_vendors  : partenaires propres à l'éditeur (co-reg, sponsors...)
    Dékuple DMC est quasi toujours dans custom_vendors.
    """
    partners = []

    # Partenaires IAB standard
    for v in data.get("vendors", {}).get("include", []):
        name = v.get("name", "") or v.get("id", "")
        partners.append(Partner(
            id=str(v.get("id", "")),
            name=name,
            privacy_policy_url=v.get("privacyPolicyUrl"),
            is_dekuple=_is_dekuple(name),
        ))

    # Partenaires custom — c'est ici qu'on trouve Dékuple DMC
    for v in data.get("custom_vendors", []):
        name = v.get("name", "") or v.get("id", "")
        partners.append(Partner(
            id=str(v.get("id", "")),
            name=name,
            privacy_policy_url=v.get("privacyPolicyUrl"),
            is_dekuple=_is_dekuple(name),
        ))

    # Certaines configs Didomi utilisent un format alternatif
    for v in data.get("app", {}).get("vendors", {}).get("include", []):
        if isinstance(v, dict):
            name = v.get("name", "") or str(v.get("id", ""))
            partners.append(Partner(
                id=str(v.get("id", "")),
                name=name,
                privacy_policy_url=v.get("privacyPolicyUrl"),
                is_dekuple=_is_dekuple(name),
            ))

    return partners


def _parse_purposes(data: dict) -> list[str]:
    """Extrait les finalités déclarées dans la config Didomi."""
    purposes = []
    for p in data.get("purposes", {}).get("include", []):
        if isinstance(p, dict):
            name = p.get("name", {})
            if isinstance(name, dict):
                purposes.append(name.get("fr") or name.get("en") or str(p.get("id", "")))
            else:
                purposes.append(str(name))
        elif isinstance(p, str):
            purposes.append(p)
    return purposes


async def extract_didomi(api_endpoint: str, timeout: int = 15) -> DidomiResult:
    """
    Appelle l'API JSON Didomi et extrait toutes les données.
    Point d'entrée principal du module.
    """
    result = DidomiResult(
        success=False,
        api_endpoint=api_endpoint,
        fetched_at=datetime.now().isoformat(),
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": "https://www.lecoindestesteurs.fr",  # requis par le CDN Didomi
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            response = await client.get(api_endpoint)
            response.raise_for_status()
            data = response.json()

        partners = _parse_partners(data)
        purposes = _parse_purposes(data)

        # Recherche Dékuple DMC
        dekuple_partner = next((p for p in partners if p.is_dekuple), None)

        # Comptage par type
        vendor_ids = {
            str(v.get("id", ""))
            for v in data.get("vendors", {}).get("include", [])
        }

        result.success             = True
        result.partners            = partners
        result.dekuple_found       = dekuple_partner is not None
        result.dekuple_partner     = dekuple_partner
        result.total_partners      = len(partners)
        result.vendor_count        = sum(1 for p in partners if p.id in vendor_ids)
        result.custom_vendor_count = sum(1 for p in partners if p.id not in vendor_ids)
        result.raw_purposes        = purposes

    except httpx.HTTPStatusError as e:
        result.error = f"HTTP {e.response.status_code} — {e.response.text[:200]}"
    except httpx.TimeoutException:
        result.error = f"Timeout après {timeout}s"
    except Exception as e:
        result.error = f"{type(e).__name__} — {e}"

    return result


def format_result(result: DidomiResult) -> str:
    """Formatage lisible pour logs et debug."""
    lines = [
        f"Endpoint    : {result.api_endpoint}",
        f"Succès      : {result.success}",
        f"Récupéré à  : {result.fetched_at}",
    ]

    if not result.success:
        lines.append(f"Erreur      : {result.error}")
        return "\n".join(lines)

    lines += [
        f"Partenaires : {result.total_partners} total "
        f"({result.vendor_count} IAB + {result.custom_vendor_count} custom)",
        f"Finalités   : {len(result.raw_purposes)}",
        "",
        f"DÉKUPLE DMC : {'✅ TROUVÉ' if result.dekuple_found else '❌ ABSENT'}",
    ]

    if result.dekuple_found and result.dekuple_partner:
        p = result.dekuple_partner
        lines += [
            f"  Nom        : {p.name}",
            f"  ID         : {p.id}",
            f"  Politique  : {p.privacy_policy_url or 'non renseignée'}",
        ]

    lines += ["", "LISTE COMPLÈTE DES PARTENAIRES :"]
    for i, p in enumerate(result.partners, 1):
        marker = " ← DÉKUPLE DMC" if p.is_dekuple else ""
        lines.append(f"  {i:3}. {p.name}{marker}")

    return "\n".join(lines)
