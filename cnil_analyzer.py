"""
Module 4b — Analyseur CNIL
============================
Analyse le texte extrait par M3 contre les critères CNIL
du questionnaire de conformité Dékuple DMC (Anne-Laure).

Structure du questionnaire :
  Bloc A — Consentement ÉCLAIRÉ (3 niveaux)
    A1 : Mention d'information sur le formulaire
    A2 : Lien vers liste des partenaires sur le formulaire
    A3 : Dékuple DMC dans la liste partenaires (affichage direct)
    A4 : Dékuple DMC via lien vers liste partenaires
    A5 : Lien vers politique Dékuple DMC fonctionnel
    A6 : Possibilité d'accepter/refuser chaque partenaire
    A7 : Politique du primo-collectant accessible en footer
    A8 : Politique contient toutes les infos RGPD art.13
    A9 : Politique précise les canaux de prospection

  Bloc B — Consentement SPÉCIFIQUE
    B1 : Opt-in tout canal (email + postal + tel)
    B2 : Opt-in email/SMS uniquement
    B3 : Opt-out postal et tel avec case à cocher
    B4 : Catégories d'activités des annonceurs précisées

  Bloc C — Consentement UNIVOQUE
    C1 : Bouton de refus effectif
    C2 : Boutons acceptation/refus de même couleur
    C3 : Boutons acceptation/refus de même taille
    C4 : Mention visible par rapport aux boutons

Entrée  : PlaywrightResult (M3) + DidomiResult (M2) si disponible
Sortie  : CNILAnalysis avec score par critère + preuve textuelle
"""

import re
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class CritereScore(str, Enum):
    CONFORME     = "conforme"      # critère rempli
    NON_CONFORME = "non_conforme"  # critère non rempli
    NON_VERIFIABLE = "non_verifiable"  # impossible à vérifier automatiquement


@dataclass
class Critere:
    code: str                          # ex: "A3"
    libelle: str                       # description courte
    question: str                      # question exacte du questionnaire
    score: CritereScore = CritereScore.NON_VERIFIABLE
    preuve: str = ""                   # extrait du texte qui justifie le score
    source: str = ""                   # zone où la preuve a été trouvée
    note: str = ""                     # observation complémentaire


@dataclass
class CNILAnalysis:
    # Scores par bloc
    criteres: list[Critere] = field(default_factory=list)

    # Synthèse
    score_total: int = 0               # nombre de critères conformes
    score_max: int = 0                 # nombre de critères vérifiables
    taux_conformite: float = 0.0       # score_total / score_max * 100

    # Points clés
    dekuple_sur_formulaire: bool = False    # A3 — affichage direct
    dekuple_via_lien: bool = False          # A4 — via liste
    dekuple_politique_lien: bool = False    # A5 — lien politique
    opt_in_present: bool = False            # B1/B2
    bouton_refus_present: bool = False      # C1

    # Texte analysé
    full_text: str = ""


# ─── Patterns de détection par critère ────────────────────────────────────────

