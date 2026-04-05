import json
import os
import sys
import traceback

import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── 설정 ──────────────────────────────────────────────
CLASSROOM_URL = os.getenv(
    "CLASSROOM_URL",
    "https://schools.duolingo.com/classroom/7349589/students",
)
LOGIN_URL = "https://schools.duolingo.com/login"

DUOLINGO_EMAIL = os.getenv("DUOLINGO_EMAIL")
DUOLINGO_PASSWORD = os.getenv("DUOLINGO_PASSWORD")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

MAX_RETRIES = 2


# ── 로그인 ─────────────────────────────────────────────
def login(page):
    """Duolingo Schools에 로그인한다."""
    print("[1/3] Duolingo Schools 로그인 페이지 접속...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2_000)
    page.screenshot(path="screenshot_01_login_page.png", full_page=True)
    print(f"       현재 페이지: {page.url}")

    # 이메일 입력
    email_input = page.locator(
        'input[placeholder*="Email"], input[placeholder*="email"], '
        'input[data-test="email-input"], input[type="email"]'
    ).first
    email_input.wait_for(state="visible", timeout=15_000)
    email_input.fill(DUOLINGO_EMAIL)
    print("       이메일 입력 완료")

    # 비밀번호 입력
    pwd_input = page.locator(
        'input[placeholder*="Password"], input[placeholder*="password"], '
        'input[type="password"]'
    ).first
    pwd_input.wait_for(state="visible", timeout=15_000)
    pwd_input.fill(DUOLINGO_PASSWORD)
    print("       비밀번호 입력 완료")

    # 로그인 버튼 클릭
    submit_btn = page.locator(
        'button:has-text("LOG IN"), button:has-text("Log in"), '
        'button:has-text("로그인"), button[type="submit"]'
    ).first
    submit_btn.click()
    print("       로그인 버튼 클릭...")

    # 로그인 완료 대기
    page.wait_for_timeout(5_000)
    page.screenshot(path="screenshot_02_after_login.png", full_page=True)

    # 로그인 성공 검증
    current_url = page.url
    if "login" in current_url.lower():
        raise Exception(f"로그인 실패: 여전히 로그인 페이지에 있음 ({current_url})")

    print(f"       로그인 성공 → {current_url}")


# ── API 응답에서 학생 데이터 추출 ──────────────────────────
def extract_students_from_responses(captured):
    """캡처된 API JSON 응답들에서 학생 이름+XP 데이터를 추출한다."""
    # 학생 데이터를 포함할 가능성이 있는 키 이름들
    list_keys = ["students", "members", "users", "learners", "data", "results", "items"]
    name_keys = ["name", "displayName", "display_name", "username", "fullName", "full_name"]
    xp_keys = ["xp", "totalXp", "total_xp", "points", "score", "xpGained", "xp_gained"]

    for resp in captured:
        data = resp["data"]

        # data가 딕셔너리인 경우: 알려진 키에서 배열 탐색
        if isinstance(data, dict):
            candidates = []
            for key in list_keys:
                if key in data and isinstance(data[key], list) and len(data[key]) > 0:
                    candidates.append((key, data[key]))

            # 키를 못 찾았으면 딕셔너리의 모든 값 중 배열인 것 시도
            if not candidates:
                for key, val in data.items():
                    if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                        candidates.append((key, val))

            for key, items in candidates:
                students = _try_extract_from_list(items, name_keys, xp_keys)
                if students:
                    print(f"       [추출 성공] 키: '{key}', URL: {resp['url']}")
                    return students

        # data가 리스트인 경우: 직접 학생 리스트일 수 있음
        elif isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            students = _try_extract_from_list(data, name_keys, xp_keys)
            if students:
                print(f"       [추출 성공] 최상위 배열, URL: {resp['url']}")
                return students

    return []


def _try_extract_from_list(items, name_keys, xp_keys):
    """항목 리스트에서 이름+XP 쌍을 추출 시도한다."""
    students = []
    for item in items:
        if not isinstance(item, dict):
            continue

        name = None
        xp = None

        # 이름 찾기
        for nk in name_keys:
            if nk in item and item[nk]:
                name = str(item[nk]).strip()
                break

        # XP 찾기
        for xk in xp_keys:
            if xk in item:
                try:
                    xp = int(item[xk])
                    break
                except (ValueError, TypeError):
                    continue

        # 중첩 구조에서 XP 찾기 (예: item["progress"]["xp"])
        if xp is None:
            for sub_key in ["progress", "stats", "statistics", "course"]:
                if sub_key in item and isinstance(item[sub_key], dict):
                    for xk in xp_keys:
                        if xk in item[sub_key]:
                            try:
                                xp = int(item[sub_key][xk])
                                break
                            except (ValueError, TypeError):
                                continue
                    if xp is not None:
                        break

        if name and xp is not None:
            students.append({"name": name, "xp": xp})

    # 최소 2명 이상이어야 유효한 학생 데이터로 판단
    return students if len(students) >= 2 else []


# ── DOM 스크래핑 폴백 ──────────────────────────────────────
def scrape_from_dom(page):
    """API 인터셉션 실패 시 다양한 셀렉터로 DOM 스크래핑을 시도한다."""
    selectors = [
        "tbody tr",
        'div[role="row"]',
        'tr[class*="student"], tr[class*="member"], tr[class*="user"]',
        'div[class*="student"], div[class*="member"], div[class*="row"]',
    ]

    for selector in selectors:
        try:
            count = page.locator(selector).count()
            if count > 0:
                print(f"       [DOM] 셀렉터 '{selector}'로 {count}개 요소 발견")
                break
        except Exception:
            continue
    else:
        # 모든 셀렉터 실패 시 페이지 구조 디버깅
        html_snippet = page.evaluate("() => document.body ? document.body.innerHTML.substring(0, 3000) : 'NO BODY'")
        print(f"       [DOM] 모든 셀렉터 실패. 페이지 HTML 스니펫:")
        print(f"       {html_snippet[:2000]}")
        return []

    # 기존 tbody tr 방식 시도
    students = page.evaluate(
        r"""
        () => {
            const results = [];

            for (const row of document.querySelectorAll("tbody tr")) {
                const cells = [...row.querySelectorAll("td")];
                if (cells.length >= 2) {
                    const name = cells[0].textContent.trim();
                    const xpText = cells[1].textContent.trim();
                    const xpMatch = xpText.match(/([\d,]+)\s*(?:EXP|XP)/i);

                    if (name && xpMatch) {
                        results.push({
                            name: name,
                            xp: parseInt(xpMatch[1].replace(/,/g, "")),
                        });
                    }
                }
            }

            // tbody tr에서 못 찾았으면 div role=row 시도
            if (results.length === 0) {
                for (const row of document.querySelectorAll('[role="row"]')) {
                    const cells = [...row.querySelectorAll('[role="cell"], [role="gridcell"]')];
                    if (cells.length >= 2) {
                        const name = cells[0].textContent.trim();
                        const xpText = cells[1].textContent.trim();
                        const xpMatch = xpText.match(/([\d,]+)\s*(?:EXP|XP)/i);
                        if (name && xpMatch) {
                            results.push({
                                name: name,
                                xp: parseInt(xpMatch[1].replace(/,/g, "")),
                            });
                        }
                    }
                }
            }

            // 여전히 없으면 숫자+XP 패턴을 페이지 전체에서 탐색
            if (results.length === 0) {
                const allText = document.body.innerText;
                const lines = allText.split('\n').filter(l => l.trim());
                for (const line of lines) {
                    const match = line.match(/^(.+?)\s+([\d,]+)\s*(?:EXP|XP)/i);
                    if (match) {
                        results.push({
                            name: match[1].trim(),
                            xp: parseInt(match[2].replace(/,/g, "")),
                        });
                    }
                }
            }

            return results;
        }
        """
    )

    return students


# ── 스크래핑 (API 인터셉션 + DOM 폴백) ─────────────────────
def scrape_leaderboard(page):
    """교실 페이지에서 API 응답을 인터셉트하여 학생 데이터를 추출한다."""
    print(f"[2/3] 교실 페이지 접속: {CLASSROOM_URL}")

    captured = []

    def on_response(response):
        url = response.url
        content_type = response.headers.get("content-type", "")
        if response.status == 200 and "json" in content_type:
            try:
                data = response.json()
                captured.append({"url": url, "data": data})
                print(f"       [API] {response.request.method} {url}")
            except Exception:
                pass

    page.on("response", on_response)
    page.goto(CLASSROOM_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(10_000)  # SPA 렌더링 및 API 호출 대기
    page.screenshot(path="screenshot_03_classroom.png", full_page=True)

    print(f"       캡처된 API 응답 수: {len(captured)}")

    # 1차: API 응답에서 학생 데이터 추출
    students = extract_students_from_responses(captured)

    # 2차: DOM 스크래핑 폴백
    if not students:
        print("       API 인터셉션에서 학생 데이터 미발견, DOM 폴백 시도...")
        students = scrape_from_dom(page)

    # 디버그: 모든 방법 실패 시 캡처된 API 응답 구조 출력
    if not students:
        print("       === 디버그: 캡처된 API 응답 구조 ===")
        for i, r in enumerate(captured):
            data = r["data"]
            if isinstance(data, dict):
                # 각 키의 값 타입과 길이 출력
                summary = {}
                for k, v in data.items():
                    if isinstance(v, list):
                        summary[k] = f"list[{len(v)}]"
                        if v and isinstance(v[0], dict):
                            summary[k] += f" keys={list(v[0].keys())[:8]}"
                    elif isinstance(v, dict):
                        summary[k] = f"dict keys={list(v.keys())[:8]}"
                    else:
                        summary[k] = f"{type(v).__name__}={str(v)[:50]}"
                print(f"       [{i}] {r['url']}")
                print(f"           {json.dumps(summary, ensure_ascii=False, default=str)}")
            elif isinstance(data, list):
                print(f"       [{i}] {r['url']}")
                print(f"           list[{len(data)}]", end="")
                if data and isinstance(data[0], dict):
                    print(f" keys={list(data[0].keys())[:8]}")
                else:
                    print()
            else:
                print(f"       [{i}] {r['url']} → {type(data).__name__}")
        print("       === 디버그 끝 ===")

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

    # 월요일(0)이면 주간 리더보드, 그 외엔 일일 리더보드
    if now.weekday() == 0:
        title = "🏆 **이번 주 듀오링고 리더보드** 🏆"
    else:
        title = "🏆 **오늘의 듀오링고 리더보드** 🏆"

    medals = ["🥇", "🥈", "🥉"]
    lines = [
        title,
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

        for attempt in range(MAX_RETRIES + 1):
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                locale="ko-KR",
            )
            page = context.new_page()

            try:
                login(page)
                students = scrape_leaderboard(page)

                if not students:
                    raise Exception("학생 데이터를 찾을 수 없습니다.")

                message = format_message(students)
                print(f"\n{message}\n")
                send_to_discord(message)
                break  # 성공

            except Exception as exc:
                try:
                    page.screenshot(path="screenshot_error.png", full_page=True)
                except Exception:
                    pass

                if attempt < MAX_RETRIES:
                    print(f"\n       재시도 {attempt + 1}/{MAX_RETRIES} (오류: {exc})")
                    context.close()
                    continue
                else:
                    print(f"Error: 최대 재시도 횟수 초과. 마지막 오류: {exc}")
                    traceback.print_exc()
                    browser.close()
                    sys.exit(1)

            finally:
                context.close()

        browser.close()


if __name__ == "__main__":
    main()
