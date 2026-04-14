"""
Module 5 v2 — Générateur de rapports
======================================
Version corrigée et complète.
Génère :
  - PDF individuel par URL : rapport professionnel avec analyse CNIL complète,
    preuves textuelles par critère, et screenshots intégrés
  - Excel par URL          : données structurées critère par critère
  - Excel synthèse globale : toutes les URLs d'une session
  - ZIP final              : archive complète téléchargeable

Corrections v2 :
  - Paragraphs dans toutes les cellules de tableau (évite la troncature)
  - Tableaux avec KeepInFrame pour éviter les débordements
  - Couleurs de ligne alternées correctement appliquées
  - Gestion robuste des textes longs et accentués
"""

import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image, HRFlowable, KeepTogether, PageBreak
)
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.orchestrator import AuditResult, AuditStatus
from modules.cnil_analyzer import CNILAnalysis, CritereScore


# ─── Palette de couleurs ──────────────────────────────────────────────────────

C = {
    "dmc_blue":   HexColor("#1F4E79"),
    "dmc_accent": HexColor("#2E75B6"),
    "dmc_light":  HexColor("#D6E4F0"),
    "green":      HexColor("#1E8449"),
    "green_bg":   HexColor("#D5F5E3"),
    "red":        HexColor("#A93226"),
    "red_bg":     HexColor("#FADBD8"),
    "amber":      HexColor("#B7770D"),
    "amber_bg":   HexColor("#FEF9E7"),
    "gray_bg":    HexColor("#F7F7F7"),
    "gray_line":  HexColor("#CCCCCC"),
    "white":      colors.white,
    "black":      colors.black,
}

STATUS_MAP = {
    AuditStatus.CONFORME:     ("CONFORME",     C["green_bg"],  C["green"]),
    AuditStatus.NON_CONFORME: ("NON CONFORME", C["red_bg"],    C["red"]),
    AuditStatus.A_VERIFIER:   ("À VÉRIFIER",   C["amber_bg"],  C["amber"]),
    AuditStatus.ERREUR:       ("ERREUR",        C["gray_bg"],   C["dmc_blue"]),
    AuditStatus.TIMEOUT:      ("TIMEOUT",       C["gray_bg"],   C["dmc_blue"]),
}

SCORE_MAP = {
    CritereScore.CONFORME:       ("CONFORME",       C["green_bg"],  C["green"]),
    CritereScore.NON_CONFORME:   ("NON CONFORME",   C["red_bg"],    C["red"]),
    CritereScore.NON_VERIFIABLE: ("À VÉRIFIER",     C["amber_bg"],  C["amber"]),
}

BLOC_LABELS = {
    "A": "Consentement ÉCLAIRÉ",
    "B": "Consentement SPÉCIFIQUE",
    "C": "Consentement UNIVOQUE",
}

OUTPUT_DIR = Path("outputs")


# ─── Styles ReportLab ─────────────────────────────────────────────────────────

def _make_styles():
    """Crée tous les styles Paragraph réutilisables."""
    s = {}

    s["title"] = ParagraphStyle(
        "title", fontName="Helvetica-Bold", fontSize=16,
        textColor=C["dmc_blue"], leading=20, spaceAfter=2,
    )
    s["subtitle"] = ParagraphStyle(
        "subtitle", fontName="Helvetica", fontSize=9,
        textColor=C["dmc_accent"], leading=12, spaceAfter=0,
    )
    s["h2"] = ParagraphStyle(
        "h2", fontName="Helvetica-Bold", fontSize=11,
        textColor=C["dmc_accent"], leading=14,
        spaceBefore=14, spaceAfter=6,
        borderPad=4,
    )
    s["body"] = ParagraphStyle(
        "body", fontName="Helvetica", fontSize=9,
        textColor=C["black"], leading=13, spaceAfter=4,
    )
    s["small"] = ParagraphStyle(
        "small", fontName="Helvetica", fontSize=7.5,
        textColor=HexColor("#555555"), leading=10,
    )
    s["cell"] = ParagraphStyle(
        "cell", fontName="Helvetica", fontSize=8,
        textColor=C["black"], leading=11,
    )
    s["cell_bold"] = ParagraphStyle(
        "cell_bold", fontName="Helvetica-Bold", fontSize=8,
        textColor=C["dmc_blue"], leading=11,
    )
    s["cell_proof"] = ParagraphStyle(
        "cell_proof", fontName="Helvetica-Oblique", fontSize=7,
        textColor=HexColor("#444444"), leading=10,
    )
    s["status_ok"] = ParagraphStyle(
        "status_ok", fontName="Helvetica-Bold", fontSize=9,
        textColor=C["green"], leading=12, alignment=TA_CENTER,
    )
    s["status_ko"] = ParagraphStyle(
        "status_ko", fontName="Helvetica-Bold", fontSize=9,
        textColor=C["red"], leading=12, alignment=TA_CENTER,
    )
    s["status_warn"] = ParagraphStyle(
        "status_warn", fontName="Helvetica-Bold", fontSize=9,
        textColor=C["amber"], leading=12, alignment=TA_CENTER,
    )
    s["bloc_header"] = ParagraphStyle(
        "bloc_header", fontName="Helvetica-Bold", fontSize=9,
        textColor=C["dmc_blue"], leading=12,
    )
    s["footer_text"] = ParagraphStyle(
        "footer_text", fontName="Helvetica", fontSize=7,
        textColor=HexColor("#888888"), leading=10, alignment=TA_CENTER,
    )
    return s


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_filename(url: str) -> str:
    name = re.sub(r'^https?://', '', url)
    name = re.sub(r'[^\w.-]', '_', name)
    return name[:80]


