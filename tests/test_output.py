"""纯文本兜底覆盖真实常见格式，避免破坏网址、代码或原人格。"""
import copy
import unittest

from wxbridge.ai.output import plain_text, response_prompt
from wxbridge.wechat.messages import reply_chunks


class OutputTests(unittest.TestCase):
    def test_markdown_report_becomes_plain_readable_text(self):
        text = '# 更新\n\n> 我查过了，**重点**是这个。\n- *速度更快*\n1. 价格不变\n\n| 型号 | 结论 |\n| --- | --- |\n| A | 能用 |\n\n---'
        self.assertEqual(plain_text(text), '更新\n\n我查过了，重点是这个。\n速度更快\n价格不变\n\n型号，结论\nA，能用')

    def test_links_keep_exact_targets_and_reference_definitions_do_not_leak(self):
        text = '[说明](https://example.com/release_(stable)?x=1&y=2)\n[公告][src]\n[src]: https://example.com/news "标题"\n<https://example.com>'
        self.assertEqual(plain_text(text), '说明 https://example.com/release_(stable)?x=1&y=2\n公告 https://example.com/news\nhttps://example.com')

    def test_code_content_and_identifier_underscores_survive(self):
        text = '用 `some_value`，传入 `**kwargs`。\n```python\n# comment\ndef f(**kwargs):\n    return 2 * 3\n```'
        self.assertEqual(plain_text(text), '用 some_value，传入 **kwargs。\n# comment\ndef f(**kwargs):\n    return 2 * 3')
        self.assertEqual(plain_text('some_variable_name 和 2 * 3 = 6'), 'some_variable_name 和 2 * 3 = 6')

    def test_long_plain_answer_is_not_truncated(self):
        text = '明确要求的完整说明。'*100
        self.assertEqual(plain_text(text), text)

    def test_two_prose_paragraphs_send_separately_without_cutting_source_url(self):
        first = '先把连接地址和端口核对一下，看看是否一致。'
        second = '再检查服务器监听状态。https://example.com/check?q=one_two'
        self.assertEqual(reply_chunks(first+'\n\n'+second, 1800), [first, second])
        self.assertEqual(reply_chunks('知道还问。先顾好你自己。', 1800), ['知道还问。先顾好你自己。'])

    def test_professional_code_keeps_internal_blank_lines_and_full_content(self):
        code = 'def connect(host):\n    return host.strip()\n\ndef main():\n    return connect("example")'
        self.assertEqual(reply_chunks(code, 1800), [code])

    def test_plain_urls_math_and_chinese_emphasis_do_not_conflict(self):
        url = 'https://example.com/__assets__/?q=**name**&key=some_value'
        self.assertEqual(plain_text(url), url)
        self.assertEqual(plain_text('2*3*4、2**3**4 和 a*b*c'), '2*3*4、2**3**4 和 a*b*c')
        self.assertEqual(plain_text('这是__重点__，请**留意**。'), '这是重点，请留意。')

    def test_output_rules_keep_persona_and_multimodal_input_unchanged(self):
        messages = [dict(role='system', content='有主见的合成人格'), dict(role='user', content=[dict(type='image_url', image_url={'url':'data:image/png;base64,AAAA'})])]
        saved = copy.deepcopy(messages)
        prompt = response_prompt(messages)
        self.assertTrue(prompt[0]['content'].startswith('有主见的合成人格'))
        self.assertIn('只输出纯文本', prompt[0]['content'])
        self.assertIn('默认约10个字', prompt[0]['content'])
        self.assertIn('专业问答', prompt[0]['content'])
        self.assertEqual(prompt[1], messages[1])
        self.assertEqual(messages, saved)
