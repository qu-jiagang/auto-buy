# 阿里云 Coding Plan 自动抢购脚本

抢购地址: https://common-buy.aliyun.com/coding-plan
补货时间: 每日 09:30:00 (UTC+8)

## 原理

阿里云下单接口有风控（滑块、JS 签名、umid token），直接构造 HTTP 请求极易失败，且被判定为机器后会封号。
所以采用 **Playwright 浏览器自动化**：

1. 第一次运行 `save_login.py`，手动扫码/登录，把 cookie/storage 保存到 `state.json`
2. 抢购时运行 `grab.py`，它会：
   - 用保存的登录状态打开购买页（避免抢购瞬间登录卡死）
   - 使用 NTP 对齐系统时间，倒计时到 09:29:59.8
   - 到点后循环快速点「立即购买」→「确认订单」→「去支付」
   - 失败自动重试，成功后停下等你手动完成支付

## 安装

```bash
pip install playwright ntplib
playwright install chromium
```

## 使用

```bash
# 1. 登录一次（保存 cookie，几周内有效）
python3 save_login.py

# 2. 抢购（建议 09:25 前启动）
python3 grab.py
```

## 注意

- 脚本只做下单，不自动支付（防止刷错）。成功后自行在 30 分钟内付款。
- 服务条款明确禁止用「额度」跑自动化调用（会被封订阅），但下单本身是正常消费行为。
- 如果页面 DOM 变化导致选择器失效，调 `grab.py` 里的 `SELECTORS` 常量即可。