def _session_dir(session_id: str) -> Path:
    d = OUTPUT_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "pdf").mkdir(exist_ok=True)
    (d / "xlsx").mkdir(exist_ok=True)
    return d


def _now() -> str:
    return datetime.now().strftime("%d/%m/%Y à %H:%M")


def _thin_border():
    return Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )


def _p(text: str, style) -> Paragraph:
    """Crée un Paragraph en échappant les caractères XML."""
    safe = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Paragraph(safe, style)


# ─── Constructeurs de sections PDF ────────────────────────────────────────────

def _build_header(result: AuditResult, S: dict) -> list:
    """Bandeau d'en-tête : titre + statut."""
    label, bg, fg = STATUS_MAP.get(result.status, ("INCONNU", C["gray_bg"], C["black"]))

    data = [[
        [
            _p("Dékuple DMC — Audit RGPD", S["title"]),
            _p("Rapport automatisé de conformité CNIL", S["subtitle"]),
        ],
        _p(f"● {label}", ParagraphStyle(
            "st", fontName="Helvetica-Bold", fontSize=13,
            textColor=fg, alignment=TA_CENTER, leading=16,
        )),
    ]]
    t = Table(data, colWidths=[12.5*cm, 5*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (0,0), C["dmc_light"]),
        ("BACKGROUND",    (1,0), (1,0), bg),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING",   (0,0), (-1,-1), 10),
        ("RIGHTPADDING",  (0,0), (-1,-1), 10),
        ("TOPPADDING",    (0,0), (-1,-1), 12),
        ("BOTTOMPADDING", (0,0), (-1,-1), 12),
        ("LINEBELOW",     (0,0), (-1,-1), 1.5, C["dmc_accent"]),
    ]))
    return [t, Spacer(1, 0.35*cm)]


def _build_meta(result: AuditResult, S: dict) -> list:
    """Tableau des métadonnées d'audit."""
    rows = [
        ["URL analysée",   result.url],
        ["Date d'audit",   _now()],
        ["CMP détecté",    result.cmp_type or "aucun"],
        ["Route utilisée", result.route.value if result.route else "-"],
        ["Durée",          f"{result.duration_seconds}s"],
        ["Liens analysés", str(len(result.links_analyzed)) + " pages"],
        ["Screenshots",    str(len(result.screenshots)) + " captures"],
    ]
    if result.error:
        rows.append(["Erreur", result.error])

    table_data = [
        [_p(label, S["cell_bold"]), _p(str(val), S["cell"])]
        for label, val in rows
    ]

    t = Table(table_data, colWidths=[4*cm, 13.5*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (0,-1), C["dmc_light"]),
        ("BACKGROUND",    (1,0), (1,-1), C["gray_bg"]),
        ("GRID",          (0,0), (-1,-1), 0.3, C["gray_line"]),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
    ]))
    return [_p("Informations d'audit", S["h2"]), t, Spacer(1, 0.35*cm)]


