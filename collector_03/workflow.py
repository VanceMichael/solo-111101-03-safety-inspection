"""--run 工作流：来源拉取、原子归档、对账与业务投影。

所有落库操作围绕内容指纹幂等；同一状态库可以被多个独立进程重复执行，
重跑不产生新历史、新通知或伪造的附件取得时间。
"""

import json
import sqlite3
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .adapter import decode
from .domain import (
    MALFORMED_RECORD,
    reconcile,
    safe_attachment_name,
    validate_record,
)
from .sources import FixtureTransport

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS incoming (
    fingerprint TEXT PRIMARY KEY,
    project TEXT,
    issue TEXT,
    revision INTEGER,
    kind TEXT,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    malformed INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    first_received_at TEXT NOT NULL,
    seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS attachment_versions (
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (filename, sha256)
);
CREATE TABLE IF NOT EXISTS attachment_fetch_log (
    id INTEGER PRIMARY KEY,
    route TEXT NOT NULL,
    filename TEXT,
    status INTEGER NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER,
    fetched_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS pending_events (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    reason TEXT NOT NULL,
    project TEXT,
    issue TEXT,
    revision INTEGER,
    detail TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_open
    ON pending_events(fingerprint, reason)
    WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS issue_states (
    project TEXT NOT NULL,
    issue TEXT NOT NULL,
    state TEXT NOT NULL,
    severity TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project, issue)
);
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def record_fingerprint(source, payload):
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(f"{source}\0{canonical}".encode()).hexdigest()


def _open(database):
    path = Path(database)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    return connection


def _next_seq(connection):
    row = connection.execute("SELECT coalesce(max(seq), 0) + 1 AS next_seq FROM incoming").fetchone()
    return row["next_seq"]


def archive_root_records(connection, transport, route):
    """拉取并归档一个根来源；返回 (新增数, 跳过数, 总归档数)。"""
    response = transport.request(route)
    if response.status != 200:
        raise ValueError(f"source {route} status {response.status}")
    decoded = decode(response.body)
    received_at = utc_now()
    inserted = 0
    seq = _next_seq(connection)
    for raw in decoded:
        fingerprint = record_fingerprint(route, raw)
        existing = connection.execute(
            "SELECT fingerprint FROM incoming WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if existing:
            continue
        normalized, error = validate_record(raw)
        if normalized is None:
            connection.execute(
                "INSERT INTO incoming(fingerprint, project, issue, revision, kind, source,"
                " payload, malformed, error, first_received_at, seq) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fingerprint,
                    str(raw.get("project", "")).strip() or None,
                    str(raw.get("issue", "")).strip() or None,
                    None,
                    None,
                    route,
                    json.dumps(raw, ensure_ascii=False, sort_keys=True),
                    1,
                    error,
                    received_at,
                    seq,
                ),
            )
        else:
            connection.execute(
                "INSERT INTO incoming(fingerprint, project, issue, revision, kind, source,"
                " payload, malformed, error, first_received_at, seq) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fingerprint,
                    normalized["project"],
                    normalized["issue"],
                    normalized["revision"],
                    normalized["kind"],
                    route,
                    json.dumps(normalized["payload"], ensure_ascii=False, sort_keys=True),
                    0,
                    None,
                    received_at,
                    seq,
                ),
            )
        seq += 1
        inserted += 1
    total = connection.execute("SELECT count(*) AS total FROM incoming").fetchone()["total"]
    return inserted, len(decoded) - inserted, total


def fetch_attachment(connection, transport, filename):
    """采集单个附件并按内容摘要归档；空体永远不成为证据。"""
    safe = safe_attachment_name(filename)
    if safe is None:
        connection.execute(
            "INSERT INTO attachment_fetch_log(route, filename, status, fetched_at, note)"
            " VALUES(?,?,?,?,?)",
            (f"/attachment/{filename}", filename, 0, utc_now(), "unsafe attachment name"),
        )
        return None
    route = f"/attachment/{safe}"
    response = transport.request(route)
    fetched_at = utc_now()
    if response.status != 200:
        connection.execute(
            "INSERT INTO attachment_fetch_log(route, filename, status, fetched_at, note)"
            " VALUES(?,?,?,?,?)",
            (route, safe, response.status, fetched_at, "source not available"),
        )
        return None
    body = response.body
    if not body:
        connection.execute(
            "INSERT INTO attachment_fetch_log(route, filename, status, fetched_at, note)"
            " VALUES(?,?,?,?,?)",
            (route, safe, response.status, fetched_at, "empty body cannot be evidence"),
        )
        return None
    digest = sha256(body).hexdigest()
    connection.execute(
        "INSERT INTO attachment_fetch_log(route, filename, status, sha256, size_bytes,"
        " fetched_at, note) VALUES(?,?,?,?,?,?,?)",
        (route, safe, response.status, digest, len(body), fetched_at, "archived"),
    )
    # 同名同内容重复采集不新增版本；同名不同内容并存，取得时间各自保留。
    connection.execute(
        "INSERT OR IGNORE INTO attachment_versions(filename, sha256, size_bytes, archived_at)"
        " VALUES(?,?,?,?)",
        (safe, digest, len(body), fetched_at),
    )
    return digest


def _load_records(connection):
    rows = connection.execute(
        "SELECT * FROM incoming ORDER BY seq"
    ).fetchall()
    records = []
    for row in rows:
        record = dict(row)
        record["payload"] = json.loads(row["payload"])
        records.append(record)
    return records


def _load_attachment_names(connection):
    return {
        row["filename"]
        for row in connection.execute(
            "SELECT DISTINCT filename FROM attachment_versions"
        ).fetchall()
    }


def _reconcile_pending(connection, events):
    """全量对账待核实事件：已消失的事件结案留痕，新事件有序入队。"""
    wanted = {(event["fingerprint"], event["reason"]): event for event in events}
    open_rows = connection.execute(
        "SELECT id, fingerprint, reason FROM pending_events WHERE resolved_at IS NULL"
    ).fetchall()
    for row in open_rows:
        if (row["fingerprint"], row["reason"]) not in wanted:
            connection.execute(
                "UPDATE pending_events SET resolved_at = ? WHERE id = ?",
                (utc_now(), row["id"]),
            )
    for (fingerprint, reason), event in wanted.items():
        existing = connection.execute(
            "SELECT id FROM pending_events WHERE fingerprint = ? AND reason = ?"
            " AND resolved_at IS NULL",
            (fingerprint, reason),
        ).fetchone()
        if existing:
            continue
        connection.execute(
            "INSERT INTO pending_events(fingerprint, reason, project, issue, revision,"
            " detail, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                fingerprint,
                reason,
                event.get("project"),
                event.get("issue"),
                event.get("revision"),
                event["detail"],
                utc_now(),
            ),
        )


