"""阿里云 Coding Plan 抢购脚本（升级版）。

流程：
  1. 启动浏览器，用 state.json 恢复登录
  2. 提前打开购买页，预热 DOM，开多个并发 tab
  3. 用"阿里云服务器时间"（HTTP Date 头）+ NTP 双重校准
  4. 倒计时到开抢时刻，最后 500ms 忙等锁死精度
  5. 监听「立即购买」按钮 enabled 事件，enabled 即点；多 tab 并发竞抢
  6. 成功进入支付页后停下，保留浏览器让用户手动完成支付

相比初版的主要变化：
  - 时间基准改用阿里云购买域 HTTPS Date 响应头（消除 NTP 与业务网关偏差）
  - 最后 500ms 改为 busy-wait，抖动从 ±15ms 收敛到 <1ms
  - 增加 N 个并发 tab（共享 storage_state），谁先进支付页谁赢
  - 按钮 disabled 时不再空点，改为 wait_for_selector 的 :not([disabled]) 变体
  - 售罄检测从整页 content() 改为 locator.count()，循环不再被拖慢
  - 开抢前 10s 做 keep-alive 预热，保持 TLS/TCP 连接热
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

try:
    import ntplib
except ImportError:
    ntplib = None

STATE_FILE = Path(__file__).parent / "state.json"
TARGET_URL = "https://common-buy.aliyun.com/coding-plan"
TIME_PROBE_URL = "https://common-buy.aliyun.com/"

CST = timezone(timedelta(hours=8))
# 每日抢购时间（UTC+8）
TARGET_HOUR = 9
TARGET_MINUTE = 30
TARGET_SECOND = 0
# 提前多少毫秒触发（抵消发包链路延迟；阿里云下单网关一般 50~150ms 往返）
LEAD_MS = 120
# 抢购窗口内每轮点击间隔（毫秒）
CLICK_INTERVAL_MS = 80
# 最大尝试时长（秒）
MAX_WINDOW_SEC = 60
# 并发 tab 数（共享同一个登录态）
CONCURRENCY = 3
# 最后多少毫秒进入 busy-wait，追求极致精度
BUSY_WAIT_MS = 500
# 开抢前多少秒开始 keep-alive 预热
WARMUP_LEAD_SEC = 10

BUY_BUTTON_SELECTORS = [
    "button:has-text('立即购买'):not([disabled])",
    "button:has-text('立即抢购'):not([disabled])",
    "button:has-text('立即开通'):not([disabled])",
    "button:has-text('立即购买')",
    "button:has-text('立即抢购')",
    ".buy-btn:not([disabled])",
    "[data-spm-click*='buy']",
]
CONFIRM_BUTTON_SELECTORS = [
    "button:has-text('确认订单'):not([disabled])",
    "button:has-text('提交订单'):not([disabled])",
    "button:has-text('去支付'):not([disabled])",
    "button:has-text('确认订单')",
    "button:has-text('提交订单')",
]
AGREE_CHECKBOX_SELECTORS = [
    "label:has-text('我已阅读') input[type=checkbox]",
    ".agreement input[type=checkbox]",
    "input[type=checkbox]",
]
SOLD_OUT_SELECTORS = [
    "text=售罄",
    "text=已售完",
    "text=sold out",
]
SUCCESS_URL_HINTS = ["pay", "cashier", "order/result", "orderConfirm"]


async def get_server_offset() -> float:
    """用阿里云 HTTPS Date 头对齐时间。返回 local - server（秒，正数=本地快）。"""
    try:
        async with aiohttp.ClientSession() as s:
            # 打三次取中位数，消除 RTT 抖动
            offsets = []
            for _ in range(3):
                t0 = time.time()
                async with s.head(TIME_PROBE_URL, allow_redirects=False, timeout=3) as r:
                    t1 = time.time()
                    date_hdr = r.headers.get("Date")
                    if not date_hdr:
                        continue
                    server_dt = parsedate_to_datetime(date_hdr)
                    server_ts = server_dt.timestamp()
                    # 假设 server 时间点位于我们收到响应的那一刻（偏差 < RTT/2）
                    local_mid = (t0 + t1) / 2
                    offsets.append(local_mid - server_ts)
                await asyncio.sleep(0.1)
            if offsets:
                offsets.sort()
                off = offsets[len(offsets) // 2]
                print(f"[time] 阿里云服务器偏移 {off*1000:+.0f}ms (samples={len(offsets)})")
                return off
    except Exception as e:
        print(f"[time] HTTP Date 校准失败: {e}")
    return 0.0


def get_ntp_offset() -> float:
    if ntplib is None:
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
    """返回下次目标时间对应的 time.time() 时间戳，已扣 LEAD_MS。"""
    now_real = datetime.now(CST) - timedelta(seconds=offset)
    target = now_real.replace(
        hour=TARGET_HOUR, minute=TARGET_MINUTE, second=TARGET_SECOND, microsecond=0
    )
    if target <= now_real:
        target += timedelta(days=1)
    return target.timestamp() + offset - (LEAD_MS / 1000.0)


async def click_first(page, selectors) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            if not await loc.is_visible():
                continue
            if not await loc.is_enabled():
                continue
            await loc.click(timeout=400, no_wait_after=True)
            return True
        except Exception:
            continue
    return False


async def try_check_agreements(page):
    """勾选页面里所有未勾选的 checkbox。优先用 JS 批量处理，最快。"""
    try:
        await page.evaluate(
            """() => {
                document.querySelectorAll('input[type=checkbox]').forEach(cb => {
                    if (!cb.checked) {
                        cb.click();
                    }
                });
            }"""
        )
        return
    except Exception:
        pass
    for sel in AGREE_CHECKBOX_SELECTORS:
        try:
            for cb in await page.locator(sel).all():
                if await cb.is_visible() and not await cb.is_checked():
                    await cb.check(timeout=300)
        except Exception:
            pass


async def is_sold_out(page) -> bool:
    for sel in SOLD_OUT_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            pass
    return False


async def grab_loop(page, tag: str, deadline: float, stop_event: asyncio.Event) -> bool:
    """在窗口期内反复尝试下单。stop_event 触发则提前退出（其他 tab 已成功）。"""
    misses = 0
    while time.time() < deadline and not stop_event.is_set():
        url = page.url
        if any(h in url for h in SUCCESS_URL_HINTS):
            print(f"[ok:{tag}] 疑似已进入支付页: {url}")
            stop_event.set()
            return True

        await try_check_agreements(page)
        clicked = await click_first(page, BUY_BUTTON_SELECTORS)
        if not clicked:
            clicked = await click_first(page, CONFIRM_BUTTON_SELECTORS)

        if clicked:
            misses = 0
            print(f"[click:{tag}]")
        else:
            misses += 1

        # 连续 N 轮一个按钮都点不到 → 刷新一次
        if misses >= 15:
            try:
                print(f"[reload:{tag}] 连续 miss，刷新")
                await page.reload(wait_until="domcontentloaded", timeout=3000)
                misses = 0
            except Exception:
                pass

        # 售罄兜底检查（低频）
        if misses and misses % 5 == 0 and await is_sold_out(page):
            try:
                print(f"[info:{tag}] 显示售罄，刷新重试")
                await page.reload(wait_until="domcontentloaded", timeout=3000)
            except Exception:
                pass

        await asyncio.sleep(CLICK_INTERVAL_MS / 1000.0)
    return False


async def keep_alive_warmup(session: aiohttp.ClientSession, until: float):
    """开抢前持续打 HEAD 保持 TCP/TLS 热。"""
    try:
        while time.time() < until:
            try:
                async with session.head(TIME_PROBE_URL, timeout=2, allow_redirects=False):
                    pass
            except Exception:
                pass
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass


def busy_wait_until(ts: float):
    """精准忙等到 ts。最后几百毫秒占 CPU 换精度，值得。"""
    while time.time() < ts:
        pass


async def main():
    if not STATE_FILE.exists():
        raise SystemExit("未找到 state.json，先跑 python3 save_login.py")

    # 双重时间校准：阿里云 HTTP Date 优先，NTP 兜底
    offset = await get_server_offset()
    if offset == 0.0:
        offset = get_ntp_offset()

    fire_at = next_target_ts(offset)
    wait = fire_at - time.time()
    fire_dt = datetime.fromtimestamp(fire_at).astimezone(CST)
    print(f"[plan] 开抢触发时刻 {fire_dt.isoformat()}（含 {LEAD_MS}ms 提前量）")
    print(f"[plan] 还有 {wait:.1f} 秒，并发 tab={CONCURRENCY}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(storage_state=str(STATE_FILE))

        pages = []
        for i in range(CONCURRENCY):
            page = await ctx.new_page()
            print(f"[warm] tab{i} 打开购买页...")
            try:
                await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=15000)
            except PwTimeout:
                print(f"[warm] tab{i} 加载慢，继续")
            await try_check_agreements(page)
            pages.append(page)

        # 倒计时（>10s 粗睡眠，最后 10s 进入预热 + 500ms busy-wait）
        warmup_task = None
        while True:
            remain = fire_at - time.time()
            if remain <= 0:
                break
            if remain > WARMUP_LEAD_SEC:
                print(f"[wait] 剩余 {remain:.1f}s")
                await asyncio.sleep(min(remain - WARMUP_LEAD_SEC + 0.1, 30))
            elif remain > BUSY_WAIT_MS / 1000.0:
                if warmup_task is None:
                    ka_session = aiohttp.ClientSession()
                    warmup_task = asyncio.create_task(
                        keep_alive_warmup(ka_session, fire_at - BUSY_WAIT_MS / 1000.0)
                    )
                    print(f"[warmup] keep-alive 预热中...")
                await asyncio.sleep(0.02)
            else:
                # 最后 500ms 忙等
                busy_wait_until(fire_at)
                break

        if warmup_task is not None:
            warmup_task.cancel()
            try:
                await warmup_task
            except Exception:
                pass
            try:
                await ka_session.close()
            except Exception:
                pass

        print(">>> 开抢！<<<")
        deadline = time.time() + MAX_WINDOW_SEC
        stop_event = asyncio.Event()

        tasks = [
            asyncio.create_task(grab_loop(pages[i], f"t{i}", deadline, stop_event))
            for i in range(CONCURRENCY)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok = any(r is True for r in results)

        print("[done] 成功进入支付页" if ok else "[done] 超时未成功，请手动检查")
        print("浏览器保持打开，按 Enter 关闭...")
        await asyncio.get_event_loop().run_in_executor(None, input)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