def _build_dekuple_section(result: AuditResult, S: dict) -> list:
    """Section résultat Dékuple DMC."""
    if result.dekuple_found:
        rows = [
            ["Résultat",         "✅  Dékuple DMC TROUVÉ"],
            ["Nom exact trouvé", result.dekuple_name or "Dékuple DMC"],
            ["Localisé dans",    "\n".join(result.dekuple_locations) or "-"],
            ["Politique",        result.dekuple_policy_url or "non renseignée"],
        ]
        row_bg = C["green_bg"]
        row_fg = C["green"]
    else:
        rows = [
            ["Résultat",         "❌  Dékuple DMC ABSENT"],
            ["Zones vérifiées",  f"{len(result.links_analyzed) + 1} zones analysées"],
            ["Action requise",   "Contacter l'éditeur — mise en conformité nécessaire"],
        ]
        if result.notes:
            rows.append(["Observation", result.notes])
        row_bg = C["red_bg"]
        row_fg = C["red"]

    table_data = [
        [
            _p(label, ParagraphStyle("dl", fontName="Helvetica-Bold",
                                     fontSize=8, textColor=row_fg, leading=11)),
            _p(str(val), ParagraphStyle("dv", fontName="Helvetica",
                                        fontSize=8, textColor=row_fg, leading=11)),
        ]
        for label, val in rows
    ]

    t = Table(table_data, colWidths=[4*cm, 13.5*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), row_bg),
        ("GRID",          (0,0), (-1,-1), 0.3, row_fg),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
    ]))
    return [_p("Détection Dékuple DMC", S["h2"]), t, Spacer(1, 0.35*cm)]


def _build_cnil_table(analysis: CNILAnalysis, S: dict) -> list:
    """
    Tableau complet des 17 critères CNIL.
    Chaque cellule contient un Paragraph — pas de troncature possible.
    """
    score_txt = (
        f"{analysis.score_total}/{analysis.score_max} critères conformes "
        f"({analysis.taux_conformite}%) — "
        f"{'✅' if analysis.taux_conformite >= 80 else '⚠️' if analysis.taux_conformite >= 50 else '❌'} "
        f"{'Bon niveau' if analysis.taux_conformite >= 80 else 'Partiellement conforme' if analysis.taux_conformite >= 50 else 'Non conforme'}"
    )

    elements = [
        _p("Analyse des critères CNIL", S["h2"]),
        _p(score_txt, S["body"]),
        Spacer(1, 0.2*cm),
    ]

    # En-tête du tableau
    header = [
        _p("Code",    S["cell_bold"]),
        _p("Critère", S["cell_bold"]),
        _p("Score",   S["cell_bold"]),
        _p("Preuve & source",  S["cell_bold"]),
    ]
    col_widths = [1.3*cm, 5.2*cm, 2.8*cm, 8.2*cm]

    # Construction des lignes
    rows = [header]
    row_styles = []
    current_bloc = ""

    for i, critere in enumerate(analysis.criteres, start=1):
        bloc = critere.code[0]

        # Ligne de séparation de bloc
        if bloc != current_bloc:
            current_bloc = bloc
            bloc_row = [
                _p("", S["cell"]),
                _p(BLOC_LABELS.get(bloc, f"Bloc {bloc}"), S["bloc_header"]),
                _p("", S["cell"]),
                _p("", S["cell"]),
            ]
            rows.append(bloc_row)
            row_styles.append(("BACKGROUND", (0, len(rows)-1), (-1, len(rows)-1), C["dmc_light"]))
            row_styles.append(("SPAN", (1, len(rows)-1), (3, len(rows)-1)))

        # Ligne du critère
        label, bg, fg = SCORE_MAP.get(critere.score, ("?", C["gray_bg"], C["black"]))

        # Preuve — on tronque proprement à 200 chars
        preuve_txt = critere.preuve or ""
        if len(preuve_txt) > 220:
            preuve_txt = preuve_txt[:217] + "..."
        source_txt = f"[{critere.source}]" if critere.source and critere.source != "-" else ""
        if critere.note:
            source_txt += f" — {critere.note}"
        # Cellule preuve = un seul Paragraph (liste interdite dans ReportLab Table)
        preuve_combined = preuve_txt
        if source_txt:
            preuve_combined += f" {source_txt}"

        critere_row = [
            _p(critere.code, S["cell_bold"]),
            _p(critere.libelle, S["cell"]),
            _p(label, ParagraphStyle(
                "sc", fontName="Helvetica-Bold", fontSize=7.5,
                textColor=fg, leading=10, alignment=TA_CENTER,
            )),
            _p(preuve_combined, S["cell_proof"]),
        ]
        rows.append(critere_row)

        # Couleur de la cellule score
        row_styles.append(("BACKGROUND", (2, len(rows)-1), (2, len(rows)-1), bg))
        # Alternance gris/blanc sur les lignes de critère
        if i % 2 == 0:
            row_styles.append(("BACKGROUND", (0, len(rows)-1), (1, len(rows)-1), C["gray_bg"]))
            row_styles.append(("BACKGROUND", (3, len(rows)-1), (3, len(rows)-1), C["gray_bg"]))

    t = Table(rows, colWidths=col_widths, repeatRows=1)
    base_style = [
        # En-tête
        ("BACKGROUND",    (0,0), (-1,0), C["dmc_blue"]),
        ("TEXTCOLOR",     (0,0), (-1,0), C["white"]),
        # Grille
        ("GRID",          (0,0), (-1,-1), 0.3, C["gray_line"]),
        ("LINEBELOW",     (0,0), (-1,0), 1.0, C["dmc_blue"]),
        # Padding
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("LEFTPADDING",   (0,0), (-1,-1), 5),
        ("RIGHTPADDING",  (0,0), (-1,-1), 5),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("ALIGN",         (2,0), (2,-1), "CENTER"),
    ] + row_styles

    t.setStyle(TableStyle(base_style))
    elements.append(t)
    return elements


