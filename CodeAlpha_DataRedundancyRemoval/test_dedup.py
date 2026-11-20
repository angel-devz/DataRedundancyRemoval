"""Run:  python -m unittest -v"""

import csv
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite://")  # app tests use an in-memory DB

import dedup
from database import Store

HERE = Path(__file__).parent
BASE = {"name": "John Smith", "email": "john.smith@gmail.com", "phone": "+1 415 555 0101", "city": "San Francisco"}


class ValidationTests(unittest.TestCase):
    def test_valid_record_is_normalized(self):
        r, errors = dedup.validate({"name": "  JOSÉ  Álvarez ", "email": " Jose@Gmail.COM ",
                                    "phone": "+57 (301) 234-5678", "city": "Bogotá"})
        self.assertEqual(errors, [])
        self.assertEqual((r.name, r.email, r.phone, r.city), ("jose alvarez", "jose@gmail.com", "3012345678", "bogota"))

    def test_invalid_records_are_rejected(self):
        for bad in ({**BASE, "email": "nope@"}, {**BASE, "phone": "123"}, {**BASE, "name": ""}):
            self.assertEqual(dedup.validate(bad)[0], None, bad)


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store("sqlite://")
        self.store.ingest(BASE)

    def status(self, **changes):
        return self.store.ingest({**BASE, **changes}).status

    def test_exact_duplicate_is_redundant(self):
        self.assertEqual(self.status(), "redundant")

    def test_formatting_differences_are_redundant(self):
        self.assertEqual(self.status(name="JOHN  SMITH", email="John.Smith@GMAIL.com", phone="415-555-0101"), "redundant")

    def test_same_email_or_phone_is_redundant(self):
        self.assertEqual(self.status(name="Johnny S", phone="+1 212 000 1111"), "redundant")
        self.assertEqual(self.status(name="J Smith", email="other@example.com"), "redundant")

    def test_typo_near_duplicate_is_redundant(self):
        self.assertEqual(self.status(email="john.smith@gmial.com", phone="+1 415 555 0102"), "redundant")

    def test_same_name_different_person_is_false_positive(self):
        d = self.store.ingest({**BASE, "email": "jsmith.dev@protonmail.com", "phone": "+44 20 7946 0958", "city": "London"})
        self.assertEqual(d.status, "false_positive")
        self.assertIsNotNone(d.record_id)  # false positives ARE stored

    def test_different_person_is_unique(self):
        self.assertEqual(self.status(name="Emily Brown", email="emily@b.com", phone="+61 412 345 678", city="Sydney"), "unique")

    def test_only_unique_and_false_positive_are_appended(self):
        self.status()                       # redundant
        self.status(email="bad")            # invalid
        self.assertEqual(len(self.store.list_records()), 1)

    def test_dry_run_does_not_write(self):
        self.store.ingest({**BASE, "name": "New Person", "email": "new@x.com", "phone": "+1 999 888 7777"}, dry_run=True)
        self.assertEqual(len(self.store.list_records()), 1)

    def test_concurrent_duplicates_store_only_one(self):
        tmp = tempfile.TemporaryDirectory()
        store = Store(f"sqlite:///{tmp.name}/c.db")
        rec = {"name": "Race Test", "email": "race@x.com", "phone": "+1 222 333 4444", "city": ""}
        threads = [threading.Thread(target=store.ingest, args=(rec,)) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(store.list_records()), 1)
        store.engine.dispose()
        tmp.cleanup()

    def test_sample_file_matches_expected_labels(self):
        store = Store("sqlite://")
        with open(HERE / "sample_data.csv", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                expected = row.pop("expected")
                self.assertEqual(store.ingest(row).status, expected, row)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.client = app_module.app.test_client()

    def test_add_then_duplicate(self):
        rec = {"name": "Api User", "email": "api.user@example.com", "phone": "+1 555 123 9876", "city": "Austin"}
        r1 = self.client.post("/api/records", json=rec)
        r2 = self.client.post("/api/records", json=rec)
        self.assertEqual((r1.status_code, r1.get_json()["status"]), (201, "unique"))
        self.assertEqual((r2.status_code, r2.get_json()["status"]), (409, "redundant"))

    def test_invalid_is_422_and_bad_body_is_400(self):
        self.assertEqual(self.client.post("/api/records", json={"name": "x"}).status_code, 422)
        self.assertEqual(self.client.post("/api/records", data="text").status_code, 400)

    def test_check_does_not_store(self):
        before = len(self.client.get("/api/records").get_json())
        self.client.post("/api/check", json={"name": "Check Only", "email": "c@o.com", "phone": "+1 444 555 6666"})
        self.assertEqual(len(self.client.get("/api/records").get_json()), before)

    def test_csv_upload(self):
        data = (HERE / "sample_data.csv").read_bytes()
        res = self.client.post("/api/upload", data={"file": (io.BytesIO(data), "sample.csv")},
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(sum(res.get_json()["summary"].values()), 20)

    def test_upload_requires_columns(self):
        res = self.client.post("/api/upload", data={"file": (io.BytesIO(b"a,b\n1,2\n"), "x.csv")},
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)

    def test_page_stats_health(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn("redundancy_rate", self.client.get("/api/stats").get_json())
        self.assertEqual(self.client.get("/health").get_json(), {"status": "ok"})


class CleanCsvTests(unittest.TestCase):
    def test_clean_csv(self):
        from clean_csv import clean
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clean.csv"
            counts = clean(str(HERE / "sample_data.csv"), str(out))
            with out.open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), counts["unique"] + counts["false_positive"])
            self.assertEqual(len({r["email"] for r in rows}), len(rows))
            self.assertTrue((Path(tmp) / "clean_report.csv").exists())


if __name__ == "__main__":
    unittest.main()
