import json
import tempfile
import unittest
from pathlib import Path

from collector_03.domain import (
    AWAITING_INSPECTION,
    MALFORMED_RECORD,
    MISSING_ATTACHMENT,
    REVISION_CONFLICT,
    WITHDRAW_TARGET_MISSING,
    reconcile,
    safe_attachment_name,
    validate_record,
)
from collector_03.workflow import (
    _open,
    archive_root_records,
    fetch_attachment,
    record_fingerprint,
    run_workflow,
)
from collector_03.sources import FixtureTransport


def make_fixture(root, files, routes):
    for name, content in files.items():
        (root / name).write_text(content)
    (root / "routes.json").write_text(json.dumps(routes, ensure_ascii=False))


def manifest(root, path, sources, fetch, run_id="run-x"):
    path.write_text(json.dumps({
        "name": "unit",
        "run_id": run_id,
        "sources": sources,
        "options": {"fetch_attachments": fetch},
        "fixture_root": str(root),
    }))


def record(project, issue, revision, kind, source="/inspection", **extra):
    payload = {"project": project, "issue": issue, "revision": str(revision), "kind": kind, **extra}
    return {
        "fingerprint": record_fingerprint(source, payload),
        "project": project,
        "issue": issue,
        "revision": revision,
        "kind": kind,
        "source": source,
        "payload": payload,
        "malformed": False,
        "error": None,
        "seq": 0,
    }


class DomainRuleTests(unittest.TestCase):
    def test_issue_number_is_reused_across_projects(self):
        first = record("P1", "H1", 1, "inspection", severity="major")
        second = record("P2", "H1", 1, "inspection", severity="minor")
        second["seq"] = 1
        result = reconcile([first, second], set())
        states = {(i["project"], i["issue"]): i["state"] for i in result["issues"]}
        self.assertEqual(states, {("P1", "H1"): "stop", ("P2", "H1"): "open"})

    def test_review_before_inspection_is_staged_not_dropped(self):
        review = record("P1", "H1", 2, "review", source="/review", result="pass")
        result = reconcile([review], set())
        self.assertEqual(result["issues"], [])
        self.assertFalse(result["linked"][review["fingerprint"]])
        self.assertEqual([e["reason"] for e in result["events"]], [AWAITING_INSPECTION])
        # 检查到达后重新关联，同一复查记录被链接。
        inspection = record("P1", "H1", 1, "inspection", severity="minor")
        inspection["seq"] = 1
        linked = reconcile([review, inspection], set())
        self.assertTrue(linked["linked"][review["fingerprint"]])

    def test_duplicate_revision_with_different_content_goes_to_pending(self):
        first = record("P1", "H1", 1, "inspection", severity="major")
        second = record("P1", "H1", 1, "inspection", severity="minor")
        second["seq"] = 1
        result = reconcile([first, second], set())
        reasons = {e["reason"] for e in result["events"]}
        self.assertIn(REVISION_CONFLICT, reasons)
        self.assertEqual(result["issues"][0]["severity"], "major", "先归档版本保持权威")

    def test_major_issue_never_reopens_without_full_evidence(self):
        inspection = record("P1", "H1", 1, "inspection", severity="major", attachment="a.txt")
        review = record("P1", "H1", 2, "review", source="/review", result="pass", attachment="b.txt")
        review["seq"] = 1
        # 只有检查附件，缺复查附件。
        result = reconcile([inspection, review], {"a.txt"})
        self.assertEqual(result["issues"][0]["state"], "stop")
        missing = {e["detail"] for e in result["events"] if e["reason"] == MISSING_ATTACHMENT}
        self.assertEqual(missing, {"attachment not archived: b.txt"})

    def test_withdraw_targeting_missing_revision_is_queued(self):
        inspection = record("P1", "H1", 1, "inspection", severity="major")
        withdraw = record("P1", "H1", 3, "withdraw", source="/withdraw", target_revision=9)
        withdraw["seq"] = 1
        result = reconcile([inspection, withdraw], set())
        self.assertIn(WITHDRAW_TARGET_MISSING, {e["reason"] for e in result["events"]})
        self.assertEqual(result["issues"][0]["state"], "stop")

    def test_withdraw_revokes_close_but_keeps_history(self):
        inspection = record("P1", "H1", 1, "inspection", severity="major", attachment="a.txt")
        review = record("P1", "H1", 2, "review", source="/review", result="pass", attachment="b.txt")
        review["seq"] = 1
        withdraw = record("P1", "H1", 3, "withdraw", source="/withdraw", target_revision=2)
        withdraw["seq"] = 2
        result = reconcile([inspection, review, withdraw], {"a.txt", "b.txt"})
        self.assertEqual(result["issues"][0]["state"], "stop")
        self.assertIn(review["fingerprint"], result["canonical"], "复查历史不删除")

    def test_invalid_severity_fails_closed(self):
        inspection = record("P1", "H1", 1, "inspection", severity="unknown")
        result = reconcile([inspection], set())
        self.assertEqual(result["issues"][0]["state"], "stop")
        self.assertIn(MALFORMED_RECORD, {e["reason"] for e in result["events"]})

    def test_unsafe_attachment_name_is_rejected(self):
        for name in ("../x.txt", "dir/x.txt", ".hidden", "a\\b.txt"):
            self.assertIsNone(safe_attachment_name(name))
        self.assertEqual(safe_attachment_name("ok.txt"), "ok.txt")

    def test_validate_record_reports_errors_instead_of_raising(self):
        normalized, error = validate_record({"project": "P1", "issue": "H1", "kind": "inspection"})
        self.assertIsNone(normalized)
        self.assertIn("revision", error)
        normalized, error = validate_record(
            {"project": "P1", "issue": "H1", "revision": 2, "kind": "withdraw"}
        )
        self.assertIsNone(normalized)