def _refresh_issue_states(connection, issues):
    now = utc_now()
    for issue in issues:
        connection.execute(
            "INSERT INTO issue_states(project, issue, state, severity, updated_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(project, issue) DO UPDATE SET"
            " state = excluded.state, severity = excluded.severity, updated_at = ?",
            (
                issue["project"],
                issue["issue"],
                issue["state"],
                issue["severity"],
                now,
                now,
            ),
        )


def _build_projection(connection, reconciliation):
    records = _load_records(connection)
    record_by_fingerprint = {row["fingerprint"]: row for row in records}

    issues = [
        {
            "project": issue["project"],
            "issue": issue["issue"],
            "state": issue["state"],
            "severity": issue["severity"],
        }
        for issue in reconciliation["issues"]
    ]
    issues.sort(key=lambda item: (item["project"], item["issue"]))

    history = []
    for row in records:
        linked = reconciliation["linked"].get(row["fingerprint"], False)
        entry = {
            "project": row["project"],
            "issue": row["issue"],
            "revision": row["revision"],
            "kind": row["kind"],
            "source": row["source"],
            "fingerprint": row["fingerprint"],
            "malformed": bool(row["malformed"]),
            "linked": linked,
            "canonical": row["fingerprint"] in reconciliation["canonical"],
            "received_at": row["first_received_at"],
            "payload": row["payload"],
        }
        if not row.get("malformed") and "target_revision" in row["payload"]:
            entry["target_revision"] = row["payload"]["target_revision"]
        history.append(entry)
    history.sort(
        key=lambda item: (
            item["project"] or "",
            item["issue"] or "",
            item["revision"] if item["revision"] is not None else -1,
            item["received_at"],
            item["fingerprint"],
        )
    )

    pending_rows = connection.execute(
        "SELECT fingerprint, reason, project, issue, revision, detail, created_at,"
        " resolved_at FROM pending_events WHERE resolved_at IS NULL ORDER BY id"
    ).fetchall()
    pending = [dict(row) for row in pending_rows]

    attachment_rows = connection.execute(
        "SELECT filename, sha256, size_bytes, archived_at FROM attachment_versions"
        " ORDER BY filename, archived_at, sha256"
    ).fetchall()
    attachments = []
    for row in attachment_rows:
        versions = []
        for record in records:
            if record.get("malformed"):
                continue
            name = safe_attachment_name(record["payload"].get("attachment", ""))
            if name != row["filename"]:
                continue
            versions.append(
                {
                    "project": record["project"],
                    "issue": record["issue"],
                    "revision": record["revision"],
                    "kind": record["kind"],
                    "source": record["source"],
                    "fingerprint": record["fingerprint"],
                }
            )
        versions.sort(key=lambda item: (item["project"], item["issue"], item["revision"]))
        attachments.append(
            {
                "filename": row["filename"],
                "sha256": row["sha256"],
                "size_bytes": row["size_bytes"],
                "archived_at": row["archived_at"],
                "linked_revisions": versions,
            }
        )

    return {
        "issues": issues,
        "history": history,
        "pending": pending,
        "attachments": attachments,
        "notifications": [dict(row) for row in pending_rows],
    }


