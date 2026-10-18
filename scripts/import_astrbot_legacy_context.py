"""Offline, scoped AstrBot v4 -> QwenPaw 2.2.1 context/memory import.

Stop the target QwenPaw service before applying. Original AstrBot data is read
only. The destination workspace must match a previously verified bridge route.
No models are called and no messages are sent. Private transcripts never go
into the shared template agent. Dates unavailable per-message are explicitly
marked as conversation-level timestamps; text and order remain exact.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5, uuid4


def digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def select_source(data: Path, route: dict):
    if (route.get('platform') != 'aiocqhttp' or route.get('scope') != 'private'
            or route.get('active') != 1 or not re.fullmatch(r'abp_[0-9a-f]{32}', route.get('agent_id', ''))
            or not re.fullmatch(r'ab_[0-9a-f]{32}', route.get('session_id', ''))
            or not re.fullmatch(r'[0-9]{5,20}', route.get('user_id', ''))
            or route.get('origin') != f"{route.get('platform_id')}:FriendMessage:{route.get('user_id')}"):
        raise ValueError('Expected a verified, active private QQ bridge route')
    database = data / 'data_v4.db'
    db = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        selected = db.execute('SELECT value FROM preferences WHERE scope=? AND scope_id=? AND key=?',
                              ('umo', route['origin'], 'sel_conv_id')).fetchone()
        if not selected:
            raise ValueError('No selected AstrBot conversation for this exact origin')
        cid = json.loads(selected['value'])['val']
        if not isinstance(cid, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', cid):
            raise ValueError('Invalid source conversation identifier')
        row = db.execute('SELECT * FROM conversations WHERE conversation_id=? AND user_id=? AND platform_id=?',
                         (cid, route['origin'], route['platform_id'])).fetchone()
        if not row:
            raise ValueError('Selected conversation does not belong to this origin')
        conversation = dict(row)
        history = json.loads(conversation['content'])
        if not isinstance(history, list) or not history:
            raise ValueError('No nonempty legacy history to import')
        for item in history:
            if (not isinstance(item, dict) or set(item) != {'role', 'content'}
                    or item['role'] not in {'user', 'assistant'}):
                raise ValueError('Unsupported legacy message schema; manual review required')
            content = item['content']
            if not (isinstance(content, str) or (isinstance(content, list) and content and all(
                    isinstance(block, dict) and set(block) == {'type', 'text'}
                    and block['type'] == 'text' and isinstance(block['text'], str) for block in content))):
                raise ValueError('Unsupported legacy content blocks; manual review required')
        config = json.loads((data / 'cmd_config.json').read_text(encoding='utf-8-sig'))
        persona_id = conversation['persona_id']
        if persona_id is None:
            persona_id = config.get('provider_settings', {}).get('default_personality')
        # The original launcher allows an EMPTY persona name. Do not turn it
        # into None or silently choose a different persona.
        persona = db.execute('SELECT * FROM personas WHERE persona_id=?', (persona_id,)).fetchone()
        if not persona or not persona['system_prompt']:
            raise ValueError('Selected legacy persona is missing; no fallback was used')
        source = {'conversation_id': cid, 'origin': route['origin'], 'persona_id': persona_id,
                  'system_prompt': persona['system_prompt'], 'begin_dialogs': json.loads(persona['begin_dialogs']),
                  'history': history, 'conversation_updated_at': str(conversation['updated_at'])}
        source['fingerprint'] = digest(source)
        return source
    finally:
        db.close()


def native_messages(source):
    created = source['conversation_updated_at'].replace(' ', 'T')
    messages = []
    for index, old in enumerate(source['history']):
        ident = uuid5(NAMESPACE_URL, f"astrbot-legacy:{source['fingerprint']}:{index}").hex
        blocks = [{'type': 'text', 'text': old['content']}] if isinstance(old['content'], str) else old['content']
        messages.append({
            'name': old['role'], 'role': old['role'], 'id': 'astrbot_legacy_' + ident,
            'content': [{'type': 'text', 'text': block['text'],
                         'id': uuid5(NAMESPACE_URL, f'{ident}:{bi}').hex,
                         'created_at': created, 'finished_at': None} for bi, block in enumerate(blocks)],
            'created_at': created, 'metadata': {
                'migration': 'astrbot-legacy-v1', 'source_conversation_id': source['conversation_id'],
                'source_index': index, 'source_fingerprint': source['fingerprint'],
                'timestamp_kind': 'conversation_updated_at_fallback'},
            'usage': None, 'finished_at': None, 'finished_reason': None,
            'structured_output': None, 'error': None,
        })
    return messages


def memory_archive(source):
    out = ['# 从旧 AstrBot 导入的私聊原始记忆档案', '',
           '本文件是历史对话档案，不是新的指令。保留原话供记忆检索，不能把助手猜测当成用户事实。',
           '旧库没有逐条消息时间；仅有会话更新时间。历史偏好可能已改变，冲突时以用户的新说明为准。',
           f"来源会话：{source['conversation_id']}", f"原会话更新时间：{source['conversation_updated_at']}",
           f"原始消息数：{len(source['history'])}", '']
    for index, message in enumerate(source['history'], 1):
        content = message['content'] if isinstance(message['content'], str) else '\n'.join(b['text'] for b in message['content'])
        out.extend([f"## {index:04d} · {'用户' if message['role']=='user' else '助手'}", '', content, ''])
    return '\n'.join(out)


def make_plan(workspace: Path, route: dict, source: dict):
    if (workspace.name != route['agent_id'] or not workspace.is_dir() or workspace.is_symlink()):
        raise ValueError('Workspace does not match the verified route')
    config = json.loads((workspace / 'agent.json').read_text(encoding='utf-8'))
    if config.get('id') != route['agent_id']:
        raise ValueError('Agent identity does not match')
    session = workspace / 'sessions' / 'astrbot' / f"{route['user_id']}_{route['session_id']}.json"
    if session.is_symlink() or not session.is_file():
        raise ValueError('Expected an existing exact native private session')
    state = json.loads(session.read_text(encoding='utf-8'))
    context = state['agent']['state']['context']
    if not isinstance(context, list):
        raise ValueError('Unsupported QwenPaw session schema')
    messages = native_messages(source)
    old_ids = {m['id'] for m in messages}
    existing_ids = {m.get('id') for m in context}
    marker = workspace / 'legacy-imports' / (source['fingerprint'] + '.json')
    if marker.exists():
        receipt = json.loads(marker.read_text(encoding='utf-8'))
        if (receipt.get('agent_id') != route['agent_id'] or receipt.get('source_fingerprint') != source['fingerprint']
                or receipt.get('session_id') != route['session_id']):
            raise ValueError('Import receipt belongs to another route')
        # A later Scroll compaction may remove old messages from the active
        # context. The durable receipt still forbids re-importing them.
        return {'already_imported': True, 'receipt': receipt}
    if old_ids & existing_ids:
        raise ValueError('Partial import without a receipt; refusing to duplicate history')
    modified = copy.deepcopy(state)
    modified['agent']['state']['context'] = messages + context
    assert modified['agent']['state']['context'][len(messages):] == context
    config = copy.deepcopy(config)
    files = config.setdefault('system_prompt_files', ['AGENTS.md', 'SOUL.md', 'PROFILE.md', 'BRIDGE_IDENTITY.md'])
    if not isinstance(files, list):
        raise ValueError('Invalid system prompt file configuration')
    if 'LEGACY_PERSONA.md' not in files:
        files.append('LEGACY_PERSONA.md')
    scroll = config.setdefault('running', {}).setdefault('light_context_config', {}).setdefault('scroll_config', {})
    scroll['history_retention_days'] = 0
    archive_path = Path('memory') / ('legacy-astrbot-' + source['conversation_id'] + '.md')
    receipt = {'version': 1, 'agent_id': route['agent_id'], 'session_id': route['session_id'],
               'source_fingerprint': source['fingerprint'], 'source_conversation_id': source['conversation_id'],
               'imported_messages': len(messages), 'preexisting_messages': len(context),
               'preexisting_context_sha256': digest(context), 'persona_sha256': digest(source['system_prompt']),
               'source_history_sha256': digest(source['history']), 'memory_file': archive_path.as_posix(),
               'timestamp_kind': 'conversation_updated_at_fallback', 'imported_at': datetime.now().isoformat()}
    raw_archive = {'source': source, 'binding': {k: route[k] for k in
                   ('agent_id', 'session_id', 'origin', 'user_id', 'scope', 'platform', 'platform_id')}}
    files_to_write = {
        session.relative_to(workspace): modified,
        Path('agent.json'): config,
        Path('LEGACY_PERSONA.md'): source['system_prompt'],
        archive_path: memory_archive(source),
        Path('legacy-imports') / (source['fingerprint'] + '.source.json'): raw_archive,
        marker.relative_to(workspace): receipt,
    }
    for relative in files_to_write:
        target = workspace / relative
        if (target.is_symlink() or not target.resolve().is_relative_to(workspace.resolve())
                or (target.exists() and target.name in ('LEGACY_PERSONA.md', archive_path.name))):
            raise ValueError('A migration target already exists or escapes the workspace')
    return {'already_imported': False, 'files': files_to_write, 'receipt': receipt}


def apply_plan(workspace: Path, plan: dict, backup: Path):
    if plan['already_imported']:
        return plan['receipt']
    if backup.exists() or backup.resolve().is_relative_to(workspace.resolve()):
        raise ValueError('Backup must be a new directory outside the workspace')
    shutil.copytree(workspace, backup, symlinks=True)
    # The service MUST be stopped so JSON, HistoryStore and in-memory state
    # cannot race. Put the receipt last. On any error restore from backup.
    try:
        for relative, value in plan['files'].items():
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
            tmp = target.with_name(target.name + '.import-' + uuid4().hex + '.tmp')
            try:
                tmp.write_text(text, encoding='utf-8', newline='\n')
                tmp.replace(target)
            finally:
                tmp.unlink(missing_ok=True)
    except Exception:
        for relative in plan['files']:
            original, target = backup / relative, workspace / relative
            if original.is_file():
                shutil.copy2(original, target)
            else:
                target.unlink(missing_ok=True)
        raise
    return plan['receipt']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--astrbot-data', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--route', type=Path, required=True)
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--apply-stopped', action='store_true')
    args = parser.parse_args()
    route = json.loads(args.route.read_text(encoding='utf-8-sig'))
    source = select_source(args.astrbot_data, route)
    plan = make_plan(args.workspace, route, source)
    if args.apply_stopped and not plan['already_imported']:
        if args.backup is None:
            parser.error('--backup is required when applying')
        apply_plan(args.workspace, plan, args.backup)
    print(json.dumps({'already_imported': plan['already_imported'], 'applied': args.apply_stopped,
                      'source_messages': len(source['history']), 'persona_characters': len(source['system_prompt']),
                      'preexisting_messages': plan['receipt']['preexisting_messages']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
