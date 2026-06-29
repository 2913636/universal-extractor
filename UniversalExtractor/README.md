# Universal Extractor — 通用网页内容提取器

6 层降级链，自动选择最佳策略提取正文。

```
extract(url)
  ├── ① DOM 提取              → 直接从页面拿文字
  ├── ② API 拦截               → 从 XHR/Fetch 响应中提取
  ├── ③ Canvas Hook            → 拦截 Canvas fillText 获取绑图文字
  ├── ④ CDP 内存扫描           → 扫 JS 堆内存找文本对象
  ├── ⑤ 截图 + OCR             → 逐屏截图 → 多模型 Vision AI 识别
  └── ⑥ Vision LLM 全页        → 拼接全页截图 → Vision LLM 结构化提取
```

## 安装

```bash
pip install scrapling[all] pillow python-dotenv

# Vision OCR 后端（至少选一个）
pip install openai          # GPT-4o / Qwen-VL
pip install anthropic       # Claude Sonnet

# 本地 OCR（可选）
pip install pytesseract     # + 安装 tesseract.exe + 中文语言包
```

## 配置 Vision 后端

至少设置一个环境变量：

| 环境变量 | 后端 | 模型 |
|---------|------|------|
| `OPENAI_API_KEY` | GPT-4o-mini | `gpt-4o-mini` |
| `ANTHROPIC_API_KEY` | Claude Sonnet | `claude-sonnet-4-6` |
| `DASHSCOPE_API_KEY` | Qwen-VL-Max | `qwen-vl-max` |
| `DEEPSEEK_API_KEY` | DeepSeek（自动探测 vision） | `deepseek-chat` |

无 Vision 后端时，Layer ⑤/⑥ 不可用，但仍可走前 4 层。

## 使用

```python
from universal_extractor import UniversalExtractor

ue = UniversalExtractor(headless=True)
text = ue.extract("https://example.com/article")
print(text)
```

高级配置：

```python
from universal_extractor import OpenAIProvider, AnthropicProvider

ue = UniversalExtractor(
    headless=False,
    vision_providers=[
        OpenAIProvider(model="gpt-4o"),
        AnthropicProvider(model="claude-sonnet-4-6"),
    ],
    max_passes=3,            # 流式应用多轮提取
    screenshot_overlap=0.2,  # 截帧 20% 重叠防遗漏
)
```

## 模块结构

```
UniversalExtractor/
  __init__.py          # 公开 API
  extractor.py         # 编排层（6 层降级 + 多轮提取）
  ocr_providers.py     # Vision 后端注册表（5 个后端）
  scrolling.py         # Canvas 滚动 4 层降级链
  screenshot.py        # 截帧 / 感知哈希去重 / 垂直拼接
  completeness.py      # 连续完整性评分（0.0~1.0）
  canvas_hook.py       # Hook JS + 多向量并行注入
  demo.py              # 使用示例
  README.md            # 本文件
```

## 适用场景

| 网站类型 | 生效层 | 提取率 |
|---------|-------|-------|
| 博客、新闻站 | ① DOM | ~100% |
| SPA（React/Vue） | ① DOM | ~95% |
| BOSS直聘等招聘站 | ①+② API | ~95% |
| 普通 Canvas 页面 | ③ Hook | ~90% |
| 流式文档应用 | ③→⑥ 多轮+全页 | ~70% (目标 85%+) |
| WPS/飞书/腾讯文档 | ③→⑥ 多轮+Vision LLM | 受限于 Canvas 滚动 |

## 技术栈

- **Scrapling 0.4.9**：浏览器自动化（Playwright Chromium）
- **CDP**：Chrome DevTools Protocol 底层注入 / 滚动 / 内存扫描
- **Multi-Vision-LLM**：GPT-4o / Claude / Qwen-VL 云端 OCR
- **Tesseract**：本地 OCR 保底
- **感知哈希**：截帧去重（pHash）
