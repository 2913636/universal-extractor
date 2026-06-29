"""
全平台 JD 抓取引擎 — UniversalExtractor + LLM 结构化。

替换 job-hunter 原有的 per-site 硬编码抓取逻辑。
任意招聘页面 URL → 6 层降级链提取全文 → LLM 结构化 → 喂给匹配引擎。

用法:
    from universal_extractor.jd_engine import JDEngine

    engine = JDEngine(headless=False)
    jd = engine.fetch_jd("https://www.zhipin.com/job_detail/xxx.html")
    # → {"title": "AI Agent 工程师", "requirements": [...], ...}

多平台搜索:
    jobs = engine.discover("AI Agent", city="长沙",
                           platforms=["boss", "liepin", "lagou"])
    # → [{"title": "...", "url": "...", "company": "..."}, ...]
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .extractor import UniversalExtractor

logger = logging.getLogger(__name__)


# ============================================================
# 多平台 URL 模板
# ============================================================

PLATFORM_CONFIG = {
    "boss": {
        "name": "BOSS直聘",
        "search_url": "https://www.zhipin.com/web/geek/job?query={keyword}&city={city_code}&page={page}",
        "detail_pattern": r"zhipin\.com/job_detail/[a-f0-9]+\.html",
        "city_codes": {
            "长沙": "100010000", "深圳": "101280600", "北京": "101010100",
            "上海": "101020100", "广州": "101280100", "杭州": "101210100",
            "南京": "101190100", "成都": "101270100", "武汉": "101200100",
        },
    },
    "liepin": {
        "name": "猎聘",
        "search_url": "https://www.liepin.com/zhaopin/?key={keyword}&dqs={city_code}&curPage={page}",
        "detail_pattern": r"liepin\.com/job/\d+\.s?html",
        "city_codes": {
            "长沙": "070200", "深圳": "050090", "北京": "010000",
            "上海": "020000", "广州": "050020", "杭州": "080020",
        },
    },
    "lagou": {
        "name": "拉勾",
        "search_url": "https://www.lagou.com/wn/jobs?kd={keyword}&city={city}&pn={page}",
        "detail_pattern": r"lagou\.com/jobs/\d+\.html",
        "city_codes": {
            "长沙": "长沙", "深圳": "深圳", "北京": "北京",
            "上海": "上海", "广州": "广州", "杭州": "杭州",
        },
    },
    "zhaopin": {
        "name": "智联招聘",
        "search_url": "https://sou.zhaopin.com/?kw={keyword}&city={city_code}&p={page}",
        "detail_pattern": r"zhaopin\.com/jobdetail/[A-Z0-9]+\.html",
        "city_codes": {"长沙": "730", "深圳": "765", "北京": "530", "上海": "538"},
    },
    "maimai": {
        "name": "脉脉",
        "search_url": "https://maimai.cn/web/jobs?query={keyword}&city={city}&page={page}",
        "detail_pattern": r"maimai\.cn/web/jobs\?",
        "city_codes": {},
    },
    "51job": {
        "name": "前程无忧",
        "search_url": "https://we.51job.com/pc/search?keyword={keyword}&area={city_code}&page={page}",
        "detail_pattern": r"51job\.com/\w+/\d+\.html",
        "city_codes": {"长沙": "180200", "深圳": "040000", "北京": "010000"},
    },
}


def _resolve_city(platform: str, city: str) -> str:
    """将中文城市名转为平台的城市编码。"""
    codes = PLATFORM_CONFIG.get(platform, {}).get("city_codes", {})
    return codes.get(city, city)


def _build_search_url(platform: str, keyword: str, city: str, page: int) -> str:
    """根据平台和参数构造搜索 URL。"""
    cfg = PLATFORM_CONFIG.get(platform)
    if not cfg:
        raise ValueError(f"Unknown platform: {platform}")
    city_code = _resolve_city(platform, city)
    return cfg["search_url"].format(
        keyword=keyword, city=city, city_code=city_code, page=page,
    )


# ============================================================
# JD 结构化 Prompt（通用，不 per-site）
# ============================================================

STRUCTURE_SYSTEM_PROMPT = """你是一个招聘 JD 解析系统。从招聘页面文本中提取结构化信息。

