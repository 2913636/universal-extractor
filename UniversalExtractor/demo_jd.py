"""
JD Engine 快速测试。

用法：
    py demo_jd.py                                          # 用默认 URL
    py demo_jd.py https://www.zhipin.com/job_detail/xxx    # 指定 URL
    py demo_jd.py --discover "AI Agent" --city 长沙          # 多平台搜索

前提：
    - 已安装 Scrapling：pip install scrapling[all]
    - 已设置 DEEPSEEK_API_KEY 环境变量（或在 D:/job-hunter/.env 中）
"""

import sys
import os

# Ensure the parent directory is importable
_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from universal_extractor import JDEngine

# ---- 默认测试 URL ----
DEFAULT_JD_URL = "https://www.zhipin.com/job_detail/fc09b8f258bcd8b81HBy2tW5FVZT.html"


def test_fetch(url: str):
    """测试单条 JD 抓取。"""
    print(f"Target: {url}")
    print("=" * 60)

    engine = JDEngine(headless=False)
    jd = engine.fetch_jd(url)

    print("=" * 60)
    print(f"Title:     {jd.title}")
    print(f"Company:   {jd.company}")
    print(f"Salary:    {jd.salary}")
    print(f"Location:  {jd.location}")
    print(f"Complete:  {jd.is_complete}")
    print(f"Raw chars: {jd.raw_length}")
    print(f"Requirements ({len(jd.requirements)}):")
    for r in jd.requirements:
        print(f"  - {r}")
    if jd.tech_stack:
        print(f"Tech stack: {', '.join(jd.tech_stack)}")
    print()
    print("--- Full text for matching engine ---")
    print(jd.to_text()[:2000])


def test_discover(keyword: str, city: str):
    """测试多平台搜索。"""
    print(f"Search: {keyword} @ {city}")
    print("=" * 60)

    engine = JDEngine(headless=False)
    jobs = engine.discover(keyword, city=city, platforms=["boss", "liepin"])

    print("=" * 60)
    print(f"Found {len(jobs)} jobs:")
    for job in jobs[:10]:
        print(f"  [{job['platform']}] {job.get('title', '?')[:50]} | {job.get('company', '?')[:30]}")
        print(f"         {job.get('url', '')[:100]}")
    if len(jobs) > 10:
        print(f"  ... and {len(jobs) - 10} more")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--discover":
        keyword = sys.argv[2] if len(sys.argv) > 2 else "AI Agent"
        city = sys.argv[4] if len(sys.argv) > 4 and sys.argv[3] == "--city" else "长沙"
        test_discover(keyword, city)
    else:
        url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_JD_URL
        test_fetch(url)
