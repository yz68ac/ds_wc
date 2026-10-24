"""Conservative lexical retrieval, independent of memory persistence and scoring."""
import re


STYLE = re.compile(r'回答|回复|答复|解释|称呼|叫我|别叫|不要叫|语气|说话|简洁|简短|结论|字数|表情|剧透')
GENERAL_WORDS = {'markdown', 'json', 'xml', 'url', 'api', 'ai', 'bot', 'ds', 'http', 'https',
                 'emoji', 'emoticon', 'tldr', 'deepseek', 'chatgpt'}
STOP = set('喜欢 觉得 认为 表示 提到 今天 昨天 之前 之后 时候 这个 那个 什么 怎么 就是 不是 但是 因为 所以 一个 一些 自己 对方 群友 聊天 游戏 作品 有趣 意思 真的 还是 没有 现在 已经 进行 让我 我们 你们 他们 看了 看着 可以 不要 知道 东西'.split())


def words(text):
    return {s.casefold() for s in re.findall(r'[A-Za-z][A-Za-z0-9_+-]{2,}', text)}-GENERAL_WORDS


def grams(text):
    result = set()
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        for size in (2, 3, 4):
            result.update(run[i:i+size] for i in range(len(run)-size+1)
                          if not any(run[j:j+2] in STOP for j in range(i, i+size-1)))
    return result


class MemoryTopic:
    def __init__(self, dialogue, question=''):
        self.empty = not dialogue.strip()
        self.terms, self.grams = words(dialogue), grams(dialogue)
        asks = re.search(r'记不记得|还记得|记得我|什么|哪个|哪些|[?？]', question)
        personal = re.search(r'之前|以前|上次|刚才|曾经|我的.{0,8}(?:爱好|偏好|兴趣)|我.{0,8}(?:喜欢什么|爱玩什么|玩什么)', question)
        self.recall = bool('我' in question and asks and (personal or '记' in question) and not words(question))

    def score(self, entry, *, personal=False):
        # Context-free inspection keeps legacy behavior; production passes an
        # explicit member-only snapshot. Tokenize that snapshot only once.
        if self.empty:
            return 1
        if personal and self.recall:
            return 3  # Explicitly recalling one's past can intentionally use an old topic.
        summary = entry['summary']
        terms = words(summary)
        shared = terms & self.terms
        if terms and not shared:
            return 0
        if shared:
            return 10+len(shared)
        if entry['kind'] == 'impression' or (entry['kind'] == 'preference' and STYLE.search(summary)):
            return 2
        overlap = grams(summary) & self.grams
        pairs = {s for s in overlap if len(s) == 2}
        return max((len(s) for s in overlap), default=0) if len(pairs) >= 2 else 0


def memory_dialogue(context, triggers, account):
    """Topic evidence comes from real member messages and explicitly selected quotes."""
    rows = {m.get('local_id'): m for m in [*context, *triggers]}
    end = max((m.get('time', 0) for m in triggers), default=0)
    through = max((m.get('local_id') or 0 for m in triggers), default=0)
    texts = [m.get('text', '') for m in sorted(rows.values(), key=lambda m: (m.get('time', 0), m.get('local_id') or 0))
             if m.get('sender') and m['sender'] != account and m.get('type', 1) == 1
             and m.get('time_source') not in ('unknown', 'observed')
             and (not end or m.get('time', 0) <= end)
             and (not through or not m.get('local_id') or m['local_id'] <= through)]
    for message in triggers:
        ref = message.get('reference')
        for _ in range(8):
            if not isinstance(ref, dict):
                break
            if ref.get('available') and ref.get('text'):
                texts.append(ref['text'])
            ref = ref.get('reference')
    return '\n'.join(texts) or '（当前没有明确话题）'
