"""纯文本兜底覆盖真实常见格式，避免破坏网址、代码或原人格。"""
import copy
from dataclasses import replace
import unittest

from wxbridge.ai.reply.output import format_reply, plain_text, reply_rules, response_prompt
from wxbridge.config import InstanceConfig, ReplyStyleConfig
from wxbridge.emoticons import tokens
from wxbridge.wechat.messages import reply_chunks


class OutputTests(unittest.TestCase):
    def test_invalid_chunk_limits_are_rejected_even_for_empty_text(self):
        for limit in (0, -1, True, 1.5):
            for text in ('', 'abc'):
                with self.subTest(limit=limit,text=text), self.assertRaisesRegex(ValueError,'invalid_chunk_limit'):
                    reply_chunks(text,limit)

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
        from wxbridge.ai.reply.output import reply_rules
        self.assertIn(reply_rules(), prompt[0]['content'])
        self.assertNotIn('尽量不超过30字', prompt[0]['content'])
        self.assertEqual(prompt[1], messages[1])
        self.assertEqual(messages, saved)


class ReplyStyleTests(unittest.TestCase):
    def config(self, *, emoticons=False, omit_period=False):
        return replace(InstanceConfig(), reply_style=ReplyStyleConfig(
            wechat_emoticons_enabled=emoticons, omit_trailing_period=omit_period))

    def test_prompt_applies_explicit_preferences_without_changing_the_persona(self):
        messages = [dict(role='system', content='原有角色允许使用表情。'), dict(role='user', content='你好')]
        cfg = self.config(omit_period=True)
        prompt = response_prompt(messages, cfg)
        self.assertTrue(prompt[0]['content'].startswith(messages[0]['content']))
        self.assertIn('微信文字表情已关闭', prompt[0]['content'])
        self.assertIn('Unicode emoji 不受', prompt[0]['content'])
        self.assertIn('省略回复正文末尾', prompt[0]['content'])
        self.assertNotIn('已关闭', reply_rules(self.config(emoticons=True)))
        self.assertEqual(response_prompt(prompt, cfg), prompt)

    def test_disabled_emoticons_remove_catalog_codes_and_aliases_but_keep_unicode(self):
        text = '收到'+''.join(sorted(tokens()))+'🙂👨‍👩‍👧‍👦👍🏽[备注]。'
        self.assertEqual(format_reply(text, self.config()), '收到🙂👨‍👩‍👧‍👦👍🏽[备注]。')
        self.assertEqual(format_reply(text, self.config(emoticons=True)), text)

    def test_preferences_preserve_quoted_literals_code_commands_and_urls(self):
        text = ('引用“[微笑]。”，字面量 `[Facepalm]`，普通回复[捂脸]。\n'
                '```python\nvalue = "[微笑]。"\n```\n'
                'echo [微笑].\nhttps://example.com/[微笑].')
        expected = ('引用“[微笑]。”，字面量 [Facepalm]，普通回复。\n'
                    'value = "[微笑]。"\necho [微笑].\nhttps://example.com/[微笑].')
        self.assertEqual(format_reply(text, self.config(omit_period=True)), expected)

    def test_omit_period_only_changes_the_final_prose_sentence(self):
        cfg = self.config(omit_period=True)
        self.assertEqual(format_reply('第一句。\n第二句[微笑]。', cfg), '第一句。\n第二句')
        self.assertEqual(format_reply('Okay.', cfg), 'Okay')
        self.assertEqual(format_reply('收到🙂。', cfg), '收到🙂')
        self.assertEqual(format_reply('收到。\n\n---', cfg), '收到')
        self.assertEqual(format_reply('收到。\n[捂脸]', cfg), '收到')
        for text in ('等等...', '等等。。。', '等等……', '版本 1.2.3', '地址 127.0.0.1', '真的？', '好！', '“原话。”', '`value.`', '```text\nvalue.\n```'):
            with self.subTest(text=text):
                self.assertEqual(format_reply(text, cfg), plain_text(text))

    def test_period_preference_is_independent_of_the_emoticon_switch(self):
        self.assertEqual(format_reply('收到[微笑]。', self.config(emoticons=True, omit_period=True)), '收到[微笑]')
        self.assertEqual(format_reply('收到[微笑]。', self.config(emoticons=True)), '收到[微笑]。')

    def test_per_message_character_limit_preserves_the_complete_reply(self):
        chunks = reply_chunks('abcdefghij', 4)
        self.assertEqual(chunks, ['abcd', 'efgh', 'ij'])
        self.assertEqual(''.join(chunks), 'abcdefghij')
