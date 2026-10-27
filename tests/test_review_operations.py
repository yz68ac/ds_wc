"""Readiness, bounded context and release planning without service mutations."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from wxbridge.config import DEFAULTS
from wxbridge.ai.context import ContextError
from wxbridge.ai.prompts import build_prompt
from wxbridge.storage.memory_selection import MemoryTopic
from wxbridge.native import helper
import test_boot_recovery as recovery_tests
from synthetic import NOW, msg

spec=importlib.util.spec_from_file_location('deploy_release',Path(__file__).parent.parent/'scripts/deploy-release.py')
deployment=importlib.util.module_from_spec(spec);spec.loader.exec_module(deployment)


class ReviewOperationTests(unittest.TestCase):
    def test_native_recovery_uses_helper_readiness_without_constructing_gui(self):
        cfg=copy.deepcopy(DEFAULTS);cfg['ui']['backend']='native_service';cfg['target']['chat']='synthetic@chatroom'
        store=Mock(self_wxid='wxid_synthetic');store.resolve_chat.return_value=('synthetic@chatroom','synthetic')
        store.last_local_id.return_value=10
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent/'.test-tmp') as folder:
            root=Path(folder)
            with patch.object(recovery_tests.boot,'RUN',root),patch.object(recovery_tests.boot,'PERSIST',root/'saved'),\
                 patch('wxbridge.config.load_config',return_value=cfg),patch('wxbridge.wechat.store.WeChatStore',return_value=store),\
                 patch('wxbridge.wechat.ui.A11y') as gui,patch('wxbridge.native.sender.readiness',return_value={'ready':True}) as ready:
                self.assertTrue(recovery_tests.boot.probe()['ready'])
            gui.assert_not_called();ready.assert_called_once_with(cfg)

    def test_helper_health_checks_client_but_never_attaches_or_sends(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent/'.test-tmp') as folder:
            root=Path(folder);(root/'profile.json').write_text(json.dumps(dict(sha256='synthetic')))
            with patch.object(helper,'find_client',return_value=123) as find:
                result=helper.health(dict(uid=995,assets=str(root)))
            self.assertTrue(result['result']['client_ready']);find.assert_called_once_with(995,'synthetic')

    def test_optional_context_degradation_preserves_full_question_and_reports_omissions(self):
        cfg=copy.deepcopy(DEFAULTS);cfg['deepseek'].update(system_prompt_file='',system_prompt='synthetic')
        cfg['target']['context_max_chars']=1200
        rows=[msg(i,NOW-20+i,'OLD_'+str(i)+'x'*150) for i in range(1,11)]
        trigger=msg(20,NOW,'CURRENT_QUESTION_UNCHANGED')
        with self.assertRaises(ContextError):build_prompt(cfg,rows,trigger,'synthetic')
        cfg['target']['context_budget_strategy']='recent'
        content=build_prompt(cfg,rows,trigger,'synthetic')[1]['content']
        self.assertIn(trigger['text'],content);self.assertIn('因预算未附送',content)
        self.assertLessEqual(len(content),1200);self.assertNotIn('OLD_1x',content)
        self.assertIn('OLD_10x',content)

    def test_manual_aliases_match_same_entity_without_activating_unrelated_topics(self):
        aliases=[['warframe','星际战甲']]
        entry=dict(summary='上周聊过Warframe',kind='event')
        self.assertGreater(MemoryTopic('今天讨论星际战甲',aliases=aliases).score(entry),0)
        self.assertEqual(MemoryTopic('今天讨论JOJO',aliases=aliases).score(entry),0)

    def test_release_plan_preserves_prompts_and_rejects_private_or_removed_paths(self):
        info=dict(sha256='a'*64,mode='100644')
        old=dict(files={'prompts/system.md':info,'wxbridge/config.py':info})
        incoming=dict(files={**old['files'],'prompts/system.md':dict(info,sha256='b'*64),
                             'prompts/topic_aliases.json':info,'wxbridge/config.py':dict(info,sha256='b'*64)})
        self.assertEqual(deployment.changes(old,incoming),['prompts/topic_aliases.json','wxbridge/config.py'])
        for invalid in ('../outside','.env','config.json','memory/private.json'):
            with self.subTest(path=invalid),self.assertRaises(ValueError):
                deployment.changes(old,dict(files={**incoming['files'],invalid:info}))
        with self.assertRaises(ValueError):deployment.changes(old,dict(files={}))

    def test_release_removals_need_an_exact_list_and_prompt_updates_are_explicit(self):
        info=dict(sha256='a'*64,mode='100644')
        old=dict(files={'scripts/old.py':info,'prompts/system.md':info})
        incoming=dict(files={'scripts/diagnostics/old.py':info,
                             'prompts/system.md':dict(info,sha256='b'*64)})
        for allow in ((),('scripts/other.py',),('scripts/old.py','extra.py')):
            with self.subTest(allow=allow),self.assertRaises(ValueError):
                deployment.changes(old,incoming,allow_removed=allow)
        self.assertEqual(deployment.changes(old,incoming,allow_removed=['scripts/old.py']),
                         ['scripts/diagnostics/old.py'])
        self.assertEqual(deployment.changes(old,incoming,allow_removed=['scripts/old.py'],update_prompts=True),
                         ['prompts/system.md','scripts/diagnostics/old.py'])
        with self.assertRaises(ValueError):
            deployment.changes(old,dict(files={}),allow_removed=list(old['files']),update_prompts=True)
