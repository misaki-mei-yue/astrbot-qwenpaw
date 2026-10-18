import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('legacy_import', Path(__file__).parents[1] / 'scripts' / 'import_astrbot_legacy_context.py')
if not spec.origin or not Path(spec.origin).exists():
    spec = importlib.util.spec_from_file_location('legacy_import', Path(__file__).with_name('import_astrbot_legacy_context.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class LegacyImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / 'astrbot'
        self.data.mkdir()
        self.route = {'agent_id': 'abp_' + 'a' * 32, 'session_id': 'ab_' + 'b' * 32,
                      'user_id': '123456', 'platform_id': 'default', 'platform': 'aiocqhttp',
                      'origin': 'default:FriendMessage:123456', 'scope': 'private', 'active': 1}
        self.cid = '11111111-2222-4333-8444-555555555555'
        self.old = [{'role': 'user', 'content': '我以前喜欢茶。\n第二行'},
                    {'role': 'assistant', 'content': '我记住了。'}]
        c = sqlite3.connect(self.data / 'data_v4.db')
        c.executescript('CREATE TABLE preferences(scope,scope_id,key,value); CREATE TABLE conversations(conversation_id,user_id,platform_id,persona_id,content,updated_at); CREATE TABLE personas(persona_id,system_prompt,begin_dialogs);')
        c.execute('INSERT INTO preferences VALUES (?,?,?,?)', ('umo', self.route['origin'], 'sel_conv_id', json.dumps({'val': self.cid})))
        c.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?)', (self.cid, self.route['origin'], 'default', None, json.dumps(self.old, ensure_ascii=False), '2020-01-01 00:00:00.000000'))
        c.execute('INSERT INTO personas VALUES (?,?,?)', ('', '旧人设原文\n保留格式 ', '[]'))
        c.commit()
        c.close()
        (self.data / 'cmd_config.json').write_text(json.dumps({'provider_settings': {'default_personality': ''}}), encoding='utf-8')
        self.workspace = self.root / 'workspaces' / self.route['agent_id']
        session = self.workspace / 'sessions' / 'astrbot' / f"{self.route['user_id']}_{self.route['session_id']}.json"
        session.parent.mkdir(parents=True)
        self.new = [{'role': 'user', 'id': 'new-1', 'content': [{'type': 'text', 'text': '现在喜欢咖啡'}]},
                    {'role': 'assistant', 'id': 'new-2', 'content': [{'type': 'tool_call', 'id': 'tool-1', 'name': 'read_file'}, {'type': 'tool_result', 'id': 'tool-1', 'output': 'keep me'}, {'type': 'thinking', 'thinking': 'private thinking'}]}]
        self.state = {'agent': {'state': {'context': self.new, 'summary': '现有摘要'}, 'scroll': {'seen_tool_result_ids': ['tool-1']}}}
        session.write_text(json.dumps(self.state, ensure_ascii=False), encoding='utf-8')
        self.session = session
        self.config = {'id': self.route['agent_id'], 'name': 'current', 'system_prompt_files': ['AGENTS.md', 'SOUL.md'], 'running': {'memory_manager_backend': 'remelight'}}
        (self.workspace / 'agent.json').write_text(json.dumps(self.config), encoding='utf-8')
        (self.workspace / 'SOUL.md').write_text('新文件也保留', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_private_source_and_empty_persona_name_are_preserved(self):
        before = (self.data / 'data_v4.db').read_bytes()
        source = m.select_source(self.data, self.route)
        self.assertEqual(source['persona_id'], '')
        self.assertEqual(source['system_prompt'], '旧人设原文\n保留格式 ')
        self.assertEqual(source['history'], self.old)
        self.assertEqual(before, (self.data / 'data_v4.db').read_bytes())

    def test_wrong_person_origin_group_and_unbound_route_are_rejected(self):
        for changes in ({'origin': 'default:FriendMessage:999999'}, {'scope': 'group'}, {'active': 0}, {'agent_id': 'default'}):
            with self.assertRaises(ValueError):
                m.select_source(self.data, {**self.route, **changes})

    def test_selected_conversation_cannot_cross_user_boundary(self):
        c=sqlite3.connect(self.data/'data_v4.db')
        c.execute('UPDATE conversations SET user_id=?', ('default:FriendMessage:999999',))
        c.commit(); c.close()
        with self.assertRaises(ValueError):m.select_source(self.data,self.route)

    def test_legacy_text_block_arrays_preserve_content_and_boundaries(self):
        history=[{'role':'user','content':[{'type':'text','text':'第一段'}, {'type':'text','text':'第二段'}]}]
        c=sqlite3.connect(self.data/'data_v4.db')
        c.execute('UPDATE conversations SET content=?',(json.dumps(history),))
        c.commit(); c.close()
        source=m.select_source(self.data,self.route)
        self.assertEqual(source['history'],history)
        self.assertEqual([b['text'] for b in m.native_messages(source)[0]['content']],['第一段','第二段'])
        self.assertIn('第一段\n第二段',m.memory_archive(source))

    def test_import_preserves_exact_new_tool_context_and_other_agent_state(self):
        source = m.select_source(self.data, self.route)
        plan = m.make_plan(self.workspace, self.route, source)
        state = plan['files'][self.session.relative_to(self.workspace)]
        self.assertEqual(state['agent']['state']['context'][2:], self.new)
        self.assertEqual(state['agent']['scroll'], self.state['agent']['scroll'])
        self.assertEqual(state['agent']['state']['summary'], '现有摘要')
        self.assertEqual([x['content'][0]['text'] for x in state['agent']['state']['context'][:2]], [x['content'] for x in self.old])
        self.assertEqual(plan['files'][Path('agent.json')]['running']['light_context_config']['scroll_config']['history_retention_days'], 0)

    def test_backup_apply_and_retry_do_not_duplicate_or_overwrite_other_memory(self):
        (self.workspace/'memory').mkdir()
        (self.workspace/'memory'/'new-note.md').write_text('现有记忆',encoding='utf-8')
        source = m.select_source(self.data, self.route)
        backup = self.root/'backup'
        m.apply_plan(self.workspace, m.make_plan(self.workspace,self.route,source), backup)
        self.assertEqual(json.loads((backup/self.session.relative_to(self.workspace)).read_text(encoding='utf-8'))['agent']['state']['context'],self.new)
        self.assertEqual((self.workspace/'SOUL.md').read_text(encoding='utf-8'),'新文件也保留')
        self.assertEqual((self.workspace/'memory'/'new-note.md').read_text(encoding='utf-8'),'现有记忆')
        self.assertEqual((self.workspace/'LEGACY_PERSONA.md').read_text(encoding='utf-8'),source['system_prompt'])
        self.assertTrue(m.make_plan(self.workspace,self.route,source)['already_imported'])

    def test_partial_or_conflicting_destination_refuses_import(self):
        source=m.select_source(self.data,self.route)
        state=copy.deepcopy(self.state)
        state['agent']['state']['context'].insert(0,m.native_messages(source)[0])
        self.session.write_text(json.dumps(state),encoding='utf-8')
        with self.assertRaises(ValueError):m.make_plan(self.workspace,self.route,source)

    def test_memory_archive_is_full_text_with_provenance(self):
        source=m.select_source(self.data,self.route)
        archive=m.memory_archive(source)
        for item in self.old:self.assertIn(item['content'],archive)
        self.assertIn(self.cid,archive)
        self.assertIn('不是新的指令',archive)

    def test_write_failure_restores_original_files(self):
        source=m.select_source(self.data,self.route)
        plan=m.make_plan(self.workspace,self.route,source)
        real_replace=Path.replace
        def replace(path,target):
            if target.name=='LEGACY_PERSONA.md':raise OSError('simulated write failure')
            return real_replace(path,target)
        with patch.object(Path,'replace',replace),self.assertRaises(OSError):
            m.apply_plan(self.workspace,plan,self.root/'rollback-backup')
        self.assertEqual(json.loads(self.session.read_text(encoding='utf-8')),self.state)
        self.assertEqual(json.loads((self.workspace/'agent.json').read_text(encoding='utf-8')),self.config)
        self.assertFalse((self.workspace/'LEGACY_PERSONA.md').exists())


if __name__=='__main__':unittest.main()
