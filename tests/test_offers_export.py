"""Tests de src/offers.py et src/export.py."""
import io
from datetime import date

from openpyxl import load_workbook

from tests.conftest import DEV_QUERIES
from src import config, db, export, extract, jsearch, offers, scoring


def loaded(conn):
    cfg = config.load_config()
    jsearch.collect(conn, DEV_QUERIES, cfg, dev=True)
    extract.extract_new_jobs(conn, cfg, dev=True)
    scoring.score_all(conn, config.load_profile())
    return offers.load_offers(conn)


def test_load_offers_dedupes_and_flattens(conn):
    rows = loaded(conn)
    assert len(rows) == 19  # doublon Altaïr fusionné
    lumen = rows[0]
    assert lumen["entreprise"] == "Lumen Capital Partners" and lumen["durée"] == "6 mois"
    assert lumen["début"] == date(2027, 2, 1) and lumen["mode"] == "hybride"


def test_filters(conn):
    rows = loaded(conn)
    assert len(offers.filter_offers(rows)) == 8
    assert len(offers.filter_offers(rows, hide_eliminated=False)) == 19
    assert {r["pays"] for r in offers.filter_offers(rows, countries=["Germany"])} == {"Germany"}
    assert all(r["score"] >= 90 for r in offers.filter_offers(rows, min_score=90))
    jan = offers.filter_offers(rows, start_before=date(2027, 1, 31))
    assert all(r["début"] is None or r["début"] <= date(2027, 1, 31) for r in jan)
    assert {r["mode"] for r in offers.filter_offers(rows, modes=["remote"])} == {"remote"}
    assert all((r["durée_min"] or 0) <= 5 for r in offers.filter_offers(rows, max_duration=5))


def test_excel_export(conn):
    rows = loaded(conn)
    db.set_application(conn, rows[0]["job_id"], "postulé", "relance le 20/11")
    rows = offers.load_offers(conn)
    wb = load_workbook(io.BytesIO(export.offers_to_excel(rows)))
    ws = wb["Offres"]
    header = [c.value for c in ws[1]]
    assert header[:3] == ["Titre", "Entreprise", "Ville"] and ws.max_row == 20
    values = dict(zip(header, [c.value for c in ws[2]]))
    assert values["Statut"] == "postulé" and values["Notes"] == "relance le 20/11"
    assert ws.cell(row=2, column=len(header)).hyperlink.target.startswith("https://")
    assert ws.freeze_panes == "A2"


def test_excel_export_strips_control_characters():
    rows = [{"titre": "Stage\x0b contrôle\x00 de gestion", "notes": "ok\x1f", "lien": "https://x"}]
    ws = load_workbook(io.BytesIO(export.offers_to_excel(rows)))["Offres"]
    assert ws.cell(row=2, column=1).value == "Stage contrôle de gestion"
