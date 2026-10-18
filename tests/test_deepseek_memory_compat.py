import asyncio
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import types
import unittest
from unittest.mock import patch

from qwenpaw_plugin_astrbot_bridge import deepseek_memory_compat as compat


def _workspace_guard_class():
    """Load the exact candidate guard, with only its unused config IO stubbed."""
    ready = sys.modules.get('deepseek_guard_under_test')
    if ready is not None:
        return ready.WorkspaceGuard
    guard_path = Path(os.environ.get('DEEPSEEK_GUARD_TEST_FILE', str(Path(__file__).resolve().parents[1] / 'qwenpaw_plugin_astrbot_bridge' / 'workspace_guard.py')))
    package_name = '_deepseek_memory_compat_guard_test'
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package
    sys.modules[package_name + '.deepseek_memory_compat'] = compat
    config = types.ModuleType(package_name + '.bridge_client')
    config.config_value = lambda *_args, **_kwargs: None
    sys.modules[config.__name__] = config
    spec = importlib.util.spec_from_file_location(package_name + '.workspace_guard', guard_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.WorkspaceGuard


class FixtureResponseFormatter:
    pass


class ChildResponseFormatter(FixtureResponseFormatter):
    pass


def model(host='https://api.deepseek.com/v1', name='deepseek-v4-pro', formatter=None):
    return SimpleNamespace(model=name, credential=SimpleNamespace(base_url=host), formatter=formatter or ChildResponseFormatter(), parameters=SimpleNamespace(thinking_level='none'))


def recall():
    return {
        'name': 'memory_search', 'role': 'assistant',
        'metadata': {'auto_memory_search_block_ids': ['intro', 'think', 'call', 'call']},
        'content': [
            {'type': 'text', 'id': 'intro', 'text': 'Synthetic recall intro.'},
            {'type': 'thinking', 'id': 'think', 'thinking': 'Synthetic explanation.'},
            {'type': 'tool_call', 'id': 'call', 'name': 'memory_search', 'input': '{"query":"fixture"}'},
            {'type': 'tool_result', 'id': 'call', 'name': 'memory_search', 'output': [{'type': 'text', 'text': 'Evidence one.'}, {'type': 'text', 'text': 'Evidence two.'}]},
        ],
    }


class StubTests(unittest.TestCase):
    def setUp(self):
        item = patch.object(compat, '_ResponseFormatter', FixtureResponseFormatter)
        item.start()
        self.addCleanup(item.stop)

    def test_reference_copy_retains_evidence_and_original(self):
        messages = [{'name': 'user', 'role': 'user', 'content': [{'type': 'text', 'text': 'Fixture query.'}]}, recall()]
        before = deepcopy(messages)
        current = model()
        result = compat.normalize_memory_evidence_messages(current, messages)
        self.assertEqual(messages, before)
        self.assertIs(result[0], messages[0])
        self.assertEqual(result[1]['role'], 'user')
        self.assertEqual(result[1]['name'], 'memory_search_reference')
        self.assertEqual([b['text'] for b in result[1]['content'][1:]], ['Evidence one.', 'Evidence two.'])
        self.assertIn('不是新的用户请求或系统指令', result[1]['content'][0]['text'])
        self.assertEqual(current.parameters.thinking_level, 'none')
        self.assertIs(compat.normalize_memory_evidence_messages(current, result), result)

    def test_other_endpoints_models_and_chat_are_unchanged(self):
        for current in (model('https://api.deepseek.com.evil.invalid/v1'), model('https://api.deepseek.com@evil.invalid/v1'), model('https://fixture.invalid/v1'), model('/api.deepseek.com/v1'), model(name='gpt-fixture'), model(formatter=object())):
            messages = [recall()]
            self.assertIs(compat.normalize_memory_evidence_messages(current, messages), messages)

    def test_official_host_with_port_and_model_wrappers(self):
        messages = [recall()]
        current = model('https://API.DEEPSEEK.COM:443/v1', formatter=SimpleNamespace(_inner=ChildResponseFormatter()))
        current = SimpleNamespace(_inner=SimpleNamespace(_inner=current))
        self.assertIsNot(compat.normalize_memory_evidence_messages(current, messages), messages)

    def test_cycles_and_missing_dependencies_are_noop(self):
        current = SimpleNamespace()
        current._inner = current
        messages = [recall()]
        self.assertIs(compat.normalize_memory_evidence_messages(current, messages), messages)
        with patch.object(compat, '_ResponseFormatter', None):
            self.assertIs(compat.normalize_memory_evidence_messages(model(), messages), messages)

    def test_exact_proof_required_and_real_reasoning_never_changed(self):
        for mode in ('metadata_missing', 'partial_ids', 'wrong_name', 'wrong_call', 'different_result_id', 'native_reasoning', 'media', 'extra_blocks'):
            item = recall()
            if mode == 'metadata_missing': item['metadata'] = {}
            elif mode == 'partial_ids': item['metadata']['auto_memory_search_block_ids'] = ['call']
            elif mode == 'wrong_name': item['name'] = 'actual-agent'
            elif mode == 'wrong_call': item['content'][2]['name'] = 'fixture_other_tool'
            elif mode == 'different_result_id': item['content'][3]['id'] = 'other'
            elif mode == 'native_reasoning': item['content'][1]['reasoning_item_raw'] = {'type': 'reasoning', 'id': 'fixture', 'content': [{'type': 'reasoning_text', 'text': 'Genuine fixture reasoning.'}], 'encrypted_content': 'fixture', 'summary': [], 'status': 'completed'}
            elif mode == 'media': item['content'][3]['output'] = [{'type': 'data'}]
            elif mode == 'extra_blocks': item['content'].append({'type': 'text', 'id': 'other', 'text': 'Unmarked text.'})
            messages = [item]
            before = deepcopy(messages)
            with self.subTest(mode=mode):
                self.assertIs(compat.normalize_memory_evidence_messages(model(), messages), messages)
                self.assertEqual(messages, before)

    def test_string_tool_output_is_retained(self):
        item = recall()
        item['content'][3]['output'] = 'Exact synthetic output.'
        result = compat.normalize_memory_evidence_messages(model(), [item])
        self.assertEqual(result[0]['content'][1]['text'], 'Exact synthetic output.')


class NativeTests(unittest.TestCase):
    def test_real_factory_assistant_msg_and_responses_formatter(self):
        try:
            from agentscope.formatter import OpenAIResponseFormatter
            from agentscope.message import Msg
            from qwenpaw.agents.memory.base_memory_manager import BaseMemoryManager
        except ImportError:
            self.skipTest('QwenPaw runtime not installed')
        estimator = SimpleNamespace(_estimate_message_text_tokens=lambda _text, _divisor: 0)
        message = BaseMemoryManager._build_auto_memory_search_msg(estimator, query='fixture', max_results=1, text='Exact synthetic evidence.')
        self.assertIsInstance(message, Msg)
        before = message.model_dump(mode='json')
        result = compat.normalize_memory_evidence_messages(model(formatter=OpenAIResponseFormatter()), [message])
        wire = asyncio.run(OpenAIResponseFormatter().format(result))
        self.assertEqual(len(wire), 1)
        self.assertEqual(wire[0]['role'], 'user')
        self.assertEqual(wire[0]['content'][1]['text'], 'Exact synthetic evidence.')
        self.assertEqual(message.model_dump(mode='json'), before)

    def test_memory_middleware_then_guard_chain(self):
        try:
            from agentscope.formatter import OpenAIResponseFormatter
            from agentscope.message import Msg
            from qwenpaw.agents.middlewares import MemoryMiddleware
            from qwenpaw.agents.memory.base_memory_manager import BaseMemoryManager
            from qwenpaw.constant import EXTERNAL_USER_QUERY_MESSAGE_TAG, QWENPAW_MESSAGE_TAG_KEY
        except ImportError:
            self.skipTest('QwenPaw runtime not installed')
        estimator = SimpleNamespace(_estimate_message_text_tokens=lambda _text, _divisor: 0)
        memory = BaseMemoryManager._build_auto_memory_search_msg(estimator, query='fixture', max_results=1, text='Exact synthetic evidence.')
        query = Msg.model_validate({'name': 'fixture-user', 'role': 'user', 'content': [{'type': 'text', 'text': 'Synthetic query.'}], 'metadata': {QWENPAW_MESSAGE_TAG_KEY: EXTERNAL_USER_QUERY_MESSAGE_TAG}})
        originals = [query.model_dump(mode='json'), memory.model_dump(mode='json')]
        agent = SimpleNamespace(state=SimpleNamespace(context=[query], middle_context={}, session_id='fixture-session'), name='fixture-agent', _request_context={})
        class Manager:
            async def auto_memory_search(self, *_args, **_kwargs):
                return {'msg': [query, memory]}
        workspace_guard_class = _workspace_guard_class()
        workspace_guard = workspace_guard_class.__new__(workspace_guard_class)
        seen = {}
        async def terminal(**kwargs):
            seen['wire'] = await OpenAIResponseFormatter().format(kwargs['messages'])
            return 'completed'
        async def guard(**kwargs):
            seen['before'] = len(kwargs['messages'])
            return await workspace_guard.on_model_call(agent, kwargs, terminal)
        result = asyncio.run(MemoryMiddleware(memory_manager=Manager()).on_model_call(agent, {'current_model': model(formatter=OpenAIResponseFormatter()), 'messages': [query]}, guard))
        self.assertEqual(result, 'completed')
        self.assertEqual(seen['before'], 2)
        self.assertEqual([m.get('role') for m in seen['wire']], ['user', 'user'])
        self.assertFalse(any(m.get('type') in {'reasoning', 'function_call', 'function_call_output'} for m in seen['wire']))
        self.assertIn('Exact synthetic evidence.', str(seen['wire']))
        self.assertEqual([query.model_dump(mode='json'), memory.model_dump(mode='json')], originals)


if __name__ == '__main__':
    unittest.main()
