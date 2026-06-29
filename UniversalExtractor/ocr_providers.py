"""
Vision OCR Provider 注册表 — 多模型后端自动选择。

支持的后端：
  - OpenAIProvider    : GPT-4o / GPT-4o-mini（通过 OPENAI_API_KEY）
  - AnthropicProvider : Claude Sonnet（通过 ANTHROPIC_API_KEY）
  - QwenProvider      : Qwen-VL（通过 DASHSCOPE_API_KEY 或 QWEN_API_KEY）
  - DeepSeekProvider  : 自动探测是否支持 vision（通过 DEEPSEEK_API_KEY）
  - TesseractProvider : 本地 OCR（需安装 tesseract.exe + 中文语言包）

用法:
    from .ocr_providers import auto_configure_providers

    providers = auto_configure_providers()
    for p in providers:
        if p.can_handle():
            text = p.extract_text(b64_image, prompt="提取所有文字")
"""

from __future__ import annotations

import base64
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# Abstract Base
# ============================================================

class VisionProvider(ABC):
    """Vision OCR 后端基类。"""

    name: str = "base"

    @abstractmethod
    def can_handle(self) -> bool:
        """此 provider 是否可用（API Key 已配置等）。"""
        ...

    @abstractmethod
    def extract_text(
        self,
        image_b64: str,
        prompt: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> str:
        """从 base64 编码的图片中提取文本。"""
        ...


# ============================================================
# OpenAI Provider
# ============================================================

class OpenAIProvider(VisionProvider):
    """GPT-4o / GPT-4o-mini 视觉 OCR。"""

    name = "openai"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4o-mini",
        base_url: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
        self.model = model
        self.base_url = base_url

    def can_handle(self) -> bool:
        return bool(self.api_key)

    def extract_text(
        self,
        image_b64: str,
        prompt: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> str:
        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("OpenAI SDK not installed; pip install openai")
            return ""

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        response = client.chat.completions.create(
            model=self.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}",
                            "detail": "high",
                        },
                    },
                ],
            }],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""


# ============================================================
# Anthropic Provider
# ============================================================

class AnthropicProvider(VisionProvider):
    """Claude Sonnet / Haiku 视觉 OCR。"""

    name = "anthropic"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-6",
    ):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY") or ""
        self.model = model

    def can_handle(self) -> bool:
        return bool(self.api_key)

    def extract_text(
        self,
        image_b64: str,
        prompt: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> str:
        try:
            import anthropic
        except ImportError:
            logger.warning("Anthropic SDK not installed; pip install anthropic")
            return ""

        client = anthropic.Anthropic(api_key=self.api_key)

        message = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_b64,
                        },
                    },
                ],
            }],
        )
        # Response content is a list of blocks
        for block in message.content:
            if block.type == "text":
                return block.text
        return ""


# ============================================================
# Qwen-VL Provider (DashScope 兼容 API)
# ============================================================

class QwenProvider(VisionProvider):
    """通义千问 VL 系列视觉 OCR。"""

    name = "qwen"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "qwen-vl-max",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ):
        self.api_key = (
            api_key
            or os.getenv("DASHSCOPE_API_KEY")
            or os.getenv("QWEN_API_KEY")
            or ""
        )
        self.model = model
        self.base_url = base_url

    def can_handle(self) -> bool:
        return bool(self.api_key)

    def extract_text(
        self,
        image_b64: str,
        prompt: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> str:
        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("OpenAI SDK not installed; pip install openai")
            return ""

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        response = client.chat.completions.create(
            model=self.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            }],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""


# ============================================================
# DeepSeek Provider
# ============================================================

