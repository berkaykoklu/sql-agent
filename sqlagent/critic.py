import re

from sqlagent import prices

SYSTEM = """<role>You review a SQLite query that another analyst wrote to answer a question.</role>

<context>You see the question, the hint, the database map, the column descriptions the analyst looked up,
the query and the first rows of its result. You do not know the correct answer.</context>

<checklist>
1. Parts: does the query answer every part of the question (each requested value or entity)?
2. Columns: does it return exactly the requested columns, with no extra IDs, counts or helper columns and none missing?
3. Meaning: do the tables and columns mean what the question asks (for example per-race points versus
   season standings, or a person's id versus their name)?
4. Hint: does it follow the hint's definitions of columns, values and formulas?
5. Result: is the result plausible: not empty, not all NULL, and no integer division giving 0 or 1 where a fraction is expected?
</checklist>

<output>If every check passes, reply with exactly <verdict>OK</verdict>.
Otherwise reply <verdict>REVISE</verdict><feedback>the failing check, the concrete problem and how to fix it</feedback>.
Flag only problems that change the answer. Never flag style, aliases, SQL formatting or row order.</output>

<examples>
<example>
<question>Which author wrote the most books?</question>
<query>SELECT a.name, COUNT(*) FROM authors AS a JOIN books AS b ON b.author_id = a.author_id GROUP BY a.author_id ORDER BY 2 DESC LIMIT 1</query>
<result>('Frank Herbert', 6)</result>
<review><verdict>REVISE</verdict><feedback>Check 2: the question asks only for the author; drop the COUNT column.</feedback></review>
</example>
<example>
<question>What percentage of books are science fiction?</question>
<query>SELECT CAST(SUM(genre = 'Sci-Fi') AS REAL) * 100 / COUNT(book_id) FROM books</query>
<result>(18.04,)</result>
<review><verdict>OK</verdict></review>
</example>
</examples>"""

REVISE_TURN = ("<reviewer_feedback>{feedback}</reviewer_feedback>\n"
               "Fix the query if the reviewer is right, then reply with the final SQL inside <sql></sql> tags.")


def parse_review(text: str) -> tuple[str, str]:
    verdict = re.search(r"<verdict>\s*(OK|REVISE)\s*</verdict>", text, re.I)
    feedback = re.search(r"<feedback>(.*?)</feedback>", text, re.S | re.I)
    if verdict and verdict.group(1).upper() == "REVISE" and feedback and feedback.group(1).strip():
        return "REVISE", feedback.group(1).strip()
    return "OK", ""  # malformed or vague reviews must not change a working answer


def review(client, question: str, evidence: str, overview: str, columns: str, sql: str, result: str):
    user = (f"<question>{question}</question>\n<hint>{evidence or 'none'}</hint>\n{overview}\n"
            f"<columns>\n{columns or '(none looked up)'}\n</columns>\n<query>{sql}</query>\n<result>\n{result}\n</result>")
    resp = client.chat.completions.create(
        model=prices.MODEL, reasoning_effort=prices.REASONING_EFFORT,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
    verdict, feedback = parse_review(resp.choices[0].message.content or "")
    return verdict, feedback, resp
