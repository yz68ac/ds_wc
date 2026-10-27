"""Citation presentation regressions; no network or private conversation data."""
from copy import deepcopy
import unittest

from wxbridge.ai.citations import without_citations
from wxbridge.ai.providers import get_adapter

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
                     '官网地址:https://example.com/download\n后面还有安装条件','array[0][1]'):
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
        original=deepcopy(data);result=get_adapter('openai_responses').decode(data)
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
        result=get_adapter('openai_responses').decode(data)
        self.assertEqual(without_citations(result.message['content'],SOURCES,result.search['citations']),'第一段事实\n第二段')