# Chaque entrée : (pattern regex, score si trouvé)
PATTERNS = {

    # ── Bloc A : Éclairé ──────────────────────────────────────────────────

    "A1_mention_info": [
        r"mention\s+d.information",
        r"finalit[eé]s\s+du\s+traitement",
        r"donn[eé]es\s+personnelles",
        r"traitement\s+de\s+vos\s+donn",
        r"responsable\s+du\s+traitement",
        r"politique\s+de\s+protection",
        r"politique\s+de\s+confidentialit[eé]",
        r"politique\s+de\s+donn[eé]es",
        r"protection\s+des\s+donn[eé]es",
        r"RGPD",
        r"vie\s+priv[eé]e",
    ],

    "A2_lien_partenaires": [
        r"liste\s+des\s+(destinataires|partenaires)",
        r"partenaires\s+de\s+l.op[eé]ration",
        r"nos\s+partenaires",
        r"voir\s+(nos\s+)?partenaires",
        r"partenaires\s+commerciaux",
        r"partenaires\s+sponsors",
        r"partenaires\s+destinataires",
        r"consulter\s+la\s+liste",
        r"liste\s+compl[eè]te",
    ],

    "A3_dekuple_direct": [
        r"d[ée]kuple\s+dmc",
        r"dekuple\s+dmc",
        r"groupe\s+d[eé]kuple",
        r"d[eé]kuple",
        r"dekuple",
        r"(?<![a-zA-Z])DMC(?![a-zA-Z])",
    ],

    "A4_dekuple_via_lien": [
        # Même patterns — on vérifie si c'est dans une liste/politique
        r"d[ée]kuple",
        r"dekuple",
        r"(?<![a-zA-Z])DMC(?![a-zA-Z])",
    ],

    "A5_lien_politique_dekuple": [
        r"agence\.dekuple\.com",
        r"dekuple\.com/politique",
        r"politique.*dekuple",
        r"dekuple.*politique",
        r"confidentialit[eé].*dekuple",
    ],

    "A6_choix_partenaires": [
        r"personnaliser\s+votre\s+consentement",
        r"choisir\s+vos\s+partenaires",
        r"accepter\s+ou\s+refuser",
        r"s[eé]lectionner\s+certains",
        r"personnaliser\s+mes\s+choix",
        r"mes\s+pr[eé]f[eé]rences",
        r"g[eé]rer\s+mes\s+consentements",
        r"refuser\s+tout",
        r"tout\s+accepter.*tout\s+refuser",
    ],

    "A7_politique_footer": [
        r"politique\s+de\s+(protection|confidentialit[eé]|donn[eé]es)",
        r"mentions\s+l[eé]gales",
        r"protection\s+des\s+donn[eé]es",
        r"politique\s+de\s+donn[eé]es",
        r"privacy\s+policy",
    ],

    "A8_politique_art13": [
        r"finalit[eé]s\s+du\s+traitement",
        r"dur[eé]e\s+de\s+conservation",
        r"droits\s+des\s+personnes",
        r"droit\s+d.acc[eè]s",
        r"droit\s+de\s+rectification",
        r"retrait\s+du\s+consentement",
        r"base\s+l[eé]gale",
        r"int[eé]r[eê]t\s+l[eé]gitime",
        r"article\s+13",
    ],

    "A9_canaux_prospection": [
        r"courrier\s+[eé]lectronique",
        r"courrier\s+postal",
        r"t[eé]l[eé]phone|t[eé]l[eé]phonique",
        r"SMS",
        r"email.*postal.*t[eé]l[eé]",
        r"canaux\s+de\s+prospection",
        r"voie\s+[eé]lectronique",
        r"voie\s+postale",
    ],

    # ── Bloc B : Spécifique ───────────────────────────────────────────────

    "B1_optin_tout_canal": [
        r"j.accepte\s+que\s+mes\s+donn[eé]es\s+soient\s+transmises",
        r"j.accepte\s+de\s+recevoir\s+des\s+offres",
        r"j.accepte\s+la\s+transmission\s+de\s+mes\s+donn[eé]es",
        r"consentement\s+[àa]\s+la\s+transmission",
        r"opt.in",
        r"opt'in",
    ],

    "B2_optin_email": [
        r"offres.*courrier\s+[eé]lectronique",
        r"prospection.*email",
        r"prospection.*SMS",
        r"offres.*email",
        r"offres.*SMS",
    ],

    "B3_optout_postal_tel": [
        r"je\s+ne\s+souhaite\s+pas\s+recevoir",
        r"m.opposer\s+[àa]\s+la\s+prospection",
        r"opt.out",
        r"opt'out",
        r"refus.*courrier\s+postal",
        r"refus.*t[eé]l[eé]phone",
        r"bloctel",
    ],

    "B4_categories_annonceurs": [
        r"cat[eé]gories\s+d.activit[eé]",
        r"secteurs?\s+d.activit[eé]",
        r"cat[eé]gories\s+d.annonceurs",
        r"domaines?\s+d.activit[eé]",
        r"activit[eé]s\s+agricoles",
        r"automobile.*banque.*assurance",
        r"immobilier",
    ],

    # ── Bloc C : Univoque ─────────────────────────────────────────────────

    "C1_bouton_refus": [
        r"refuser\s+tout",
        r"tout\s+refuser",
        r"je\s+refuse",
        r"bouton\s+de\s+refus",
        r"je\s+ne\s+souhaite\s+pas",
        r"non\s+merci",
        r"continuer\s+sans\s+accepter",
    ],

    "C2_couleur_boutons": [
        # Non vérifiable automatiquement — nécessite analyse visuelle
    ],

    "C3_taille_boutons": [
        # Non vérifiable automatiquement — nécessite analyse visuelle
    ],

    "C4_mention_visible": [
        r"mention\s+d.information",
        r"en\s+validant",
        r"en\s+cliquant",
        r"en\s+soumettant",
        r"en\s+vous\s+inscrivant",
        r"j.ai\s+lu",
        r"avoir\s+lu",
        r"avoir\s+pris\s+connaissance",
    ],
}


# ─── Définition des critères ──────────────────────────────────────────────────

