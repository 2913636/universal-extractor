# Scrapling 使用感受与踩坑记录

> 基于 Python 3.12.9 + Scrapling 0.4.9，Windows 11 环境，约 6 小时实战经验

---

## 一、项目简介

Scrapling 是一个 Python 网页抓取框架，自称"自适应解析 + 反反爬 + 爬虫框架"三位一体。GitHub 66.4k Stars，BSD-3 开源协议。

核心卖点：
- **TLS 指纹伪装**，绕过 Cloudflare Turnstile 等反爬机制
- **自适应解析**，网站改版后自动重新定位目标元素
- **Spider 框架**，类似 Scrapy 的异步爬虫，支持断点续传
- **MCP Server**，让 AI 助手（Claude/Cursor）直接调用抓取能力

---

## 二、优点

### 2.1 反爬绕过能力强

- `StealthyFetcher`（Playwright 真浏览器）能穿透 Cloudflare 验证页面
- `Fetcher`（curl_cffi）的 TLS 指纹伪装对大多数非 JS 渲染站够用
- `ProxyRotator` 内置代理轮换，支持自定义策略

### 2.2 解析 API 直观

```python
# CSS 选择器
page.css('.product')           # 选集合
page.find('.content')          # 选单个元素
page.find_all('p')             # 选所有子元素

# XPath
page.xpath('//h3/a')

# 文本搜索
page.regex(r'\d+\.\d{2}')
page.search('关键词', limit=5)
```

风格接近 jQuery，上手快。

### 2.3 双引擎设计合理

| 引擎 | 底层 | 速度 | 适用场景 |
|------|------|------|---------|
| `Fetcher` | curl_cffi (HTTP) | ~1秒/请求 | 无 JS 渲染的静态页面 |
| `StealthyFetcher` | Playwright Chromium | ~5秒/请求 | 需要 JS 渲染、绕反爬 |

### 2.4 Spider 框架功能完整

- 并发抓取 + 域名限速
- `Ctrl+C` 暂停，重启自动续传
- 开发模式缓存（不重复请求服务器）
- 内置 JSON/JSONL 导出

---

## 三、坑点（按严重程度排序）

### 🔴 3.1 API 文档严重滞后于代码

这是最大的问题。官方文档和 ReadTheDocs 上的 API 与实际代码不一致：

| 文档写法 | 实际用法 | 影响 |
|---------|---------|------|
| `Fetcher().fetch(url)` | `Fetcher().get(url)` | 直接报错 |
| `Fetcher.adaptive = True` | `Fetcher.configure(adaptive=True)` | 警告但不生效 |
| `page.css().first()` | `page.find()` | `'Selector' object is not callable` |
| `StealthySession()` 直接用 | 必须 `session.start()` | `Context manager has been closed` |

每次启动都打印 deprecation warning：
```
WARNING: This logic is deprecated now, and have no effect;
It will be removed with v0.3. Use `Fetcher.configure()` instead before fetching
```

### 🔴 3.2 StealthySession 和 StealthyFetcher 行为不一致

同一个 URL，两者返回的 HTML **完全不同**：

```python
# StealthyFetcher.fetch() → HTML 有 JS 渲染后的章节列表 ✅
# StealthySession.fetch() → HTML 没有章节列表 ❌（0 条匹配）
```

根因未明，怀疑是 Session 复用的浏览器上下文与独立启动的浏览器有差异。**结论：如果需要 JS 渲染内容，优先用 `StealthyFetcher.fetch()`。**

### 🟡 3.3 错误信息不友好

```python
# 实际错误：API 不存在
# 报错信息：
TypeError: 'Selector' object is not callable
```

Python 新手看到这个错误完全不知道怎么修。

### 🟡 3.4 Windows 兼容问题

- `curl_cffi` 在某些 Windows 环境有 OpenSSL 库冲突（`TLS connect error: invalid library`）
- 终端输出中文需要手动 `$env:PYTHONIOENCODING = 'utf-8'`

### 🟡 3.5 无内置超时/重试控制

请求失败会自动重试 3 次，但：
- 无指数退避（每次间隔 1 秒）
- 无法自定义重试策略
- 长时间运行时因网络波动断开且不会自动恢复

