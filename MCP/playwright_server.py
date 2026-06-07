#!/usr/bin/env python3
# ================================================================
# Playwright MCP Server — AgentQA
# ----------------------------------------------------------------
# Управляет браузером через MCP. Позволяет сканировать  
# сайты, инвентаризировать UI-элементы, взаимодействовать с 
# UI-интерфейсом.
# ================================================================
import asyncio
import json
import os
import sys
import re
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright

page = None     # основная страница (для навигации, кликов, type)
browser = None  # экземпляр браузера
context = None  # общий browser context


# ----------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ----------------------------------------------------------------
async def new_tab():
    return await context.new_page()


async def smart_wait(pg, buffer_ms=400):
    try:
        await pg.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    if buffer_ms:
        await pg.wait_for_timeout(buffer_ms)


async def do_login(pg, args):
    login_url = args.get("loginUrl", "")
    if not login_url or not args.get("email"):
        return False
    email_sel = args.get("emailSelector", "input[id='email']")
    pwd_sel = args.get("passwordSelector", "input[id='password']")
    sub_sel = args.get("submitSelector", "button[type='submit']")
    await pg.goto(login_url, wait_until="load", timeout=30000)
    await pg.fill(email_sel, args["email"])
    await pg.fill(pwd_sel, args["password"])
    await pg.click(sub_sel)
    await smart_wait(pg, buffer_ms=1500)
    return True


def write_output(args, payload):
    out_path = args.get("outputPath")
    if not out_path:
        return payload
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        size = os.path.getsize(out_path)
        summary = {
            "outputPath": out_path,
            "bytes": size,
            "savedFull": True,
        }
        if isinstance(payload, dict):
            if "urls" in payload:
                summary["total"] = payload.get("total", len(payload.get("urls", [])))
                summary["urls"] = payload["urls"]
            if "snapshots" in payload:
                summary["pages"] = list(payload["snapshots"].keys())
                summary["pageCount"] = len(payload["snapshots"])
        return summary
    except Exception as e:
        payload["_outputPathError"] = str(e)
        return payload


_REF_RE = re.compile(r'\s*\[ref=e\d+\]')
_CUR_RE = re.compile(r'\s*\[cursor=[^\]]*\]')


def _norm_line(ln):
    return _CUR_RE.sub('', _REF_RE.sub('', ln)).rstrip()


def dedupe_shell(results, threshold=0.8):
    pages = [v for v in results.values() if isinstance(v.get("snapshot"), str)
             and not v["snapshot"].startswith("ERROR:")]
    n = len(pages)
    if n < 2:
        return []
    from collections import Counter
    counts = Counter()
    for v in pages:
        for nl in {_norm_line(l) for l in v["snapshot"].split("\n") if l.strip()}:
            counts[nl] += 1
    common = {nl for nl, c in counts.items() if c >= n * threshold and nl.strip()}
    if not common:
        return []
    for v in pages:
        out, skipping = [], False
        for l in v["snapshot"].split("\n"):
            if _norm_line(l) in common:
                if not skipping:
                    out.append("  # …общий каркас (см. common_shell)…")
                    skipping = True
            else:
                out.append(l)
                skipping = False
        v["snapshot"] = "\n".join(out)
    return sorted(common)

DEFAULT_SKIP_EXT = {
    "pdf", "zip", "rar", "7z", "gz", "tar", "tgz",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "rtf",
    "jpg", "jpeg", "png", "gif", "svg", "webp", "ico", "bmp", "tiff",
    "mp4", "webm", "avi", "mov", "mkv", "mp3", "wav", "ogg", "flac",
    "woff", "woff2", "ttf", "eot", "otf",
    "exe", "dmg", "apk", "msi", "bin", "iso",
}


def is_crawlable(parsed, skip_ext):
    if parsed.scheme not in ("http", "https"):
        return False
    last = parsed.path.rsplit("/", 1)[-1]
    if "." in last:
        ext = last.rsplit(".", 1)[-1].lower()
        if ext in skip_ext:
            return False
    return True


