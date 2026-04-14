"""
Module 3 Cloud — Navigateur HTTP + Browserless
================================================
Version adaptée pour déploiement cloud (Render, Railway...).
Remplace Playwright par :
  1. fetch HTTP + BeautifulSoup pour les sites HTML statiques
  2. API Browserless pour les sites JS dynamiques
  
Browserless : https://browserless.io
  - Plan gratuit : 400 minutes/mois
  - Token à configurer dans la variable d'environnement BROWSERLESS_TOKEN

Entrée  : URL
Sortie  : NavigatorResult avec texte extrait + screenshots (base64)
"""

import re
import os
import base64
import asyncio
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup


BROWSERLESS_TOKEN = os.environ.get("BROWSERLESS_TOKEN", "")
BROWSERLESS_URL   = f"https://chrome.browserless.io/content?token={BROWSERLESS_TOKEN}"
SCREENSHOT_URL    = f"https://chrome.browserless.io/screenshot?token={BROWSERLESS_TOKEN}"
SCREENSHOT_DIR    = Path("screenshots")

DEKUPLE_KEYWORDS = [
    "dékuple", "dekuple", "dékuple dmc", "dekuple dmc",
    "groupe dékuple", "groupe dekuple",
]

DEKUPLE_PATTERN = re.compile(r"(?<![a-zA-Z])dmc(?![a-zA-Z])", re.IGNORECASE)

