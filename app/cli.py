from __future__ import annotations

import argparse
import json

from app.dependencies import get_db, get_knowledge_file_service


def main() -> None:
    parser = argparse.ArgumentParser(description="Bambi knowledge agent maintenance CLI")
    parser.add_argument("command", choices=["status", "conflicts", "list-tools"])
    args = parser.parse_args()

    db = get_db()
    knowledge_files = get_knowledge_file_service()

    if args.command == "status":
        tools = knowledge_files.list_tools()
        payload = {
            "source_type": "local_knowledge_files",
            "total_sources": sum(1 for item in tools if item["file_name"]),
            "total_tools": len(tools),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.command == "conflicts":
        rows = [dict(row) for row in db.get_conflicts()]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif args.command == "list-tools":
        print(json.dumps(knowledge_files.list_tools(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
