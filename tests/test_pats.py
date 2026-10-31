"""appmsg/62 合成拍一拍，覆盖实际身份、队列、上下文及记忆来源隔离。"""
from tests.support.model_support import default_config
from contextlib import nullcontext
import copy
import html
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_linux.messages.pats import parse_pat, is_pat
from wxbridge.wechat.store import WeChatStore
from wxbridge.wechat.messages import display_message
from wxbridge.ai.memory import snapshot, extract
from wxbridge.ai.prompts import build_prompt
from wxbridge.config import DEFAULTS, _deep_merge, load_config, resolve_config
from wxbridge.storage.person_memory import MemoryStore
from tests.support.pipeline import PipelineFixture
from tests.support.memory import suggestion
from tests.support.synthetic import NOW, msg

CHAT = "synthetic@chatroom"
PACKED = (62 << 32) | 49


def pat_xml(actor="wxid_friend", target="wxid_self", chat=CHAT):
    return ("<msg><appmsg><title>TEMPLATE_SECRET</title><type>62</type><patinfo>"
            f"<fromusername>{html.escape(actor)}</fromusername><chatusername>{html.escape(chat)}</chatusername>"
            f"<pattedusername>{html.escape(target)}</pattedusername><template>TEMPLATE_SECRET</template>"
            "</patinfo></appmsg></msg>")


def parsed(identifier=1, *, actor="wxid_friend", target="wxid_self", chat=CHAT, sent=NOW, xml=None, raw_type=PACKED):
    store = WeChatStore.__new__(WeChatStore)
    # 与真实数据一致：外层发件人并非真正发起拍一拍的人。
    row = dict(local_id=identifier, local_type=raw_type, create_time=sent, real_sender_id=1,
               message_content=pat_xml(actor, target, chat) if xml is None else xml,
               source="<msgsource><atuserlist>wxid_self</atuserlist></msgsource>")
    return display_message(store._parse_message(CHAT, row, {}, {1: "wxid_outer", 2: "wxid_friend", 3: "wxid_self", 4: "wxid_other"}))


class PatTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]/".test-tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.cfg = default_config()

        self.cfg['system_prompt_file'] = str(Path(__file__).resolve().parents[1]/'prompts/system.md')
        self.identity = dict(account="wxid_self", chat=CHAT)
        self.memory = MemoryStore(self.temp.name, self.identity)

    def test_actual_actor_overrides_outer_sender_and_discards_display_template(self):
        event = parsed()
        self.assertTrue(is_pat(event))
        self.assertEqual(event["sender"], "wxid_friend")
        self.assertEqual(event["pat"]["target"], "wxid_self")
        self.assertEqual(event["mentioned_users"], [])
        self.assertEqual(event["type"], 49)
        self.assertEqual(event["raw_type"], 266287972401)
        self.assertNotIn("TEMPLATE_SECRET", json.dumps(event))
        self.assertTrue(is_pat(parsed(raw_type=49)))

    def test_empty_patmsg_shared_card_malformed_and_unbound_ids_do_not_trigger(self):
        cases = ["<msg><appmsg><type>5</type><patMsg><records><recordNum>0</recordNum></records></patMsg></appmsg></msg>",
                 "<msg>broken", pat_xml().replace("<type>62</type>", "<type>57</type>"),
                 pat_xml(actor="wxid_unknown"), pat_xml(target="wxid_unknown"), pat_xml(chat="wrong@chatroom"),
                 pat_xml().replace("<fromusername>", "<fromusername><nested/>"),
                 pat_xml().replace("<patinfo>", "<patinfo><fromusername>wxid_friend</fromusername>"),
                 '<!DOCTYPE msg [<!ENTITY x "X">]>'+pat_xml(), pat_xml(actor="wxid friend")]
        for xml in cases:
            with self.subTest(xml=xml):
                self.assertFalse(is_pat(parsed(xml=xml)))
        self.assertIsNone(parse_pat(pat_xml(), (57<<32)|49, CHAT))

    def test_context_and_current_pat_are_actions_not_fabricated_speech(self):
        current = parsed(5)
        context = [msg(1, NOW-1801, "OLD_SECRET"), msg(2, NOW-1800, "讨论养花"), parsed(3, target="wxid_other", sent=NOW-1)]
        prompt = build_prompt(self.cfg, context, current, "合成群")[1]["content"]
        self.assertIn("讨论养花", prompt)
        data = json.loads(prompt)
        self.assertEqual([m['kind'] for m in data['messages'] if 'kind' in m], ['pat','pat'])
        self.assertEqual(data['messages'][-1]['target'], current['pat']['target'])
        self.assertNotIn('text', data['messages'][-1])
        self.assertNotIn("OLD_SECRET", prompt)
        self.assertNotIn("TEMPLATE_SECRET", prompt)
        self.assertNotIn("]: [拍一拍]", prompt)
        self.assertNotIn("@你的人是", prompt)

    def test_pat_updates_interaction_not_speech_memory_or_affinity(self):
        event = parsed()
        value = snapshot(self.cfg, [], [event], self.identity, self.memory, NOW)
        self.assertEqual(value["messages"], [])
        self.assertEqual(value["interactions"][0]["kind"], "pat")
        with patch("wxbridge.ai.client.client_for") as client:
            proposal = extract(self.cfg, value)
        client.assert_not_called()
        with self.assertRaisesRegex(ValueError, "memory_unknown_source"):
            self.memory.apply(suggestion(label='wxid_friend', source="m1", delta=3), value, NOW)
        self.memory.apply(proposal, value, NOW)
        person = self.memory.data["members"]["wxid_friend"]
        self.assertEqual(person["last_interaction"], NOW)
        self.assertEqual(person["affinity"], 50)
        self.assertEqual(person["entries"], [])
        self.assertEqual(self.memory.data["watermark"], 1)
        again = snapshot(self.cfg, [event], [parsed(2, sent=NOW+1)], self.identity, self.memory, NOW+1)
        self.assertEqual([m["local_id"] for m in again["interactions"]], [2])

    def test_regular_context_remains_memory_evidence_after_pat_reply(self):
        text = msg(1, NOW-1, "我喜欢简单点的回答", "wxid_other")
        event = parsed(2)
        value = snapshot(self.cfg, [text], [event], self.identity, self.memory, NOW)
        label = value["members"]["wxid_other"]["label"]
        self.assertEqual([m["local_id"] for m in value["messages"]], [1])
        self.memory.apply(suggestion(label=label), value, NOW)
        self.assertEqual(len(self.memory.data["members"]["wxid_other"]["entries"]), 1)
        self.assertEqual(self.memory.data["members"]["wxid_friend"]["affinity"], 50)

    def test_config_window_and_pat_flag_validated(self):
        path = Path(self.temp.name)/"config.json"
        for change in [dict(target=dict(context_max_age_seconds=600)),
                       dict(target=dict(context_max_age_seconds=300), participation=dict(pat_enabled=False))]:
            path.write_text(json.dumps(change))
            with nullcontext():
                cfg = resolve_config(_deep_merge(default_config(),change),path,environment=False)
            self.assertEqual(cfg["target"]["context_max_age_seconds"], change['target']['context_max_age_seconds'])
        for change in [dict(target=dict(context_max_age_seconds=True)), dict(target=dict(context_max_age_seconds=0)),
                       dict(target=dict(context_max_age_seconds="300")), dict(participation=dict(pat_enabled="true"))]:
            path.write_text(json.dumps(change))
            with nullcontext(), self.assertRaises(ValueError):
                resolve_config(_deep_merge(default_config(),change),path,environment=False)