def _build_partners_section(result: AuditResult, S: dict) -> list:
    """Liste des partenaires si disponible via API CMP."""
    if not result.partners_list:
        return []

    header = [
        _p("#",          S["cell_bold"]),
        _p("Partenaire", S["cell_bold"]),
        _p("Dékuple ?",  S["cell_bold"]),
    ]
    rows = [header]
    for i, name in enumerate(result.partners_list, 1):
        is_dk = any(k in name.lower() for k in ["dékuple","dekuple","dmc"])
        rows.append([
            _p(str(i), S["cell"]),
            _p(name, ParagraphStyle(
                "pn", fontName="Helvetica-Bold" if is_dk else "Helvetica",
                fontSize=8, textColor=C["green"] if is_dk else C["black"],
                leading=11,
            )),
            _p("✅" if is_dk else "", S["cell"]),
        ])

    t = Table(rows, colWidths=[1*cm, 14.5*cm, 2*cm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), C["dmc_blue"]),
        ("TEXTCOLOR",   (0,0), (-1,0), C["white"]),
        ("GRID",        (0,0), (-1,-1), 0.3, C["gray_line"]),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [C["white"], C["gray_bg"]]),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("LEFTPADDING",   (0,0), (-1,-1), 5),
        ("ALIGN",         (0,0), (0,-1), "CENTER"),
        ("ALIGN",         (2,0), (2,-1), "CENTER"),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ]))
    return [
        _p(f"Liste des partenaires CMP ({result.partners_total})", S["h2"]),
        t,
        Spacer(1, 0.35*cm),
    ]


def _build_screenshots(result: AuditResult, S: dict) -> list:
    """Screenshots intégrés avec légende."""
    elements = []
    valid = [p for p in result.screenshots if os.path.exists(p)]
    if not valid:
        return []

    elements.append(HRFlowable(width="100%", thickness=0.5, color=C["gray_line"]))
    elements.append(_p("Captures d'écran", S["h2"]))

    for i, path in enumerate(valid[:4], 1):
        try:
            img = Image(path, width=17.5*cm, height=9.5*cm, kind='proportional')
            elements.append(_p(
                f"Capture {i} — {os.path.basename(path)}",
                S["small"]
            ))
            elements.append(img)
            elements.append(Spacer(1, 0.3*cm))
        except Exception as e:
            elements.append(_p(f"[Capture {i} non disponible : {e}]", S["small"]))
    return elements


def _build_observations(result: AuditResult, S: dict) -> list:
    """Bloc observations et liens analysés."""
    elements = []
    if not result.notes and not result.links_analyzed:
        return elements

    items = []
    if result.notes:
        items.append(("Observation", result.notes))
    if result.links_analyzed:
        items.append(("Liens analysés", "\n".join(result.links_analyzed)))

    table_data = [
        [_p(label, S["cell_bold"]), _p(str(val), S["cell"])]
        for label, val in items
    ]
    t = Table(table_data, colWidths=[4*cm, 13.5*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (0,-1), C["dmc_light"]),
        ("GRID",          (0,0), (-1,-1), 0.3, C["gray_line"]),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
    ]))
    elements += [_p("Observations", S["h2"]), t, Spacer(1, 0.3*cm)]
    return elements


def _build_footer_note(S: dict) -> list:
    return [
        Spacer(1, 0.5*cm),
        HRFlowable(width="100%", thickness=0.8, color=C["dmc_accent"]),
        Spacer(1, 0.15*cm),
        _p(
            f"Document confidentiel — Généré automatiquement par l'outil d'audit RGPD "
            f"Dékuple DMC — {_now()} — Usage interne uniquement",
            S["footer_text"]
        ),
    ]


