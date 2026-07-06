"""Ad-hoc question tester: log in as a user, ask questions, print SQL + rows.

Usage: python scripts/qtest.py <username> "question one" "question two" ...
Defaults to a ZSM battery if no questions are given.
"""

from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"
PASSWORD = "botree123"

ZSM_BATTERY = [
    "what are the total sales this month",
    "top distributors by sales value in my zone",
    "total order value this month",
    "how many invoices were raised this month",
    "which product sold the most this month",
    "top 5 salesmen by sales this month",
    "total outstanding amount for customers",
    "sales by state in my zone",
]


def login(client: httpx.Client, user: str) -> str:
    r = client.post(f"{BASE}/api/auth/login", json={"username": user, "password": PASSWORD})
    r.raise_for_status()
    return r.json()["token"]


def ask(client: httpx.Client, token: str, question: str) -> dict:
    body = {"messages": [{"role": "user", "parts": [{"type": "text", "text": question}]}]}
    out = {"sql": None, "rows": None, "columns": None, "row_count": None, "cached": None, "text": ""}
    with client.stream(
        "POST", f"{BASE}/api/chat", json=body,
        headers={"Authorization": f"Bearer {token}"}, timeout=120,
    ) as resp:
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                evt = json.loads(data)
            except json.JSONDecodeError:
                continue
            t = evt.get("type")
            if t == "tool-input-available":
                out["sql"] = (evt.get("input") or {}).get("sql")
            elif t == "tool-output-available":
                o = evt.get("output") or {}
                out["rows"] = o.get("rows")
                out["columns"] = o.get("columns")
                out["row_count"] = o.get("row_count")
                out["cached"] = o.get("cached")
            elif t == "text-delta":
                out["text"] += str(evt.get("delta", ""))
    return out


def main() -> None:
    user = sys.argv[1] if len(sys.argv) > 1 else "zsm"
    questions = sys.argv[2:] or ZSM_BATTERY
    with httpx.Client() as client:
        token = login(client, user)
        print(f"\n===== USER: {user.upper()} =====\n")
        ok = 0
        for q in questions:
            r = ask(client, token, q)
            has = bool(r["rows"])
            if has:
                ok += 1
            status = "DATA" if has else ("EMPTY" if r["row_count"] is not None else "NO-SQL")
            print(f"[{status}] {q}")
            if r["sql"]:
                print(f"   SQL: {r['sql'][:150]}{'...' if r['sql'] and len(r['sql']) > 150 else ''}")
            if has:
                cols = r["columns"] or list(r["rows"][0].keys())
                print(f"   cols: {', '.join(map(str, cols))}  (rows={r['row_count']}, cached={r['cached']})")
                for row in r["rows"][:3]:
                    print("     " + " | ".join(str(row.get(c)) for c in cols))
            elif r["row_count"] is not None:
                print(f"   (query ran, 0 rows) — answer: {r['text'][:80]}")
            else:
                print(f"   answer: {r['text'][:100]}")
            print()
        print(f"----- {ok}/{len(questions)} returned data -----\n")


if __name__ == "__main__":
    main()
