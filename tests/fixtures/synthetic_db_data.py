"""Source of truth for the synthetic 835 database fixture.

Every patient name and member id here is fictional. The NPIs are the
repo's **synthetic** placeholders (`_SYNTHETIC_NPI_TO_DOCTOR` in
`remit/config.py`), never the clinic's real ones -- see SECURITY.md. The payer
names are real company names, which identify no patient and already appear in
`remit/config.py`.

The roster is built to exercise the routing rules the 835 source introduced:

* a Noridian visit that fills an existing `2026 Medicare` row, plus the HPSM
  crossover (CARC 23) that settles the same session -- the crossover's money
  must land in that row's `Co-pays Paid` and nowhere else;
* an HPSM primary visit that fills an existing `2026 Medical` row;
* a brand-new visit per payer, so append routing has something to route;
* an HPSM crossover whose Medicare visit is nowhere in the schedule, which must
  be flagged rather than dropped onto a tab;
* a Carelon line, which is out of scope and must be filtered out;
* a telehealth line (modifier 95 with no place of service, as HPSM sends it);
* an OA-18 duplicate of a paid line, which must not zero the payment.
"""

from __future__ import annotations

from dataclasses import dataclass

PAYER_MEDICARE = "NORIDIAN HEALTHCARE SOLUTIONS, LLC"
PAYER_HPSM = "HEALTH PLAN OF SAN MATEO"
PAYER_OUT_OF_SCOPE = "CARELON BEHAVIORAL HEALTH, INC"

#: Synthetic NPIs matching the repo's placeholder provider map.
NPI_SUPERVISING = "1000000001"   # "Dr. A" -- names no employees tab
NPI_ANA = "1000000011"
NPI_OXANA = "1000000012"


@dataclass
class Line:
    """One `master_remittance_records` service-line row."""

    last: str
    first: str
    service_date: str            # ISO
    payer: str
    npi: str
    procedure_code: str
    payment: str                 # TEXT, as the real DB stores it
    patient_responsibility: str  # TEXT
    carc: str
    trace: str
    payment_date: str            # ISO
    modifier_1: str = ""
    location_number: str = "11"

    @property
    def filing_indicator(self) -> str:
        if self.payer == PAYER_MEDICARE:
            return "MB"
        if self.payer == PAYER_HPSM:
            return "HM"
        return "16"


#: Trace numbers, one per remittance batch.
TRACE_MEDICARE = "700000001"
TRACE_HPSM = "800000001"
TRACE_HPSM_LATER = "800000002"
TRACE_CARELON = "900000009"

REMIT_DATE_MEDICARE = "2026-08-10"
REMIT_DATE_HPSM = "2026-08-17"
REMIT_DATE_HPSM_LATER = "2026-09-02"
REMIT_DATE_CARELON = "2026-08-20"

LINES: list[Line] = [
    # --- Fills an existing `2026 Medicare` row --------------------------------
    Line("ASHGROVE", "PETRA", "2026-07-06", PAYER_MEDICARE, NPI_ANA,
         "99213", "92.18", "23.52", "45,253,2", TRACE_MEDICARE, REMIT_DATE_MEDICARE),
    Line("ASHGROVE", "PETRA", "2026-07-06", PAYER_MEDICARE, NPI_ANA,
         "90833", "74.19", "18.92", "45,253,2", TRACE_MEDICARE, REMIT_DATE_MEDICARE),
    # ...and the HPSM crossover settling that same session. CARC 23.
    # Total 42.44 == the coinsurance Medicare left above.
    Line("ASHGROVE", "PETRA", "2026-07-06", PAYER_HPSM, NPI_ANA,
         "99213", "23.52", "0.00", "45,23", TRACE_HPSM, REMIT_DATE_HPSM),
    Line("ASHGROVE", "PETRA", "2026-07-06", PAYER_HPSM, NPI_ANA,
         "90833", "18.92", "0.00", "45,23", TRACE_HPSM, REMIT_DATE_HPSM),

    # --- HPSM primary: fills an existing `2026 Medical` row ------------------
    Line("BELLWEATHER", "COLM", "2026-07-08", PAYER_HPSM, NPI_OXANA,
         "90834", "104.27", "26.60", "45", TRACE_HPSM, REMIT_DATE_HPSM),

    # --- Brand-new Noridian visit -> appended to `2026 Medicare` -------------
    Line("CORDOVA", "INES", "2026-07-14", PAYER_MEDICARE, NPI_SUPERVISING,
         "99214", "130.46", "33.28", "45,253,2", TRACE_MEDICARE, REMIT_DATE_MEDICARE),

    # --- Brand-new HPSM primary visit -> appended to `2026 Medical` ----------
    # Telehealth: modifier 95 and, as HPSM sends it, no place of service.
    Line("DRUMMOND", "FAYE", "2026-07-15", PAYER_HPSM, NPI_OXANA,
         "90837", "133.00", "0.00", "45", TRACE_HPSM, REMIT_DATE_HPSM,
         modifier_1="95", location_number=""),

    # --- Crossover with no Medicare visit anywhere -> flagged ---------------
    Line("EASTMAN", "OSKAR", "2026-07-20", PAYER_HPSM, NPI_ANA,
         "99213", "23.52", "0.00", "45,23", TRACE_HPSM, REMIT_DATE_HPSM),

    # --- OA-18: a later HPSM remit re-adjudicates Bellweather at $0 ---------
    Line("BELLWEATHER", "COLM", "2026-07-08", PAYER_HPSM, NPI_OXANA,
         "90834", "0.00", "0.00", "18", TRACE_HPSM_LATER, REMIT_DATE_HPSM_LATER),

    # --- Out of scope: must be filtered out entirely ------------------------
    Line("FAIRWEATHER", "NOLA", "2026-07-09", PAYER_OUT_OF_SCOPE, NPI_OXANA,
         "90834", "88.00", "12.00", "45", TRACE_CARELON, REMIT_DATE_CARELON),
]

#: Rows added to the schedule fixture so the fills above have somewhere to land.
#: (sheet, patient, service date)
SCHEDULE_SEED = [
    ("2026 Medicare", "Ashgrove, Petra", "2026-07-06"),
    ("2026 Medical", "Bellweather, Colm", "2026-07-08"),
]

#: Row added to Ana's employees tab, so the crossover copay has a row to fill.
EMPLOYEE_SEED = ("Ana", "Ashgrove, Petra", "07/06/2026")
