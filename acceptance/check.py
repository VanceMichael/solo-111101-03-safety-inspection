import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def contains(actual, expected, location="result"):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise AssertionError(f"{location}: expected object")
        for key, value in expected.items():
            if key not in actual:
                raise AssertionError(f"{location}: missing {key}")
            contains(actual[key], value, f"{location}.{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list):
            raise AssertionError(f"{location}: expected array")
        if not expected and actual:
            raise AssertionError(f"{location}: expected empty array, got {actual!r}")
        for wanted in expected:
            failures = []
            for candidate in actual:
                try:
                    contains(candidate, wanted, location)
                    break
                except AssertionError as error:
                    failures.append(str(error))
            else:
                raise AssertionError(f"{location}: missing item {wanted!r}; {failures}")
    elif type(actual) is not type(expected) or actual != expected:
        raise AssertionError(f"{location}: expected {expected!r}, got {actual!r}")


def explain_missing_evidence(result):
    """缺件场景的 stop 必须能从归档证据反查，而不是状态机的默认值。"""
    issue = next(item for item in result["issues"] if (item["project"], item["issue"]) == ("P1", "H1"))
    assert issue["state"] == "stop"
    assert result["attachments"] == [], "未采集附件时不得出现任何归档证据"
    reasons = {(item["project"], item["issue"], item["revision"], item["reason"]) for item in result["pending"]}
    assert ("P1", "H1", 1, "missing_attachment") in reasons
    assert ("P1", "H1", 2, "missing_attachment") in reasons
    referenced = {
        history["payload"]["attachment"]
        for history in result["history"]
        if (history["project"], history["issue"]) == ("P1", "H1") and history["payload"].get("attachment")
    }
    missing = {item["detail"].split(": ", 1)[1] for item in result["pending"] if item["reason"] == "missing_attachment"}
    assert referenced == missing, "待核实条目必须精确对应检查与复查引用的附件"
    by_revision = {history["revision"]: history for history in result["history"] if (history["project"], history["issue"]) == ("P1", "H1")}
    assert by_revision[1]["kind"] == "inspection" and by_revision[1]["source"] == "/inspection"
    assert by_revision[2]["kind"] == "review" and by_revision[2]["source"] == "/review"


def explain_withdrawal(result):
    """撤回只撤销销号效力：历史与附件仍可反查，状态回到 stop。"""
    issue = next(item for item in result["issues"] if (item["project"], item["issue"]) == ("P1", "H1"))
    assert issue["state"] == "stop"
    history = {(h["revision"], h["kind"]): h for h in result["history"] if (h["project"], h["issue"]) == ("P1", "H1")}
    assert set(history) == {(1, "inspection"), (2, "review"), (3, "withdraw")}
    withdraw = history[(3, "withdraw")]
    assert withdraw["target_revision"] == 2 and withdraw["source"] == "/withdraw"
    assert history[(2, "review")]["canonical"] is True, "撤回不删除被指向的复查历史"
    archived = {a["filename"]: a for a in result["attachments"]}
    assert "photo-a.txt" in archived and "review-a.txt" in archived
    for attachment in archived.values():
        assert len(attachment["sha256"]) == 64
        assert attachment["linked_revisions"], "附件必须保留与记录版本的关联"
    assert all(item["resolved_at"] is None for item in result["pending"])


def run_cli(root, manifest, database, output):
    command = [sys.executable, "-m", "collector_03.cli", "--run", str(manifest),
               "--database", str(database), "--output", str(output)]
    run = subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=5)
    if run.returncode:
        raise AssertionError(f"cli exit={run.returncode}\n{run.stdout}\n{run.stderr}")
    return json.loads(output.read_text())


def write_fixture_tree(directory, files, routes):
    for name, content in files.items():
        (directory / name).write_text(content)
    (directory / "routes.json").write_text(json.dumps(routes, ensure_ascii=False))


