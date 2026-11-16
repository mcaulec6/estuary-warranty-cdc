"""
Ask questions about the warranty data in plain English.

How it works:
  1. Reads the table and column names from MotherDuck (information_schema).
  2. Sends your question plus that schema to Claude, which writes one DuckDB SELECT.
  3. Checks the SQL is read-only, runs it, and retries once if the query errors.
  4. Asks Claude to summarise the result rows in a sentence or two.

Usage:
    python ask.py "Which component has the highest total claim cost this week?"
    python ask.py            # interactive mode
    python ask.py --show-sql "How many claims are still open per market?"

Env vars: MOTHERDUCK_TOKEN, ANTHROPIC_API_KEY,
          MOTHERDUCK_DATABASE (default warranty), MOTHERDUCK_SCHEMA (default public),
          ANTHROPIC_MODEL (default claude-sonnet-5-5)
"""

import argparse
import os
import re
import sys

import anthropic
import duckdb

DATABASE = os.environ.get("MOTHERDUCK_DATABASE", "warranty")
SCHEMA = os.environ.get("MOTHERDUCK_SCHEMA", "public")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
ROW_LIMIT = 200

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|attach|detach|"
    r"copy|export|import|install|load|pragma|set|reset|call|grant|revoke|vacuum|checkpoint)\b",
    re.IGNORECASE,
)


def connect() -> duckdb.DuckDBPyConnection:
    if not os.environ.get("MOTHERDUCK_TOKEN"):
        sys.exit("Set MOTHERDUCK_TOKEN (see .env.example)")
    # DuckDB picks up MOTHERDUCK_TOKEN from the environment automatically.
    return duckdb.connect(f"md:{DATABASE}")


def describe_schema(con) -> tuple[str, bool]:
    """Return a text description of the user tables, and whether a soft-delete column exists."""
    rows = con.execute(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_catalog = ? AND table_schema = ?
          AND table_name NOT LIKE 'flow\\_%' ESCAPE '\\'   -- skip Estuary's metadata tables
          AND column_name <> 'flow_document'               -- raw JSON copy of each row
        ORDER BY table_name, ordinal_position
        """,
        [DATABASE, SCHEMA],
    ).fetchall()
    if not rows:
        sys.exit(f"No tables found in {DATABASE}.{SCHEMA}. Is the Estuary materialization running?")

    tables: dict[str, list[str]] = {}
    has_op = False
    for table, column, dtype in rows:
        tables.setdefault(table, []).append(f'"{column}" {dtype}')
        has_op = has_op or column == "_meta/op"
    text = "\n".join(f"{SCHEMA}.{t}({', '.join(cols)})" for t, cols in tables.items())
    return text, has_op


def system_prompt(schema_text: str, has_op: bool) -> str:
    notes = [
        "Data: fake vehicle warranty claims streamed from Postgres via Estuary CDC.",
        "claim_amount is in whole euros. status moves submitted -> under_review -> approved/rejected -> paid.",
        "high_value_claims (if present) holds only claims >= EUR 2000, with a severity column.",
        "flow_published_at is when Estuary last published that row.",
        "Always double-quote column names that contain '/' (e.g. \"_meta/op\").",
    ]
    if has_op:
        notes.append("Rows where \"_meta/op\" = 'd' are deleted records: exclude them unless asked about deletions.")
    return (
        "You translate questions into a single read-only DuckDB SQL query.\n"
        f"Tables:\n{schema_text}\n\nNotes:\n- " + "\n- ".join(notes) + "\n\n"
        "Reply with only the SQL in a ```sql code block. One SELECT (or WITH ... SELECT) statement, no commentary."
    )


def extract_sql(text: str) -> str:
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    sql = (match.group(1) if match else text).strip().rstrip(";").strip()
    return sql


def check_read_only(sql: str) -> None:
    if not re.match(r"^(select|with)\b", sql, re.IGNORECASE):
        raise ValueError("Refusing to run: query must start with SELECT or WITH")
    if ";" in sql:
        raise ValueError("Refusing to run: only one statement allowed")
    if FORBIDDEN.search(sql):
        raise ValueError("Refusing to run: query contains a write or admin keyword")


def ask_claude(client, system: str, messages: list[dict]) -> str:
    resp = client.messages.create(model=MODEL, max_tokens=1024, system=system, messages=messages)
    return "".join(block.text for block in resp.content if block.type == "text")


def answer(question: str, con, client, show_sql: bool) -> None:
    schema_text, has_op = describe_schema(con)
    system = system_prompt(schema_text, has_op)
    messages = [{"role": "user", "content": question}]

    for attempt in (1, 2):
        reply = ask_claude(client, system, messages)
        sql = extract_sql(reply)
        try:
            check_read_only(sql)
            result = con.execute(f"SELECT * FROM ({sql}) AS q LIMIT {ROW_LIMIT}")
            columns = [d[0] for d in result.description]
            rows = result.fetchall()
            break
        except Exception as err:  # bad SQL or failed safety check
            if attempt == 2:
                print(f"Couldn't answer that.\nSQL: {sql}\nError: {err}")
                return
            # Self-correction: give Claude the error and let it fix the query once
            messages += [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": f"That query failed with: {err}\nFix it and reply with only the corrected SQL."},
            ]

    if show_sql:
        print(f"\nSQL:\n{sql}\n")

    preview = "\n".join([" | ".join(columns)] + [" | ".join(map(str, r)) for r in rows[:50]])
    summary = ask_claude(
        client,
        "Answer the user's question in one to three plain sentences using only the query result given. "
        "If the result is empty, say so.",
        [{"role": "user", "content": f"Question: {question}\n\nQuery result ({len(rows)} rows):\n{preview}"}],
    )
    print(summary.strip())
    print(f"\n{preview}" if len(rows) <= 20 else f"\n({len(rows)} rows; first 50 shown above to Claude)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Plain-English questions over the warranty data")
    parser.add_argument("question", nargs="*", help="your question (omit for interactive mode)")
    parser.add_argument("--show-sql", action="store_true", help="print the generated SQL")
    args = parser.parse_args()

    con = connect()
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    if args.question:
        answer(" ".join(args.question), con, client, args.show_sql)
        return

    print("Ask about the warranty data (blank line to quit).")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            break
        answer(q, con, client, args.show_sql)


if __name__ == "__main__":
    main()