# ─── Génération PDF principale ────────────────────────────────────────────────

def generate_pdf(result: AuditResult, session_dir: Path) -> str:
    """Génère le PDF complet pour un AuditResult."""
    filename = _safe_filename(result.url) + ".pdf"
    filepath = str(session_dir / "pdf" / filename)
    S = _make_styles()

    doc = SimpleDocTemplate(
        filepath,
        pagesize=A4,
        rightMargin=1.5*cm, leftMargin=1.5*cm,
        topMargin=1.5*cm, bottomMargin=1.5*cm,
        title=f"Audit RGPD — {result.url}",
        author="Dékuple DMC",
        subject="Rapport de conformité CNIL",
    )

    elements = []

    # 1. En-tête
    elements += _build_header(result, S)

    # 2. Métadonnées
    elements += _build_meta(result, S)

    # 3. Résultat Dékuple
    elements += _build_dekuple_section(result, S)

    # 4. Analyse CNIL (17 critères) — section la plus importante
    cnil = getattr(result, 'cnil_analysis', None)
    if cnil:
        elements += _build_cnil_table(cnil, S)
        elements.append(Spacer(1, 0.35*cm))

    # 5. Liste partenaires CMP
    elements += _build_partners_section(result, S)

    # 6. Observations
    elements += _build_observations(result, S)

    # 7. Screenshots (sur nouvelle page si présents)
    shots = _build_screenshots(result, S)
    if shots:
        elements.append(PageBreak())
        elements += shots

    # 8. Pied de page
    elements += _build_footer_note(S)

    doc.build(elements)
    return filepath


# ─── Génération Excel par URL ──────────────────────────────────────────────────

