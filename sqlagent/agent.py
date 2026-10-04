import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlagent import critic, prices
from sqlagent.db import QueryError, column_values, describe_table, execute, run

SYSTEM = """<role>You are an expert SQLite analyst.</role>

<task>Write one SQLite SELECT statement that answers the <question>. The <database> block only
lists tables and joins; use the tools to learn the columns and values you need.</task>

<tools_usage>
- describe_table: columns, types, keys and what each column means. Call it for every table you plan to use.
- column_values: how values are actually written. Pass search for any value taken from the question
  (names, places, categories); omit search to see a column's categories.
- run_sql: run your query and check the result before answering.
You may call several tools in one turn.
</tools_usage>

<rules>
- Return exactly the columns the question asks for, in the order the question mentions them.
  No extra columns (no IDs, counts or helper columns unless the question asks for them).
- Use the <hint>'s definitions of columns, values and formulas. Write a formula with its SQL meaning:
  AVG for an average, CAST(numerator AS REAL) * 100 / denominator for a percentage and
  CAST(numerator AS REAL) / denominator for any other ratio, so no division is done on integers.
- The <hint> may use pseudo-functions such as DIVIDE, SUBTRACT or MAX(COUNT(...)).
  Translate them to SQLite: a / b, a - b, ORDER BY COUNT(*) DESC LIMIT 1.
- Return numbers as numbers. For "N decimal places" use ROUND(value, N); never printf, string formatting or a % sign.
- Return names as stored: first name and last name in separate columns, never concatenated.
- When the question asks to rank, include the rank as a column computed with RANK() OVER (...).
- For "the highest/lowest X" use ORDER BY ... LIMIT 1; for "top N" use LIMIT N. Otherwise do not use LIMIT.
  When ordering to find a highest or lowest value, exclude NULL and empty values.
- Do not add filters that neither the question nor the hint asks for.
- When similar columns exist in several tables (for example per-race results and season standings),
  check the candidates with describe_table and pick the one that matches the question's meaning.
- Copy literal values exactly as they are written in the data (spelling and case).
- Use only tables and columns that exist.
</rules>

<output>When you are done, reply with only the final SQL inside <sql></sql> tags.</output>

<examples>
<example>
<question>Which author wrote the most books?</question>
<hint>most books refers to MAX(COUNT(book_id))</hint>
<learned>books(book_id, title, author_id, year, genre); authors(author_id, name, country)</learned>
<sql>SELECT a.name FROM authors AS a JOIN books AS b ON b.author_id = a.author_id
GROUP BY a.author_id ORDER BY COUNT(b.book_id) DESC LIMIT 1</sql>
</example>
<example>
<question>What percentage of books are science fiction?</question>
<hint>percentage = DIVIDE(COUNT(genre = 'Sci-Fi'), COUNT(book_id)) * 100</hint>
<learned>column_values(books, genre) → 'Novel' (310), 'Sci-Fi' (74), 'Poetry' (21)</learned>
<sql>SELECT CAST(SUM(genre = 'Sci-Fi') AS REAL) * 100 / COUNT(book_id) FROM books</sql>
</example>
<example>
<question>List the titles of books by authors from the uk that were loaned in 2023.</question>
<hint>loaned in 2023 refers to strftime('%Y', loan_date) = '2023'</hint>
<learned>column_values(authors, country, search="uk") → 'UK' (12); loans(loan_id, book_id, member_id, loan_date)</learned>
<sql>SELECT DISTINCT b.title FROM books AS b JOIN authors AS a ON a.author_id = b.author_id
JOIN loans AS l ON l.book_id = b.book_id
WHERE a.country = 'UK' AND strftime('%Y', l.loan_date) = '2023'</sql>
</example>
</examples>"""


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


TOOLS = [
    _tool("describe_table", "List a table's columns with types, keys and the meaning of each column.",
          {"table": {"type": "string"}}, ["table"]),
    _tool("column_values", "Show how values in a column are written. With search: up to 20 distinct "
          "values containing the text (case-insensitive) and their counts. Without: the 10 most "
          "common values and the number of distinct values.",
          {"table": {"type": "string"}, "column": {"type": "string"}, "search": {"type": "string"}},
          ["table", "column"]),
    _tool("run_sql", "Run a read-only SQLite query and see the first 20 rows or the error message.",
          {"query": {"type": "string"}}, ["query"]),
]


