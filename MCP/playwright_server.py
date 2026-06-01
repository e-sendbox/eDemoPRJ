#!/usr/bin/env python3
# ================================================================
# Playwright MCP Server — ETA AgentQA
# ----------------------------------------------------------------
# Управляет браузером через MCP. Позволяет сканировать неизвестные 
# сайты, инвентаризировать UI-элементы и далее взаимодействовать с 
# UI-интерфейсом.
# ================================================================
import asyncio
import json
import sys
import re
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright

page = None     # основная страница (для навигации, кликов, type)
browser = None  # экземпляр браузера (для создания доп. вкладок)

# ================================================================
# ОБРАБОТЧИК JSON-RPC
# ================================================================
async def handle_request(msg):
    global page, browser
    try:
        req = json.loads(msg)
    except json.JSONDecodeError as e:
        return respond_error(None, -32700, f"Parse error: {e}")
    rid = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {})

    # Notifications (no id) — no response
    if rid is None:
        return ""

# ----------------------------------------------------------------
# initialization
# ----------------------------------------------------------------
    if method == "initialize":
        return respond(rid, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "playwright-mcp", "version": "1.0.0"}
        })

# ----------------------------------------------------------------
# список доступных инструментов (tools/list)
# ----------------------------------------------------------------
    if method == "tools/list":
        return respond(rid, {"tools": [
            # ---- базовая навигация и ввод ----
            {"name": "browser_navigate", "description": "Navigate to URL", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
            {"name": "browser_type", "description": "Type into element", "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}, "text": {"type": "string"}}, "required": ["target", "text"]}},
            {"name": "browser_click", "description": "Click element by text (matches title, aria-label, placeholder, text) or CSS selector", "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}, "text": {"type": "string"}}, "required": ["target"]}},

            # ---- чтение состояния страницы ----
            {"name": "browser_snapshot", "description": "Get page snapshot (accessible elements) — заменяет aria_snapshot из spider", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "browser_aria_snapshot", "description": "Get Playwright aria snapshot (mode=ai). Полное дерево доступности.", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "browser_find", "description": "Find elements by text matching title, aria-label, placeholder, or innerText", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
            {"name": "browser_evaluate", "description": "Run JS", "inputSchema": {"type": "object", "properties": {"function": {"type": "string"}}, "required": ["function"]}},

            # ---- краулинг (замена spider_mapper.py) ----
            {"name": "browser_crawl", "description": "[spider_mapper] Crawl site from current domain, collect all URLs. Returns site map JSON. Uses a separate tab.", "inputSchema": {"type": "object", "properties": {"maxPages": {"type": "number", "default": 50}}}},

            # ---- массовый snapshot (замена spider_aria.py) ----
            {"name": "browser_bulk_snapshot", "description": "[spider_aria] Navigate to each URL and take aria snapshots. Uses a separate tab. Current page state preserved.", "inputSchema": {"type": "object", "properties": {"urls": {"type": "array", "items": {"type": "string"}}, "loginUrl": {"type": "string"}, "emailSelector": {"type": "string"}, "passwordSelector": {"type": "string"}, "submitSelector": {"type": "string"}, "email": {"type": "string"}, "password": {"type": "string"}}}},
        ]})

# ----------------------------------------------------------------
# вызов инструмента (tools/call)
# ----------------------------------------------------------------
    if method == "tools/call":
        tool = params.get("name", "")
        args = params.get("arguments", {})

        # ======== browser_navigate ========
        if tool == "browser_navigate":
            try:
                await page.goto(args["url"], wait_until="load", timeout=30000)
            except Exception:
                try:
                    await page.goto(args["url"], wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
            return respond(rid, {"content": [{"type": "text", "text": f"Navigated to {args['url']}"}]})

        # ======== browser_snapshot ========
        # Собирает все видимые интерактивные элементы страницы.
        # Включает text (innerText), title, aria-label, placeholder.
        # Отдаёт структурированный JSON.
        # Удобен для быстрого анализа агентом.
        elif tool == "browser_snapshot":
            html = await page.evaluate("""() => {
                const items = [];
                const sel = 'button, a[href], input:not([type=hidden]), select, textarea, [role=button], [role=menuitem], [role=tab], [role=link], h1, h2, h3, h4';
                document.querySelectorAll(sel).forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return;
                    const text = (el.innerText || el.textContent || '').trim().substring(0, 80);
                    const tag = el.tagName.toLowerCase();
                    const role = el.getAttribute('role') || '';
                    const href = el.href || '';
                    const type = el.type || '';
                    const title = (el.getAttribute('title') || '').substring(0, 80);
                    const aria = (el.getAttribute('aria-label') || '').substring(0, 80);
                    const placeholder = (el.getAttribute('placeholder') || '').substring(0, 80);
                    items.push({tag, role, text, title, aria, placeholder, href: href.substring(0,150), type, classes: (el.className||'').substring(0,60)});
                });
                return items;
            }""")
            return respond(rid, {"content": [{"type": "text", "text": json.dumps(html, ensure_ascii=False, indent=2)}]})

        # ======== browser_find ========
        # Поиск элементов по тексту. Проверяет innerText, title,
        # aria-label и placeholder. Возвращает все совпадения
        # с указанием где именно найден текст (matches).
        elif tool == "browser_find":
            text = args.get("text", "")
            if not text:
                return respond(rid, {"content": [{"type": "text", "text": "[]"}]})
            found = await page.evaluate(f"""() => {{
                const q = {json.dumps(text.lower())};
                const sel = 'button, a[href], input:not([type=hidden]), select, textarea, [role=button], [role=menuitem], [role=tab], [role=link], h1, h2, h3, h4';
                const results = [];
                document.querySelectorAll(sel).forEach(el => {{
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return;
                    const inner = (el.innerText || el.textContent || '').trim();
                    const title = (el.title || '').trim();
                    const aria = (el.getAttribute('aria-label') || '').trim();
                    const placeholder = (el.getAttribute('placeholder') || '').trim();
                    const matches = [inner, title, aria, placeholder].filter(v => v.toLowerCase().includes(q));
                    if (matches.length > 0) {{
                        results.push({{
                            tag: el.tagName.toLowerCase(),
                            text: inner.substring(0, 80),
                            title: title.substring(0, 80),
                            aria: aria.substring(0, 80),
                            placeholder: placeholder.substring(0, 80),
                            matches: matches[0].substring(0, 80),
                            selector: (el.tagName.toLowerCase() + (el.id ? '#'+el.id : '') + (el.getAttribute('data-index') ? '[data-index="'+el.getAttribute('data-index')+'"]' : '')).substring(0, 100)
                        }});
                    }}
                }});
                return results;
            }}""")
            return respond(rid, {"content": [{"type": "text", "text": json.dumps(found, ensure_ascii=False, indent=2)}]})

        # ======== browser_click ========
        # Умный клик: пробует 4 стратегии по цепочке, чтобы
        # находить кнопки независимо от того, где у них текст:
        #  1) get_by_role("button", name=text)  — стандартные кнопки
        #  2) get_by_title(text)                — иконки с title="..."
        #  3) get_by_label(text)                — aria-label
        #  4) get_by_placeholder(text)          — поля с placeholder
        elif tool == "browser_click":
            target = args.get("target", "")
            text = args.get("text", "")
            if text:
                try:
                    await page.get_by_role("button", name=text).first.click(timeout=2000)
                except:
                    try:
                        await page.get_by_title(text).first.click(timeout=2000)
                    except:
                        try:
                            await page.get_by_label(text).first.click(timeout=2000)
                        except:
                            await page.get_by_placeholder(text).first.click(timeout=2000)
            elif target:
                await page.click(target)
            await page.wait_for_timeout(500)
            return respond(rid, {"content": [{"type": "text", "text": "Clicked"}]})

        # ======== browser_type ========
        elif tool == "browser_type":
            target = args.get("target", "")
            text = args.get("text", "")
            if target:
                try:
                    locator = page.get_by_placeholder(target).first
                    locator.fill(text, timeout=2000)
                except Exception:
                    try:
                        locator = page.get_by_label(target).first
                        locator.fill(text, timeout=2000)
                    except Exception:
                        try:
                            await page.fill(target, text)
                        except Exception:
                            try:
                                await page.keyboard.type(text)
                            except:
                                pass
            else:
                try:
                    await page.keyboard.type(text)
                except:
                    pass
            return respond(rid, {"content": [{"type": "text", "text": "Typed"}]})

        # ======== browser_crawl ========
        # Краулит сайт начиная с текущего домена. Собирает все
        # внутренние ссылки через a[href], обходит BFS, сортирует:
        #   - статические страницы (/, /catalog, /orders) — выше
        #   - UUID-страницы (/catalog/{uuid}) — ниже
        #   - страницы с query-параметрами — ещё ниже
        # Использует отдельную вкладку, не трогает основную страницу.
        elif tool == "browser_crawl":
            max_pages = args.get("maxPages", 50)
            base_url = page.url
            parsed_base = urlparse(base_url)
            domain = f"{parsed_base.scheme}://{parsed_base.netloc}"
            uuid_pat = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')

            def prioritize(url):
                p = urlparse(url)
                score = 0
                if uuid_pat.search(p.path):
                    score += 100
                if p.query:
                    score += 10
                if p.path in ('/', ''):
                    score = -10
                return score

            visit_page = await browser.new_page()
            visited = set()
            to_visit = [domain]

            try:
                while to_visit and len(visited) < max_pages:
                    url = to_visit.pop(0)
                    if url in visited:
                        continue
                    try:
                        await visit_page.goto(url, wait_until="load", timeout=30000)
                        await visit_page.wait_for_timeout(1500)
                        visited.add(url)
                        links = await visit_page.eval_on_selector_all(
                            "a[href]",
                            "els => els.map(el => el.href)"
                        )
                        for link in links:
                            full = urljoin(domain, link)
                            p = urlparse(full)
                            if p.netloc == parsed_base.netloc:
                                cleaned = f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}" if p.path else full
                                if cleaned not in visited and cleaned not in to_visit:
                                    to_visit.append(cleaned)
                        to_visit.sort(key=prioritize)
                    except Exception as e:
                        visited.add(url)
            finally:
                await visit_page.close()

            return respond(rid, {"content": [{"type": "text", "text": json.dumps({
                "domain": domain,
                "total": len(visited),
                "urls": sorted(visited, key=prioritize)
            }, ensure_ascii=False, indent=2)}]})
        elif tool == "browser_aria_snapshot":
            snap = await page.aria_snapshot(mode="ai")
            return respond(rid, {"content": [{"type": "text", "text": snap}]})
        elif tool == "browser_bulk_snapshot":
            urls = args.get("urls", [])
            if not urls:
                return respond(rid, {"content": [{"type": "text", "text": json.dumps({"error": "no urls provided"})}]})

            snap_page = await browser.new_page()
            try:
                login_url = args.get("loginUrl", "")
                if login_url:
                    await snap_page.goto(login_url, wait_until="load", timeout=30000)
                    if args.get("email"):
                        email_sel = args.get("emailSelector", "input[id='email']")
                        pwd_sel = args.get("passwordSelector", "input[id='password']")
                        sub_sel = args.get("submitSelector", "button[type='submit']")
                        await snap_page.fill(email_sel, args["email"])
                        await snap_page.fill(pwd_sel, args["password"])
                        await snap_page.click(sub_sel)
                        await snap_page.wait_for_timeout(3000)
                        await snap_page.wait_for_load_state("load")

                results = {}
                for i, url in enumerate(urls, 1):
                    try:
                        await snap_page.goto(url, wait_until="load", timeout=30000)
                        await snap_page.wait_for_timeout(2000)
                        title = await snap_page.title()
                        snap = await snap_page.aria_snapshot(mode="ai")
                        results[url] = {"title": title, "snapshot": snap}
                    except Exception as e:
                        results[url] = {"title": "", "snapshot": f"ERROR: {e}"}

                return respond(rid, {"content": [{"type": "text", "text": json.dumps(results, ensure_ascii=False, indent=2)}]})
            finally:
                await snap_page.close()
        elif tool == "browser_evaluate":
            try:
                result = await page.evaluate(args["function"])
            except Exception as e:
                result = f"Error: {e}"
            return respond(rid, {"content": [{"type": "text", "text": str(result)}]})

    return respond_error(rid, -32601, f"Method {method} not found")

def respond(rid, body):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": body}) + "\n"

def respond_error(rid, code, message):
    if rid is None:
        return json.dumps({"jsonrpc": "2.0", "error": {"code": code, "message": message}}) + "\n"
    return json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}) + "\n"

async def main():
    global page, browser
    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=True)
    page = await browser.new_page(viewport={"width": 1920, "height": 1080})

    # MCP stdio transport: read line, write line
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        try:
            line = await reader.readline()
            if not line:
                break
            line = line.decode().strip()
            if line:
                try:
                    resp = await handle_request(line)
                except Exception as e:
                    resp = respond_error(None, -32603, f"Internal error: {e}")
                sys.stdout.write(resp)
                sys.stdout.flush()
        except Exception:
            break

    await browser.close()
    await p.stop()

if __name__ == "__main__":
    asyncio.run(main())