CRITERES_DEFINITION = [
    ("A1", "Mention d'information sur le formulaire",
     "Une mention d'information est intégrée sur le formulaire précisant les finalités du traitement ?"),
    ("A2", "Lien vers liste des partenaires",
     "Le lien vers la liste des destinataires est intégré sur le formulaire avant le bouton de consentement ?"),
    ("A3", "Dékuple DMC mentionné directement",
     "\"Dékuple DMC\" est mentionnée comme partenaire destinataire par affichage direct ?"),
    ("A4", "Dékuple DMC via lien liste partenaires",
     "Dékuple DMC figure dans la liste des partenaires accessible via un lien ?"),
    ("A5", "Lien vers politique Dékuple DMC",
     "Un lien vers la politique de Dékuple DMC est inséré à côté de Dékuple DMC et fonctionne ?"),
    ("A6", "Choix partenaires individuels",
     "La personne peut accepter/refuser chaque partenaire individuellement ou personnaliser son consentement ?"),
    ("A7", "Politique primo-collectant en footer",
     "La politique de données du primo-collectant est accessible en footer de chaque page ?"),
    ("A8", "Politique conforme article 13 RGPD",
     "La politique contient toutes les informations obligatoires (finalités, durées, droits, bases légales) ?"),
    ("A9", "Canaux de prospection précisés",
     "La politique précise les canaux de prospection (email, postal, téléphone) ?"),
    ("B1", "Opt-in tout canal présent",
     "Une phrase d'opt-in pour la transmission des données aux partenaires (tout canal) est présente ?"),
    ("B2", "Opt-in email/SMS présent",
     "Un opt-in spécifique courrier électronique (email/SMS) est présent si applicable ?"),
    ("B3", "Opt-out postal/tel avec case",
     "Une case d'opt-out pour le courrier postal et téléphone est prévue ?"),
    ("B4", "Catégories annonceurs précisées",
     "Les catégories d'activités des annonceurs sont précisées dans la politique ?"),
    ("C1", "Bouton de refus effectif",
     "Un bouton de refus pour la prospection par les partenaires est présent et effectif ?"),
    ("C2", "Boutons même couleur",
     "Les boutons d'acceptation et de refus sont de même couleur ? (vérification manuelle)"),
    ("C3", "Boutons même taille",
     "Les boutons d'acceptation et de refus sont de même taille ? (vérification manuelle)"),
    ("C4", "Mention visible",
     "La mention d'information est visible par rapport aux champs et boutons du formulaire ?"),
]


# ─── Analyseur ────────────────────────────────────────────────────────────────

def _find_proof(text: str, patterns: list[str]) -> tuple[bool, str]:
    """
    Cherche les patterns dans le texte.
    Retourne (trouvé, extrait de preuve).
    """
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            # Extraire contexte autour du match (100 chars avant/après)
            start = max(0, match.start() - 80)
            end   = min(len(text), match.end() + 80)
            excerpt = text[start:end].strip()
            excerpt = re.sub(r'\s+', ' ', excerpt)
            return True, f"...{excerpt}..."
    return False, ""


def _analyze_critere(
    code: str,
    libelle: str,
    question: str,
    all_texts: dict[str, str],   # {source: texte}
    dekuple_result=None,         # DidomiResult si disponible
) -> Critere:
    """Analyse un critère sur tous les textes disponibles."""

    c = Critere(code=code, libelle=libelle, question=question)
    patterns = PATTERNS.get(f"{code}_{libelle.lower().replace(' ', '_')[:20]}", [])

    # Trouver les bons patterns selon le code
    pattern_key = None
    for key in PATTERNS:
        if key.startswith(code + "_"):
            pattern_key = key
            break

    if not pattern_key:
        # Critères non vérifiables automatiquement (C2, C3)
        c.score  = CritereScore.NON_VERIFIABLE
        c.note   = "Nécessite vérification visuelle manuelle"
        return c

    patterns = PATTERNS[pattern_key]

    if not patterns:
        c.score = CritereScore.NON_VERIFIABLE
        c.note  = "Nécessite vérification manuelle"
        return c

    # Cas spécial A3/A4 : utiliser la liste Didomi si disponible
    if code in ("A3", "A4") and dekuple_result:
        if hasattr(dekuple_result, 'dekuple_found'):
            if dekuple_result.dekuple_found:
                partner = getattr(dekuple_result, 'dekuple_partner', None)
                name    = partner.name if partner else "Dékuple DMC"
                c.score  = CritereScore.CONFORME
                c.preuve = f"Trouvé dans la liste CMP : {name}"
                c.source = "API CMP (liste partenaires complète)"
                return c
            else:
                c.score  = CritereScore.NON_CONFORME
                c.preuve = f"Absent de la liste CMP ({dekuple_result.total_partners} partenaires analysés)"
                c.source = "API CMP"
                return c

    # Cherche dans tous les textes disponibles
    best_proof  = ""
    best_source = ""
    found       = False

    # Priorité de recherche : formulaire > footer > mentions légales > politique PDF
    priority_order = ["main", "footer", "accordion_or_modal", "legal_page"]

    for source in priority_order:
        text = all_texts.get(source, "")
        if not text:
            continue
        ok, proof = _find_proof(text, patterns)
        if ok:
            found       = True
            best_proof  = proof
            best_source = source
            break

    # Si pas trouvé dans l'ordre prioritaire, chercher dans tout le reste
    if not found:
        for source, text in all_texts.items():
            if source in priority_order or not text:
                continue
            ok, proof = _find_proof(text, patterns)
            if ok:
                found       = True
                best_proof  = proof
                best_source = source
                break

    if found:
        c.score  = CritereScore.CONFORME
        c.preuve = best_proof
        c.source = best_source
    else:
        c.score  = CritereScore.NON_CONFORME
        c.preuve = "Aucune mention trouvée dans les zones analysées"
        c.source = "-"

    return c


