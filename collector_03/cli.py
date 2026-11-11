import argparse
import json
from pathlib import Path

from .store import RecordStore
from .capture import capture
from .workflow import run_workflow


def main(argv=None):
    parser = argparse.ArgumentParser(description='施工安全巡检证据链采集')
    parser.add_argument("--database", default="collector.sqlite3")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--capture", metavar="FIXTURE_ROOT")
    parser.add_argument("--route", default='/inspection')
    parser.add_argument("--observed-at", default="2026-11-11T10:00:00Z")
    parser.add_argument("--run", metavar="SCENARIO", dest="run_manifest")
    parser.add_argument("--output", metavar="RESULT")
    args = parser.parse_args(argv)
    if args.capture:
        print(json.dumps(capture(args.capture, args.route, args.database, args.observed_at), ensure_ascii=False))
        return 0
    if args.run_manifest:
        result = run_workflow(args.run_manifest, args.database)
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(rendered + "\n")
        print(rendered)
        return 0
    if args.init:
        store = RecordStore(args.database)
        store.initialize()
        print(json.dumps({"stored": store.count()}))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