class WorkflowPersistenceTests(unittest.TestCase):
    def build(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        work = Path(temp.name)
        root = work / "fixtures"
        root.mkdir()
        return work, root

    def test_duplicate_import_does_not_grow_history(self):
        work, root = self.build()
        make_fixture(
            root,
            {"in.xml": '<export><record project="P1" issue="H1" revision="1">'
                       "<kind>inspection</kind><severity>minor</severity></record></export>"},
            {"/inspection": [{"status": 200, "file": "in.xml"}]},
        )
        scenario = work / "s.json"
        manifest(root, scenario, ["/inspection"], False)
        first = run_workflow(scenario, work / "state.sqlite3")
        second = run_workflow(scenario, work / "state.sqlite3")
        self.assertEqual(len(first["history"]), 1)
        self.assertEqual(len(second["history"]), 1)
        self.assertEqual(second["notifications_new"], 0)

    def test_same_filename_changed_content_keeps_two_versions(self):
        work, root = self.build()
        make_fixture(
            root,
            {"e.txt": "version one"},
            {"/attachment/e.txt": [{"status": 200, "file": "e.txt"}]},
        )
        connection = _open(work / "state.sqlite3")
        transport = FixtureTransport(root)
        first = fetch_attachment(connection, transport, "e.txt")
        (root / "e.txt").write_text("version two with different bytes")
        second = fetch_attachment(connection, transport, "e.txt")
        again = fetch_attachment(connection, transport, "e.txt")
        versions = connection.execute("SELECT count(*) AS total FROM attachment_versions").fetchone()["total"]
        self.assertEqual(versions, 2)
        self.assertNotEqual(first, second)
        self.assertEqual(second, again, "同内容重复采集不新增版本")
        connection.close()

    def test_empty_attachment_body_is_not_evidence(self):
        work, root = self.build()
        make_fixture(
            root,
            {"empty.txt": ""},
            {"/attachment/empty.txt": [{"status": 200, "file": "empty.txt"}]},
        )
        connection = _open(work / "state.sqlite3")
        digest = fetch_attachment(connection, FixtureTransport(root), "empty.txt")
        versions = connection.execute("SELECT count(*) AS total FROM attachment_versions").fetchone()["total"]
        self.assertIsNone(digest)
        self.assertEqual(versions, 0)
        connection.close()

    def test_malformed_record_enters_pending_instead_of_being_dropped(self):
        work, root = self.build()
        make_fixture(
            root,
            {"bad.xml": '<export><record project="P1" issue="H9" revision="x">'
                        "<kind>inspection</kind></record></export>"},
            {"/inspection": [{"status": 200, "file": "bad.xml"}]},
        )
        scenario = work / "s.json"
        manifest(root, scenario, ["/inspection"], False)
        result = run_workflow(scenario, work / "state.sqlite3")
        self.assertEqual(len(result["history"]), 1)
        self.assertTrue(result["history"][0]["malformed"])
        self.assertEqual(result["pending"][0]["reason"], MALFORMED_RECORD)
        self.assertEqual(result["issues"], [])

    def test_archive_is_idempotent_at_store_level(self):
        work, root = self.build()
        make_fixture(
            root,
            {"in.xml": '<export><record project="P1" issue="H1" revision="1">'
                       "<kind>inspection</kind><severity>minor</severity></record></export>"},
            {"/inspection": [{"status": 200, "file": "in.xml"}]},
        )
        connection = _open(work / "state.sqlite3")
        transport = FixtureTransport(root)
        first_inserted, _, _ = archive_root_records(connection, transport, "/inspection")
        second_inserted, _, total = archive_root_records(connection, transport, "/inspection")
        self.assertEqual((first_inserted, second_inserted, total), (1, 0, 1))
        connection.close()


if __name__ == "__main__":
    unittest.main()