规则：
1. 输入文本可能包含页面噪音（导航栏、推荐岗位、广告），只提取目标 JD 的内容
2. salary 统一为"月薪范围-薪制"，如"15k-25k·15薪"
3. 如果某个字段在文本中找不到，用空字符串或空数组，不要编造
4. is_complete 判断：职责+要求都有具体内容（非大纲/占位符）则为 true
5. 技术栈从 requirements 和 nice_to_have 中提取具体技术名词"""

STRUCTURE_USER_PROMPT = """从以下招聘页面文本中提取结构化 JD。

页面文本：
{raw_text}

返回 JSON（不要 markdown 代码块标记，只输出纯 JSON）：
{{
  "title": "岗位名称",
  "company": "公司名称",
  "salary": "薪资范围",
  "location": "工作地点",
  "experience": "经验要求",
  "education": "学历要求",
  "responsibilities": ["职责1", "职责2"],
  "requirements": ["要求1", "要求2"],
  "nice_to_have": ["加分项1"],
  "benefits": ["福利1"],
  "tech_stack": ["Python", "LangChain"],
  "is_complete": true
}}"""


# ============================================================
# Data classes
# ============================================================

@dataclass
class JDResult:
    """结构化 JD 结果。"""
    title: str = ""
    company: str = ""
    salary: str = ""
    location: str = ""
    experience: str = ""
    education: str = ""
    responsibilities: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    nice_to_have: list[str] = field(default_factory=list)
    benefits: list[str] = field(default_factory=list)
    tech_stack: list[str] = field(default_factory=list)
    is_complete: bool = False
    # 元数据
    source_url: str = ""
    source_layer: int = 0
    raw_length: int = 0

    def to_text(self) -> str:
        """转为人类可读文本，供匹配引擎使用。"""
        parts = []
        if self.title:
            parts.append(f"【{self.title}】")
        if self.company:
            parts.append(f"公司：{self.company}")
        if self.salary:
            parts.append(f"薪资：{self.salary}")
        if self.location:
            parts.append(f"地点：{self.location}")
        if self.experience:
            parts.append(f"经验：{self.experience}")
        if self.education:
            parts.append(f"学历：{self.education}")
        if self.requirements:
            parts.append("\n要求：\n  " + "\n  ".join(self.requirements))
        if self.responsibilities:
            parts.append("\n职责：\n  " + "\n  ".join(self.responsibilities))
        if self.nice_to_have:
            parts.append("\n加分：\n  " + "\n  ".join(self.nice_to_have))
        if self.tech_stack:
            parts.append("\n技术栈：" + ", ".join(self.tech_stack))
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "company": self.company,
            "salary": self.salary,
            "location": self.location,
            "experience": self.experience,
            "education": self.education,
            "responsibilities": self.responsibilities,
            "requirements": self.requirements,
            "nice_to_have": self.nice_to_have,
            "benefits": self.benefits,
            "tech_stack": self.tech_stack,
            "is_complete": self.is_complete,
            "source_url": self.source_url,
            "source_layer": self.source_layer,
        }


# ============================================================
# Main Engine
# ============================================================

class JDEngine:
    """全平台 JD 抓取引擎。

    Parameters:
        headless: 浏览器是否无头模式
        timeout: 浏览器超时（毫秒）
        llm_api_key: LLM API Key（None 则从环境变量）
        llm_base_url: LLM API 地址
        llm_model: LLM 模型名
    """

    def __init__(
        self,
        headless: bool = True,
        timeout: int = 120_000,
        llm_api_key: str | None = None,
        llm_base_url: str = "https://api.deepseek.com",
        llm_model: str = "deepseek-chat",
    ):
        self.extractor = UniversalExtractor(
            headless=headless,
            timeout=timeout,
        )

        # LLM 配置
        self.llm_api_key = (
            llm_api_key
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("DEEPSEEK_KEY")
            or ""
        )
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model

        if not self.llm_api_key:
            logger.warning(
                "No LLM API key configured. "
                "Set DEEPSEEK_API_KEY or pass llm_api_key. "
                "fetch_jd() will still return raw text."
            )

    # --------------------------------------------------------
    # 核心：单条 JD 抓取
    # --------------------------------------------------------

    def fetch_jd(self, url: str) -> JDResult:
        """
        抓取并结构化一个招聘页面。

        自动选择最优提取策略（DOM/API/Canvas/OCR），
        然后用 LLM 结构化。

        Args:
            url: 任何招聘详情页 URL

        Returns:
            JDResult: 结构化 JD
        """
        result = JDResult(source_url=url)

        try:
            print(f"[JDEngine] Extracting: {url}")
            raw_text = self.extractor.extract(url)
            result.raw_length = len(raw_text)

            if not raw_text or len(raw_text.strip()) < 50:
                logger.warning("Extraction returned insufficient text (%d chars)",
                               len(raw_text))
                return result

            print(f"[JDEngine] Got {len(raw_text)} chars, structuring...")

            # LLM 结构化
            if self.llm_api_key:
                structured = self._structure(raw_text, url)
                self._fill_result(result, structured)
            else:
                # 无 LLM：原始文本填 title
                result.title = raw_text.split("\n")[0][:80] if raw_text else ""

        except Exception as exc:
            logger.error("fetch_jd error for %s: %s", url, exc)

        return result

    def _structure(self, raw_text: str, source_url: str) -> dict:
        """用 LLM 将原始文本转为结构化 JD。"""
        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("openai package not installed; cannot structure JD")
            return {}

        client = OpenAI(
            api_key=self.llm_api_key,
            base_url=self.llm_base_url,
        )

        # 截断过长文本（保留关键部分）
        text = self._trim_for_llm(raw_text)

        try:
            response = client.chat.completions.create(
                model=self.llm_model,
                messages=[
                    {"role": "system", "content": STRUCTURE_SYSTEM_PROMPT},
                    {"role": "user", "content": STRUCTURE_USER_PROMPT.format(
                        raw_text=text,
                    )},
                ],
                temperature=0.1,
                max_tokens=2048,
            )
        except Exception as exc:
            logger.error("LLM call failed: %s", exc)
            return {}

        content = response.choices[0].message.content or ""

        # 清洗 LLM 输出（可能带了 markdown 代码块标记）
        content = self._clean_json_response(content)

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # 尝试提取第一个 JSON 对象
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            logger.warning("LLM returned non-JSON: %s...", content[:200])
            return {}

    def _fill_result(self, result: JDResult, data: dict) -> None:
        """将 LLM 输出的 dict 填充到 JDResult。"""
        result.title = data.get("title", "")
        result.company = data.get("company", "")
        result.salary = data.get("salary", "")
        result.location = data.get("location", "")
        result.experience = data.get("experience", "")
        result.education = data.get("education", "")
        result.responsibilities = data.get("responsibilities", [])
        result.requirements = data.get("requirements", [])
        result.nice_to_have = data.get("nice_to_have", [])
        result.benefits = data.get("benefits", [])
        result.tech_stack = data.get("tech_stack", [])
        result.is_complete = data.get("is_complete", False)

    # --------------------------------------------------------
    # 多平台岗位发现
    # --------------------------------------------------------

    def discover(
        self,
        keyword: str,
        city: str = "长沙",
        platforms: list[str] | None = None,
        max_pages: int = 2,
    ) -> list[dict]:
        """
        多平台并行搜索岗位。

        Args:
            keyword: 搜索关键词
            city: 城市
            platforms: 平台列表，默认 ["boss", "liepin", "lagou"]
            max_pages: 每平台最多搜几页

        Returns:
            岗位列表：[{"title": "...", "url": "...", "platform": "boss"}, ...]
        """
        if platforms is None:
            platforms = ["boss", "liepin", "lagou"]

        all_jobs: list[dict] = []

        for platform in platforms:
            cfg = PLATFORM_CONFIG.get(platform)
            if not cfg:
                logger.warning("Unknown platform: %s, skipping", platform)
                continue

            print(f"\n[Discover] {cfg['name']} — {keyword} @ {city}")
            platform_jobs = self._discover_one_platform(
                platform, keyword, city, max_pages,
            )
            all_jobs.extend(platform_jobs)
            print(f"[Discover] {cfg['name']}: {len(platform_jobs)} jobs found")

        # 去重（按 URL）
        seen_urls = set()
        unique: list[dict] = []
        for job in all_jobs:
            url = job.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique.append(job)
            elif not url:
                unique.append(job)

        print(f"\n[Discover] Total: {len(unique)} unique jobs across {len(platforms)} platforms")
        return unique

    def _discover_one_platform(
        self, platform: str, keyword: str, city: str, max_pages: int,
    ) -> list[dict]:
        """在单个平台上搜索岗位列表。"""
        jobs: list[dict] = []
        cfg = PLATFORM_CONFIG[platform]

        for page in range(1, max_pages + 1):
            url = _build_search_url(platform, keyword, city, page)
            print(f"  Page {page}: {url[:100]}...")

            try:
                raw_text = self.extractor.extract(url)
            except Exception as exc:
                logger.warning("  Page %d extract failed: %s", page, exc)
                continue

            if not raw_text or len(raw_text.strip()) < 200:
                logger.warning("  Page %d: insufficient content (%d chars), stopping",
                               page, len(raw_text))
                break

            # 用 LLM 从搜索列表文本中提取岗位卡片
            page_jobs = self._parse_search_results(raw_text, platform)
            if not page_jobs:
                logger.warning("  Page %d: no jobs found in text", page)
                break

            jobs.extend(page_jobs)

        return jobs

    def _parse_search_results(
        self, raw_text: str, platform: str,
    ) -> list[dict]:
        """从搜索页文本中解析岗位列表。"""
        if not self.llm_api_key:
            # 无 LLM：尝试正则提取 URL
            return self._regex_extract_jobs(raw_text, platform)

        try:
            from openai import OpenAI
        except ImportError:
            return self._regex_extract_jobs(raw_text, platform)

        client = OpenAI(
            api_key=self.llm_api_key,
            base_url=self.llm_base_url,
        )

        text = raw_text[:6000]  # 搜索列表不需要太长的上下文

        try:
            response = client.chat.completions.create(
                model=self.llm_model,
                messages=[{
                    "role": "user",
                    "content": f"""从以下招聘搜索页面文本中，提取所有岗位卡片信息。

