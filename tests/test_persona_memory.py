"""Synthetic persona, preference and relationship checks; no live messages or API."""
import copy
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from wxbridge.ai.memory import extract, snapshot
from wxbridge.config import load_config
from wxbridge.storage import relationships
from synthetic import NOW, msg
import test_memory as memory_tests


class PersonaMemoryTests(unittest.TestCase):
    setUp = memory_tests.MemoryTests.setUp

    def test_persona_and_member_evidence_reach_extractor_with_a_different_name(self):
        config_path = Path(self.temp.name)/'config.json'
        config_path.write_text(json.dumps({'persona':{'name':'橙子'},
            'deepseek':{'system_prompt':'{bot_name}是独立角色'}}, ensure_ascii=False), encoding='utf-8')
        with patch('wxbridge.config.load_dotenv'), patch.dict('os.environ', {}, clear=True):
            self.cfg = load_config(config_path)
        bot = msg(1, NOW-1, '刚才的玩笑', memory_tests.IDENTITY['account'])
        member = msg(2, NOW, '以后不要拿我输游戏开玩笑', 'wxid_a')
        batch = snapshot(self.cfg, [bot, member], [member], memory_tests.IDENTITY, self.memory, NOW)
        model_proposal = {'updates': [{'member':'U1',
            'memories':[{'kind':'preference','mode':'real','summary':'本人不喜欢被拿输游戏开玩笑',
                         'sources':['m2'],'confidence':0.9,'replaces':None}],
            'affinity':{'delta':0,'reason':'','sources':[]},
            'relationship':{'trust':{'delta':0,'reason':'','event':'','sources':[],
                                     'strength':'weak','revises':None},
                            'mood':{'state':'helpless','intensity':1,'reason':'玩笑后的具体边界',
                                    'sources':['m2']}}}], 'learning':[]}
        with patch('wxbridge.ai.client.ModelClient') as client:
            client.return_value.complete.return_value = {'content': json.dumps(model_proposal, ensure_ascii=False)}
            result = extract(self.cfg, batch)
        messages = client.return_value.complete.call_args.args[0]
        system = messages[0]['content']
        public = json.loads(messages[1]['content'])
        self.assertEqual(public['bot_persona'], '橙子是独立角色')
        self.assertEqual(public['source_ids_by_member'], {'U1':['m2']})
        self.assertEqual([row['id'] for row in public['messages']], ['m2'])
        self.assertTrue(any(row['member'] == 'BOT' for row in public['dialogue']))
        for constraint in ('不扮演', '玩笑', '边界', '情绪', '好感'):
            self.assertIn(constraint, system)
        self.assertIsNone(re.search(r'(?<![A-Za-z0-9_])ds(?![A-Za-z0-9_])', system, re.I))
        self.memory.apply(result, batch, NOW)
        person = self.memory.data['members']['wxid_a']
        self.assertEqual((person['affinity'], person['relationship']['trust']), (50, 50))
        self.assertEqual(person['relationship']['mood']['state'], 'helpless')
        self.assertEqual(person['entries'][0]['kind'], 'preference')

    def test_each_affinity_band_preserves_persona_and_factual_boundaries(self):
        persona = '【身份】性格独立，好胜直接但不刻意演戏。'
        for score in range(0, 101, 10):
            with self.subTest(score=score):
                relationship = {'affinity':score, 'familiarity':25, 'trust':50,
                                'mood':{'state':'shy', 'intensity':1}}
                original = copy.deepcopy(relationship)
                result = relationships.with_reply_policy(persona, relationship)
                self.assertTrue(result.startswith(persona))
                self.assertIn('亲疏', result)
                self.assertIn('专业问题', result)
                self.assertIn('偏好优先', result)
                self.assertIn('人格决定说话气质', result)
                self.assertIn('不为展示人格硬加调侃', result)
                self.assertIn('不改变长期亲疏或事实判断', result)
                self.assertIn('不公开', result)
                self.assertEqual(relationship, original)


if __name__ == '__main__':
    unittest.main()
