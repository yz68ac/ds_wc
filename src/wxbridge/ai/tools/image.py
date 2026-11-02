"""Image tool declaration and proposal preparation; the owner loop enqueues it."""
from wxbridge.ai.composer import PromptComposer
from .base import ToolError, ToolResult


IMAGE_TOOL = dict(type='function', function=dict(
    name='generate_image',
    description='仅在当前用户明确要求画图、生成图片时使用。输入完整绘图描述，一次提交一个任务。只支持文生图，不支持修改引用图片。',
    parameters=dict(type='object', properties=dict(prompt=dict(type='string', description='完整绘图描述，最多4000字')),
                    required=['prompt'], additionalProperties=False)))


def apply_image_instructions(messages):
    return PromptComposer.instructions(messages, 'image_generation',
        '[图片生成工具]只有当前用户明确请求绘图时才调用 generate_image，讨论图片、看图和普通聊天不触发。'
        '工具参数写完整绘图描述，保留用户要求，不受聊天短答字数限制；不把整段群聊或无关个人信息传入。'
        '本轮仅支持文生图，不能声称已经修改或使用了未传入的参考图。'
        '工具返回 prepared 只表示请求已准备，仍需系统接收；不要声称已经开始、生成成功或已经发送。'
        '每轮回复最多准备一个生图任务；交接确认由系统发送，不重复输出等待语。')


def generate_image(arguments, context):
    if context.image_requests:
        raise ToolError('image_tool_limit')
    if (set(arguments) != {'prompt'} or not isinstance(arguments.get('prompt'), str)
            or not 1 <= len(arguments['prompt'].strip()) <= 4000):
        raise ToolError('invalid_image_tool')
    return ToolResult(output=dict(ok=True, status='prepared'),
                      image_request=dict(prompt=arguments['prompt'].strip()))
