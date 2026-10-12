#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于真实浏览器的登录（可解 Cloudflare Turnstile）

为什么需要它
------------
部分站点登录接口开启了 Turnstile，直接 POST 会返回「Turnstile token 为空」。
而 Turnstile 的 sitekey **绑定站点域名**：
- 在本地 127.0.0.1 页面渲染 → `error 110200 (invalid domain)`，页面被导航到
  invaliddomain 并销毁（所以 `get_turnstile_token()` 对受限 sitekey 无效）
- 在**站点自己的页面**里注入渲染 → 域名正确，可正常解出 token ✅

因此流程是：用真实 Chrome 打开站点登录页（顺便过 CF）→ 在页面内注入 Turnstile
组件并求解 → 用**页面自身的 fetch** 带 `?turnstile=<token>` 提交登录 →
从浏览器上下文取出 cookie。

服务端把 token 读作**查询参数** `?turnstile=`（不是请求体），这一点已实测确认。

对外接口
--------
    browser_login(base_url, username, password, sitekey=None, proxy=None,
                  max_wait=120, verbose=True)
        -> dict  登录成功后的 cookies（含 new_api_refresh / server_name_session）
        -> None  失败
"""
import time

from turnstile_solver import (_launch_chrome, _get_system_proxy,
                              _solve_in_page, RENDER_JS)

# 页面内提交登录：与求解 Turnstile 处于同一浏览器上下文，
# 避免 token 与请求指纹/出口 IP 不匹配的问题。
LOGIN_JS = '''async (args) => {
    const url = '/api/user/login?turnstile=' + encodeURIComponent(args.token);
    const resp = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: args.username, password: args.password}),
        credentials: 'include',
    });
    const text = await resp.text();
    let body = null;
    try { body = JSON.parse(text); } catch (e) {}
    return {status: resp.status, body: body, raw: text.slice(0, 200)};
}'''


def fetch_sitekey(base_url, verbose=True):
    """从 /api/status 读取 turnstile_site_key。"""
    try:
        import requests
        s = requests.Session()
        s.trust_env = False
        s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        r = s.get(base_url.rstrip('/') + '/api/status', timeout=20)
        key = ((r.json() or {}).get('data') or {}).get('turnstile_site_key')
        if verbose:
            print(f'  [浏览器登录] sitekey = {key}')
        return key
    except Exception as e:
        if verbose:
            print(f'  [浏览器登录] 读取 sitekey 失败: {e}')
        return None


def _wait_cf(page, timeout=60, verbose=True):
    """
    等待**当前页面**通过 Cloudflare 验证（不导航）。

    不用 turnstile_solver._wait_cf_pass 的原因：它内部会 `page.goto(base_url)`，
    会把已经进入的登录页重新拉回根路径，且对加载缓慢的站点（如 cpolar 隧道）
    容易在 domcontentloaded 上超时。
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            title = page.title()
        except Exception:
            title = ''
        if 'Attention Required' not in title and 'Just a moment' not in title:
            return True
        if verbose:
            print(f'  [浏览器登录] 等待 CF 验证通过: {title[:40]}')
        time.sleep(4)
    return False


def browser_login(base_url: str, username: str, password: str, sitekey: str = None,
                  proxy: str = None, max_wait: int = 120, verbose: bool = True):
    """
    在站点登录页用真实 Chrome 解 Turnstile 并完成登录，返回 cookies dict；失败返回 None。
    """
    base_url = base_url.rstrip('/')
    login_url = f'{base_url}/login'
    if proxy is None:
        proxy = _get_system_proxy()

    if not sitekey:
        sitekey = fetch_sitekey(base_url, verbose=verbose)
    if not sitekey:
        print('  [浏览器登录] 站点未暴露 turnstile_site_key，无法自动登录')
        return None

    proc, dbg_port = _launch_chrome(proxy, url=login_url)
    cookies = None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = None
            last_err = None
            for _ in range(3):
                try:
                    browser = p.chromium.connect_over_cdp(f'http://127.0.0.1:{dbg_port}')
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(3)
            if browser is None:
                print(f'  [浏览器登录] CDP 连接失败: {last_err}')
                return None

            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # 用 commit 而非 domcontentloaded：后者会被慢站点（cpolar 隧道等）
            # 上阻塞加载的静态资源拖到超时。
            try:
                page.goto(login_url, wait_until='commit', timeout=60000)
            except Exception as e:
                print(f'  [浏览器登录] 导航到登录页超时，尝试继续: {str(e)[:70]}')

            if not _wait_cf(page, timeout=60, verbose=verbose):
                print('  [浏览器登录] Cloudflare 验证未通过')
                browser.close()
                return None

            # 等 SPA 把登录表单渲染出来（慢站点给足时间）
            try:
                page.wait_for_selector('input[type="password"]', timeout=45000)
            except Exception:
                print('  [浏览器登录] 未找到登录表单（页面结构可能不同或加载过慢）')
                if verbose:
                    try:
                        print('    当前 URL:', page.url)
                    except Exception:
                        pass
                browser.close()
                return None
            time.sleep(2)

            # 在站点页面内注入 Turnstile 组件并求解（origin 正确才能过域名校验）
            try:
                page.evaluate(RENDER_JS, sitekey)
            except Exception as e:
                print(f'  [浏览器登录] 注入 Turnstile 组件失败: {e}')
                browser.close()
                return None
            token = _solve_in_page(page, sitekey, max_wait=max_wait, verbose=verbose)
            if not token:
                print('  [浏览器登录] 未能解出 Turnstile token')
                browser.close()
                return None

            # 页面内提交登录
            try:
                res = page.evaluate(LOGIN_JS, {'token': token,
                                               'username': username,
                                               'password': password})
            except Exception as e:
                print(f'  [浏览器登录] 提交登录失败: {e}')
                browser.close()
                return None

            body = res.get('body') or {}
            ok = bool(body.get('success'))
            msg = str(body.get('message') or res.get('raw') or '')[:140]
            if verbose:
                print(f'  [浏览器登录] 登录响应: HTTP {res.get("status")} success={ok} {msg}')

            cookies = {c['name']: c['value'] for c in ctx.cookies() if c.get('value')}
            browser.close()
    except Exception as e:
        print(f'  [浏览器登录] 异常: {e}')
        return None
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    if not cookies:
        return None
    got = [k for k in ('new_api_refresh', 'server_name_session', 'session') if k in cookies]
    if verbose:
        print(f'  [浏览器登录] 取到 cookie: {got or "（无）"}')
    if not ('new_api_refresh' in cookies or 'session' in cookies):
        print('  [浏览器登录] cookie 里没有可用凭据，判定失败')
        return None
    return cookies


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 4:
        print(__doc__)
        print('用法: python browser_login.py <base_url> <username> <password> [sitekey]')
        sys.exit(1)
    ck = browser_login(sys.argv[1], sys.argv[2], sys.argv[3],
                       sys.argv[4] if len(sys.argv) > 4 else None)
    if ck:
        print('\n登录成功，cookies:')
        for k, v in ck.items():
            print('  %-24s %s...' % (k, v[:20]))
    else:
        print('\n登录失败')
        sys.exit(2)