class DeepSeekProvider(VisionProvider):
    """
    DeepSeek 视觉 OCR（自动探测是否支持 image_url）。

    注意：截至 2026-06，``deepseek-chat`` 不支持 vision。
    此 provider 会在首次调用时探测，不支持则标记为不可用。
    """

    name = "deepseek"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "deepseek-chat",
    ):
        self.api_key = (
            api_key
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("DEEPSEEK_KEY")
            or ""
        )
        self.model = model
        self._vision_supported: Optional[bool] = None  # None=未探测

    def can_handle(self) -> bool:
        if not self.api_key:
            return False
        if self._vision_supported is False:
            return False
        return True

    def extract_text(
        self,
        image_b64: str,
        prompt: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> str:
        # 如果已知不支持，直接返回空
        if self._vision_supported is False:
            return ""

        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("OpenAI SDK not installed; pip install openai")
            return ""

        client = OpenAI(
            api_key=self.api_key,
            base_url="https://api.deepseek.com",
        )

        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                    ],
                }],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            self._vision_supported = True
            return response.choices[0].message.content or ""
        except Exception as exc:
            err_msg = str(exc).lower()
            if any(kw in err_msg for kw in ["image", "vision", "multimodal", "not supported", "invalid"]):
                self._vision_supported = False
                logger.info(
                    "DeepSeek model '%s' does not support image input — "
                    "will skip this provider in future calls", self.model)
            else:
                logger.warning("DeepSeek API error: %s", exc)
            return ""


# ============================================================
# Tesseract Provider
# ============================================================

class TesseractProvider(VisionProvider):
    """本地 Tesseract OCR（需安装 tesseract.exe + chi_sim 语言包）。"""

    name = "tesseract"

    def __init__(self, tesseract_cmd: Optional[str] = None):
        self.tesseract_cmd = tesseract_cmd

    def can_handle(self) -> bool:
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
            return True
        except ImportError:
            return False

    def extract_text(
        self,
        image_b64: str,
        prompt: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> str:
        # prompt/temperature/max_tokens ignored for Tesseract
        import pytesseract
        from PIL import Image
        import tempfile

        try:
            img_data = base64.b64decode(image_b64)

            # Write to temp file
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(img_data)
                tmp_path = f.name

            try:
                if self.tesseract_cmd:
                    pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd

                img = Image.open(tmp_path).convert("L")
                config = "--oem 3 --psm 6 -l chi_sim+eng"
                text = pytesseract.image_to_string(img, config=config)
                return text.strip() if text else ""
            finally:
                os.unlink(tmp_path)
        except Exception as exc:
            logger.warning("Tesseract OCR error: %s", exc)
            return ""


# ============================================================
# Auto-configuration
# ============================================================

def auto_configure_providers(
    *,
    tesseract_cmd: Optional[str] = None,
    prefer: Optional[list[str]] = None,
) -> list[VisionProvider]:
    """
    从环境变量自动配置所有可用 provider。

    探测顺序：
      1. OPENAI_API_KEY    → OpenAIProvider (gpt-4o-mini)
      2. ANTHROPIC_API_KEY → AnthropicProvider (claude-sonnet-4-6)
      3. DASHSCOPE_API_KEY → QwenProvider (qwen-vl-max)
      4. DEEPSEEK_API_KEY  → DeepSeekProvider（自动探测 vision 支持）
      5. pytesseract 已安装 → TesseractProvider

    Parameters:
        tesseract_cmd: tesseract.exe 路径
        prefer: 指定优先级顺序的 provider name 列表，
                如 ``["openai", "anthropic"]``

    Returns:
        可用 provider 列表（已按 prefer 排序）
    """
    providers: list[VisionProvider] = []

    # OpenAI
    if os.getenv("OPENAI_API_KEY"):
        providers.append(OpenAIProvider())

    # Anthropic
    if os.getenv("ANTHROPIC_API_KEY"):
        providers.append(AnthropicProvider())

    # Qwen
    if os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_API_KEY"):
        providers.append(QwenProvider())

    # DeepSeek
    if os.getenv("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_KEY"):
        providers.append(DeepSeekProvider())

    # Tesseract (always add if available, as fallback)
    tp = TesseractProvider(tesseract_cmd=tesseract_cmd)
    if tp.can_handle():
        providers.append(tp)

    # 按 prefer 排序
    if prefer:
        order = {name: i for i, name in enumerate(prefer)}
        providers.sort(key=lambda p: order.get(p.name, 999))

    return providers
