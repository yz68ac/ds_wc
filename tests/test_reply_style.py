"""合成输入覆盖所有输出路径；不联网、不操作微信。"""
from __future__ import annotations

import copy
import json
import logging
import unittest
from unittest.mock import Mock, patch

from wxbridge.ai.emoticons import catalog, tokens
from wxbridge.ai.output import format_reply, plain_text, reply_rules, response_prompt
from wxbridge.ai.proactive import build_prompt, parse_decision
from wxbridge.ai.search import answer_with_search
from wxbridge.app.workers import _work
from wxbridge.config import DEFAULTS, load_config
from synthetic import NOW, msg
import test_context_pipeline as pipeline


def settings(enabled=True, omit=True):
    cfg=copy.deepcopy(DEFAULTS)
    cfg['reply_style'].update(wechat_emoticons_enabled=enabled,omit_trailing_period=omit)
    cfg['deepseek'].update(system_prompt_file='',system_prompt='合成女性人格')
    return cfg


class ReplyStyleTests(unittest.TestCase):
    def test_catalog_has_twenty_verified_entries_and_reviewable_evidence(self):
        data=catalog()
        self.assertEqual(len(data['emoticons']),20)
        self.assertEqual(data['client_support']['panel_entries'],109)
        for item in data['emoticons']:
            self.assertIn(item['code'],tokens())
            for field in ('literal','meanings','contexts','avoid','risk','example','sources'):
                self.assertTrue(item[field],(item['code'],field))
            self.assertTrue(all(source in data['sources'] for source in item['sources']))
        smile=next(item for item in data['emoticons'] if item['code']=='[微笑]')
        definition=' '.join(smile['meanings'])+smile['example']
        self.assertNotIn('礼貌',definition)
        self.assertNotIn('友好',definition)

    def test_emoticons_and_unresolved_technical_brackets_are_not_reference_links(self):
        for text in ('[捂脸]','[捂脸][偷笑]','哈哈[旺柴]','[Doge][Facepalm]',
                     'array[0][1]','data[index][size]','[未定义][reference]'):
            with self.subTest(text=text):
                self.assertEqual(plain_text(text),text)

    def test_real_markdown_links_still_render_and_emoji_labels_survive(self):
        self.assertEqual(plain_text('[说明][r]\n[r]: https://example.com'), '说明 https://example.com')
        self.assertEqual(plain_text('[捂脸][r]\n[r]: https://example.com'), '[捂脸] https://example.com')
        self.assertEqual(plain_text('[捂脸](https://example.com)'), '[捂脸] https://example.com')

    def test_inline_and_fenced_code_brackets_and_punctuation_are_preserved(self):
        self.assertEqual(plain_text('`print("[捂脸][偷笑]。")`'), 'print("[捂脸][偷笑]。")')
        text='```python\na[0][1] = "[微笑]。"\n```'
        self.assertEqual(format_reply(text,settings()),'a[0][1] = "[微笑]。"')

    def test_chinese_final_period_removed_before_or_after_single_emoticon(self):
        cases={'知道啦。':'知道啦','知道啦。[偷笑]':'知道啦[偷笑]',
               '知道啦[偷笑]。':'知道啦[偷笑]','知道啦。[偷笑]。':'知道啦[偷笑]',
               '[偷笑]。':'[偷笑]',
               '**知道啦。**':'知道啦','DeepSeek很有意思。':'DeepSeek很有意思'}
        for source,expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(format_reply(source,settings()),expected)

    def test_other_punctuation_standalone_emoticon_and_mid_sentence_period_unchanged(self):
        for source in ('真的吗？','这也行！','行，知道了','嗯……','[偷笑]',
                       '知道啦。你先忙','好。[未核实]','Okay.','v1.2.3','好[旺柴][偷笑]'):
            with self.subTest(source=source):
                self.assertEqual(format_reply(source,settings()),source)

    def test_quotes_decimals_urls_commands_and_long_professional_text_not_rewritten(self):
        for source in ('原话“知道啦。”','预算3.5万元。','地址https://example.com/a。',
                       'sudo echo 中文。','> 引用原文。',
                       '这是第一步。\n这是第二步。','专业说明需要保持完整。'*6):
            with self.subTest(source=source):
                expected=source[2:] if source.startswith('> ') else source
                self.assertEqual(format_reply(source,settings()),expected)

    def test_period_switch_and_emoticon_switch_are_independent(self):
        self.assertEqual(format_reply('知道啦。[偷笑]',settings(omit=False)),'知道啦。[偷笑]')
        self.assertEqual(format_reply('引用“[微笑]”',settings(enabled=False)),'引用“[微笑]”')
        enabled=reply_rules(settings())
        disabled=reply_rules(settings(enabled=False))
        self.assertIn('本轮微信表情已开启',enabled)
        self.assertIn('本轮自动微信表情已关闭',disabled)
        self.assertNotIn('本轮微信表情已开启',disabled)
        self.assertIn('单凭一个表情',enabled)
        self.assertIn('[微笑]：冷淡、无语、反讽',disabled)

    def test_prompt_does_not_mutate_persona_images_or_group_messages(self):
        messages=[dict(role='system',content='合成女性人格'),dict(role='user',content=[
            dict(type='text',text='群友原文。[旺柴]'),dict(type='image_url',image_url=dict(url='data:image/png;base64,AAAA'))])]
        original=copy.deepcopy(messages)
        rendered=response_prompt(messages,settings())
        self.assertTrue(rendered[0]['content'].startswith('合成女性人格'))
        self.assertEqual(rendered[1],messages[1])
        self.assertEqual(messages,original)

    def test_old_config_inherits_defaults_and_invalid_switches_are_rejected(self):
        with patch('wxbridge.config.load_dotenv'):
            self.assertEqual(load_config('__missing_synthetic__.json')['reply_style'],
                dict(wechat_emoticons_enabled=False,omit_trailing_period=True))
            for value in ({'wechat_emoticons_enabled':'true'},{'omit_trailing_period':1},None,[]):
                defaults=copy.deepcopy(DEFAULTS)
                defaults['reply_style']=value
                with patch('wxbridge.config.DEFAULTS',defaults),self.assertRaises((ValueError,KeyError)):
                    load_config('__missing_synthetic__.json')

    def test_normal_generation_uses_config_and_formats_once_without_extra_api_call(self):
        import logging
        connection=Mock()
        prior=logging.root.manager.disable
        cfg=settings()
        cfg['search']['enabled']=False
        client=Mock()
        client.chat.return_value='知道啦。[偷笑]'
        try:
            with patch('wxbridge.ai.client.DeepSeekClient',return_value=client):
                _work(connection,'generate',(cfg,[dict(role='system',content='合成人格')],None))
        finally:
            logging.disable(prior)
        self.assertEqual(connection.send.call_args.args[0],dict(ok=True,reply='知道啦[偷笑]'))
        client.chat.assert_called_once()
        self.assertIn('本轮微信表情已开启',client.chat.call_args.args[0][0]['content'])

    def test_search_path_shares_rules_and_preserves_source_url(self):
        client=Mock()
        client.complete.return_value=dict(content='知道啦。[偷笑]')
        reply,meta=answer_with_search(settings(),client,[dict(role='system',content='合成人格')])
        self.assertEqual(reply,'知道啦[偷笑]')
        self.assertFalse(meta['attempted'])
        self.assertIn('本轮微信表情已开启',client.complete.call_args.args[0][0]['content'])
        client.complete.return_value=dict(content='https://example.com/a?q=1.2')
        reply,_=answer_with_search(settings(),client,[])
        self.assertEqual(reply,'https://example.com/a?q=1.2')

    def test_proactive_rules_and_parser_respect_both_settings(self):
        cfg=settings()
        prompt=build_prompt(cfg,[msg(1,NOW,'合成闲聊')],'合成群')
        self.assertIn('本轮微信表情已开启',prompt[0]['content'])
        payload=json.dumps(dict(speak=True,text='知道啦。[偷笑]'),ensure_ascii=False)
        self.assertEqual(parse_decision(payload,30,cfg)['text'],'知道啦[偷笑]')
        self.assertEqual(parse_decision(payload,30,settings(omit=False))['text'],'知道啦。[偷笑]')
        self.assertEqual(parse_decision('{"speak":true,"text":"[旺柴]"}',30,cfg)['text'],'[旺柴]')

    def test_memory_uses_same_meanings_without_single_emoticon_personality_judgments(self):
        from wxbridge.ai.memory import extract
        value=dict(revision=1,watermark=0,existing={},
                   members={'synthetic':dict(label='U1',nickname='合成群友')},
                   messages=[dict(id='m1',member='U1',text='[微笑]')])
        client=Mock()
        client.complete.return_value=dict(content='{"updates":[]}')
        with patch('wxbridge.ai.client.DeepSeekClient',return_value=client):
            self.assertEqual(extract(settings(),value),dict(updates=[]))
        prompt=client.complete.call_args.args[0]
        self.assertIn('[微笑]：冷淡、无语、反讽',prompt[0]['content'])
        self.assertIn('不能单凭一个表情认定恶意、形成性格印象或改变好感度',prompt[0]['content'])
        self.assertEqual(json.loads(prompt[1]['content'])['messages'][0]['text'],'[微笑]')
        client.complete.assert_called_once()


