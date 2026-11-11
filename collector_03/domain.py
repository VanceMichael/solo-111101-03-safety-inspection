"""巡检证据链的纯领域规则。

本模块不接触 SQLite 与传输层，只对已归档的记录做校验、对账与状态归并，
便于在不构造完整运行环境的情况下做边界回归。
"""

from collections import defaultdict

INSPECTION = "inspection"
REVIEW = "review"
WITHDRAW = "withdraw"
VALID_KINDS = (INSPECTION, REVIEW, WITHDRAW)
VALID_SEVERITIES = ("major", "minor")

AWAITING_INSPECTION = "awaiting_inspection"
MISSING_ATTACHMENT = "missing_attachment"
INVALID_ATTACHMENT_REF = "invalid_attachment_ref"
REVISION_CONFLICT = "revision_conflict"
WITHDRAW_TARGET_MISSING = "withdraw_target_missing"
MALFORMED_RECORD = "malformed_record"

CLOSED = "closed"
STOP = "stop"
OPEN = "open"


def safe_attachment_name(name):
    """附件名只允许纯文件名，任何路径形态都拒绝并转入待核实。"""
    if not isinstance(name, str) or not name:
        return None
    if name.startswith(".") or "/" in name or "\\" in name or ".." in name:
        return None
    return name


def validate_record(raw):
    """校验单条解码记录，返回 (normalized, error)。

    校验失败不抛异常：调用方负责把失败记录原样归档并挂到待核实队列，
    异常记录永远不会被静默丢弃。
    """
    if not isinstance(raw, dict):
        return None, "record is not an object"
    payload = dict(raw)
    project = str(payload.get("project", "")).strip()
    issue = str(payload.get("issue", "")).strip()
    kind = str(payload.get("kind", "")).strip()
    if not project or not issue:
        return None, "missing project or issue"
    if kind not in VALID_KINDS:
        return None, f"unknown kind {kind!r}"
    try:
        revision = int(payload.get("revision"))
    except (TypeError, ValueError):
        return None, "revision must be an integer"
    if revision < 1:
        return None, "revision must start at 1"
    if kind == WITHDRAW:
        try:
            payload["target_revision"] = int(payload.get("target_revision"))
        except (TypeError, ValueError):
            return None, "withdraw needs integer target_revision"
    normalized = {
        "project": project,
        "issue": issue,
        "revision": revision,
        "kind": kind,
        "payload": payload,
    }
    return normalized, None


def _event(events, reason, record, detail):
    events.append(
        {
            "reason": reason,
            "project": record.get("project"),
            "issue": record.get("issue"),
            "revision": record.get("revision"),
            "fingerprint": record["fingerprint"],
            "detail": detail,
        }
    )


def _evidence_complete(record, archived_filenames):
    """一条记录引用的附件是否已真实归档；空附件名视为无附件要求。"""
    name = record["payload"].get("attachment", "")
    if not name:
        return True
    safe = safe_attachment_name(name)
    if safe is None:
        return False
    return safe in archived_filenames


def reconcile(records, archived_filenames):
    """对全部已归档记录做一次幂等对账。

    records: 含 fingerprint/seq/source/received_at/malformed 的归档行。
    archived_filenames: 至少存在一个非空归档版本的附件文件名集合。
    返回 issues、待核实事件、关联与规范版本标记。
    """
    archived_filenames = set(archived_filenames)
    events = []
    by_identity = defaultdict(list)
    linked = {}
    canonical = set()

    for record in records:
        if record.get("malformed"):
            linked[record["fingerprint"]] = False
            _event(events, MALFORMED_RECORD, record, record.get("error") or "malformed record")
            continue
        by_identity[(record["project"], record["issue"])].append(record)

    issues = []
    for (project, issue), rows in sorted(by_identity.items()):
        rows.sort(key=lambda row: (row["revision"], row["seq"]))
        inspections = [row for row in rows if row["kind"] == INSPECTION]

        # 复查/撤回先到：联合身份尚无检查记录，整体暂存待关联。
        if not inspections:
            for row in rows:
                linked[row["fingerprint"]] = False
                _event(
                    events,
                    AWAITING_INSPECTION,
                    row,
                    "inspection record not yet archived for this identity",
                )
            continue

        for row in rows:
            linked[row["fingerprint"]] = True

        groups = defaultdict(list)
        for row in rows:
            groups[row["revision"]].append(row)
        canonical_by_revision = {}
        for revision, group in sorted(groups.items()):
            group.sort(key=lambda row: row["seq"])
            winner = group[0]
            canonical_by_revision[revision] = winner
            canonical.add(winner["fingerprint"])
            for loser in group[1:]:
                _event(
                    events,
                    REVISION_CONFLICT,
                    loser,
                    f"revision {revision} already archived with different content",
                )

        # 证据缺项检查：检查与复查引用的附件都必须真实归档。
        for row in rows:
            name = row["payload"].get("attachment", "")
            if not name:
                continue
            if safe_attachment_name(name) is None:
                _event(
                    events,
                    INVALID_ATTACHMENT_REF,
                    row,
                    f"unsafe attachment reference: {name!r}",
                )
            elif name not in archived_filenames:
                _event(
                    events,
                    MISSING_ATTACHMENT,
                    row,
                    f"attachment not archived: {name}",
                )

        present_revisions = set(groups)
        for row in rows:
            if row["kind"] != WITHDRAW:
                continue
            target = row["payload"].get("target_revision")
            target_row = canonical_by_revision.get(target)
            if target not in present_revisions or target_row is None or target_row["kind"] != REVIEW:
                _event(
                    events,
                    WITHDRAW_TARGET_MISSING,
                    row,
                    f"target revision {target} is not an archived review",
                )

        establishing = sorted(inspections, key=lambda row: (row["revision"], row["seq"]))[0]
        severity = establishing["payload"].get("severity")
        if severity not in VALID_SEVERITIES:
            # 资料异常时向更严格一侧归并，绝不放松为可复工。
            severity = "major"
            _event(
                events,
                MALFORMED_RECORD,
                establishing,
                "invalid severity; fail closed as major",
            )

        withdrawn_targets = {
            row["payload"].get("target_revision")
            for row in rows
            if row["kind"] == WITHDRAW
        }
        closed = False
        for row in rows:
            if row["kind"] != REVIEW or row["payload"].get("result") != "pass":
                continue
            if row["fingerprint"] not in canonical:
                continue
            if row["revision"] in withdrawn_targets:
                continue
            if not _evidence_complete(establishing, archived_filenames):
                continue
            if not _evidence_complete(row, archived_filenames):
                continue
            closed = True

        state = CLOSED if closed else (STOP if severity == "major" else OPEN)
        issues.append(
            {
                "project": project,
                "issue": issue,
                "state": state,
                "severity": severity,
            }
        )

    return {
        "issues": issues,
        "events": events,
        "linked": linked,
        "canonical": canonical,
    }
