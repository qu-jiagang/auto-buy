"""手动登录阿里云一次，把登录状态保存到 state.json，后续抢购复用。"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

STATE_FILE = Path(__file__).parent / "state.json"
TARGET_URL = "https://common-buy.aliyun.com/coding-plan"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(TARGET_URL)
        print("=" * 60)
        print("请在浏览器里完成登录（扫码或账密），看到 Coding Plan 购买页后")
        print("回到这个终端按 Enter 保存登录状态。")
        print("=" * 60)
        await asyncio.get_event_loop().run_in_executor(None, input)
        await ctx.storage_state(path=str(STATE_FILE))
        print(f"已保存到 {STATE_FILE}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
