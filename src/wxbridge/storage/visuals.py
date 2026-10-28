"""Pure, closed visual enums shared by extraction and memory validation."""

VISUAL = {
    "subject": {"human": "人物元素", "animal": "动物元素", "landscape": "风景元素", "object": "物品元素",
                "text": "文字元素", "logo": "标志元素", "abstract": "抽象元素", "mixed": "混合元素", "unknown": ""},
    "medium": {"photo": "摄影风格", "illustration": "插画风格", "graphic": "平面图形", "unknown": ""},
    "palette": {"warm": "暖色", "cool": "冷色", "neutral": "中性色", "mixed": "多色", "monochrome": "单色", "unknown": ""},
    "composition": {"simple": "构图简洁", "detailed": "构图丰富", "unknown": ""},
}


def validate_visual(value):
    if not isinstance(value, dict) or set(value) != set(VISUAL):
        raise ValueError("avatar_invalid_visual")
    if any(not isinstance(v, str) or v not in VISUAL[k] for k, v in value.items()):
        raise ValueError("avatar_invalid_visual")
    return {k: value[k] for k in VISUAL}


def visual_description(value):
    value = validate_visual(value)
    return "、".join(VISUAL[k][v] for k, v in value.items() if VISUAL[k][v])
