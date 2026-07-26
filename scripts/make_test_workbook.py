"""Generate a synthetic bank workbook + a GROUND-TRUTH manifest for auditing detection.

    uv run python scripts/make_test_workbook.py tests/fixtures/generated

Writes `audit_workbook.xlsx` and `audit_manifest.json`. The manifest is what makes
this a measurement instrument rather than a demo file: it records every planted
secret (value, sheet, cell, expected data class) AND every deliberate DECOY that
must survive untouched. `scripts/score_test_workbook.py` reads it back and reports
recall and false-positive rates.

Everything here is FICTIONAL -- invented names, invented projects, invented
counterparties. Nothing is derived from a real customer, and no real file is read.

DESIGN: realistic first, but deliberately containing the shapes we know are hard:
  * a bare-surname column (NER collapses without a sentence around the name)
  * an ENGLISH sheet beside German ones (per-document language routing)
  * a sensitive SHEET NAME (lives in xl/workbook.xml, no cell walk reaches it)
  * a value repeated across sheets (document-wide propagation)
  * a literal embedded in a FORMULA (invisible to a cell-text walk)
  * a cell COMMENT (a separate XML surface)
  * a HIDDEN sheet (must be scanned like any other)
  * GDPR Art. 9 data in BOTH languages
  * decoys that look sensitive but are ordinary banking prose

The seed is fixed so the file is reproducible: an audit you cannot re-run is an
anecdote. Pass a different seed to generate an independent variant.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import openpyxl
from openpyxl.comments import Comment

SEED = 20260726

# --- fictional content pools -------------------------------------------------
FIRST = ["Klaus", "Petra", "Anja", "Thomas", "Miriam", "Jonas", "Elif", "Lukas", "Sofia", "Bernd"]
LAST = ["Mueller", "Weber", "Bauer", "Schneider", "Hoffmann", "Yilmaz", "Kowalski", "Brandt"]
CITIES = [("50667", "Koeln"), ("60311", "Frankfurt"), ("20095", "Hamburg"), ("80331", "Muenchen")]
STREETS = ["Hauptstrasse", "Kirchweg", "Rudolf-Breitscheid-Str.", "Am Wall", "Lindenallee"]
PROJECTS = ["Delphin", "Nordstern", "Kolibri", "Adler", "Seidenpfad", "Marschall"]
TOOLS = ["Signavio", "DeepL Pro", "Camunda", "Alteryx", "Collibra", "OpenClaw"]
DEPTS = ["Zahlungsverkehr", "Kreditrisiko", "Treasury", "Compliance", "Marktfolge"]
DIVISIONS = ["Firmenkunden", "Privatkunden", "Kapitalmarkt"]
VENDORS = ["Aurexa Systems GmbH", "Northgate Analytics Ltd", "Vertano AG", "Blauwerk Software"]
DIAGNOSES = ["Diabetes mellitus Typ 2, insulinpflichtig", "chronische Migraene", "Bandscheibenvorfall L4/L5"]
CONFESSIONS = ["roemisch-katholisch", "evangelisch", "konfessionslos"]
# Deliberately no 3-letter acronyms (DGB, GEW): too short to score reliably --
# a substring coincidence elsewhere in the workbook would fake a hit. The tool
# does detect them; they just make a poor measuring stick.
UNIONS = ["ver.di", "IG Metall", "Marburger Bund"]


def main(out_dir: Path, seed: int = SEED) -> int:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()

    must_catch: list[dict] = []
    must_not_catch: list[dict] = []

    def secret(sheet, cell, value, cls, why):
        must_catch.append({"sheet": sheet, "cell": cell, "value": value, "data_class": cls, "why": why})
        return value

    def decoy(sheet, cell, value, why):
        must_not_catch.append({"sheet": sheet, "cell": cell, "value": value, "why": why})
        return value

    def iban(n: int) -> str:
        # Valid mod-97 German IBAN so the checksum validator actually passes.
        bban = f"{37040044:08d}{n:010d}"
        rearranged = bban + "131400"  # DE00 -> 1314 00
        check = 98 - (int(rearranged) % 97)
        return f"DE{check:02d}{bban}"

    # ---------------------------------------------------------------- Sheet 1
    ws = wb.active
    ws.title = "Kundenstamm"
    ws.append(["KundenNr", "Nachname", "Vorname", "IBAN", "Telefon", "Anschrift", "Geburtsdatum"])
    people = []
    for i in range(2, 9):
        fn, ln = rng.choice(FIRST), rng.choice(LAST)
        people.append((fn, ln))
        plz, city = rng.choice(CITIES)
        addr = f"{rng.choice(STREETS)} {rng.randint(1, 99)}, {plz} {city}"
        ib = iban(rng.randint(10**8, 10**9))
        phone = f"0{rng.choice([170, 171, 151, 160])} {rng.randint(1000000, 9999999)}"
        dob = f"{rng.randint(1,28):02d}.{rng.randint(1,12):02d}.{rng.randint(1955,1999)}"
        ws.append([f"K-{rng.randint(10000,99999)}", ln, fn, ib, phone, addr, dob])
        # The bare-surname column is the hard case: no sentence for NER to lean on.
        secret("Kundenstamm", f"B{i}", ln, "people", "bare surname in a name column")
        secret("Kundenstamm", f"D{i}", ib, "financial_ids", "IBAN, checksum-valid")
        secret("Kundenstamm", f"E{i}", phone, "contact", "German mobile number")
        secret("Kundenstamm", f"F{i}", addr, "contact", "street + PLZ + city")

    # ---------------------------------------------------------------- Sheet 2
    ws2 = wb.create_sheet("Personal Vertraulich")
    ws2.append(["Mitarbeiter", "Abteilung", "Diagnose", "Konfession", "Gewerkschaft", "Notiz"])
    for i in range(2, 6):
        fn, ln = rng.choice(people)
        diag, conf, uni = rng.choice(DIAGNOSES), rng.choice(CONFESSIONS), rng.choice(UNIONS)
        ws2.append([f"{fn} {ln}", rng.choice(DEPTS), diag, conf, uni,
                    "Wiedereingliederung ab dem Folgequartal geplant."])
        secret("Personal Vertraulich", f"C{i}", diag, "special_category", "Art.9 health data (DE)")
        secret("Personal Vertraulich", f"D{i}", conf, "special_category", "Art.9 religion (DE)")
        secret("Personal Vertraulich", f"E{i}", uni, "special_category", "Art.9 union membership (DE)")

    # ---------------------------------------------------------------- Sheet 3
    ws3 = wb.create_sheet("Projektportfolio")
    ws3.append(["Projektname", "Eingesetztes Tool", "Abteilung", "Lizenzgeber", "Beschreibung"])
    for i in range(2, 8):
        proj, tool, dept, vend = rng.choice(PROJECTS), rng.choice(TOOLS), rng.choice(DEPTS), rng.choice(VENDORS)
        ws3.append([proj, tool, dept, vend,
                    f"Ablösung der Altsysteme im {dept}. Zielbild bis Jahresende abgestimmt."])
        secret("Projektportfolio", f"A{i}", proj, "internal_topical", "project name via header")
        secret("Projektportfolio", f"B{i}", tool, "internal_topical", "internal tool via header")
        secret("Projektportfolio", f"D{i}", vend, "internal_topical", "licensor via header")
    # A decoy column whose header must NOT become a category (word-boundary rule).
    ws3["F1"] = "Produktgruppe"
    ws3["F2"] = decoy("Projektportfolio", "F2", "Vorsorge", "Produktgruppe is not a DEPARTMENT column")
    ws3["F3"] = decoy("Projektportfolio", "F3", "Sparen", "Produktgruppe is not a DEPARTMENT column")

    # ---------------------------------------------------------------- Sheet 4 (ENGLISH)
    ws4 = wb.create_sheet("Client Register UK")
    ws4.append(["Surname", "IBAN", "Nationality", "Religion", "Medical condition", "Trade union"])
    en_rows = [
        ("Okonkwo", iban(rng.randint(10**8, 10**9)), "Nigerian", "Roman Catholic",
         "type 2 diabetes, insulin dependent", "UNISON"),
        ("Fitzgerald", iban(rng.randint(10**8, 10**9)), "Irish", "Protestant",
         "chronic back pain", "Unite the Union"),
        ("Haddad", iban(rng.randint(10**8, 10**9)), "Lebanese", "Muslim",
         "diagnosed with multiple sclerosis", "Trades Union Congress"),
    ]
    for i, row in enumerate(en_rows, start=2):
        ws4.append(list(row))
        secret("Client Register UK", f"A{i}", row[0], "people", "bare surname, ENGLISH sheet")
        secret("Client Register UK", f"B{i}", row[1], "financial_ids", "IBAN on an English sheet")
        secret("Client Register UK", f"C{i}", row[2], "special_category", "Art.9 ethnic origin (EN)")
        secret("Client Register UK", f"D{i}", row[3], "special_category", "Art.9 religion (EN)")
        secret("Client Register UK", f"E{i}", row[4], "special_category", "Art.9 health data (EN)")
        secret("Client Register UK", f"F{i}", row[5], "special_category", "Art.9 union membership (EN)")

    # ---------------------------------------------------------------- Sheet 5 (ENGLISH topical)
    ws5 = wb.create_sheet("Vendor Contracts")
    ws5.append(["Supplier", "Project", "Department", "Description"])
    for i in range(2, 6):
        vend, proj, dept = rng.choice(VENDORS), rng.choice(PROJECTS), rng.choice(["Group Risk", "Treasury", "Compliance"])
        ws5.append([vend, proj, dept,
                    "Quarterly review of the migration backlog and the renewal options."])
        secret("Vendor Contracts", f"A{i}", vend, "internal_topical", "supplier via ENGLISH header")
        secret("Vendor Contracts", f"B{i}", proj, "internal_topical", "project via ENGLISH header")

    # ---------------------------------------------------------------- Sheet 6 (strategy + decoys)
    ws6 = wb.create_sheet("Strategie Notizen")
    ws6.append(["Thema", "Entscheidung"])
    strat_person = f"{people[0][0]} {people[0][1]}"
    strat_tool = TOOLS[0]
    rows6 = [
        ("Zielbild 2027", f"Ausstieg aus dem Segment Kapitalmarkt bis Q3. Verantwortlich: {strat_person}."),
        ("Toolstrategie", f"{strat_tool} wird konzernweit abgeloest; Migration beginnt im Folgejahr."),
        ("Standort", "Verlagerung von 40 Stellen; Kommunikation erst nach der Betriebsratsanhoerung."),
    ]
    for i, (topic, decision) in enumerate(rows6, start=2):
        ws6.append([topic, decision])
    # Cross-sheet repeats: both already secrets elsewhere; here they must ALSO be
    # caught in free text, which is what document-wide propagation exists for.
    secret("Strategie Notizen", "B2", people[0][1], "people", "surname recurring in free text (propagation)")
    secret("Strategie Notizen", "B3", strat_tool, "internal_topical", "tool recurring in free text (propagation)")

    ws6.append(["Marktumfeld", decoy("Strategie Notizen", "B5",
                                     "The Great Depression started in 1929 and reshaped banking.",
                                     "historical prose, not health data")])
    ws6.append(["Gegenpartei", decoy("Strategie Notizen", "B6",
                                     "Credit Union: Nationwide Building Society",
                                     "counterparty field, not union membership")])
    ws6.append(["Methodik", decoy("Strategie Notizen", "B7",
                                  "An orthodox approach to risk management was applied.",
                                  "ordinary adjective, not religion")])
    ws6.append(["Technik", decoy("Strategie Notizen", "B8",
                                 "Race conditions in the settlement engine were fixed.",
                                 "engineering term, not ethnic origin")])
    ws6.append(["Regulatorik", decoy("Strategie Notizen", "B9",
                                     "European Union regulations apply to this transaction.",
                                     "not union membership")])
    ws6.append(["Effizienz", decoy("Strategie Notizen", "B10",
                                   "Die Effizienz der Reaktionszeiten war zufriedenstellend.",
                                   "German nominalizations, not names")])
    ws6.append(["Betrag", decoy("Strategie Notizen", "B11",
                                "Ruecklage von 66450 Euro gebildet.",
                                "5-digit amount, not a postal code")])

    # ---------------------------------------------------------------- hard surfaces
    # A literal inside a FORMULA: no cell-text walk reaches it.
    ws7 = wb.create_sheet("Abrechnung")
    ws7.append(["Position", "Hinweis"])
    formula_secret = f"{people[1][0]} {people[1][1]}"
    ws7["B2"] = f'="Sachbearbeiter: "&"{formula_secret}"'
    secret("Abrechnung", "B2", formula_secret, "people", "name inside a FORMULA literal")

    # A cell COMMENT: a separate XML surface.
    comment_secret = iban(rng.randint(10**8, 10**9))
    ws7["A2"] = "Sammelbuchung"
    ws7["A2"].comment = Comment(f"Rueckfrage zur IBAN {comment_secret}", "Revision")
    secret("Abrechnung", "A2", comment_secret, "financial_ids", "IBAN inside a cell COMMENT")

    # A HIDDEN sheet whose TITLE is itself sensitive.
    hidden_name = f"Archiv {people[2][0]} {people[2][1]}"[:31]
    ws8 = wb.create_sheet(hidden_name)
    ws8.sheet_state = "hidden"
    ws8.append(["Vermerk"])
    hidden_secret = f"{people[2][0]} {people[2][1]}"
    ws8.append([f"Altbestand {hidden_secret}, Auskunft nur nach Ruecksprache."])
    secret(hidden_name, "A2", hidden_secret, "people", "name on a HIDDEN sheet")
    must_catch.append({"sheet": hidden_name, "cell": "<sheet title>", "value": hidden_secret,
                       "data_class": "people", "why": "sensitive SHEET NAME (xl/workbook.xml)"})

    xlsx_path = out_dir / "audit_workbook.xlsx"
    wb.save(xlsx_path)

    manifest = {
        "seed": seed,
        "workbook": xlsx_path.name,
        "sheets": wb.sheetnames,
        "must_catch": must_catch,
        "must_not_catch": must_not_catch,
    }
    manifest_path = out_dir / "audit_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"workbook : {xlsx_path}  ({xlsx_path.stat().st_size/1024:.0f} KB)")
    print(f"manifest : {manifest_path}")
    print(f"sheets   : {len(wb.sheetnames)}  ({', '.join(wb.sheetnames)})")
    print(f"planted  : {len(must_catch)} secrets, {len(must_not_catch)} decoys")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/generated").resolve()
    raise SystemExit(main(target, int(sys.argv[2]) if len(sys.argv) > 2 else SEED))
