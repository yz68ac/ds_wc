"""Small offline reply fixtures for the shared tool-loop contract."""
from provider import ModelResult
from wxbridge.ai.reply.runner import run_tool_loop


def model_result(message):
    return ModelResult(message, 'tool_calls' if message.get('tool_calls') else 'stop', {})


def run_answer(*args, **kwargs):
    result = run_tool_loop(*args, **kwargs)
    return result.text, result.search