def regression_review_before_inspection(root, work):
    """复查先到必须暂存待核实；检查到达后重新关联并销号，重启不新增历史。"""
    fixture = work / "rb_fixtures"
    fixture.mkdir()
    review_xml = (
        '<export><record project="P9" issue="H7" revision="2">'
        "<kind>review</kind><result>pass</result><attachment>rev.txt</attachment>"
        "</record></export>"
    )
    inspection_xml = (
        '<export><record project="P9" issue="H7" revision="1">'
        "<kind>inspection</kind><severity>minor</severity><attachment>ins.txt</attachment>"
        "</record></export>"
    )
    write_fixture_tree(
        fixture,
        {
            "review-only.xml": review_xml,
            "inspection.xml": inspection_xml,
            "ins.txt": "synthetic inspection P9",
            "rev.txt": "synthetic review P9",
        },
        {
            "/review": [{"status": 200, "file": "review-only.xml"}],
            "/inspection": [{"status": 200, "file": "inspection.xml"}],
            "/attachment/ins.txt": [{"status": 200, "file": "ins.txt"}],
            "/attachment/rev.txt": [{"status": 200, "file": "rev.txt"}],
        },
    )
    first_manifest = work / "rb-stage1.json"
    first_manifest.write_text(json.dumps({
        "name": "rb-stage1", "run_id": "run-rb-1", "sources": ["/review"],
        "options": {"fetch_attachments": True}, "fixture_root": str(fixture),
    }))
    second_manifest = work / "rb-stage2.json"
    second_manifest.write_text(json.dumps({
        "name": "rb-stage2", "run_id": "run-rb-2", "sources": ["/review", "/inspection"],
        "options": {"fetch_attachments": True}, "fixture_root": str(fixture),
    }))
    database = work / "rb.sqlite3"
    output = work / "rb-result.json"

    staged = run_cli(root, first_manifest, database, output)
    assert staged["issues"] == [], "检查未到时不得凭空建立隐患投影"
    assert len(staged["history"]) == 1 and staged["history"][0]["linked"] is False
    assert [item["reason"] for item in staged["pending"]] == ["awaiting_inspection"]

    linked = run_cli(root, second_manifest, database, output)
    identity_history = [h for h in linked["history"] if (h["project"], h["issue"]) == ("P9", "H7")]
    assert {h["revision"] for h in identity_history} == {1, 2}
    assert all(h["linked"] for h in identity_history), "检查到达后暂存复查必须重新关联"
    assert linked["issues"] == [{"project": "P9", "issue": "H7", "state": "closed", "severity": "minor"}]
    assert linked["pending"] == []
    history_count = len(linked["history"])
    attachment_count = len(linked["attachments"])

    restarted = run_cli(root, second_manifest, database, output)
    assert len(restarted["history"]) == history_count, "重启重复输入不得新增历史"
    assert len(restarted["attachments"]) == attachment_count, "重复采集不得新增附件版本"
    assert restarted["notifications_new"] == 0 and restarted["pending"] == []
    print("PASS regression_review_before_inspection")


