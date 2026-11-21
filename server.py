import asyncio
import os
import traceback
import sys
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright
from playwright_recaptcha import recaptchav2

app = FastAPI()

class SolveReq(BaseModel):
    sitekey: str
    pageurl: str
    proxy: str | None = None
    capsolver_key: str | None = None

class TokenResp(BaseModel):
    token: str
    user_agent: str


def log(msg):
    print(f"[server] {msg}", flush=True)
    sys.stdout.flush()


@app.get("/health")
def health():
    log("health check")
    return {"ok": True}


@app.get("/debug")
async def debug():
    log("debug: launching chromium")
    try:
        async with async_playwright() as p:
            log(f"debug: playwright version ok")
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            version = browser.version
            log(f"debug: chromium {version}")
            page = await browser.new_page()
            await page.goto("https://example.com", timeout=30000)
            title = await page.title()
            log(f"debug: example.com title = {title}")
            await browser.close()
        return {"playwright": "ok", "chromium": version, "title": title}
    except Exception as e:
        tb = traceback.format_exc()
        log(f"debug fail: {e}")
        log(tb)
        return {"playwright": "fail", "error": str(e)[:500], "tb": tb[-2000:]}


@app.post("/solve/recaptcha", response_model=TokenResp)
async def solve_recaptcha(req: SolveReq):
    log("=" * 60)
    log(f"request: sitekey={req.sitekey[:20]}... pageurl={req.pageurl}")
    log(f"request: proxy={'yes' if req.proxy else 'no'} capsolver={'yes' if req.capsolver_key else 'no'}")

    try:
        async with async_playwright() as p:
            log("launching chromium")
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            log(f"chromium launched: {browser.version}")

            ua = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Mobile Safari/537.36"
            ctx = await browser.new_context(
                user_agent=ua,
                viewport={"width": 412, "height": 915},
                locale="en-US",
            )
            log("context created")

            page = await ctx.new_page()
            log(f"navigating to {req.pageurl}")

            try:
                resp = await page.goto(req.pageurl, wait_until="domcontentloaded", timeout=60000)
                log(f"page loaded: status={resp.status if resp else 'none'} url={page.url}")
            except Exception as e:
                log(f"goto failed: {e}")
                log(traceback.format_exc()[-1500:])
                await browser.close()
                raise HTTPException(500, f"goto failed: {str(e)[:200]}")

            title = await page.title()
            log(f"page title: {title}")

            await page.wait_for_timeout(3000)
            log("waited 3s post-load")

            try:
                html_len = await page.evaluate("() => document.documentElement.outerHTML.length")
                log(f"page html length: {html_len}")
            except Exception as e:
                log(f"html length check failed: {e}")

            try:
                recaptcha_info = await page.evaluate("""() => {
                    const scripts = Array.from(document.querySelectorAll('script[src*="recaptcha"]')).map(s => s.src);
                    const iframes = Array.from(document.querySelectorAll('iframe[src*="recaptcha"]')).map(f => f.src);
                    const grecaptchaExists = typeof window.grecaptcha !== 'undefined';
                    const respField = document.querySelector('[name=g-recaptcha-response]');
                    return {
                        scripts: scripts,
                        iframes: iframes,
                        grecaptcha: grecaptchaExists,
                        respFieldExists: !!respField,
                        respFieldValue: respField ? (respField.value || '').slice(0, 40) : null,
                    };
                }""")
                log(f"recaptcha scan: {recaptcha_info}")
            except Exception as e:
                log(f"recaptcha scan failed: {e}")

            token = None

            log("attempt 1: direct scrape of g-recaptcha-response field")
            try:
                await page.wait_for_function(
                    "() => { const el = document.querySelector('[name=g-recaptcha-response]'); return el && el.value && el.value.length > 20; }",
                    timeout=15000,
                )
                token = await page.evaluate(
                    "() => document.querySelector('[name=g-recaptcha-response]').value"
                )
                log(f"attempt 1 SUCCESS: token len={len(token)} prefix={token[:40]}")
            except Exception as e:
                log(f"attempt 1 failed: {e}")

            if not token:
                log("attempt 2: trigger grecaptcha.execute then scrape")
                try:
                    triggered = await page.evaluate("""() => {
                        if (typeof window.grecaptcha === 'undefined') return false;
                        try {
                            if (window.grecaptcha.execute) {
                                window.grecaptcha.execute();
                                return true;
                            }
                        } catch(e) { return false; }
                        return false;
                    }""")
                    log(f"grecaptcha.execute triggered: {triggered}")
                    await page.wait_for_timeout(3000)
                    await page.wait_for_function(
                        "() => { const el = document.querySelector('[name=g-recaptcha-response]'); return el && el.value && el.value.length > 20; }",
                        timeout=20000,
                    )
                    token = await page.evaluate(
                        "() => document.querySelector('[name=g-recaptcha-response]').value"
                    )
                    log(f"attempt 2 SUCCESS: token len={len(token)} prefix={token[:40]}")
                except Exception as e:
                    log(f"attempt 2 failed: {e}")

            if not token:
                log("attempt 3: playwright-recaptcha AsyncSolver")
                try:
                    async with recaptchav2.AsyncSolver(page, capsolver_api_key=req.capsolver_key) as solver:
                        log("solver context entered, calling solve_recaptcha")
                        token = await solver.solve_recaptcha(
                            wait=True,
                            image_challenge=bool(req.capsolver_key),
                        )
                        log(f"attempt 3 SUCCESS: token len={len(token) if token else 0} prefix={token[:40] if token else ''}")
                except Exception as e:
                    log(f"attempt 3 failed: {e}")
                    log(traceback.format_exc()[-1000:])

            if not token:
                log("attempt 4: click submit button then scrape")
                try:
                    clicked = await page.evaluate("""() => {
                        const btns = Array.from(document.querySelectorAll('button[type=submit], input[type=submit]'));
                        if (btns.length > 0) { btns[0].click(); return true; }
                        return false;
                    }""")
                    log(f"submit button clicked: {clicked}")
                    await page.wait_for_timeout(4000)
                    token = await page.evaluate(
                        "() => { const el = document.querySelector('[name=g-recaptcha-response]'); return el ? el.value : ''; }"
                    )
                    if token:
                        log(f"attempt 4 SUCCESS: token len={len(token)} prefix={token[:40]}")
                    else:
                        log("attempt 4: token still empty")
                except Exception as e:
                    log(f"attempt 4 failed: {e}")

            if not token:
                log("ALL ATTEMPTS FAILED - no token acquired")
                try:
                    final_html = await page.content()
                    log(f"final page length: {len(final_html)}")
                    log(f"final page snippet: {final_html[:500]}")
                except Exception:
                    pass
                await browser.close()
                raise HTTPException(500, "no token captured after all attempts")

            final_ua = await page.evaluate("() => navigator.userAgent")
            log(f"returning token len={len(token)} ua={final_ua[:50]}")
            await browser.close()
            log("browser closed, returning response")
            return TokenResp(token=token, user_agent=final_ua)

    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        log(f"endpoint UNHANDLED: {e}")
        log(tb)
        raise HTTPException(500, f"solve failed: {str(e)[:200]}")