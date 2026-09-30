"""Build the synthetic 835 database fixture.

Written to a temporary path by the test suite rather than committed: the real
database is PHI and `*.sqlite` is gitignored, so shipping a binary fixture
would mean carving an exception into that rule for a file that takes
milliseconds to generate.

Only the columns the app actually reads are created, plus a couple it ignores,
so a query that assumes a column exists fails loudly here rather than against
the real database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .synthetic_db_data import LINES

#: Mirrors the real table's shape for the columns the app reads. Amounts and
#: dates are TEXT exactly as `remits-extractor` writes them.
_DDL = """
CREATE TABLE master_remittance_records (
    payment_id TEXT,
    claim_id TEXT,
    service_line_id TEXT,
    row_level TEXT,
    payment_trace_number TEXT,
    check_number TEXT,
    payment_date TEXT,
    payer_name TEXT,
    claim_filing_indicator_code TEXT,
    rendering_provider_npi TEXT,
    patient_first_name TEXT,
    patient_middle_name TEXT,
    patient_last_name TEXT,
    patient_member_id TEXT,
    patient_control_number TEXT,
    payer_claim_control_number TEXT,
    service_from_date TEXT,
    service_to_date TEXT,
    procedure_code TEXT,
    modifier_1 TEXT,
    modifier_2 TEXT,
    modifier_3 TEXT,
    modifier_4 TEXT,
    location_number TEXT,
    service_charge_amount TEXT,
    service_payment_amount TEXT,
    PR_adjustment_amount TEXT,
    CO_adjustment_amount TEXT,
    CARC_codes TEXT,
    crossover_carrier_name TEXT
)
"""


def build(path: str | Path) -> Path:
    """Write the fixture database to `path` and return it."""
    path = Path(path)
    if path.exists():
        path.unlink()

    connection = sqlite3.connect(path)
    try:
        connection.execute(_DDL)
        for index, line in enumerate(LINES, start=1):
            connection.execute(
                "INSERT INTO master_remittance_records ("
                " payment_id, claim_id, service_line_id, row_level,"
                " payment_trace_number, check_number, payment_date, payer_name,"
                " claim_filing_indicator_code, rendering_provider_npi,"
                " patient_first_name, patient_last_name, patient_member_id,"
                " patient_control_number, payer_claim_control_number,"
                " service_from_date, service_to_date, procedure_code,"
                " modifier_1, modifier_2, modifier_3, modifier_4,"
                " location_number, service_charge_amount,"
                " service_payment_amount, PR_adjustment_amount,"
                " CO_adjustment_amount, CARC_codes, crossover_carrier_name"
                ") VALUES (" + ",".join("?" * 29) + ")",
                (
                    f"PMT{index:04d}", f"CLM{index:04d}", f"SVC{index:04d}",
                    "service_line",
                    line.trace, "1001", line.payment_date, line.payer,
                    line.filing_indicator, line.npi,
                    line.first, line.last, f"9FAKE{index:04d}A1",
                    f"ACCT{index:04d}", f"PCN{index:04d}",
                    line.service_date, line.service_date, line.procedure_code,
                    line.modifier_1, "", "", "",
                    line.location_number, "200.00",
                    line.payment, line.patient_responsibility,
                    "0.00", line.carc, "",
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def main() -> None:  # pragma: no cover - manual use
    out = Path(__file__).parent / "remittance_data_sample.sqlite"
    build(out)
    print(f"wrote {out} ({len(LINES)} service lines)")


if __name__ == "__main__":  # pragma: no cover
    main()
