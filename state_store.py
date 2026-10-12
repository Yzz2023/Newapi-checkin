#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轮换型凭据的状态存储（GitHub Gist）

背景：部分公益站（站点类型 B）的 `new_api_refresh` 是**一次性轮换令牌**：
每调用一次 /api/user/auth/refresh，服务端就把旧值作废并通过 Set-Cookie 下发新值。
因此必须把新值持久化，否则下次运行就会 401 AUTH_SESSION_REVOKED。

这里用私有 Gist 存状态，避免把凭据提交进（可能是公开的）仓库。

环境变量：
    GH_GIST_TOKEN   具备 gist 权限的 token（必需）
    GIST_ID         目标 gist 的 id（必需）
    GIST_FILENAME   状态文件名，默认 newapi-state.json
"""

import json
import os
import sys
import time

GIST_API = 'https://api.github.com/gists'


def _config():
    token = os.environ.get('GH_GIST_TOKEN', '').strip()
    gist_id = os.environ.get('GIST_ID', '').strip()
    filename = os.environ.get('GIST_FILENAME', 'newapi-state.json').strip() or 'newapi-state.json'
    return token, gist_id, filename


def enabled() -> bool:
    token, gist_id, _ = _config()
    return bool(token and gist_id)


def _headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'newapi-checkin-state',
    }


def load_state() -> dict:
    """读取状态；任何失败都返回空 dict（不阻断签到）。"""
    token, gist_id, filename = _config()
    if not (token and gist_id):
        return {}
    try:
        import requests
    except ImportError:
        print('[state] 缺少 requests，跳过状态读取')
        return {}

    for attempt in range(3):
        try:
            r = requests.get(f'{GIST_API}/{gist_id}', headers=_headers(token), timeout=20)
            if r.status_code == 404:
                print('[state] Gist 不存在（GIST_ID 是否正确？）')
                return {}
            if r.status_code != 200:
                print(f'[state] 读取失败 HTTP {r.status_code}: {r.text[:120]}')
                time.sleep(2)
                continue
            files = (r.json().get('files') or {})
            f = files.get(filename)
            if not f:
                print(f'[state] Gist 中没有 {filename}，按空状态处理')
                return {}
            content = f.get('content') or ''
            if not content.strip():
                return {}
            data = json.loads(content)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f'[state] 读取异常（第 {attempt + 1} 次）: {e}')
            time.sleep(2)
    return {}


def save_state(state: dict) -> bool:
    """写回状态；失败只告警，不影响签到结果。"""
    token, gist_id, filename = _config()
    if not (token and gist_id):
        return False
    try:
        import requests
    except ImportError:
        return False

    payload = {
        'files': {
            filename: {
                'content': json.dumps(state, ensure_ascii=False, indent=1)
            }
        }
    }
    for attempt in range(3):
        try:
            r = requests.patch(f'{GIST_API}/{gist_id}', headers=_headers(token),
                               json=payload, timeout=20)
            if r.status_code == 200:
                print(f'[state] 已写回 Gist（{len(state)} 条记录）')
                return True
            print(f'[state] 写回失败 HTTP {r.status_code}: {r.text[:120]}')
            time.sleep(2)
        except Exception as e:
            print(f'[state] 写回异常（第 {attempt + 1} 次）: {e}')
            time.sleep(2)
    return False


def apply_to_accounts(accounts: list) -> int:
    """把 Gist 里的状态覆盖到账号配置上（轮换后的 refresh token）。"""
    if not enabled():
        return 0
    state = load_state()
    if not state:
        return 0
    n = 0
    for acc in accounts:
        rec = state.get(acc.get('url') or '')
        if not isinstance(rec, dict):
            continue
        if rec.get('refresh_token'):
            acc['refresh_token'] = rec['refresh_token']
            n += 1
        if rec.get('session') and not acc.get('session'):
            acc['session'] = rec['session']
            n += 1
        if rec.get('user_id') and not acc.get('user_id'):
            acc['user_id'] = rec['user_id']
            n += 1
    if n:
        print(f'[state] 已从 Gist 应用 {n} 项轮换凭据')
    return n


def merge_and_save(rotated: dict) -> bool:
    """把本次轮换出的新 token 合并进 Gist 状态。"""
    if not enabled() or not rotated:
        return False
    state = load_state() or {}
    for url, tok in rotated.items():
        rec = state.get(url)
        if not isinstance(rec, dict):
            rec = {}
        rec['refresh_token'] = tok
        rec['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        state[url] = rec
    return save_state(state)


if __name__ == '__main__':
    # 自检
    if not enabled():
        print('未配置 GH_GIST_TOKEN / GIST_ID，状态存储不可用')
        sys.exit(0)
    st = load_state()
    print('当前状态记录数:', len(st))
    for k, v in list(st.items())[:10]:
        rt = (v or {}).get('refresh_token') or ''
        print('  %-32s refresh=%s...' % (k, rt[:12]))
