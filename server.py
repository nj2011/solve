import asyncio
import os
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

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/solve/recaptcha", response_model=TokenResp)
async def solve_recaptcha(req: SolveReq):
    async with async_playwright() as p:
        launch_args = {"headless": True}
        if req.proxy:
            launch_args["proxy"] = {"server": req.proxy}
        browser = await p.chromium.launch(**launch_args)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Mobile Safari/537.36",
            viewport={"width": 412, "height": 915},
        )
        page = await ctx.new_page()
        try:
            await page.goto(req.pageurl, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            token = None
            try:
                async with recaptchav2.AsyncSolver(page, capsolver_api_key=req.capsolver_key) as solver:
                    token = await solver.solve_recaptcha(wait=True, image_challenge=bool(req.capsolver_key))
            except Exception:
                pass

            if not token:
                token = await page.evaluate(
                    "() => { const el = document.querySelector('[name=g-recaptcha-response]'); return el ? el.value : ''; }"
                )

            if not token:
                raise HTTPException(500, "no token captured")

            ua = await page.evaluate("() => navigator.userAgent")
            return TokenResp(token=token, user_agent=ua)
        finally:
            await browser.close()