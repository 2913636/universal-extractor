# Universal Extractor

通用网页内容提取器。**6 层降级链**，自动选择最佳策略把任意网页变成纯文本。

```
① DOM 提取 → ② API 拦截 → ③ Canvas Hook → ④ CDP 内存扫描 → ⑤ 截图 OCR → ⑥ Vision LLM 全页
```

## 为什么需要它

Scrapling、Jina Reader、浏览器的"阅读模式"都只在 DOM 能拿到文字时有效。遇到 Canvas 绑图页面（WPS/飞书/腾讯文档）、Web Worker 渲染、反爬保护的页面，它们拿到的只是空壳。

这个库从 DOM 一路降级到 OCR——DOM 拿不到就 Hook Canvas 绑图指令，Hook 不到就截帧 OCR，OCR 不够就拼接整页送 Vision LLM。**对普通页面只用第 1 层，对困难页面自动降到底。**

## 安装

```bash
pip install universal-extractor

# 可选：Vision LLM 后端（至少选一个，OCR 层需要）
pip install universal-extractor[openai]       # GPT-4o
pip install universal-extractor[anthropic]    # Claude Sonnet
pip install universal-extractor[all]          # 全部
```

环境变量（至少配一个 Vision 后端）：

| 变量 | 后端 |
|------|------|
| `OPENAI_API_KEY` | GPT-4o-mini |
| `ANTHROPIC_API_KEY` | Claude Sonnet |
| `DASHSCOPE_API_KEY` | Qwen-VL-Max |
| `DEEPSEEK_API_KEY` | DeepSeek（自动探测） |

不配 Vision 后端也能用——前 4 层不需要 API Key。

## 使用

```python
from universal_extractor import UniversalExtractor

ue = UniversalExtractor(headless=True)
text = ue.extract("https://www.kdocs.cn/l/xxxxx")
print(text)
```

## 模块结构

```
UniversalExtractor/
  extractor.py          # 编排层 — 6 层降级 + 多轮提取
  ocr_providers.py      # Vision 后端注册表（GPT-4o / Claude / Qwen / DeepSeek / Tesseract）
  scrolling.py          # Canvas 滚动 4 层降级链（JS → CDP wheel → CDP drag）
  screenshot.py         # 截帧 / 感知哈希去重 / 垂直拼接
  completeness.py       # 连续完整性评分（0.0~1.0，7 因子）
  canvas_hook.py        # Hook JS v2 + 3 向量并行注入（CDP + init_script + HTML 拦截）
  classifier.py         # URL 分类 + 内容类型识别 + 关键词精准匹配
  search.py             # 搜索聚合（DuckDuckGo / Brave / Exa）
  weblens.py            # WebLens — 搜 + 筛 + 抓 编排引擎
  jd_engine.py          # 全平台 JD 结构化引擎
  mcp_server.py         # MCP Server — 让 Claude/Cursor 直接调用
```

## 适用场景

| 网站类型 | 生效层 | 提取率 |
|---------|-------|:--:|
| 博客、新闻、文档 | ① DOM | ~98% |
| SPA（React/Vue） | ① DOM + page_action | ~95% |
| 反爬保护站点 | ① StealthyFetcher | ~95% |
| 富文本编辑器 | ① DOM（6 种编辑器选择器） | ~95% |
| Shadow DOM / iframe | ① deepText 递归 | ~95% |
| 普通 Canvas 页面 | ③ Canvas Hook（rAF 轮询） | ~90% |
| Canvas 流式文档（WPS/飞书） | ⑤⑥ OCR + Vision LLM | ~70% |

## License

MIT
