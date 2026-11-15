"""Export Excel des offres avec statut et notes."""
from __future__ import annotations

import io
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

COLUMNS = [("titre", "Titre", 40), ("entreprise", "Entreprise", 26), ("ville", "Ville", 16), ("pays", "Pays", 12),
           ("mode", "Mode", 10), ("durée", "Durée", 11), ("début", "Début", 12), ("domaine", "Domaine", 22),
           ("gratification", "Gratification (€)", 14), ("score", "Score", 8), ("statut", "Statut", 13),
           ("notes", "Notes", 40), ("raisons", "Éliminée car", 30), ("source", "Source", 16),
           ("publiée", "Publiée le", 12), ("lien", "Lien", 14)]


def offers_to_excel(rows: list[dict[str, Any]]) -> bytes:
    """Classeur .xlsx (une ligne par offre, lien cliquable, filtres et en-tête figé)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Offres"
    ws.append([label for _, label, _ in COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for r in rows:
        ws.append(["ouvrir" if key == "lien" and r.get(key) else r.get(key) for key, _, _ in COLUMNS])
        link = r.get("lien")
        if link:
            cell = ws.cell(row=ws.max_row, column=len(COLUMNS))
            cell.hyperlink, cell.font = link, Font(color="0563C1", underline="single")
    for i, (key, _, width) in enumerate(COLUMNS, 1):
        letter = ws.cell(row=1, column=i).column_letter
        ws.column_dimensions[letter].width = width
        if key == "début":
            for cell in ws[letter][1:]:
                cell.number_format = "DD/MM/YYYY"
        if key == "notes":
            for cell in ws[letter][1:]:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