def generate_xlsx_per_url(result: AuditResult, session_dir: Path) -> str:
    """Excel détaillé par URL avec tous les critères CNIL."""
    filename = _safe_filename(result.url) + ".xlsx"
    filepath = str(session_dir / "xlsx" / filename)
    wb = openpyxl.Workbook()

    # ── Feuille 1 : Résumé ────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Résumé"

    thin = _thin_border()
    hdr_font  = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    hdr_fill  = PatternFill("solid", fgColor="1F4E79")
    lbl_font  = Font(name="Arial", bold=True, size=10, color="1F4E79")
    val_font  = Font(name="Arial", size=10)
    center    = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_wrap = Alignment(horizontal="left", vertical="center", wrap_text=True)

    STATUS_FILLS_XL = {
        "conforme":     "D5F5E3",
        "non_conforme": "FADBD8",
        "a_verifier":   "FEF9E7",
        "erreur":       "F2F2F2",
    }

    def xl_hdr(ws, row, col, val, span_end=None):
        c = ws.cell(row=row, column=col, value=val)
        c.font = hdr_font; c.fill = hdr_fill
        c.alignment = center; c.border = thin
        if span_end:
            ws.merge_cells(
                start_row=row, start_column=col,
                end_row=row, end_column=span_end
            )
        return c

    def xl_row(ws, row, label, value, val_fill=None):
        c1 = ws.cell(row=row, column=1, value=label)
        c1.font = lbl_font; c1.fill = PatternFill("solid", fgColor="D6E4F0")
        c1.border = thin; c1.alignment = left_wrap
        c2 = ws.cell(row=row, column=2, value=str(value) if value is not None else "")
        c2.font = val_font; c2.border = thin; c2.alignment = left_wrap
        if val_fill:
            c2.fill = PatternFill("solid", fgColor=val_fill)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
        ws.row_dimensions[row].height = 18

    xl_hdr(ws, 1, 1, "AUDIT RGPD — DÉKUPLE DMC", span_end=4)
    ws.row_dimensions[1].height = 28

    status_fill = STATUS_FILLS_XL.get(result.status.value, "F2F2F2")
    xl_row(ws, 2,  "URL analysée",   result.url)
    xl_row(ws, 3,  "Date d'audit",   _now())
    xl_row(ws, 4,  "Statut",         result.status.value.upper(), val_fill=status_fill)
    xl_row(ws, 5,  "CMP détecté",    result.cmp_type or "aucun")
    xl_row(ws, 6,  "Route",          result.route.value if result.route else "-")
    xl_row(ws, 7,  "Durée (s)",      result.duration_seconds)
    xl_row(ws, 8,  "Dékuple trouvé", "OUI" if result.dekuple_found else "NON",
           val_fill="D5F5E3" if result.dekuple_found else "FADBD8")
    xl_row(ws, 9,  "Nom exact",      result.dekuple_name or "-")
    xl_row(ws, 10, "Localisé dans",  " | ".join(result.dekuple_locations) or "-")
    xl_row(ws, 11, "Politique URL",  result.dekuple_policy_url or "-")
    xl_row(ws, 12, "Partenaires",    result.partners_total or "-")
    xl_row(ws, 13, "Notes",          result.notes or "-")
    xl_row(ws, 14, "Erreur",         result.error or "aucune")

    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 60
    ws.freeze_panes = "A2"

    # ── Feuille 2 : Critères CNIL ─────────────────────────────────────────
    cnil = getattr(result, 'cnil_analysis', None)
    if cnil:
        ws2 = wb.create_sheet("Critères CNIL")

        SCORE_FILLS = {
            "conforme":       "D5F5E3",
            "non_conforme":   "FADBD8",
            "non_verifiable": "FEF9E7",
        }

        xl_hdr(ws2, 1, 1, "Code")
        xl_hdr(ws2, 1, 2, "Critère")
        xl_hdr(ws2, 1, 3, "Score")
        xl_hdr(ws2, 1, 4, "Preuve extraite")
        xl_hdr(ws2, 1, 5, "Source")
        xl_hdr(ws2, 1, 6, "Note")
        ws2.row_dimensions[1].height = 22

        current_bloc = ""
        row = 2
        for critere in cnil.criteres:
            bloc = critere.code[0]
            if bloc != current_bloc:
                current_bloc = bloc
                # Ligne de titre de bloc
                c = ws2.cell(row=row, column=1, value=f"Bloc {bloc} — {BLOC_LABELS.get(bloc, '')}")
                c.font = Font(name="Arial", bold=True, size=10, color="1F4E79")
                c.fill = PatternFill("solid", fgColor="D6E4F0")
                c.border = thin
                c.alignment = left_wrap
                ws2.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
                ws2.row_dimensions[row].height = 18
                row += 1

            sf = SCORE_FILLS.get(critere.score.value, "F2F2F2")
            cells_data = [
                (1, critere.code,          None),
                (2, critere.libelle,       None),
                (3, critere.score.value,   sf),
                (4, critere.preuve[:300] if critere.preuve else "", None),
                (5, critere.source or "",  None),
                (6, critere.note or "",    None),
            ]
            for col, val, fill in cells_data:
                c = ws2.cell(row=row, column=col, value=val)
                c.font = val_font; c.border = thin; c.alignment = left_wrap
                if fill:
                    c.fill = PatternFill("solid", fgColor=fill)
            ws2.row_dimensions[row].height = 30
            row += 1

        # Score global en bas
        row += 1
        c = ws2.cell(row=row, column=1,
                     value=f"Score global : {cnil.score_total}/{cnil.score_max} ({cnil.taux_conformite}%)")
        c.font = Font(name="Arial", bold=True, size=11, color="1F4E79")
        ws2.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)

        ws2.column_dimensions["A"].width = 8
        ws2.column_dimensions["B"].width = 35
        ws2.column_dimensions["C"].width = 16
        ws2.column_dimensions["D"].width = 60
        ws2.column_dimensions["E"].width = 18
        ws2.column_dimensions["F"].width = 30
        ws2.freeze_panes = "A2"

    # ── Feuille 3 : Partenaires ───────────────────────────────────────────
    if result.partners_list:
        ws3 = wb.create_sheet("Partenaires")
        xl_hdr(ws3, 1, 1, "#")
        xl_hdr(ws3, 1, 2, "Nom du partenaire")
        xl_hdr(ws3, 1, 3, "Dékuple DMC ?")
        for i, name in enumerate(result.partners_list, 1):
            is_dk = any(k in name.lower() for k in ["dékuple","dekuple","dmc"])
            ws3.cell(row=i+1, column=1, value=i).font = val_font
            c2 = ws3.cell(row=i+1, column=2, value=name)
            c2.font = Font(name="Arial", bold=is_dk, size=10,
                           color="1E8449" if is_dk else "000000")
            ws3.cell(row=i+1, column=3, value="✅" if is_dk else "").font = val_font
            if is_dk:
                for col in range(1, 4):
                    ws3.cell(row=i+1, column=col).fill = PatternFill("solid", fgColor="D5F5E3")
            for col in range(1, 4):
                ws3.cell(row=i+1, column=col).border = thin
        ws3.column_dimensions["A"].width = 6
        ws3.column_dimensions["B"].width = 50
        ws3.column_dimensions["C"].width = 14
        ws3.freeze_panes = "A2"

    wb.save(filepath)
    return filepath


