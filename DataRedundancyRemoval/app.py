"""Flask web service for the Data Redundancy Removal System.

Endpoints
  GET  /                 dashboard web page
  POST /api/check        validate + classify a record WITHOUT saving (dry run)
  POST /api/records      validate + classify + append only if unique
  POST /api/upload       CSV file upload, every row goes through the same pipeline
  GET  /api/records      stored (clean) records
  GET  /api/log          recent decisions (audit trail)
  GET  /api/stats        counts per classification
  GET  /health           health check for the cloud host
"""

import csv
import io
import os

from flask import Flask, jsonify, render_template, request

from database import Store

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB uploads
store = Store()

FIELDS = ("name", "email", "phone", "city")
MAX_UPLOAD_ROWS = 5000
STATUS_CODES = {"unique": 201, "false_positive": 201, "redundant": 409, "invalid": 422}


def _payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None
    return {k: str(data.get(k, ""))[:254] for k in FIELDS}


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/check")
def check():
    raw = _payload()
    if raw is None:
        return jsonify(error="JSON body required"), 400
    return jsonify(store.ingest(raw, dry_run=True).to_dict())


@app.post("/api/records")
def add_record():
    raw = _payload()
    if raw is None:
        return jsonify(error="JSON body required"), 400
    decision = store.ingest(raw)
    return jsonify(decision.to_dict()), STATUS_CODES[decision.status]


@app.post("/api/upload")
def upload():
    file = request.files.get("file")
    if not file:
        return jsonify(error="upload a CSV file in the 'file' field"), 400
    try:
        text = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify(error="file must be UTF-8 encoded CSV"), 400

    reader = csv.DictReader(io.StringIO(text))
    headers = {h.strip().lower() for h in (reader.fieldnames or [])}
    missing = [f for f in ("name", "email", "phone") if f not in headers]
    if missing:
        return jsonify(error=f"CSV is missing columns: {', '.join(missing)}"), 400

    results, summary = [], {"unique": 0, "false_positive": 0, "redundant": 0, "invalid": 0}
    for i, row in enumerate(reader, start=2):  # row 1 is the header
        if i - 1 > MAX_UPLOAD_ROWS:
            break
        row = {(k or "").strip().lower(): (v or "") for k, v in row.items()}
        raw = {k: row.get(k, "")[:254] for k in FIELDS}
        d = store.ingest(raw)
        summary[d.status] += 1
        results.append({"row": i, "input": raw, "status": d.status, "reason": d.reason,
                        "score": round(d.score, 3), "matched_id": d.matched_id})
    return jsonify(summary=summary, rows=results)


@app.get("/api/records")
def list_records():
    return jsonify(store.list_records())


@app.get("/api/log")
def log():
    return jsonify(store.recent_log())


@app.get("/api/stats")
def stats():
    return jsonify(store.stats())


@app.get("/health")
def health():
    return jsonify(status="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=False)