class EmoticonQueueTests(unittest.TestCase):
    setUp=pipeline.PipelineTests.setUp
    make_bot=pipeline.PipelineTests.make_bot
    add=pipeline.PipelineTests.add
    generated=pipeline.PipelineTests.generated
    sends=pipeline.PipelineTests.sends
    restart=pipeline.PipelineTests.restart

    def test_search_notice_is_formatted_before_persisting_send_intent(self):
        self.cfg['queue']['debounce_seconds']=0
        self.cfg['search']['feedback_phrases']=['我去看看。']
        self.add(1)
        self.bot.tick()
        self.jobs[0].result=dict(event='search_started')
        self.bot.tick()
        self.assertEqual(self.sends()[0].payload[2],'我去看看')
        task=self.bot.journal.head()
        self.assertEqual(task['notice']['text'],'我去看看')
        self.assertEqual(task['intent']['kind'],'notice')

    def test_standalone_emoticon_is_exactly_confirmed_and_not_resent_after_restart(self):
        self.cfg['queue']['debounce_seconds']=0
        self.add(1)
        self.bot.tick()
        self.generated('[旺柴]')
        self.assertEqual(self.sends()[0].payload[2],'[旺柴]')
        self.add(2,sender='wxid_self',text='[旺柴]',mention=False)
        self.restart()
        self.bot.tick()
        self.assertFalse(self.bot.journal.tasks())
        self.assertEqual(len(self.sends()),1)


if __name__=='__main__':
    unittest.main()
