"""
Captcha detection and solving via CapSolver API.

Detects captcha challenges in HTML/DOM and solves them automatically.
Gracefully degrades if no API key is configured.

Usage:
    from UniversalExtractor.captcha_solver import CaptchaSolver

    solver = CaptchaSolver()
    if solver.available:
        token = solver.solve_recaptcha_v2(site_key="...", url="https://...")
"""

from __future__ import annotations

import os
import re
import time
import json
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# Captcha detection patterns (no API needed)
# ============================================================

# HTML/DOM patterns that indicate a captcha challenge
CAPTCHA_PATTERNS = {
    "recaptcha_v2": [
        r'g-recaptcha"',
        r'google\.com/recaptcha',
        r'recaptcha/api\.js',
        r'data-sitekey="([^"]+)"',
        r"data-sitekey='([^']+)'",
    ],
    "hcaptcha": [
        r'h-captcha"',
        r'hcaptcha\.com/1/api\.js',
        r'data-sitekey="([^"]+)"',
    ],
    "cloudflare_turnstile": [
        r'challenges\.cloudflare\.com',
        r'cf-turnstile',
        r'turnstile/v0/api\.js',
    ],
    "cloudflare_challenge": [
        r'Checking your browser',
        r'cf-browser-verification',
        r'_cf_chl_opt',
    ],
    "generic_captcha": [
        r'请输入验证码',
        r'请完成验证',
        r'人机验证',
        r'verify[\s_-]?(you|human|bot)',
        r'are you a (human|robot)',
        r'captcha',
    ],
}

# Check order: specific patterns first, then generic
CAPTCHA_CHECK_ORDER = [
    "recaptcha_v2",
    "hcaptcha",
    "cloudflare_turnstile",
    "cloudflare_challenge",
    "generic_captcha",
]


@dataclass
class CaptchaResult:
    """Result of a captcha solving attempt."""

    detected: bool = False
    captcha_type: str = ""        # "recaptcha_v2", "hcaptcha", "cloudflare_turnstile", etc.
    site_key: str = ""
    token: str = ""
    solved: bool = False
    cost_usd: float = 0.0
    error: Optional[str] = None