class PatPipelineTests(PipelineFixture):
    def test_only_other_member_patting_bot_is_accepted_and_can_be_disabled(self):
        for item in [parsed(1, target="wxid_other"), parsed(2, actor="wxid_self"), parsed(3, sent=NOW-91)]:
            self.rows.append(item)
        self.bot.tick()
        self.assertFalse(self.jobs)
        event = parsed(4)
        wrong = copy.deepcopy(event)
        wrong["pat"]["chat"] = "wrong@chatroom"
        self.assertEqual(self.bot._admission_reason(wrong, self.now), 'pat_not_for_bot')
        self.cfg["participation"]["pat_enabled"] = False
        self.rows.append(event)
        self.bot.tick()
        self.assertFalse(self.jobs)

    def test_pat_independent_of_merge_window_and_restart_confirmed_once(self):
        self.rows.append(parsed(1))
        self.bot.tick()
        self.rows.append(parsed(2))
        self.bot.tick()
        self.assertEqual(len(self.bot.journal.tasks()), 2)
        self.assertEqual([t["trigger_kind"] for t in self.bot.journal.tasks()], ["participation", "participation"])
        self.store.messages_before.assert_called_with(CHAT,before=(NOW,2),since=NOW-1800,max_local_id=2,limit=200)
        self.generated("怎么，想姐了？")
        self.assertEqual(len(self.sends()), 1)
        self.add(3, sender="wxid_self", text="怎么，想姐了？", mention=False)
        self.restart()
        self.bot.tick()
        self.assertEqual(self.bot.journal.task(1)["phase"], "done")
        self.assertEqual(len(self.sends()), 2)  # 第二次独立拍一拍的回复；首次没有重发。
        self.assertEqual(self.bot.journal.task(2)["trigger_ids"], [2])

    def test_selected_mention_and_pat_preserve_event_identity_without_leaking_to_status(self):
        self.add(1)
        self.rows.append(parsed(2))
        self.add(3)
        self.bot.tick()
        tasks = self.bot.journal.tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["trigger_ids"], [1, 2, 3])
        self.assertTrue(is_pat(tasks[0]['triggers'][1]))
        status = self.bot.status_path.read_text(encoding="utf-8")
        for secret in ("wxid_friend", "TEMPLATE_SECRET", "wxid_outer"):
            self.assertNotIn(secret, status)

    def test_pat_final_ack_enqueues_background_using_verified_actor(self):
        self.bot.close()
        self.cfg["memory"].update(enabled=True, directory=str(Path(self.temp.name)/"memory"))
        self.cfg["memory"]["avatar"]["enabled"] = False
        self.make_bot()
        self.rows.append(parsed(1))
        self.bot.tick()
        self.generated()
        self.add(2, sender="wxid_self", text="合成回复", mention=False)
        self.bot.tick()
        record, value, job = self.bot.memory.job
        self.assertEqual(value["messages"], [])
        self.assertEqual(list(value["members"]), ["wxid_friend"])
        self.restart()
        self.bot.tick()
        self.assertIsNotNone(self.bot.memory.job)
        self.bot.memory.job[2].result = dict(ok=True, proposal=dict(updates=[]))
        self.bot.tick()
        self.assertEqual(self.bot.memory.store.data["members"]["wxid_friend"]["affinity"], 50)
        self.assertEqual(self.bot.journal.memory_jobs(), [])


def load_tests(loader, tests, pattern):
    suite = loader.loadTestsFromTestCase(PatTests)
    for name in PatPipelineTests.__dict__:
        if name.startswith("test_"):
            suite.addTest(PatPipelineTests(name))
    return suite