### 🟢 3.6 函数名随机化（目标网站的问题，但值得注意）

部分网站会随机化 JS 函数名来反爬：
```html
<!-- 第一次请求 -->
<a onclick="xvyunk(146186633);">第1章</a>

<!-- 第二次请求 -->
<a onclick="rmxj(146186633);">第1章</a>
```

解决方案：用通配正则 `\w+\((\d+)\)` 匹配任意函数名。

---

## 四、实战教训

### 4.1 先验证内容完整性，再批量抓取 ⭐⭐⭐

**这是最惨痛的教训。** 花了几小时从两个站抓了 745+965 章，最后发现每章内容只有一半——网站把每章拆成多页显示，而我们只抓了第一页。换了第三个站（shoujixs.net）才发现完整内容。

**正确流程：**
1. 先手动对比不同来源的同一章，确认哪个站内容最完整
2. 测试提取逻辑是否正确拿到全文
3. 确认无误后再批量跑

### 4.2 注意编码

中文盗版小说站大量使用 **GBK/GB2312 编码**，不是 UTF-8：
```python
# 错误
html = r.body.decode('utf-8')  # 乱码

# 正确
html = r.body.decode('gbk')
```

### 4.3 浏览器引擎 vs HTTP 引擎的选择

```
静态 HTML → Fetcher (curl_cffi)，快
JS 渲染页 → StealthyFetcher (Playwright)，慢但准确
不确定   → 先试 Fetcher，内容为空再换 StealthyFetcher
```

### 4.4 后台任务超时

Scrapling 本身没有超时限制，但运行环境（如 Claude Code 后台任务 10 分钟限制）会截断长时间运行。对大任务要分批：
```python
BATCH_SIZE = 200  # 每批 200 章，约 3-4 分钟
```

---

## 五、适合谁用

| 场景 | 推荐度 | 原因 |
|------|--------|------|
| 简单静态页面抓取 | ⭐⭐⭐⭐⭐ | Fetcher 快且稳定 |
| 需要绕过 Cloudflare | ⭐⭐⭐⭐ | StealthyFetcher 能搞定 |
| JS 渲染页面批量采集 | ⭐⭐ | 太慢（5秒/页），不如直接找 API |
| 新手学爬虫 | ⭐⭐ | 文档坑多，报错不友好 |
| 生产环境大规模采集 | ⭐⭐⭐ | 需要自己封装容错/重试/进度层 |
| 给 AI Agent 做工具 | ⭐⭐⭐⭐ | MCP Server 开箱即用 |

---

## 六、总结

Scrapling 是一个**能力很强但打磨不够**的库。核心的反爬引擎和解析器质量很高，但 API 文档、错误提示、边界情况处理还比较粗糙。如果你愿意读源码、试错，它能帮你搞定大部分爬虫需求。如果你是新手希望"pip install 然后一帆风顺"，可能会遇到不少挫折。

**最终建议：用它的核心能力（StealthyFetcher 绕反爬 + Fetcher 快速抓取），但自己封装容错、进度、重试逻辑。** 不要完全依赖它自带的 Spider 框架做长任务。

---

---

## 七、源码深度阅读（2026-06-29 补充）

### 7.1 项目结构

阅读了 `D:\python-3.12.9\Lib\site-packages\scrapling\` 下全部 44 个 .py 文件：

```
scrapling/
├── __init__.py           # 懒加载入口，v0.4.9
├── fetchers/
│   ├── chrome.py         # DynamicFetcher（非隐身浏览器）
│   ├── requests.py       # Fetcher/AsyncFetcher（curl_cffi HTTP 引擎）
│   └── stealth_chrome.py # StealthyFetcher（隐身浏览器引擎）
├── engines/
│   └── _browsers/
│       ├── _stealth.py   # StealthySession（核心：页面池、Cloudflare 求解器）
│       ├── _base.py      # SyncSession/AsyncSession（页面生命周期管理）
│       ├── _controllers.py # DynamicSession（非隐身版）
│       ├── _page.py      # PagePool/PageInfo（多标签页管理）
│       ├── _types.py     # TypedDict 参数定义（capture_xhr 藏在这里）
│       └── _validators.py # 参数验证 + 默认值
├── parser.py             # Selector/Selectors（CSS/XPath/Regex 解析）
├── core/
│   ├── ai.py             # AI 自适应解析
│   ├── storage.py        # 持久化存储
│   └── translator.py     # 翻译功能
└── spiders/              # 完整 Spider 框架
    ├── engine.py         # 爬虫引擎
    ├── scheduler.py      # 调度器（并发控制）
    ├── checkpoint.py     # 断点续传
    └── cache.py          # 开发模式缓存
