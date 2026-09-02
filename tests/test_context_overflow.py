"""Budgeting context, and recovering when the model says the prompt is too long.

The failure this covers looked like a broken connection: the gateway was
reachable, the model was installed, and every turn ended in "non-streaming retry
also failed". The prompt was simply too long — and the engine's own estimate said
otherwise, because it counted the messages but not the tool schemas that ride
along with every request.
"""

from __future__ import annotations

from cagentic.engine import _context_overflow
from cagentic.services.compact import approx_schema_tokens, approx_tokens

# -- reading the provider's refusal -----------------------------------------


def test_ollamas_structured_overflow_is_understood():
    text = (
        'chat HTTP 400: {"error":"{\\"error\\":{\\"code\\":400,\\"message\\":\\"request '
        "(11582 tokens) exceeds the available context size (8192 tokens), try increasing "
        'it\\",\\"type\\":\\"exceed_context_size_error\\",\\"n_prompt_tokens\\":11582,'
        '\\"n_ctx\\":8192}}"}'
    )

    assert _context_overflow(text) == (11582, 8192)


def test_the_human_sentence_alone_is_enough():
    # Not every provider includes the machine-readable fields.
    text = "request (9000 tokens) exceeds the available context size (4096 tokens)"

    assert _context_overflow(text) == (9000, 4096)


def test_other_failures_are_not_mistaken_for_overflow():
    # These must keep taking the ordinary retry path.
    assert _context_overflow("connection reset by peer") is None
    assert _context_overflow("chat HTTP 500: internal error") is None
    assert _context_overflow("model 'x' not found") is None
    assert _context_overflow("") is None


def test_an_overflow_without_numbers_is_not_guessed_at():
    assert _context_overflow("exceeds the available context size") is None


# -- counting what actually goes on the wire --------------------------------


def test_tool_schemas_are_counted():
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file from the workspace.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ] * 24

    counted = approx_schema_tokens(schemas)
    # Two dozen schemas are thousands of tokens; treating them as free is what
    # let the budget check pass on a prompt the model then rejected.
    assert counted > 1000


def test_no_schemas_costs_nothing():
    assert approx_schema_tokens([]) == 0
    assert approx_schema_tokens(None) == 0


def test_unserializable_schemas_still_produce_a_number():
    class Odd:
        pass

    # Never raise while sizing a budget; an approximation beats an exception.
    assert approx_schema_tokens([{"fn": Odd()}]) > 0


def test_schema_cost_is_additive_with_the_message_estimate():
    messages = [{"role": "user", "content": "hello " * 100}]
    schemas = [{"type": "function", "function": {"name": "x", "description": "y" * 400}}]

    total = approx_tokens(messages) + approx_schema_tokens(schemas)

    assert total > approx_tokens(messages)
    assert total > approx_schema_tokens(schemas)