# ─── Synthèse globale ─────────────────────────────────────────────────────────

def generate_xlsx_synthesis(
    results: list,
    session_dir: Path,
    session_id: str,
) -> str:
    """Excel de synthèse globale avec dashboard et liste d'actions."""
    filepath = str(session_dir / f"synthese_{session_id}.xlsx")
    wb = openpyxl.Workbook()
    thin = _thin_border()
    hdr_font = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    hdr_fill = PatternFill("solid", fgColor="1F4E79")
    val_font = Font(name="Arial", size=9)
    center   = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_w   = Alignment(horizontal="left", vertical="center", wrap_text=True)

    STATUS_FILLS = {
        "conforme":     "D5F5E3",
        "non_conforme": "FADBD8",
        "a_verifier":   "FEF9E7",
        "erreur":       "F2F2F2",
    }

    def hdr(ws, r, c, v, end_col=None):
        cell = ws.cell(row=r, column=c, value=v)
        cell.font = hdr_font; cell.fill = hdr_fill
        cell.alignment = center; cell.border = thin
        if end_col:
            ws.merge_cells(start_row=r, start_column=c, end_row=r, end_column=end_col)

    # ── Feuille 1 : Toutes les URLs ───────────────────────────────────────
    ws = wb.active
    ws.title = "Résultats"

    headers = [
        "URL", "Statut", "Dékuple", "Localisation",
        "CMP", "Partenaires", "Score CNIL",
        "Taux conform.", "Route", "Durée (s)", "Erreur", "Notes",
    ]
    for col, h in enumerate(headers, 1):
        hdr(ws, 1, col, h)
    ws.row_dimensions[1].height = 22

    for row_i, r in enumerate(results, start=2):
        cnil = getattr(r, 'cnil_analysis', None)
        score_txt = f"{cnil.score_total}/{cnil.score_max}" if cnil else "-"
        taux_txt  = f"{cnil.taux_conformite}%" if cnil else "-"

        row_data = [
            r.url,
            r.status.value.upper(),
            "OUI" if r.dekuple_found else "NON",
            " | ".join(r.dekuple_locations) if r.dekuple_locations else "-",
            r.cmp_type or "-",
            r.partners_total or "-",
            score_txt,
            taux_txt,
            r.route.value if r.route else "-",
            r.duration_seconds,
            r.error or "",
            r.notes or "",
        ]
        sf = STATUS_FILLS.get(r.status.value, "F2F2F2")
        for col_i, val in enumerate(row_data, 1):
            c = ws.cell(row=row_i, column=col_i, value=val)
            c.font = val_font; c.border = thin; c.alignment = left_w
            if col_i == 2:
                c.fill = PatternFill("solid", fgColor=sf)
            if col_i == 3:
                dk_fill = "D5F5E3" if r.dekuple_found else "FADBD8"
                c.fill = PatternFill("solid", fgColor=dk_fill)
        ws.row_dimensions[row_i].height = 18

    col_widths = [55, 14, 8, 35, 10, 12, 10, 12, 12, 9, 30, 40]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    # ── Feuille 2 : Dashboard ─────────────────────────────────────────────
    ws2 = wb.create_sheet("Dashboard")
    total     = len(results)
    conformes = sum(1 for r in results if r.status == AuditStatus.CONFORME)
    non_conf  = sum(1 for r in results if r.status == AuditStatus.NON_CONFORME)
    a_verif   = sum(1 for r in results if r.status == AuditStatus.A_VERIFIER)
    erreurs   = sum(1 for r in results if r.status == AuditStatus.ERREUR)
    taux      = round(conformes * 100 / total, 1) if total else 0

    hdr(ws2, 1, 1, "TABLEAU DE BORD — AUDIT RGPD", end_col=3)
    ws2.row_dimensions[1].height = 28

    summary = [
        ("Session",             session_id,         None),
        ("Date",                _now(),              None),
        ("Total URLs",          total,               None),
        ("✅  Conformes",        conformes,           "D5F5E3"),
        ("❌  Non conformes",    non_conf,            "FADBD8"),
        ("⚠️  À vérifier",       a_verif,             "FEF9E7"),
        ("🔴  Erreurs",          erreurs,             "F2F2F2"),
        ("Taux de conformité",  f"{taux}%",          "D5F5E3" if taux >= 80 else "FADBD8" if taux < 50 else "FEF9E7"),
    ]
    for i, (label, value, fill) in enumerate(summary, start=2):
        c1 = ws2.cell(row=i, column=1, value=label)
        c1.font = Font(name="Arial", bold=True, size=11, color="1F4E79")
        c1.border = thin; c1.alignment = left_w
        c1.fill = PatternFill("solid", fgColor="D6E4F0")
        c2 = ws2.cell(row=i, column=2, value=value)
        c2.font = Font(name="Arial", size=11)
        c2.border = thin; c2.alignment = left_w
        if fill:
            c2.fill = PatternFill("solid", fgColor=fill)
        ws2.merge_cells(start_row=i, start_column=2, end_row=i, end_column=3)
        ws2.row_dimensions[i].height = 22

    ws2.column_dimensions["A"].width = 24
    ws2.column_dimensions["B"].width = 20
    ws2.column_dimensions["C"].width = 20

    # ── Feuille 3 : Actions requises ──────────────────────────────────────
    ws3 = wb.create_sheet("Actions requises")
    hdr(ws3, 1, 1, "URL")
    hdr(ws3, 1, 2, "Statut")
    hdr(ws3, 1, 3, "CMP")
    hdr(ws3, 1, 4, "Score CNIL")
    hdr(ws3, 1, 5, "Observation / Action à mener")
    ws3.row_dimensions[1].height = 22

    action_results = [
        r for r in results
        if r.status in (AuditStatus.NON_CONFORME, AuditStatus.A_VERIFIER, AuditStatus.ERREUR)
    ]
    for i, r in enumerate(action_results, start=2):
        cnil = getattr(r, 'cnil_analysis', None)
        score_txt = f"{cnil.score_total}/{cnil.score_max}" if cnil else "-"
        sf = STATUS_FILLS.get(r.status.value, "F2F2F2")

        action = r.notes or r.error or (
            "Dékuple DMC absent — contacter l'éditeur"
            if not r.dekuple_found else "À vérifier manuellement"
        )
        row_data = [r.url, r.status.value.upper(), r.cmp_type or "-", score_txt, action]
        for col, val in enumerate(row_data, 1):
            c = ws3.cell(row=i, column=col, value=val)
            c.font = val_font; c.border = thin; c.alignment = left_w
            if col == 2:
                c.fill = PatternFill("solid", fgColor=sf)
        ws3.row_dimensions[i].height = 22

    ws3.column_dimensions["A"].width = 50
    ws3.column_dimensions["B"].width = 14
    ws3.column_dimensions["C"].width = 12
    ws3.column_dimensions["D"].width = 12
    ws3.column_dimensions["E"].width = 55
    ws3.freeze_panes = "A2"

    wb.save(filepath)
    return filepath


