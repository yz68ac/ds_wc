"""Explicit business tool catalog; selection and execution share one whitelist."""
from copy import deepcopy
import json

from provider import ModelError
from .base import ToolError, ToolResult
from .image import IMAGE_TOOL, EDIT_IMAGE_TOOL, generate_image, edit_image, apply_image_instructions
from .web import WEB_TOOLS, web_fetch, web_search, apply_search_instructions


TOOLS = {
    'web_search': (WEB_TOOLS[0], web_search),
    'web_fetch': (WEB_TOOLS[1], web_fetch),
    'generate_image': (IMAGE_TOOL, generate_image),
    'edit_image': (EDIT_IMAGE_TOOL, edit_image),
}

# Tool-specific budgets belong here; tools without a special budget only use
# the common round and deadline limits.
SEARCH_TOOLS = frozenset(('web_search', 'web_fetch'))
IMAGE_TOOLS = frozenset(('generate_image', 'edit_image'))


def tool_available(name, context):
    if name in SEARCH_TOOLS:
        return context.remaining_search_calls > 0 and context.remaining_search_seconds > 0
    if name in IMAGE_TOOLS:
        return not context.image_proposals
    return True


def get_enabled_tools(settings, *, search_backend, editable=False):
    names = []
    if settings.search.enabled and search_backend == 'external':
        names.extend(('web_search', 'web_fetch'))
    if settings.image_generation.enabled:
        names.append('generate_image')
        if editable:names.append('edit_image')
    return [deepcopy(TOOLS[name][0]) for name in names]


def apply_tool_instructions(settings, messages, *, tools):
    """Compose enabled tool policies once, including hosted search without a client tool."""
    if settings.search.enabled:
        messages = apply_search_instructions(messages)
    if any(tool['function']['name'] == 'generate_image' for tool in tools):
        messages = apply_image_instructions(messages)
    return messages


def validate_tool_calls(calls, context):
    """Check the whole batch before executing any call. Never invent an ID."""
    if not isinstance(calls, list) or len(calls) > 8:
        raise ModelError('invalid_tool_calls')
    identifiers = set()
    for call in calls:
        function = call.get('function') if isinstance(call, dict) else None
        identifier = call.get('id') if isinstance(call, dict) else None
        if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 256
                or identifier in identifiers or identifier in context.seen_ids
                or call.get('type', 'function') != 'function' or not isinstance(function, dict)
                or not isinstance(function.get('name'), str) or not 1 <= len(function['name']) <= 128
                or not isinstance(function.get('arguments'), str)):
            raise ModelError('invalid_tool_calls')
        identifiers.add(identifier)
    context.seen_ids.update(identifiers)


def dispatch_tool_call(call, context):
    context.check_active()
    try:
        name = call['function']['name']
        if name not in context.allowed_tools:
            raise ToolError('invalid_tool')
        try:
            arguments = json.loads(call['function']['arguments'])
        except (ValueError, TypeError):
            raise ToolError('invalid_tool_arguments') from None
        if not isinstance(arguments, dict):
            raise ToolError('invalid_tool_arguments')
        result = TOOLS[name][1](arguments, context)
        context.check_active()
        if name not in SEARCH_TOOLS and result.output.get('ok') is True and result.user_notice is not None:
            if not isinstance(result.user_notice, str) or not 1 <= len(result.user_notice.strip()) <= 500:
                raise ToolError('invalid_tool_notice')
            context.tool_notices.append(result.user_notice.strip())
        if result.image_request is not None:
            context.image_proposals.append(result.image_request)
        return result
    except ToolError as exc:
        context.check_active()
        code = str(exc)
        context.errors.append(code)
        return ToolResult(dict(ok=False, error=code, note='工具未完成，不得声称已经核实或执行成功。'))