def run_workflow(manifest, database):
    """执行一次场景工作流并返回业务投影。"""
    manifest_path = Path(manifest)
    scenario = json.loads(manifest_path.read_text())
    run_id = str(scenario["run_id"])
    sources = list(scenario["sources"])
    options = dict(scenario.get("options") or {})
    fetch_enabled = bool(options.get("fetch_attachments", False))
    fixture_root = scenario["fixture_root"]

    connection = _open(database)
    try:
        transport = FixtureTransport(fixture_root)
        notifications_before = connection.execute(
            "SELECT count(*) AS total FROM pending_events WHERE resolved_at IS NULL"
        ).fetchone()["total"]

        for seq, route in enumerate(sources, start=1):
            connection.execute(
                "INSERT OR IGNORE INTO batches(run_id, seq, source, started_at)"
                " VALUES(?,?,?,?)",
                (run_id, seq, route, utc_now()),
            )
            archive_root_records(connection, transport, route)

        if fetch_enabled:
            referenced = set()
            for record in _load_records(connection):
                if record.get("malformed"):
                    continue
                name = safe_attachment_name(record["payload"].get("attachment", ""))
                if name:
                    referenced.add(name)
            for name in sorted(referenced):
                fetch_attachment(connection, transport, name)
        else:
            # 显式记录本次不采集附件，避免把“尚未到达”误读成“已核验无附件”。
            connection.execute(
                "INSERT INTO attachment_fetch_log(route, filename, status, fetched_at, note)"
                " VALUES(?,?,?,?,?)",
                ("*", None, 0, utc_now(), "fetch_attachments disabled for this run"),
            )

        records = _load_records(connection)
        reconciliation = reconcile(records, _load_attachment_names(connection))
        _reconcile_pending(connection, reconciliation["events"])
        _refresh_issue_states(connection, reconciliation["issues"])

        for seq, route in enumerate(sources, start=1):
            connection.execute(
                "UPDATE batches SET finished_at = ? WHERE run_id = ? AND seq = ?",
                (utc_now(), run_id, seq),
            )
        connection.commit()

        projection = _build_projection(connection, reconciliation)
        notifications_after = len(projection["notifications"])
        projection["notifications_new"] = max(0, notifications_after - notifications_before)
        projection["run_id"] = run_id
        return projection
    finally:
        connection.close()