LEGAL_PATTERNS = re.compile(
    r"mention|legal|rgpd|privacy|donn|politique|cgu|conditions|partenaire",
    re.IGNORECASE
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass
class ExtractedContent:
    source: str
    text: str
    url: str
    selector: str = ""


@dataclass
class ScreenshotRecord:
    step: str
    path: str
    timestamp: str


@dataclass
class PlaywrightResult:
    """Même interface que le Module 3 original pour compatibilité."""
    success: bool
    url: str
    final_url: str
    contents: list = field(default_factory=list)
    screenshots: list = field(default_factory=list)
    links_followed: list = field(default_factory=list)
    dekuple_found: bool = False
    dekuple_locations: list = field(default_factory=list)
    all_text: str = ""
    error: Optional[str] = None
    fetched_at: str = ""


def _contains_dekuple(text: str) -> bool:
    text_lower = text.lower()
    if any(kw in text_lower for kw in DEKUPLE_KEYWORDS):
        return True
    if DEKUPLE_PATTERN.search(text):
        return True
    return False


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_text_bs(html: str) -> str:
    """Extrait le texte visible d'un HTML avec BeautifulSoup."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "meta", "head"]):
        tag.decompose()
    return _clean_text(soup.get_text(separator=" "))


def _extract_legal_links(html: str, base_url: str) -> list[str]:
    """Extrait les liens légaux d'une page HTML."""
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        if LEGAL_PATTERNS.search(href) or LEGAL_PATTERNS.search(text):
            if href.startswith("http"):
                links.append(href)
            elif href.startswith("/"):
                # Construire URL absolue
                from urllib.parse import urlparse
                parsed = urlparse(base_url)
                links.append(f"{parsed.scheme}://{parsed.netloc}{href}")
    return list(dict.fromkeys(links))[:6]


async def _fetch_html(url: str, timeout: int = 15) -> tuple[str, str]:
    """
    Fetch HTTP simple. Retourne (html, url_finale).
    Essaie d'abord en statique, puis Browserless si JS requis.
    """
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers=HEADERS,
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text, str(r.url)


async def _fetch_with_browserless(url: str) -> tuple[str, str]:
    """
    Charge la page avec Browserless (JS exécuté).
    Retourne (html_rendu, url_finale).
    """
    if not BROWSERLESS_TOKEN:
        raise ValueError("BROWSERLESS_TOKEN non configuré")

    payload = {
        "url": url,
        "waitFor": 2000,  # attendre 2s pour le JS
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            BROWSERLESS_URL,
            json=payload,
            headers={"Cache-Control": "no-cache"},
        )
        r.raise_for_status()
        return r.text, url


async def _take_screenshot_browserless(url: str) -> Optional[str]:
    """
    Prend un screenshot via Browserless.
    Retourne le chemin du fichier PNG sauvegardé.
    """
    if not BROWSERLESS_TOKEN:
        return None

    try:
        payload = {
            "url": url,
            "options": {
                "fullPage": False,
                "type": "png",
            },
            "waitFor": 2000,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                SCREENSHOT_URL,
                json=payload,
                headers={"Cache-Control": "no-cache"},
            )
            r.raise_for_status()

            # Sauvegarder le PNG
            SCREENSHOT_DIR.mkdir(exist_ok=True)
            domain    = re.sub(r"[^\w]", "_", url.split("//")[-1].split("/")[0])
            ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename  = f"{domain}_{ts}.png"
            filepath  = str(SCREENSHOT_DIR / filename)

            with open(filepath, "wb") as f:
                f.write(r.content)

            return filepath

    except Exception:
        return None


def _needs_js(html: str) -> bool:
    """
    Détecte si la page nécessite JS pour afficher son contenu.
    Heuristique : peu de texte visible mais beaucoup de JS.
    """
    text = _extract_text_bs(html)
    has_js_framework = any(
        kw in html.lower()
        for kw in ["react", "vue", "angular", "__next", "nuxt", "gatsby"]
    )
    # Si moins de 200 chars de texte visible et framework JS détecté
    return len(text) < 200 and has_js_framework


async def navigate_and_extract(
    url: str,
    follow_legal_links: bool = True,
) -> PlaywrightResult:
    """
    Point d'entrée principal — compatible avec Module 3 original.
    Stratégie :
      1. Fetch HTTP statique
      2. Si page JS → Browserless
      3. Extraction texte + liens légaux
      4. Screenshot via Browserless
      5. Détection Dékuple
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    result = PlaywrightResult(
        success=False,
        url=url,
        final_url=url,
        fetched_at=datetime.now().isoformat(),
    )

    try:
        # ── Étape 1 : Fetch principal ──────────────────────────────
        try:
            html, final_url = await _fetch_html(url)
        except Exception as e:
            result.error = f"Fetch échoué : {e}"
            return result

        result.final_url = final_url

        # ── Étape 2 : Browserless si JS requis ────────────────────
        if _needs_js(html) and BROWSERLESS_TOKEN:
            try:
                html, _ = await _fetch_with_browserless(url)
            except Exception:
                pass  # On garde le HTML statique

        # ── Étape 3 : Extraction texte principal ───────────────────
        main_text = _extract_text_bs(html)
        result.contents.append(ExtractedContent(
            source="main",
            text=main_text,
            url=final_url,
        ))

        # ── Étape 4 : Screenshot ───────────────────────────────────
        shot_path = await _take_screenshot_browserless(url)
        if shot_path:
            result.screenshots.append(ScreenshotRecord(
                step="initial",
                path=shot_path,
                timestamp=datetime.now().strftime("%Y%m%d_%H%M%S"),
            ))

        # ── Étape 5 : Liens légaux ─────────────────────────────────
        if follow_legal_links:
            legal_links = _extract_legal_links(html, final_url)

            for link in legal_links:
                if link == final_url:
                    continue
                try:
                    link_html, _ = await _fetch_html(link, timeout=10)

                    # Essayer de lire les PDFs aussi
                    if link.lower().endswith(".pdf"):
                        try:
                            async with httpx.AsyncClient(timeout=15) as client:
                                r = await client.get(link, headers=HEADERS)
                                # Extraire le texte du PDF
                                import io
                                from pypdf import PdfReader
                                reader = PdfReader(io.BytesIO(r.content))
                                pdf_text = " ".join(
                                    page.extract_text() or ""
                                    for page in reader.pages
                                )
                                result.contents.append(ExtractedContent(
                                    source="legal_page",
                                    text=_clean_text(pdf_text),
                                    url=link,
                                ))
                                result.links_followed.append(link)
                                continue
                        except Exception:
                            pass

                    link_text = _extract_text_bs(link_html)
                    if len(link_text) > 50:
                        result.contents.append(ExtractedContent(
                            source="legal_page",
                            text=link_text,
                            url=link,
                        ))
                        result.links_followed.append(link)

                except Exception:
                    continue

        # ── Étape 6 : Détection Dékuple ───────────────────────────
        all_texts = [c.text for c in result.contents if c.text]
        result.all_text = " | ".join(all_texts)

        for content in result.contents:
            if _contains_dekuple(content.text):
                result.dekuple_found = True
                result.dekuple_locations.append(
                    f"{content.source} ({content.url})"
                )

        result.success = True

    except Exception as e:
        result.error = f"{type(e).__name__} — {e}"

    return result


def format_result(result: PlaywrightResult) -> str:
    lines = [
        f"URL          : {result.url}",
        f"Succès       : {result.success}",
        f"Dékuple      : {'✅ TROUVÉ' if result.dekuple_found else '❌ ABSENT'}",
        f"Zones        : {len(result.contents)}",
        f"Liens suivis : {len(result.links_followed)}",
        f"Screenshots  : {len(result.screenshots)}",
    ]
    if result.error:
        lines.append(f"Erreur       : {result.error}")
    return "\n".join(lines)
