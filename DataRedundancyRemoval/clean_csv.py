"""Remove redundancy from an existing CSV file.

  python clean_csv.py dirty.csv clean.csv

Runs every row through the same validation + duplicate pipeline using a temporary
in-memory database, writes only unique/verified rows to clean.csv, and writes a
report (clean_report.csv) explaining what happened to every input row.
"""

import csv
import sys
from collections import Counter
from pathlib import Path

from database import Store


def clean(src: str, dst: str):
    store = Store("sqlite://")  # in-memory, nothing touches the real database
    counts = Counter()
    report_path = Path(dst).with_name(Path(dst).stem + "_report.csv")

    with open(src, newline="", encoding="utf-8-sig") as fin, \
         open(dst, "w", newline="", encoding="utf-8") as fout, \
         open(report_path, "w", newline="", encoding="utf-8") as frep:
        reader = csv.DictReader(fin)
        out = csv.DictWriter(fout, fieldnames=["name", "email", "phone", "city"])
        rep = csv.writer(frep)
        out.writeheader()
        rep.writerow(["row", "name", "email", "status", "reason", "matched_row_id"])

        for i, row in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "") for k, v in row.items()}
            raw = {k: row.get(k, "") for k in ("name", "email", "phone", "city")}
            d = store.ingest(raw)
            counts[d.status] += 1
            rep.writerow([i, raw["name"], raw["email"], d.status, d.reason, d.matched_id or ""])
            if d.should_insert:
                r = d.record
                out.writerow({"name": r.name.title(), "email": r.email, "phone": r.phone, "city": r.city.title()})

    total = sum(counts.values())
    kept = counts["unique"] + counts["false_positive"]
    print(f"Rows read:            {total}")
    print(f"Kept (unique):        {counts['unique']}")
    print(f"Kept (false positive):{counts['false_positive']:>3}")
    print(f"Removed (redundant):  {counts['redundant']}")
    print(f"Removed (invalid):    {counts['invalid']}")
    print(f"\n{kept}/{total} rows written to {dst}; details in {report_path}")
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python clean_csv.py <input.csv> <output.csv>")
    clean(sys.argv[1], sys.argv[2])
