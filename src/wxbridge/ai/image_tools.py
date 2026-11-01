"""Image tool contracts; workers propose requests and never enqueue or send them."""
import json

from provider import ModelError
from wxbridge.ai.prompt_parts import PromptComposer

IMAGE_TOOL = dict(type='function', function=dict(name='generate_image',
    description='仅在当前用户明确要求画图、生成图片时使用。输入完整绘图描述，一次提交一个任务。只支持文生图，不支持修改引用图片。',
    parameters=dict(type='object', properties=dict(prompt=dict(type='string', description='完整绘图描述，最多4000字')), required=['prompt'], additionalProperties=False)))


def image_tool_prompt(messages):
    return PromptComposer.instructions(messages, 'image_generation',
        '【图片生成工具】只有当前用户明确请求绘图时才调用 generate_image，讨论图片、看图和普通聊天不触发。'
        '工具参数写完整绘图描述，保留用户要求，不受聊天短答字数限制；不把整段群聊或无关个人信息传入。'
        '本轮仅支持文生图，不能声称已经修改或使用了未传入的参考图。'
        '调用代表提交任务，图片稍后由系统发送；不要声称已经生成成功或已经发送。每轮最多一个生图任务。')


def collect_image_request(tool_calls, requests):
    if not isinstance(tool_calls, list) or any(not isinstance(t, dict) for t in tool_calls):
        raise ModelError('invalid_image_tool')
    calls = [t for t in tool_calls if (t.get('function') or {}).get('name') == 'generate_image']
    if not calls:
        return False
    if requests or len(calls) != 1:
        raise ModelError('image_tool_limit')
    try:
        args = json.loads(calls[0]['function']['arguments'])
    except (KeyError, TypeError, ValueError):
        raise ModelError('invalid_image_tool') from None
    if (not isinstance(args, dict) or set(args) != {'prompt'}
            or not isinstance(args['prompt'], str) or not 1 <= len(args['prompt'].strip()) <= 4000):
        raise ModelError('invalid_image_tool')
    requests.append(dict(prompt=args['prompt'].strip()))
    return True
