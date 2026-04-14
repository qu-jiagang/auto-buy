"""阿里云 Coding Plan 抢购脚本。

流程：
  1. 启动浏览器，用 state.json 恢复登录
  2. 提前打开购买页，预热 DOM
  3. 用 NTP 校准的时间倒计时到开抢时刻
  4. 疯狂点击「立即购买」→「提交订单」按钮，直到进入支付页或检测到成功
  5. 保留浏览器，让用户手动完成支付
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PwTimeout

try:
    import ntplib
except ImportError:
    ntplib = None

STATE_FILE = Path(__file__).parent / "state.json"
TARGET_URL = "https://common-buy.aliyun.com/coding-plan"

CST = timezone(timedelta(hours=8))
# 每日抢购时间（UTC+8）。想改成其它时刻直接调这里。
TARGET_HOUR = 9
TARGET_MINUTE = 30
TARGET_SECOND = 0
# 提前多少毫秒开始点击，抵消网络延迟（-200 表示提前 200ms）
LEAD_MS = 200
# 抢购窗口内每轮点击间隔（毫秒）
CLICK_INTERVAL_MS = 120
# 最大尝试时长（秒）
MAX_WINDOW_SEC = 60

# 选择器。DOM 改版时调这里即可。按优先级匹配第一个出现的。
BUY_BUTTON_SELECTORS = [
    "button:has-text('立即购买')",
    "button:has-text('立即抢购')",
    "button:has-text('立即开通')",
    "text=立即购买",
    "text=立即抢购",
    ".buy-btn",
    "[data-spm-click*='buy']",
]
CONFIRM_BUTTON_SELECTORS = [
    "button:has-text('确认订单')",
    "button:has-text('提交订单')",
    "button:has-text('去支付')",
    "text=提交订单",
]
AGREE_CHECKBOX_SELECTORS = [
    "input[type=checkbox]:near(:text('协议'))",
    ".agreement input[type=checkbox]",
]
SUCCESS_URL_HINTS = ["pay", "cashier", "order/result", "orderConfirm"]


def get_ntp_offset() -> float:
    """返回本地时钟比真实时间快多少秒（正数=本地快）。失败返回 0。"""
    if ntplib is None:
        print("[warn] 未装 ntplib，跳过时钟校准")
        return 0.0
    for server in ("ntp.aliyun.com", "ntp.tencent.com", "pool.ntp.org"):
        try:
            c = ntplib.NTPClient()
            r = c.request(server, version=3, timeout=3)
            offset = -r.offset
            print(f"[ntp] {server} 偏移 {offset*1000:+.0f}ms")
            return offset
        except Exception as e:
            print(f"[ntp] {server} 失败: {e}")
    return 0.0


def next_target_ts(offset: float) -> float:
    """返回下次目标时间对应的 time.time() 时间戳。"""
    now_real = datetime.now(CST) - timedelta(seconds=offset)
    target = now_real.replace(
        hour=TARGET_HOUR, minute=TARGET_MINUTE, second=TARGET_SECOND, microsecond=0
    )
    if target <= now_real:
        target += timedelta(days=1)
    # 转成本地 time.time() 可比较的戳
    return target.timestamp() + offset - (LEAD_MS / 1000.0)


async def click_first(page, selectors) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            if not await loc.is_visible():
                continue
            await loc.click(timeout=500, no_wait_after=True)
            print(f"[click] {sel}")
            return True
        except Exception:
            continue
    return False


async def try_check_agreements(page):
    for sel in AGREE_CHECKBOX_SELECTORS:
        try:
            for cb in await page.locator(sel).all():
                if await cb.is_visible() and not await cb.is_checked():
                    await cb.check(timeout=300)
        except Exception:
            pass


async def grab_loop(page, deadline: float):
    """在窗口期内反复尝试下单。"""
    while time.time() < deadline:
        url = page.url
        if any(h in url for h in SUCCESS_URL_HINTS):
            print(f"[ok] 疑似已进入支付页: {url}")
            return True

        await try_check_agreements(page)
        clicked = await click_first(page, BUY_BUTTON_SELECTORS)
        if not clicked:
            await click_first(page, CONFIRM_BUTTON_SELECTORS)

        await asyncio.sleep(CLICK_INTERVAL_MS / 1000.0)

        # 尝试刷新库存信息（失败也无所谓）
        try:
            if "sold" in (await page.content()).lower() or "售罄" in await page.content():
                print("[info] 页面显示售罄，继续尝试...")
                await page.reload(wait_until="domcontentloaded", timeout=3000)
        except Exception:
            pass
    return False


async def main():
    if not STATE_FILE.exists():
        raise SystemExit("未找到 state.json，先跑 python3 save_login.py")

    offset = get_ntp_offset()
    fire_at = next_target_ts(offset)
    wait = fire_at - time.time()
    fire_dt = datetime.fromtimestamp(fire_at).astimezone(CST)
    print(f"[plan] 开抢时刻 {fire_dt.isoformat()}（含 {LEAD_MS}ms 提前量）")
    print(f"[plan] 还有 {wait:.1f} 秒")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(storage_state=str(STATE_FILE))
        page = await ctx.new_page()
        print("[warm] 打开购买页预热...")
        await page.goto(TARGET_URL, wait_until="domcontentloaded")

        # 预先尝试勾选协议，减少开抢时的动作
        await try_check_agreements(page)

        # 倒计时
        while True:
            remain = fire_at - time.time()
            if remain <= 0:
                break
            if remain > 10:
                print(f"[wait] 剩余 {remain:.1f}s")
                await asyncio.sleep(min(remain - 5, 30))
            else:
                await asyncio.sleep(0.05)

        print(">>> 开抢！<<<")
        deadline = time.time() + MAX_WINDOW_SEC
        ok = await grab_loop(page, deadline)
        print("[done] 成功进入支付页" if ok else "[done] 超时未成功，请手动检查")

        print("浏览器保持打开，按 Enter 关闭...")
        await asyncio.get_event_loop().run_in_executor(None, input)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
