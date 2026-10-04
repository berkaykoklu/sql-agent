import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlagent import prices
from sqlagent.db import run

SYSTEM = (
    "You are an expert SQLite analyst. Answer the question with a single SQLite SELECT "
    "statement over the database below. Reply with only the SQL in a ```sql block."
)
VERIFY = " Before answering, always run your query with run_sql and check the result."
TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": "Run a read-only SQLite query on the database and see the first "
                       "20 rows or the error message. Use it to check a query before "
                       "giving your final answer.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}]


@dataclass
class Result:
    sql: str = ""
    steps: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    exhausted: bool = False

    @property
    def cost(self) -> float:
        return prices.cost(self.input_tokens, self.output_tokens)

    def add_usage(self, resp) -> None:
        self.input_tokens += resp.usage.prompt_tokens
        self.output_tokens += resp.usage.completion_tokens


def extract_sql(text: str) -> str:
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S | re.I)
    return (match.group(1) if match else text).strip()


def _messages(question: str, evidence: str, schema: str, verify: bool = False) -> list[dict]:
    user = f"Database schema:\n{schema}\n\nHint: {evidence or 'none'}\n\nQuestion: {question}"
    system = SYSTEM + VERIFY if verify else SYSTEM
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _create(client, messages: list[dict], tools: list[dict] | None = None):
    kwargs = {"model": prices.MODEL, "reasoning_effort": prices.REASONING_EFFORT, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    return client.chat.completions.create(**kwargs)


def one_shot(client, question: str, evidence: str, schema: str) -> Result:
    resp = _create(client, _messages(question, evidence, schema))
    result = Result(sql=extract_sql(resp.choices[0].message.content or ""))
    result.add_usage(resp)
    return result


def agent(client, question: str, evidence: str, schema: str, db_path: Path,
          max_steps: int = 5, verify: bool = False) -> Result:
    messages = _messages(question, evidence, schema, verify)
    result = Result()
    for _ in range(max_steps + 1):
        resp = _create(client, messages, TOOLS)
        result.add_usage(resp)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            result.sql = extract_sql(msg.content or "")
            return result
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
            try:
                query = str(json.loads(c.function.arguments)["query"])
            except (json.JSONDecodeError, KeyError, TypeError):
                output = "ERROR: arguments must be JSON with a 'query' string"
            else:
                output = run(db_path, query)
                result.steps.append({"sql": query, "output": output})
            messages.append({"role": "tool", "tool_call_id": c.id, "content": output})
    result.exhausted = True
    result.sql = result.steps[-1]["sql"] if result.steps else ""
    return result