页面文本：
{text}

请返回 JSON 数组，每个岗位包含：
- title: 岗位名称
- company: 公司名称
- salary: 薪资（如果有）
- url: 详情链接（如果有，保留原始路径）
- platform: "{platform}"

只输出纯 JSON 数组，不要 markdown 标记。如果没有找到岗位，返回空数组 []。""",
                }],
                temperature=0.1,
                max_tokens=2048,
            )
        except Exception as exc:
            logger.warning("  Search parse LLM error: %s", exc)
            return self._regex_extract_jobs(raw_text, platform)

        content = response.choices[0].message.content or ""
        content = self._clean_json_response(content)

        try:
            jobs = json.loads(content)
            if isinstance(jobs, list):
                # 补全相对 URL
                for job in jobs:
                    url = job.get("url", "")
                    if url and url.startswith("/"):
                        job["url"] = self._resolve_relative_url(url, platform)
                    job["platform"] = platform
                return jobs
        except json.JSONDecodeError:
            pass

        return self._regex_extract_jobs(raw_text, platform)

    def _regex_extract_jobs(self, raw_text: str, platform: str) -> list[dict]:
        """降级：用正则从文本中提取 URL。"""
        pattern = PLATFORM_CONFIG.get(platform, {}).get("detail_pattern", "")
        if not pattern:
            return []

        urls = set(re.findall(pattern, raw_text, re.IGNORECASE))
        # 去重，保留前 30 个
        return [
            {"title": "", "url": self._resolve_relative_url(u, platform),
             "platform": platform}
            for u in list(urls)[:30]
        ]

    # --------------------------------------------------------
    # 批量抓取
    # --------------------------------------------------------

    def fetch_batch(
        self,
        urls: list[str],
        progress_callback=None,
    ) -> list[JDResult]:
        """
        批量抓取多个 JD。

        Args:
            urls: URL 列表
            progress_callback: 每完成一个调用 callback(idx, total, result)

        Returns:
            JDResult 列表（与输入顺序一致）
        """
        results: list[JDResult] = []
        total = len(urls)

        for i, url in enumerate(urls):
            print(f"\n[{i + 1}/{total}] {url[:100]}...")
            result = self.fetch_jd(url)
            results.append(result)

            if progress_callback:
                progress_callback(i, total, result)

        return results

    # --------------------------------------------------------
    # Helpers
    # --------------------------------------------------------

    @staticmethod
    def _trim_for_llm(text: str, max_chars: int = 8000) -> str:
        """智能截断文本到 LLM 可处理长度。

        优先保留包含 JD 关键词的段落，裁掉尾部和明显噪音。
        """
        if len(text) <= max_chars:
            return text

        # 去除明显的页面噪音行（导航、页脚）
        noise_patterns = [
            r"^(首页|关于|联系|帮助|反馈|举报|举报|登录|注册|退出).{0,30}$",
            r"^(首页|职位|公司|校园|资讯|APP|小程序).{0,30}$",
            r"^\s*$",
        ]
        lines = text.split("\n")
        filtered = [
            ln for ln in lines
            if not any(re.match(p, ln.strip()) for p in noise_patterns)
        ]
        text = "\n".join(filtered)

        # 保留前 2/3 和关键段落（JD 内容通常在前面）
        if len(text) <= max_chars:
            return text

        # 截取前 2/3
        cutoff = int(max_chars * 0.67)
        head = text[:cutoff]

        # 从尾部找包含 JD 关键词的段落
        jd_keywords = ["要求", "职责", "技能", "经验", "学历", "技术栈",
                        "加分", "福利", "薪资"]
        tail = text[cutoff:]
        tail_lines = tail.split("\n")
        important_tail = [
            ln for ln in tail_lines
            if any(kw in ln for kw in jd_keywords)
        ]
        remaining = max_chars - cutoff
        tail_text = ""
        for ln in important_tail:
            if len(tail_text) + len(ln) + 1 <= remaining:
                tail_text += ln + "\n"
            else:
                break

        return head + "\n" + tail_text

    @staticmethod
    def _clean_json_response(content: str) -> str:
        """清洗 LLM 输出的 JSON（去掉 markdown 代码块标记）。"""
        content = content.strip()
        # 去掉 ```json ... ``` 包裹
        if content.startswith("```"):
            content = re.sub(r"^```\w*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
        return content.strip()

    @staticmethod
    def _resolve_relative_url(url: str, platform: str) -> str:
        """补全相对 URL。"""
        if not url:
            return ""
        if url.startswith("http"):
            return url

        base_urls = {
            "boss": "https://www.zhipin.com",
            "liepin": "https://www.liepin.com",
            "lagou": "https://www.lagou.com",
            "zhaopin": "https://sou.zhaopin.com",
            "maimai": "https://maimai.cn",
            "51job": "https://we.51job.com",
        }
        base = base_urls.get(platform, "")
        if not base:
            return url
        return base + url if url.startswith("/") else f"{base}/{url}"