def analyze_cnil(
    contents: list,         # liste de ExtractedContent (Module 3)
    dekuple_result=None,    # DidomiResult (Module 2) si disponible
    all_text: str = "",     # texte agrégé (fallback)
) -> CNILAnalysis:
    """
    Point d'entrée principal.
    Analyse tous les critères CNIL et retourne un CNILAnalysis complet.
    """
    analysis = CNILAnalysis()

    # Construire le dictionnaire textes par source
    all_texts = {}
    for content in contents:
        source = content.source if hasattr(content, 'source') else "main"
        existing = all_texts.get(source, "")
        text = content.text if hasattr(content, 'text') else str(content)
        all_texts[source] = existing + " " + text if existing else text

    # Fallback texte complet si contents vide
    if not all_texts and all_text:
        all_texts["main"] = all_text

    # Analyser chaque critère
    for code, libelle, question in CRITERES_DEFINITION:
        critere = _analyze_critere(
            code, libelle, question, all_texts, dekuple_result
        )
        analysis.criteres.append(critere)

    # Calcul du score global
    verifiables = [c for c in analysis.criteres
                   if c.score != CritereScore.NON_VERIFIABLE]
    conformes   = [c for c in verifiables
                   if c.score == CritereScore.CONFORME]

    analysis.score_total    = len(conformes)
    analysis.score_max      = len(verifiables)
    analysis.taux_conformite = (
        round(len(conformes) / len(verifiables) * 100, 1)
        if verifiables else 0.0
    )

    # Points clés
    def _is_conforme(code: str) -> bool:
        c = next((x for x in analysis.criteres if x.code == code), None)
        return c is not None and c.score == CritereScore.CONFORME

    analysis.dekuple_sur_formulaire = _is_conforme("A3")
    analysis.dekuple_via_lien       = _is_conforme("A4")
    analysis.dekuple_politique_lien = _is_conforme("A5")
    analysis.opt_in_present         = _is_conforme("B1") or _is_conforme("B2")
    analysis.bouton_refus_present   = _is_conforme("C1")

    analysis.full_text = all_text or " ".join(all_texts.values())[:1000]

    return analysis


def format_analysis(analysis: CNILAnalysis) -> str:
    """Formatage lisible pour debug."""
    lines = [
        "=" * 65,
        "ANALYSE CNIL — CRITÈRES DE CONFORMITÉ",
        "=" * 65,
        f"Score : {analysis.score_total}/{analysis.score_max} "
        f"({analysis.taux_conformite}%)",
        f"Dékuple sur formulaire  : {'✅' if analysis.dekuple_sur_formulaire else '❌'}",
        f"Dékuple via lien        : {'✅' if analysis.dekuple_via_lien else '❌'}",
        f"Lien politique Dékuple  : {'✅' if analysis.dekuple_politique_lien else '❌'}",
        f"Opt-in présent          : {'✅' if analysis.opt_in_present else '❌'}",
        f"Bouton refus            : {'✅' if analysis.bouton_refus_present else '❌'}",
        "",
    ]

    current_bloc = ""
    for c in analysis.criteres:
        bloc = c.code[0]
        if bloc != current_bloc:
            current_bloc = bloc
            bloc_names = {"A": "ÉCLAIRÉ", "B": "SPÉCIFIQUE", "C": "UNIVOQUE"}
            lines.append(f"\n── Bloc {bloc} : {bloc_names.get(bloc, '')} ──")

        icon = {"conforme": "✅", "non_conforme": "❌",
                "non_verifiable": "⚠️"}.get(c.score.value, "?")
        lines.append(f"  {icon} [{c.code}] {c.libelle}")
        if c.preuve and c.preuve != "Aucune mention trouvée dans les zones analysées":
            lines.append(f"       Preuve  : {c.preuve[:120]}")
        if c.source and c.source != "-":
            lines.append(f"       Source  : {c.source}")
        if c.note:
            lines.append(f"       Note    : {c.note}")

    return "\n".join(lines)
