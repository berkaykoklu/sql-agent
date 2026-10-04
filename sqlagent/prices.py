# Verified 2026-10-04 on OpenAI's models and pricing pages.
MODEL = "gpt-6-luna"
REASONING_EFFORT = "none"
USD_PER_M_INPUT = 0.10
USD_PER_M_OUTPUT = 0.50


def cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * USD_PER_M_INPUT + output_tokens * USD_PER_M_OUTPUT) / 1_000_000