class CaptchaSolver:
    """Detect and solve captcha challenges via CapSolver API.

    Set CAPSOLVER_API_KEY env var to enable solving.
    Detection works without API key.

    Pricing (CapSolver):
      - reCAPTCHA v2: ~$0.002/task
      - reCAPTCHA v3: ~$0.003/task
      - hCaptcha:      ~$0.002/task
      - Turnstile:     ~$0.001/task
    """

    API_URL = "https://api.capsolver.com"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("CAPSOLVER_API_KEY", "")
        self._task_count: int = 0
        self._total_cost: float = 0.0

    @property
    def available(self) -> bool:
        """Whether captcha solving is available (API key configured)."""
        return bool(self.api_key)

    # ----------------------------------------------------------------
    # Detection (no API needed)
    # ----------------------------------------------------------------

    @staticmethod
    def detect_captcha(html: str) -> CaptchaResult:
        """
        Detect if a page contains a captcha challenge.

        Args:
            html: Page HTML or DOM text

        Returns:
            CaptchaResult with detected=True and captcha_type filled in.
        """
        html_lower = html.lower()

        for captcha_type in CAPTCHA_CHECK_ORDER:
            patterns = CAPTCHA_PATTERNS.get(captcha_type, [])
            for pattern in patterns:
                m = re.search(pattern, html, re.IGNORECASE)
                if m:
                    site_key = m.group(1) if m.lastindex else ""
                    return CaptchaResult(
                        detected=True,
                        captcha_type=captcha_type,
                        site_key=site_key,
                    )

        return CaptchaResult(detected=False)

    @staticmethod
    def detect_captcha_js() -> str:
        """
        JavaScript snippet to detect captcha in browser DOM.
        Returns JS code that checks for common captcha elements.
        """
        return """(() => {
            // Check for common captcha elements
            var indicators = [];
            if (document.querySelector('.g-recaptcha, [data-sitekey], #recaptcha')) {
                indicators.push('recaptcha_v2');
            }
            if (document.querySelector('.h-captcha, iframe[src*="hcaptcha"]')) {
                indicators.push('hcaptcha');
            }
            if (document.querySelector('.cf-turnstile, iframe[src*="cloudflare"]')) {
                indicators.push('cloudflare_turnstile');
            }
            if (document.title && /captcha|验证|人机验证/i.test(document.title)) {
                indicators.push('title_captcha');
            }
            if (document.body && document.body.innerText &&
                /are you a (human|robot)|verify you are human|请输入验证码|请完成验证/i
                    .test(document.body.innerText.slice(0, 2000))) {
                indicators.push('text_captcha');
            }
            return JSON.stringify({
                detected: indicators.length > 0,
                types: indicators,
                siteKey: (document.querySelector('[data-sitekey]') || {}).getAttribute?.('data-sitekey') || ''
            });
        })()"""

    # ----------------------------------------------------------------
    # Solving (needs API key)
    # ----------------------------------------------------------------

    def solve_recaptcha_v2(
        self, site_key: str, page_url: str, invisible: bool = False
    ) -> CaptchaResult:
        """
        Solve reCAPTCHA v2 challenge.

        Args:
            site_key: Google reCAPTCHA site key (from data-sitekey attribute)
            page_url: URL of the page with the captcha
            invisible: Whether it's an invisible reCAPTCHA

        Returns:
            CaptchaResult with token on success.
        """
        if not self.available:
            return CaptchaResult(
                detected=True, captcha_type="recaptcha_v2",
                site_key=site_key, error="No CAPSOLVER_API_KEY configured",
            )

        task = {
            "type": "RecaptchaV2TaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "isInvisible": invisible,
        }
        return self._create_task(task, "recaptcha_v2", site_key)

    def solve_hcaptcha(
        self, site_key: str, page_url: str
    ) -> CaptchaResult:
        """
        Solve hCaptcha challenge.

        Args:
            site_key: hCaptcha site key
            page_url: URL of the page with the captcha

        Returns:
            CaptchaResult with token on success.
        """
        if not self.available:
            return CaptchaResult(
                detected=True, captcha_type="hcaptcha",
                site_key=site_key, error="No CAPSOLVER_API_KEY configured",
            )

        task = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
        return self._create_task(task, "hcaptcha", site_key)

    def solve_turnstile(
        self, site_key: str, page_url: str
    ) -> CaptchaResult:
        """
        Solve Cloudflare Turnstile challenge.
        (Backup for when StealthyFetcher's solve_cloudflare doesn't work)
        """
        if not self.available:
            return CaptchaResult(
                detected=True, captcha_type="cloudflare_turnstile",
                site_key=site_key, error="No CAPSOLVER_API_KEY configured",
            )

        task = {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
        return self._create_task(task, "cloudflare_turnstile", site_key)

    # ----------------------------------------------------------------
    # Internal
    # ----------------------------------------------------------------

    def _create_task(
        self, task: dict, captcha_type: str, site_key: str
    ) -> CaptchaResult:
        """Submit task to CapSolver, poll until done, return result."""
        try:
            import urllib.request

            # Step 1: Create task
            body = json.dumps({
                "clientKey": self.api_key,
                "task": task,
            }).encode()

            req = urllib.request.Request(
                f"{self.API_URL}/createTask",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                create_resp = json.loads(resp.read())

            if create_resp.get("errorId") != 0:
                error = create_resp.get("errorDescription", "Unknown error")
                logger.warning("CapSolver createTask failed: %s", error)
                return CaptchaResult(
                    detected=True, captcha_type=captcha_type,
                    site_key=site_key, error=error,
                )

            task_id = create_resp["taskId"]

            # Step 2: Poll for result
            for _ in range(30):  # max ~60s
                time.sleep(2)
                poll_body = json.dumps({
                    "clientKey": self.api_key,
                    "taskId": task_id,
                }).encode()
                poll_req = urllib.request.Request(
                    f"{self.API_URL}/getTaskResult",
                    data=poll_body,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(poll_req, timeout=10) as resp:
                    poll_resp = json.loads(resp.read())

                if poll_resp.get("status") == "ready":
                    solution = poll_resp.get("solution", {})
                    token = (
                        solution.get("gRecaptchaResponse")
                        or solution.get("token")
                        or solution.get("respKey")
                        or ""
                    )
                    cost = poll_resp.get("price", 0.002)
                    self._task_count += 1
                    self._total_cost += cost
                    logger.info(
                        "CapSolver solved %s (task #%d, $%.4f)",
                        captcha_type, self._task_count, cost,
                    )
                    return CaptchaResult(
                        detected=True, captcha_type=captcha_type,
                        site_key=site_key, token=token, solved=True,
                        cost_usd=cost,
                    )

                if poll_resp.get("errorId") != 0:
                    error = poll_resp.get("errorDescription", "Solve failed")
                    return CaptchaResult(
                        detected=True, captcha_type=captcha_type,
                        site_key=site_key, error=error,
                    )

            return CaptchaResult(
                detected=True, captcha_type=captcha_type,
                site_key=site_key, error="Timeout (>60s)",
            )

        except Exception as exc:
            return CaptchaResult(
                detected=True, captcha_type=captcha_type,
                site_key=site_key, error=str(exc)[:200],
            )