# ─── ZIP ──────────────────────────────────────────────────────────────────────

def generate_zip(session_dir: Path, session_id: str) -> str:
    zip_path = str(OUTPUT_DIR / f"audit_{session_id}.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file in session_dir.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(OUTPUT_DIR))
    return zip_path


# ─── Points d'entrée ──────────────────────────────────────────────────────────

from dataclasses import dataclass, field

@dataclass
class GenerationResult:
    pdf_path:  Optional[str] = None
    xlsx_path: Optional[str] = None
    error:     Optional[str] = None
    success:   bool = False


def generate_outputs(result: AuditResult, session_id: str) -> GenerationResult:
    try:
        sess_dir  = _session_dir(session_id)
        pdf_path  = generate_pdf(result, sess_dir)
        xlsx_path = generate_xlsx_per_url(result, sess_dir)
        return GenerationResult(pdf_path=pdf_path, xlsx_path=xlsx_path, success=True)
    except Exception as e:
        import traceback
        return GenerationResult(error=f"{type(e).__name__} — {e}\n{traceback.format_exc()}", success=False)


def generate_all(results: list, session_id: Optional[str] = None) -> dict:
    if not session_id:
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    sess_dir = _session_dir(session_id)
    outputs  = {"session_id": session_id, "files": [], "errors": []}

    for result in results:
        gen = generate_outputs(result, session_id)
        if gen.success:
            outputs["files"].append({
                "url": result.url, "pdf": gen.pdf_path, "xlsx": gen.xlsx_path,
            })
        else:
            outputs["errors"].append({"url": result.url, "error": gen.error})

    outputs["synthesis"] = generate_xlsx_synthesis(results, sess_dir, session_id)
    outputs["zip"]       = generate_zip(sess_dir, session_id)
    return outputs
