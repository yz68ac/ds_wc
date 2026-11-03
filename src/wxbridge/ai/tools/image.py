"""Image tool declaration and proposal preparation; the owner loop enqueues it."""
from wxbridge.ai.composer import PromptComposer
from wxbridge.contracts.image_jobs import ImageProposal
from .base import ToolError, ToolResult


IMAGE_TOOL = dict(type='function', function=dict(
    name='generate_image',
    description='仅在当前用户明确要求从文字生成新图片时使用。修改引用图片应使用 edit_image，不用文字重画代替改图。',
    parameters=dict(type='object', properties=dict(prompt=dict(type='string', description='完整绘图描述，最多4000字')),
                    required=['prompt'], additionalProperties=False)))


EDIT_IMAGE_TOOL = dict(type='function', function=dict(
    name='edit_image', description='仅在当前用户明确要求修改引用图片时使用，将已读取的引用原图和修改要求交给图片模型。',
    parameters=dict(type='object', properties=dict(
        image=dict(type='string', description='本轮已附送的引用图片标签，例如引用图片1'),
        prompt=dict(type='string', description='完整修改要求，说明需要保留和修改的内容，最多4000字')),
        required=['image', 'prompt'], additionalProperties=False)))


def apply_image_instructions(messages):
    return PromptComposer.instructions(messages, 'image_generation',
        '[图片生成工具]只有当前用户明确请求绘图时才调用 generate_image，讨论图片、看图和普通聊天不触发。'
        '工具参数写完整绘图描述，保留用户要求，不受聊天短答字数限制；不把整段群聊或无关个人信息传入。'
        '用户明确要求修改引用图片时，使用 edit_image 并选择本轮实际可用的引用图片标签。'
        'edit_image 不可用或引用图片未读取时，说明缺少原图，请用户重新引用；不能调用 generate_image 假装改图。'
        '工具返回 prepared 只表示请求已准备，仍需系统接收；不要声称已经开始、生成成功或已经发送。'
        '每轮回复最多准备一个生图任务；交接确认由系统发送，不重复输出等待语。')


def generate_image(arguments, context):
    if context.image_proposals:
        raise ToolError('image_tool_limit')
    if (set(arguments) != {'prompt'} or not isinstance(arguments.get('prompt'), str)
            or not 1 <= len(arguments['prompt'].strip()) <= 4000):
        raise ToolError('invalid_image_tool')
    return ToolResult(output=dict(ok=True, status='prepared'),
                      image_request=ImageProposal(arguments['prompt'].strip()))


def edit_image(arguments, context):
    if context.image_proposals:raise ToolError('image_tool_limit')
    if (set(arguments) != {'prompt', 'image'} or not isinstance(arguments.get('prompt'), str)
            or not 1 <= len(arguments['prompt'].strip()) <= 4000
            or not isinstance(arguments.get('image'), str)):
        raise ToolError('invalid_image_tool')
    image = context.edit_images.get(arguments['image'])
    if image is None:raise ToolError('image_edit_source_unavailable')
    return ToolResult(output=dict(ok=True, status='prepared'),
        image_request=ImageProposal(arguments['prompt'].strip(), reference_image=image))