# ================================================================
# ОБРАБОТЧИК JSON-RPC
# ================================================================
async def handle_request(msg):
    global page, browser, context
    try:
        req = json.loads(msg)
    except json.JSONDecodeError as e:
        return respond_error(None, -32700, f"Parse error: {e}")
    rid = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {})

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
            {"name": "browser_navigate", "description": "Navigate to URL. Returns finalUrl/status/redirected/clientRedirect/title (waits for client-side SPA redirects to settle).", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}, "settleMs": {"type": "number", "default": 600}}, "required": ["url"]}},
            {"name": "browser_type", "description": "Type into element", "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}, "text": {"type": "string"}}, "required": ["target", "text"]}},
            {"name": "browser_click", "description": "Click element by text (matches title, aria-label, placeholder, text) or CSS selector", "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}, "text": {"type": "string"}}, "required": ["target"]}},

            # ---- чтение состояния страницы ----
            {"name": "browser_snapshot", "description": "Get page snapshot (accessible elements) — заменяет aria_snapshot из spider", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "browser_aria_snapshot", "description": "Get Playwright aria snapshot (mode=ai). Полное дерево доступности.", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "browser_find", "description": "Find elements by text matching title, aria-label, placeholder, or innerText", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
            {"name": "browser_evaluate", "description": "Run JS", "inputSchema": {"type": "object", "properties": {"function": {"type": "string"}}, "required": ["function"]}},

            # ---- краулинг ----
            {"name": "browser_crawl", "description": "[spider_mapper] Crawl site from current domain, collect all URLs (BFS over a[href] + sitemap.xml seed). Shares auth/session with the main tab. Skips static asset links (pdf/img/zip...) — set includeAssets=true to keep them. Optional login + route-pattern cap. Use outputPath to save full JSON to disk and get a compact summary.", "inputSchema": {"type": "object", "properties": {"maxPages": {"type": "number", "default": 50}, "perPatternCap": {"type": "number", "default": 3}, "includeAssets": {"type": "boolean", "default": False}, "skipExtensions": {"type": "array", "items": {"type": "string"}}, "outputPath": {"type": "string"}, "loginUrl": {"type": "string"}, "emailSelector": {"type": "string"}, "passwordSelector": {"type": "string"}, "submitSelector": {"type": "string"}, "email": {"type": "string"}, "password": {"type": "string"}}}},

            # ---- массовый snapshot  ----
            {"name": "browser_bulk_snapshot", "description": "[spider_aria] Navigate to each URL and take aria snapshots. Shares auth/session with the main tab (login optional). Use outputPath to save full JSON to disk and get a compact summary. dedupeShell=true factors out the repeated sidebar/header.", "inputSchema": {"type": "object", "properties": {"urls": {"type": "array", "items": {"type": "string"}}, "outputPath": {"type": "string"}, "dedupeShell": {"type": "boolean", "default": False}, "loginUrl": {"type": "string"}, "emailSelector": {"type": "string"}, "passwordSelector": {"type": "string"}, "submitSelector": {"type": "string"}, "email": {"type": "string"}, "password": {"type": "string"}}}},
        ]})