def regression_attachment_late(root, work):
    """附件未到时保持停止并入待核实；补采后重算销号，补采时间不回填且重启幂等。"""
    fixture = work / "al_fixtures"
    fixture.mkdir()
    inspection_xml = (
        '<export><record project="P8" issue="H5" revision="1">'
        "<kind>inspection</kind><severity>major</severity><attachment>evi.txt</attachment>"
        "</record></export>"
    )
    review_xml = (
        '<export><record project="P8" issue="H5" revision="2">'
        "<kind>review</kind><result>pass</result><attachment>evi.txt</attachment>"
        "</record></export>"
    )
    write_fixture_tree(
        fixture,
        {
            "inspection.xml": inspection_xml,
            "review.xml": review_xml,
            "evi.txt": "synthetic shared evidence v1",
        },
        {
            "/inspection": [{"status": 200, "file": "inspection.xml"}],
            "/review": [{"status": 200, "file": "review.xml"}],
            "/attachment/evi.txt": [{"status": 200, "file": "evi.txt"}],
        },
    )
    manifest = work / "al.json"
    manifest.write_text(json.dumps({
        "name": "attachment-late", "run_id": "run-al", "sources": ["/inspection", "/review"],
        "options": {"fetch_attachments": False}, "fixture_root": str(fixture),
    }))
    enabled_manifest = work / "al-enabled.json"
    enabled_manifest.write_text(json.dumps({
        "name": "attachment-late-enabled", "run_id": "run-al-2", "sources": ["/inspection", "/review"],
        "options": {"fetch_attachments": True}, "fixture_root": str(fixture),
    }))
    database = work / "al.sqlite3"
    output = work / "al-result.json"

    blocked = run_cli(root, manifest, database, output)
    issue = next(item for item in blocked["issues"] if (item["project"], item["issue"]) == ("P8", "H5"))
    assert issue["state"] == "stop", "缺证据的重大隐患不得转为可复工"
    assert blocked["attachments"] == []
    assert {item["reason"] for item in blocked["pending"]} == {"missing_attachment"}

    restart_blocked = run_cli(root, manifest, database, output)
    assert len(restart_blocked["pending"]) == len(blocked["pending"])
    assert restart_blocked["notifications_new"] == 0, "待核实条目重复运行不得重复通知"

    resolved = run_cli(root, enabled_manifest, database, output)
    assert resolved["issues"][0]["state"] == "closed"
    assert len(resolved["attachments"]) == 1
    attachment = resolved["attachments"][0]
    assert attachment["sha256"] == hashlib.sha256(b"synthetic shared evidence v1").hexdigest()
    assert {v["revision"] for v in attachment["linked_revisions"]} == {1, 2}
    assert attachment["archived_at"] > blocked["history"][0]["received_at"], "补采时间不得伪造为原取得时间"
    assert resolved["pending"] == []
    assert resolved["notifications_new"] == 0

    restarted = run_cli(root, enabled_manifest, database, output)
    assert len(restarted["attachments"]) == 1, "同名同内容重复采集不增加版本"
    assert restarted["notifications_new"] == 0
    print("PASS regression_attachment_late")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case")
    parser.add_argument("--contracts-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    scenarios = json.loads((root / "fixtures/scenarios.json").read_text())
    if args.case and args.case not in {scenario["name"] for scenario in scenarios}:
        parser.error("unknown scenario")
    started = time.monotonic()
    for scenario in scenarios:
        if args.case and args.case != scenario["name"]:
            continue
        if args.contracts_only:
            assert scenario["expected"] and scenario["sources"]
            routes = json.loads((root / "fixtures/routes.json").read_text())
            assert all(route in routes for route in scenario["sources"])
            for responses in routes.values():
                for response in responses:
                    if "file" in response:
                        assert (root / "fixtures" / response["file"]).is_file()
            print("CONTRACT", scenario["name"])
            continue
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            manifest = work / "scenario.json"
            manifest.write_text(json.dumps({**scenario, "fixture_root": str(root / "fixtures"), "portal_database": str(work / "portal.sqlite3")}, ensure_ascii=False))
            output = work / "result.json"
            command = [sys.executable, "-m", "collector_03.cli", "--run", str(manifest), "--database", str(work / "state.sqlite3"), "--output", str(output)]
            run = subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=5)
            if run.returncode:
                raise AssertionError(f"{scenario['name']}: exit={run.returncode}\n{run.stdout}\n{run.stderr}")
            result = json.loads(output.read_text())
            contains(result, scenario["expected"])
            if scenario["name"] == "missing_evidence":
                explain_missing_evidence(result)
            if scenario["name"] == "withdrawal":
                explain_withdrawal(result)
            output.unlink()
            repeated = subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=5)
            if repeated.returncode:
                raise AssertionError(f"{scenario['name']}: restart exit={repeated.returncode}\n{repeated.stderr}")
            repeat_expected = dict(scenario["expected"])
            if "notifications" in repeat_expected and isinstance(repeat_expected["notifications"], int):
                repeat_expected["notifications"] = 0
            contains(json.loads(output.read_text()), repeat_expected)
            print("PASS", scenario["name"])

    if not args.contracts_only:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            regression_review_before_inspection(root, work)
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            regression_attachment_late(root, work)
    print(f"elapsed={time.monotonic() - started:.3f}s")


if __name__ == "__main__":
    main()