```

### 7.2 文档里没写但代码里有的功能

这些都是翻源码才发现的——官方文档和 ReadTheDocs **完全没有提及**：

| 功能 | 位置 | 作用 | 实战价值 |
|---|---|---|---|
| **`page_setup`** | `_stealth.py:240` | 导航**前**执行回调，可注册拦截器 | ⭐⭐⭐⭐⭐ |
| **`init_script`** | `_base.py:95` | 注入 JS 文件到每个页面上下文 | ⭐⭐⭐⭐ |
| **`capture_xhr`** | `_types.py:95` | 正则匹配 XHR/Fetch 响应并存入 `response.xhr_captured` | ⭐⭐⭐⭐ |
| **CDP Session** | Playwright Page → `context.new_cdp_session(page)` | Chrome DevTools 底层协议 | ⭐⭐⭐ |
| **`page.pdf()`** | Playwright Page 原生 | 导出 PDF（Scrapling 未封装但可直接用） | ⭐⭐ |
| **`force=True` 点击** | Playwright Locator | 绕过元素遮挡强制点击 | ⭐⭐⭐ |
| **`disable_resources`** | `_stealth.py:43` | 拦截图片/CSS/字体请求加速 | ⭐⭐ |
| **`blocked_domains`** | `_stealth.py:44` | 按域名拦截请求 | ⭐⭐ |
| **`real_chrome`** | `_stealth.py:97` | 使用本机 Chrome 而非 Chromium | ⭐⭐ |
| **`cdp_url`** | `_stealth.py:98` | 连接已有浏览器实例而非启动新的 | ⭐⭐⭐ |

### 7.3 执行流程（从源码追踪）

```
StealthyFetcher.fetch(url, **kwargs)
  ├── _validate(kwargs)          # 参数验证 → StealthConfig
  ├── StealthySession(**kwargs)  # 启动 Playwright Chromium
  │     ├── launch_persistent_context()  # 持久化浏览器上下文
  │     └── _initialize_context()
  │           └── ctx.add_init_script(path)  # ← 注入 init_script
  └── session.fetch(url)
        ├── _page_generator()     # 从 PagePool 获取页面
        │     └── page.route("**/*", handler)  # 资源拦截
        ├── page.on("response", handler)       # XHR 捕获
        ├── page_setup(page)      # ← 用户回调（导航前）
        ├── page.goto(url)        # 导航
        ├── _wait_for_page_stability()
        ├── solve_cloudflare()    # 可选
        ├── page_action(page)     # ← 用户回调（导航后）
        ├── wait_selector()       # 可选
        └── ResponseFactory.from_playwright_response()  # 构建 Response
              └── response.xhr_captured  # ← 捕获的 XHR 数据
```

---

## 八、WPS 金山文档抓取案例（2026-06-29）

### 8.1 目标

抓取 `https://www.kdocs.cn/l/chtgPO02obP9`（医学AI智能体需求文档，6 页 5371 字）

### 8.2 尝试过的方案（按时间顺序）

