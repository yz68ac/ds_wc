"""显式调用搜索与模型验证工具链，不读取微信或向群发送消息。"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from wxbridge.ai.client import DeepSeekClient
from wxbridge.ai.search import answer_with_search
from wxbridge.ai.prompts import system_prompt
from wxbridge.config import load_config, load_dotenv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live-api', action='store_true', help='显式调用模型和搜索，会产生少量费用')
    parser.add_argument('--config', default=str(ROOT/'config.json'))
    parser.add_argument('--env-file', default='', help='仅本机现有凭据文件，不复制或输出')
    args = parser.parse_args()
    if not args.live_api:
        parser.error('须显式传入 --live-api')
    if args.env_file:
        load_dotenv(args.env_file)
    cfg = load_config(args.config)
    cfg['deepseek']['max_tokens'] = 400
    # 验证待部署版本的人格，不修改服务器配置或已上线的人格文件。
    cfg['deepseek']['system_prompt_file'] = str(ROOT/'prompts/system.md')
    events = []
    try:
        text, meta = answer_with_search(cfg, DeepSeekClient(cfg), [dict(role='system', content=system_prompt(cfg)), dict(role='user', content=
            '请上网查询 DeepSeek 官方 Search API 的请求地址，优先查官方 API 文档，给一个简短答案。')],
            on_search=lambda: events.append('search_started'))
        passed = meta['attempted'] and meta['results'] > 0 and len(events) == 1 and bool(text.strip())
        body = text.rsplit('\n参考：', 1)[0]
        print(json.dumps(dict(passed=passed, search=meta, feedback_events=len(events),
                              reply_chars=len(text), body_chars=len(body), body_paragraphs=len(body.split('\n\n')),
                              persona_used=True, model=cfg['deepseek']['model']), ensure_ascii=False))
        return 0 if passed else 1
    except Exception as exc:
        print(json.dumps(dict(passed=False, error=type(exc).__name__)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