@dataclass
class Result:
    sql: str = ""
    steps: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    exhausted: bool = False
    sql_before_critic: str | None = None
    critic: list[dict] = field(default_factory=list)
    reverted: bool = False

    @property
    def cost(self) -> float:
        return prices.cost(self.input_tokens, self.output_tokens)

    def add_usage(self, resp) -> None:
        self.input_tokens += resp.usage.prompt_tokens
        self.output_tokens += resp.usage.completion_tokens


def extract_sql(text: str) -> str:
    match = re.search(r"<sql>(.*?)</sql>", text, re.S | re.I)
    if match:
        # inside XML tags the model sometimes writes > as &gt; and " as \"
        return html.unescape(match.group(1)).replace('\\"', '"').strip()
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S | re.I)
    return (match.group(1) if match else text).strip()


def call_tool(db_path: Path, name: str, raw_args: str) -> tuple[dict | None, str]:
    try:
        args = json.loads(raw_args)
        if name == "describe_table":
            return args, describe_table(db_path, str(args["table"]))
        if name == "column_values":
            search = args.get("search")
            return args, column_values(db_path, str(args["table"]), str(args["column"]),
                                       str(search) if search else None)
        if name == "run_sql":
            return args, run(db_path, str(args["query"]))
        return args, f"ERROR: unknown tool '{name}'"
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return None, f"ERROR: bad arguments for {name}: {str(raw_args)[:200]}"


FINAL_TURN = "Step limit reached. Reply now with your final SQL inside <sql></sql> tags."


def _messages(question: str, evidence: str, overview: str) -> list[dict]:
    user = f"{overview}\n<hint>{evidence or 'none'}</hint>\n<question>{question}</question>"
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def _explore(client, messages: list[dict], db_path: Path, result: Result, max_steps: int) -> None:
    for _ in range(max_steps + 1):
        resp = client.chat.completions.create(
            model=prices.MODEL, reasoning_effort=prices.REASONING_EFFORT, messages=messages, tools=TOOLS)
        result.add_usage(resp)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            result.sql = extract_sql(msg.content or "")
            return
        if len(result.steps) >= max_steps:
            break
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in msg.tool_calls
            ],
        })
        for c in msg.tool_calls:
            args, output = call_tool(db_path, c.function.name, c.function.arguments)
            result.steps.append({"tool": c.function.name, "args": args, "output": output})
            messages.append({"role": "tool", "tool_call_id": c.id, "content": output})
    result.exhausted = True
    messages.append({"role": "user", "content": FINAL_TURN})
    resp = client.chat.completions.create(
        model=prices.MODEL, reasoning_effort=prices.REASONING_EFFORT, messages=messages,
        tools=TOOLS, tool_choice="none")
    result.add_usage(resp)
    result.sql = extract_sql(resp.choices[0].message.content or "")
    if not result.sql:
        queries = [s["args"]["query"] for s in result.steps
                   if s["tool"] == "run_sql" and isinstance(s["args"], dict) and "query" in s["args"]]
        result.sql = str(queries[-1]) if queries else ""


def _returns_rows(db_path: Path, sql: str) -> bool:
    try:
        return bool(sql.strip()) and bool(execute(db_path, sql))
    except QueryError:
        return False


def agent(client, question: str, evidence: str, overview: str, db_path: Path,
          max_steps: int = 15, critic_rounds: int = 0) -> Result:
    messages = _messages(question, evidence, overview)
    result = Result()
    _explore(client, messages, db_path, result, max_steps)
    if critic_rounds:
        result.sql_before_critic = result.sql
    for _ in range(critic_rounds):
        columns = "\n\n".join(dict.fromkeys(s["output"] for s in result.steps if s["tool"] == "describe_table"))
        verdict, feedback, resp = critic.review(
            client, question, evidence, overview, columns, result.sql, run(db_path, result.sql))
        result.add_usage(resp)
        result.critic.append({"verdict": verdict, "feedback": feedback})
        if verdict != "REVISE":
            break
        previous = result.sql
        messages.append({"role": "assistant", "content": f"<sql>{previous}</sql>"})
        messages.append({"role": "user", "content": critic.REVISE_TURN.format(feedback=feedback)})
        _explore(client, messages, db_path, result, len(result.steps) + 5)
        if _returns_rows(db_path, previous) and not _returns_rows(db_path, result.sql):
            result.sql, result.reverted = previous, True
            break
    return result