| # | 方案 | 技术 | 结果 | 耗时 |
|---|------|------|------|------|
| 1 | curl 直接请求 | HTTP GET | ❌ 返回登录页面（SPA） | 1 min |
| 2 | Scrapling + `innerText` | DOM 提取 | ⚠️ 只拿到大纲（290 字符） | 3 min |
| 3 | 搜索 `window.wps/Application/Editor` | JS API 探测 | ❌ 无公开 API | 2 min |
| 4 | 网络请求拦截（154 个请求） | `page.on("response")` | ⚠️ 捕获到 session token 但文档走 WebSocket | 5 min |
| 5 | `Ctrl+A` 全选 + 复制 | 键盘模拟 | ❌ 选中 0 字符（Canvas 拦截） | 5 min |
| 6 | `init_script` + Canvas Hook | JS 注入 | ❌ WPS 在 webpack chunk 中运行，Hook 未触发 | 5 min |
| 7 | CDP DOMSnapshot | Chrome DevTools | ❌ 文本节点全是整数 glyph 索引 | 5 min |
| 8 | `page.pdf()` 导出 | Playwright 原生 | ❌ Canvas 内容不进 PDF（仅 12 字符） | 3 min |
| 9 | `page_setup` + WebSocket 拦截 | 网络层 Hook | ⚠️ 捕获到 WS 端点但帧数据加密 | 5 min |
| 10 | 鼠标滚轮滚动 + 截图 | `page.mouse.wheel()` | ❌ Canvas 不响应鼠标滚轮 | 5 min |
| 11 | `force=True` 点击展开内容 | Playwright Locator | ✅ 多拿到 1500+ 字符 | 3 min |
| 12 | 全页截图 | `page.screenshot()` | ✅ 4K 截图（能看到第 1 页） | 2 min |

### 8.3 最终成果

| 产出 | 数量 |
|---|---|
| DOM 文本（大纲 + 第 1 页正文开头） | 1,837 字符 |
| 全页截图 | 1 张（3840×2160） |
| 网络 API 数据 | 18 个接口响应 |
| WebSocket 端点 | 1 个（已识别但无法解密） |

### 8.4 根因分析

WPS WebOffice 的渲染链路：

```
服务器 → WebSocket (wss://kdocs.cn/websocket/v3/...)
       → 私有二进制协议（加密）
       → Canvas 2D 绑图引擎
       → 屏幕像素
```

**文本从来不以 DOM 节点形式存在。** 这与普通 SPA 不同——SPA 的 JS 动态生成 DOM，你等它渲染完再读 DOM 就行。但 Canvas 绑图是直接把文字画到 `<canvas>` 元素上，画完就没了，DOM 里不留下任何文字痕迹。

WPS 的 5 层防护：
1. Ctrl+C 禁用（JS 层面，可绕过）
2. Canvas 绑图（文字不在 DOM，无法选中）
3. 内部滚动独立（鼠标/键盘无法程序化滚动）
4. 无公开 JS API（window 下没有任何 editor 对象）
5. WebSocket 加密（文档数据走私有二进制协议）

### 8.5 这类页面的唯一可行方案

- **OCR**：截图 + Tesseract/paddleocr 识别（需要装 OCR 引擎）
- **手动复制**：真人打开浏览器，登录后 Ctrl+A → Ctrl+C
- **找替代源**：让文档所有者导出 .docx 文件

---

## 九、更新后的总结

### 核心能力矩阵

| 场景 | 推荐工具 | 关键参数 |
|------|---------|---------|
| 静态 HTML | `Fetcher` (curl_cffi) | — |
| JS 渲染 SPA（如 BOSS直聘） | `StealthyFetcher` + `page_action` | 等 `networkidle` 后提取 DOM |
| 需要绕 Cloudflare | `StealthyFetcher` | `solve_cloudflare=True` |
| 需要拦截 API 数据 | `StealthyFetcher` + `page_setup` | 在导航前注册 `page.on("response")` |
| 需要注入自定义 JS | `StealthyFetcher` + `init_script` | JS 文件路径 |
| Canvas 绑图页面 | **无法自动提取** | 用截图 + OCR |

### 最终建议

1. **遇到新目标站，先开浏览器 F12** 看内容在 DOM 里还是 Canvas 里——DOM 可抓，Canvas 只能截图
2. **`page_setup` 是最被低估的功能**——在导航前注册拦截器，能抓到很多隐藏数据
3. **`capture_xhr` 比手动拦截更方便**——正则匹配 URL 即可自动收集 XHR 响应
4. **CDP Session 是终极武器**——当 Playwright 高层 API 不够用时，降到 CDP 层操作
5. **读源码比看文档靠谱**——Scrapling 至少 7 个重要功能文档完全没提

---

*记录时间：2026-06-28 ~ 2026-06-29*
*Scrapling 版本：0.4.9*
*实战项目：① 抓取网络小说 ② 抓取 WPS 金山文档（本次新增）*
