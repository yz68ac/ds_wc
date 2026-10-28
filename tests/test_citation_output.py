"""Citation presentation regressions; no network or private conversation data."""
from copy import deepcopy
import unittest

from wxbridge.ai.citations import without_citations
from wxbridge.ai.providers import get_protocol_adapter

URL = 'https://example.com/source'
SOURCES = [dict(url=URL)]


class CitationOutputTests(unittest.TestCase):
    def test_inline_annotation_drops_the_reference_but_keeps_later_answer_text(self):
        text='结论([资料]('+URL+')),后面还有必要条件'
        start=text.index('[');end=text.index(')',start)+1
        annotation=dict(url=URL,start_index=start,end_index=end)
        self.assertEqual(without_citations(text,SOURCES,[annotation]),'结论,后面还有必要条件')

    def test_sentence_annotations_do_not_delete_the_factual_answer(self):
        text='经过核对的完整结论'
        for start,end in ((0,len(text)),(-1,1000),(True,5),(0,3)):
            with self.subTest(start=start,end=end):
                self.assertEqual(without_citations(text,SOURCES,[dict(url=URL,start_index=start,end_index=end)]),text)

    def test_reference_footers_and_source_parentheses_are_removed(self):
        cases={'结论（'+URL+'）':'结论',
               '结论([资料]('+URL+'))':'结论',
               '结论\n参考:'+URL:'结论',
               '结论\nSources:\n- [资料]('+URL+')\n\n后面仍有正文':'结论\n\n后面仍有正文'}
        for text,expected in cases.items():
            with self.subTest(text=text):self.assertEqual(without_citations(text,SOURCES),expected)

    def test_requested_links_are_not_truncated_or_removed_as_citations(self):
        for text in (URL,'官网地址:'+URL,'下载地址:('+URL+')','看这个:[资料]('+URL+')',
                     '官网地址:https://example.com/download\n后面还有安装条件','array[0][1]',
                     '不同网址('+URL+'(extra))'):
            with self.subTest(text=text):self.assertEqual(without_citations(text,SOURCES),text)

    def test_code_and_verbatim_quotes_keep_the_original_source_text(self):
        for text in ('```text\n参考:'+URL+'\n\n\n保留空行\n```','`('+URL+')`','原话“参考:'+URL+'”'):
            with self.subTest(text=text):self.assertEqual(without_citations(text,SOURCES),text)

    def test_unannotated_reference_labels_and_contractions(self):
        for label in ('来源', 'Source 1', 'example.com', '1'):
            text="It's useful ["+label+"]("+URL+") and here's the rest"
            self.assertEqual(without_citations(text,SOURCES),"It's useful  and here's the rest")
        self.assertEqual(without_citations('下载:[官网]('+URL+')',SOURCES),'下载:[官网]('+URL+')')

    def test_multiple_annotations_preserve_facts(self):
        other='https://other.example/news'
        a='[来源]('+URL+')';b='[source]('+other+')'
        text='事实甲'+a+'，事实乙'+b+'，结尾'
        annotations=[dict(url=URL,start_index=text.index(a),end_index=text.index(a)+len(a)),
                     dict(url=other,start_index=text.index(b),end_index=text.index(b)+len(b))]
        self.assertEqual(without_citations(text,SOURCES+[dict(url=other)],annotations),'事实甲，事实乙，结尾')

    def test_raw_provider_state_and_citation_metadata_remain_unchanged(self):
        text='结论([资料]('+URL+'))'
        start=text.index('[');end=text.index(')',start)+1
        data=dict(status='completed',output=[dict(type='message',content=[dict(type='output_text',text=text,
            annotations=[dict(type='url_citation',url=URL,start_index=start,end_index=end)])])])
        original=deepcopy(data);result=get_protocol_adapter('openai_responses').parse_response(data)
        self.assertEqual(result.message['content'],text)
        self.assertEqual(result.message['_provider_state']['items'],data['output'])
        self.assertEqual(without_citations(result.message['content'],SOURCES,result.search['citations']),'结论')
        self.assertEqual(data,original)
        self.assertEqual(result.search['citations'][0]['url'],URL)

    def test_multiple_text_blocks_have_correct_annotation_offsets(self):
        link='[资料]('+URL+')'
        data=dict(status='completed',output=[dict(type='message',content=[
            dict(type='output_text',text='第一段事实',annotations=[]),
            dict(type='output_text',text='第二段'+link,annotations=[
                dict(type='url_citation',url=URL,start_index=3,end_index=3+len(link))])])])
        result=get_protocol_adapter('openai_responses').parse_response(data)
        self.assertEqual(without_citations(result.message['content'],SOURCES,result.search['citations']),'第一段事实\n第二段')

    def test_nested_and_spaced_annotation_shells_do_not_leave_empty_parentheses(self):
        link = '[来源]('+URL+')'
        for left, right in (('(', ')'), ('（', '）'), ('(（(', ')）)')):
            for padding in ('', ' ', '\t'):
                text = '结论'+left+padding+link+padding+right
                start = text.index(link)
                annotation = dict(url=URL, start_index=start, end_index=start+len(link))
                with self.subTest(left=left, padding=padding):
                    self.assertEqual(without_citations(text, SOURCES, [annotation]), '结论')

    def test_unannotated_nested_references_and_multiple_sources_drop_only_their_shells(self):
        other = 'https://other.example/news'
        a, b = '[来源]('+URL+')', '[source]('+other+')'
        for suffix in ('(('+a+'))', '（('+a+')）', '( ( '+a+' ; '+b+' ) )',
                       '(('+URL+'))', '((\ue200cite\ue202turn0search0\ue201))'):
            with self.subTest(suffix=suffix):
                self.assertEqual(without_citations('结论'+suffix, SOURCES+[dict(url=other)]), '结论')

    def test_original_empty_calls_and_parentheses_survive_near_removed_sources(self):
        link = '[来源]('+URL+')'
        for original in ('f()', '原文()', '原文（ ）', 'array[0]()', '下载:'+URL,
                         '`f()`', '原话“保留()”', '```python\nf()\n```'):
            with self.subTest(original=original):
                self.assertEqual(without_citations(original+'(('+link+'))', SOURCES), original)

    def test_meaningful_parentheticals_are_retained_after_citation_removal(self):
        link = '[来源]('+URL+')'
        self.assertEqual(without_citations('结论(仅适用于测试; '+link+')', SOURCES), '结论(仅适用于测试; )')
        text = '结论(('+link+'))\n\n后面的事实(保留条件)'
        self.assertEqual(without_citations(text, SOURCES), '结论\n\n后面的事实(保留条件)')

    def test_verbatim_citation_examples_remain_exact(self):
        link = '[来源]('+URL+')'
        for text in ('`(('+link+'))`', '原话“(('+link+'))”', '```text\n(('+link+'))\n```'):
            with self.subTest(text=text):
                self.assertEqual(without_citations(text, SOURCES), text)

    def test_deep_nesting_and_marker_collision_preserve_original_data(self):
        link = '[来源]('+URL+')'
        self.assertEqual(without_citations('结论'+'('*1000+link+')'*1000, SOURCES), '结论')
        original = '\x00CITATION_REMOVED\x00'
        self.assertEqual(without_citations(original+'(('+link+'))', SOURCES), original)

    def test_common_output_formatter_does_not_reintroduce_the_shell(self):
        from wxbridge.ai.output import format_reply
        link = '[来源]('+URL+')'
        text = '合成回答(('+link+'))'
        start = text.index(link)
        annotation = dict(url=URL, start_index=start, end_index=start+len(link))
        self.assertEqual(format_reply(without_citations(text, SOURCES, [annotation])), '合成回答')
