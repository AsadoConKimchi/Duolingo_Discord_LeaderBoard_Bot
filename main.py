import os
import sys

import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── 설정 ──────────────────────────────────────────────
CLASSROOM_URL = os.getenv(
    "CLASSROOM_URL",
    "https://schools.duolingo.com/classroom/7349589/students",
)
LOGIN_URL = "https://www.duolingo.com/log-in"

DUOLINGO_EMAIL = os.getenv("DUOLINGO_EMAIL")
DUOLINGO_PASSWORD = os.getenv("DUOLINGO_PASSWORD")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")


# ── 로그인 ─────────────────────────────────────────────
def login(page):
    """듀오링고 메인 사이트에서 이메일/비밀번호로 로그인한다."""
    print("[1/3] 듀오링고 로그인 페이지 접속...")
    page.goto(LOGIN_URL, wait_until="networkidle")

    # 이메일 입력
    email_input = page.locator(
        'input[data-test="email-input"], input[type="email"]'
    ).first
    email_input.wait_for(state="visible", timeout=15_000)
    email_input.fill(DUOLINGO_EMAIL)

    # 비밀번호 입력
    pwd_input = page.locator(
        'input[data-test="password-input"], input[type="password"]'
    ).first
    pwd_input.wait_for(state="visible", timeout=15_000)
    pwd_input.fill(DUOLINGO_PASSWORD)

    # 로그인 버튼 클릭
    page.locator(
        'button[data-test="register-button"], button[type="submit"]'
    ).first.click()

    # 로그인 완료 대기
    try:
        page.wait_for_url("**/learn**", timeout=30_000)
    except PlaywrightTimeout:
        # 다른 페이지로 리다이렉트될 수도 있으므로 잠시 대기
        page.wait_for_timeout(5_000)

    print(f"       로그인 완료 → {page.url}")


# ── 스크래핑 ────────────────────────────────────────────
def scrape_leaderboard(page):
    """교실 페이지에서 학생 이름과 XP를 추출한다."""
    print(f"[2/3] 교실 페이지 접속: {CLASSROOM_URL}")
    page.goto(CLASSROOM_URL, wait_until="networkidle")
    page.wait_for_timeout(5_000)  # SPA 렌더링 대기

    # 디버그용 스크린샷
    page.screenshot(path="screenshot_classroom.png", full_page=True)

    students = page.evaluate(
        r"""
        () => {
            const results = [];

            // ── 전략 1: <table> 기반 추출 ──
            for (const row of document.querySelectorAll("tbody tr")) {
                const cells = [...row.querySelectorAll("td")];
                const texts = cells.map(c => c.textContent.trim());
                // "이름", "1,234" 형태의 셀 쌍 탐색
                for (let i = 1; i < texts.length; i++) {
                    const xpMatch = texts[i].match(/^([\d,]+)$/);
                    if (xpMatch) {
                        const name = texts[i - 1].replace(/^\d+\.?\s*/, "");
                        if (name) {
                            results.push({
                                name,
                                xp: parseInt(xpMatch[1].replace(/,/g, "")),
                            });
                        }
                        break;
                    }
                }
            }
            if (results.length > 0) return results;

            // ── 전략 2: 리스트/카드 기반 추출 ──
            const candidates = document.querySelectorAll(
                '[class*="student"], [class*="member"], [class*="row"], [class*="item"], li'
            );
            for (const el of candidates) {
                const text = el.textContent.trim();
                const m = text.match(/([\w\s\u3131-\uD79D._-]+?)\s+(\d[\d,]*)\s*(?:XP)?$/i);
                if (m) {
                    const name = m[1].trim();
                    const xp = parseInt(m[2].replace(/,/g, ""));
                    if (name && xp > 0) results.push({ name, xp });
                }
            }
            if (results.length > 0) return results;

            // ── 전략 3: 페이지 전체 텍스트에서 정규식 추출 ──
            const body = document.body.innerText;
            const re = /^(.+?)\s+(\d[\d,]*)\s*XP\s*$/gim;
            let match;
            while ((match = re.exec(body)) !== null) {
                results.push({
                    name: match[1].trim(),
                    xp: parseInt(match[2].replace(/,/g, "")),
                });
            }
            return results;
        }
        """
    )

    # 중복 제거 & XP 내림차순 정렬
    seen, unique = set(), []
    for s in students:
        if s["name"] not in seen:
            seen.add(s["name"])
            unique.append(s)
    unique.sort(key=lambda x: x["xp"], reverse=True)

    print(f"       학생 {len(unique)}명 발견")
    return unique


# ── 디스코드 전송 ───────────────────────────────────────
def format_message(students):
    """리더보드 데이터를 디스코드 메시지 문자열로 변환한다."""
    kst = ZoneInfo("Asia/Seoul")
    now = datetime.now(kst)
    date_str = now.strftime("%Y년 %m월 %d일 %H:%M")

    medals = ["🥇", "🥈", "🥉"]
    lines = [
        "🏆 **이번 주 듀오링고 리더보드** 🏆",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    for i, s in enumerate(students):
        prefix = medals[i] if i < 3 else f"**{i + 1}.**"
        lines.append(f"{prefix} {s['name']} - {s['xp']:,} XP")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📅 집계: {date_str}")
    return "\n".join(lines)


def send_to_discord(message):
    """디스코드 웹훅으로 메시지를 전송한다."""
    print("[3/3] 디스코드 전송 중...")
    resp = requests.post(
        DISCORD_WEBHOOK_URL, json={"content": message}, timeout=30
    )
    resp.raise_for_status()
    print("       전송 완료!")


# ── 메인 ───────────────────────────────────────────────
def main():
    missing = [
        name
        for name, val in [
            ("DUOLINGO_EMAIL", DUOLINGO_EMAIL),
            ("DUOLINGO_PASSWORD", DUOLINGO_PASSWORD),
            ("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL),
        ]
        if not val
    ]
    if missing:
        print(f"Error: 환경 변수 누락 → {', '.join(missing)}")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="ko-KR",
        )
        page = context.new_page()

        try:
            login(page)
            students = scrape_leaderboard(page)

            if not students:
                print("Error: 학생 데이터를 찾을 수 없습니다.")
                page.screenshot(path="screenshot_error.png", full_page=True)
                print("       screenshot_error.png 에 현재 페이지 저장됨")
                sys.exit(1)

            message = format_message(students)
            print(f"\n{message}\n")
            send_to_discord(message)

        except Exception as exc:
            print(f"Error: {exc}")
            try:
                page.screenshot(path="screenshot_error.png", full_page=True)
                print("       screenshot_error.png 에 현재 페이지 저장됨")
            except Exception:
                pass
            raise

        finally:
            browser.close()


if __name__ == "__main__":
    main()