# ----------------------------------------------------------------
# вызов инструмента (tools/call)
# ----------------------------------------------------------------
    if method == "tools/call":
        tool = params.get("name", "")
        args = params.get("arguments", {})

        # ======== browser_navigate ========
        if tool == "browser_navigate":
            requested = args["url"]
            settle_ms = args.get("settleMs", 600)
            status = None
            try:
                resp = await page.goto(requested, wait_until="load", timeout=30000)
                status = resp.status if resp else None
            except Exception:
                try:
                    resp = await page.goto(requested, wait_until="domcontentloaded", timeout=15000)
                    status = resp.status if resp else None
                except Exception:
                    pass
            url_after_load = page.url
            if settle_ms:
                await smart_wait(page, buffer_ms=settle_ms)
            final_url = page.url
            try:
                title = await page.title()
            except Exception:
                title = ""
            return respond(rid, {"content": [{"type": "text", "text": json.dumps({
                "requestedUrl": requested,
                "finalUrl": final_url,
                "redirected": final_url.rstrip("/") != requested.rstrip("/"),
                "clientRedirect": final_url.rstrip("/") != url_after_load.rstrip("/"),
                "status": status,
                "title": title,
            }, ensure_ascii=False)}]})

        # ======== browser_snapshot ========
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
            filled = False
            if target:
                for getter in ("placeholder", "label", "css"):
                    try:
                        if getter == "placeholder":
                            loc = page.get_by_placeholder(target).first
                            await loc.fill(text, timeout=2000)
                        elif getter == "label":
                            loc = page.get_by_label(target).first
                            await loc.fill(text, timeout=2000)
                        else:
                            await page.fill(target, text, timeout=2000)
                        filled = True
                        break
                    except Exception:
                        continue
                if not filled:
                    try:
                        ok = await page.evaluate(
                            """({sel, val}) => {
                                const el = document.querySelector(sel);
                                if (!el) return false;
                                const proto = Object.getPrototypeOf(el);
                                const d = Object.getOwnPropertyDescriptor(proto, 'value');
                                (d && d.set ? d.set : (v => el.value = v)).call(el, val);
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                                return el.value === val;
                            }""",
                            {"sel": target, "val": text},
                        )
                        filled = bool(ok)
                    except Exception:
                        pass
            else:
                try:
                    await page.keyboard.type(text)
                    filled = True
                except Exception:
                    pass
            return respond(rid, {"content": [{"type": "text", "text": json.dumps({"typed": filled})}]})

        # ======== browser_crawl ========
        elif tool == "browser_crawl":
            max_pages = args.get("maxPages", 50)
            per_pattern_cap = args.get("perPatternCap", 3)
            if args.get("includeAssets"):
                skip_ext = set()
            elif args.get("skipExtensions") is not None:
                skip_ext = {e.lstrip(".").lower() for e in args["skipExtensions"]}
            else:
                skip_ext = DEFAULT_SKIP_EXT
            base_url = page.url
            parsed_base = urlparse(base_url)
            domain = f"{parsed_base.scheme}://{parsed_base.netloc}"
            uuid_pat = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
            num_pat = re.compile(r'/\d+(?=/|$)')

            def route_pattern(u):
                p = urlparse(u)
                path = uuid_pat.sub('{id}', p.path)
                path = num_pat.sub('/{n}', path)
                return path or '/'

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

            visit_page = await new_tab()
            try:
                await do_login(visit_page, args)
            except Exception:
                pass

            visited = set()
            pattern_count = {}
            to_visit = [domain]

            try:
                sm = await new_tab()
                try:
                    r = await sm.goto(urljoin(domain, "/sitemap.xml"), wait_until="domcontentloaded", timeout=8000)
                    if r and r.ok:
                        body = await sm.content()
                        for m in re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', body):
                            pp = urlparse(m)
                            if pp.netloc == parsed_base.netloc and is_crawlable(pp, skip_ext):
                                cleaned = f"{pp.scheme}://{pp.netloc}{pp.path.rstrip('/')}"
                                if cleaned not in to_visit:
                                    to_visit.append(cleaned)
                finally:
                    await sm.close()
            except Exception:
                pass

            try:
                while to_visit and len(visited) < max_pages:
                    url = to_visit.pop(0)
                    if url in visited:
                        continue
                    pat = route_pattern(url)
                    if pattern_count.get(pat, 0) >= per_pattern_cap:
                        visited.add(url)
                        continue
                    try:
                        await visit_page.goto(url, wait_until="load", timeout=30000)
                        await smart_wait(visit_page, buffer_ms=300)
                        visited.add(url)
                        pattern_count[pat] = pattern_count.get(pat, 0) + 1
                        links = await visit_page.eval_on_selector_all(
                            "a[href]",
                            "els => els.map(el => el.href)"
                        )
                        for link in links:
                            full = urljoin(domain, link)
                            p = urlparse(full)
                            if p.netloc == parsed_base.netloc and is_crawlable(p, skip_ext):
                                cleaned = f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}" if p.path else full
                                if p.query:
                                    cleaned += f"?{p.query}"
                                if cleaned not in visited and cleaned not in to_visit:
                                    to_visit.append(cleaned)
                        to_visit.sort(key=prioritize)
                    except Exception:
                        visited.add(url)
            finally:
                await visit_page.close()

            payload = {
                "domain": domain,
                "total": len(visited),
                "urls": sorted(visited, key=prioritize),
                "routePatterns": sorted(pattern_count.keys()),
            }
            return respond(rid, {"content": [{"type": "text", "text": json.dumps(
                write_output(args, payload), ensure_ascii=False, indent=2)}]})
        elif tool == "browser_aria_snapshot":
            snap = await page.aria_snapshot(mode="ai")
            return respond(rid, {"content": [{"type": "text", "text": snap}]})
        elif tool == "browser_bulk_snapshot":
            urls = args.get("urls", [])
            if not urls:
                return respond(rid, {"content": [{"type": "text", "text": json.dumps({"error": "no urls provided"})}]})

            snap_page = await new_tab()
            try:
                try:
                    await do_login(snap_page, args)
                except Exception:
                    pass

                results = {}
                for url in urls:
                    try:
                        await snap_page.goto(url, wait_until="load", timeout=30000)
                        await smart_wait(snap_page, buffer_ms=400)
                        title = await snap_page.title()
                        snap = await snap_page.aria_snapshot(mode="ai")
                        results[url] = {"title": title, "snapshot": snap}
                    except Exception as e:
                        results[url] = {"title": "", "snapshot": f"ERROR: {e}"}

                payload = {"snapshots": results}

                if args.get("dedupeShell"):
                    payload["common_shell"] = dedupe_shell(results)

                return respond(rid, {"content": [{"type": "text", "text": json.dumps(
                    write_output(args, payload), ensure_ascii=False, indent=2)}]})
            finally:
                await snap_page.close()
        elif tool == "browser_evaluate":
            try:
                result = await page.evaluate(args["function"])
                text = json.dumps(result, ensure_ascii=False)
            except TypeError:
                text = json.dumps(str(result), ensure_ascii=False)
            except Exception as e:
                text = json.dumps({"error": str(e)}, ensure_ascii=False)
            return respond(rid, {"content": [{"type": "text", "text": text}]})

    return respond_error(rid, -32601, f"Method {method} not found")

def respond(rid, body):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": body}) + "\n"

def respond_error(rid, code, message):
    if rid is None:
        return json.dumps({"jsonrpc": "2.0", "error": {"code": code, "message": message}}) + "\n"
    return json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}) + "\n"

async def main():
    global page, browser, context
    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context(viewport={"width": 1920, "height": 1080})
    page = await context.new_page()

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
