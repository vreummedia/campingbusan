# -*- coding: utf-8 -*-
from __future__ import annotations

from flask import Flask, render_template, request, redirect, url_for
import os
import re
import time
import requests
import tempfile
import shutil
from uuid import uuid4

from bs4 import BeautifulSoup
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

# Selenium
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium.common.exceptions import UnexpectedAlertPresentException
from selenium.common.exceptions import WebDriverException

app = Flask(
    __name__,
    static_folder='image',        # /static → 프로젝트의 image 폴더를 바로 가리킴
    static_url_path='/static'
)

DISABLE_SCRAPERS = os.getenv("DISABLE_SCRAPERS") == "1"




from flask import jsonify
from threading import Thread, Lock, Semaphore

YEONGDO_CACHE = {}          # date -> (data, ts)
YEONGDO_LOCK = Lock()
YEONGDO_TTL = 60

# 동시에 여러 개 안 띄우도록 (기본 1~2)
SELENIUM_SEM = Semaphore(int(os.getenv("YEONGDO_MAX_CONCURRENCY", "2")))

# date(str) -> {"ts": float, "ticks": int}
INFLIGHT = {}
INFLIGHT_MAX = int(os.getenv("YEONGDO_INFLIGHT_MAX_SEC", "100"))  # 오래 걸리면 자동 리셋
PROGRESS_MAX = 60  # (1/60) 표기를 위해

def _cache_get(cache, key, ttl):
    rec = cache.get(key)
    if not rec: return None
    data, ts = rec
    if time.time() - ts > ttl:
        cache.pop(key, None)
        return None
    return data

def _cache_set(cache, key, data):
    cache[key] = (data, time.time())

def _progress_ticker(date_key: str):
    """INFLIGHT[date_key]['ticks'] 를 1초마다 올려서 (n/60) 표시 가능하게."""
    try:
        while True:
            with YEONGDO_LOCK:
                rec = INFLIGHT.get(date_key)
                if not rec:
                    return
                rec["ticks"] = min(PROGRESS_MAX, rec.get("ticks", 0) + 1)
                INFLIGHT[date_key] = rec
            time.sleep(1.0)
    except Exception:
        return

def _yeongdo_worker(d, page_url):
    data = None
    ticker = None
    try:
        # 진행률 티커 시작
        ticker = Thread(target=_progress_ticker, args=(d,), daemon=True)
        ticker.start()

        with SELENIUM_SEM:
            data = fetch_yeongdo(d, page_url)
    except Exception as e:
        print(f"[yeongdo][{d}] worker error:", repr(e), flush=True)
        data = {"error": f"크롤링 실패: {e}"}
    finally:
        if not data:
            data = {"caravan":{"available":[], "unavailable":[]},
                    "auto":{"available":[], "unavailable":[]},
                    "general":{"available":[], "unavailable":[]}}
        with YEONGDO_LOCK:
            _cache_set(YEONGDO_CACHE, d, data)
            INFLIGHT.pop(d, None)  # 끝났으니 inflight 제거


@app.route("/api/yeongdo")
def api_yeongdo():
    d = request.args.get("date") or date.today().strftime("%Y-%m-%d")
    page_url = CAMPING_TABS["yeongdo"]["url_page"]

    with YEONGDO_LOCK:
        cached = _cache_get(YEONGDO_CACHE, d, YEONGDO_TTL)
        if cached is not None:
            return jsonify({"status": "ready", "date": d, "data": cached})

        # 오래된 inflight 강제 정리
        now = time.time()
        rec = INFLIGHT.get(d)
        if rec and (now - rec.get("ts", now)) > INFLIGHT_MAX:
            INFLIGHT.pop(d, None)
            rec = None

        if rec:  # 진행 중
            tries = int(rec.get("ticks", 0))
            return jsonify({"status": "pending", "date": d, "tries": tries, "max": PROGRESS_MAX})

        # 새 작업 등록
        INFLIGHT[d] = {"ts": time.time(), "ticks": 0}

    t = Thread(target=_yeongdo_worker, args=(d, page_url), daemon=True)
    t.start()
    return jsonify({"status": "pending", "date": d, "tries": 0, "max": PROGRESS_MAX})


# === Gudeok polling cache ===
GUDEOK_CACHE = {}      # date -> (data, ts)
GUDEOK_LOCK = Lock()
GUDEOK_TTL  = 60
GUDEOK_INFLIGHT = {}   # date -> {"ts": float, "ticks": int}

def _progress_ticker_gudeok(date_key: str):
    try:
        while True:
            with GUDEOK_LOCK:
                rec = GUDEOK_INFLIGHT.get(date_key)
                if not rec:
                    return
                rec["ticks"] = min(PROGRESS_MAX, rec.get("ticks", 0) + 1)
                GUDEOK_INFLIGHT[date_key] = rec
            time.sleep(1.0)
    except Exception:
        return

def _gudeok_worker(d: str, page_url: str):
    data = None
    ticker = Thread(target=_progress_ticker_gudeok, args=(d,), daemon=True)
    ticker.start()
    try:
        with SELENIUM_SEM:
            data = fetch_gudeok_sites_with_retry(selected_date=d, page_url=page_url)
    except Exception as e:
        print(f"[gudeok][{d}] worker error:", repr(e), flush=True)
        data = {"error": f"크롤링 실패: {e}"}
    finally:
        if not data:
            data = {"deck":{"available":[], "unavailable":[], "num_available":0, "num_unavailable":0, "total":0}}
        with GUDEOK_LOCK:
            _cache_set(GUDEOK_CACHE, d, data)
            GUDEOK_INFLIGHT.pop(d, None)


@app.route("/api/gudeok")
def api_gudeok():
    d = request.args.get("date") or date.today().strftime("%Y-%m-%d")
    page_url = CAMPING_TABS["gudeok"]["url_page"]

    with GUDEOK_LOCK:
        cached = _cache_get(GUDEOK_CACHE, d, GUDEOK_TTL)
        if cached is not None:
            return jsonify({"status":"ready","date":d,"data":cached})

        now = time.time()
        rec = GUDEOK_INFLIGHT.get(d)
        if rec and (now - rec.get("ts", now)) > INFLIGHT_MAX:
            GUDEOK_INFLIGHT.pop(d, None)
            rec = None

        if rec:
            tries = int(rec.get("ticks", 0))
            return jsonify({"status":"pending","date":d,"tries":tries,"max":PROGRESS_MAX})

        GUDEOK_INFLIGHT[d] = {"ts": time.time(), "ticks": 0}

    Thread(target=_gudeok_worker, args=(d, page_url), daemon=True).start()
    return jsonify({"status":"pending","date":d,"tries":0,"max":PROGRESS_MAX})


# ─────────────────────────────────────────────────────────

def _accept_any_alert(driver, timeout=2):
    try:
        WebDriverWait(driver, timeout).until(EC.alert_is_present())
        a = driver.switch_to.alert
        txt = a.text
        a.accept()
        return txt or ""
    except TimeoutException:
        return ""

def _new_driver(headless: bool = True, window: str = "1280,1600") -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--lang=ko-KR")
    opts.add_argument(f"--window-size={window}")
    opts.add_argument("--remote-debugging-port=0")

    # 안정화 플래그 보강
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--disable-features=TranslateUI")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-timer-throttling")
    opts.add_argument("--disable-backgrounding-occluded-windows")
    opts.add_argument("--disable-renderer-backgrounding")

    # UA (그대로)
    opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    # 실행마다 고유 프로필/캐시 디렉토리(이미 적용한 구조 유지)
    import tempfile, shutil, os
    profile_dir = tempfile.mkdtemp(prefix="chrome-profile-")
    data_dir = os.path.join(profile_dir, "data")
    cache_dir = os.path.join(profile_dir, "cache")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument(f"--data-path={data_dir}")
    opts.add_argument(f"--disk-cache-dir={cache_dir}")

    chrome_bin = os.environ.get("GOOGLE_CHROME_BIN")
    if chrome_bin:
        opts.binary_location = chrome_bin

    driver_path = os.environ.get("CHROMEDRIVER_PATH")
    if driver_path:
        from selenium.webdriver.chrome.service import Service
        service = Service(executable_path=driver_path)
        driver = webdriver.Chrome(service=service, options=opts)
    else:
        driver = webdriver.Chrome(options=opts)

    driver.set_page_load_timeout(20)
    driver.set_script_timeout(20)

    driver.temp_profile_dir = profile_dir
    return driver



# 어딘가 공용 utils 근처에
def _dismiss_alert_if_any(driver):
    try:
        al = driver.switch_to.alert
        al.dismiss()
    except Exception:
        pass


# ── 각 탭별 지도/요금표 데이터 ─────────────────────────
def build_media(key: str):
    """
    탭별 지도 이미지 경로 + 요금표 메타데이터를 한 곳에서 반환.
    템플릿은 camp.media.image_url / .price_table / .price_note만 사용.
    """
    image_url = url_for('static', filename=f"map_{key}.png")
    
    price_table = {"columns": [], "rows": []}
    price_note = None

    if key == "samnak":
        price_table = {
            "columns": ["평일", "주말"],
            "rows": [
                {"label": "오토 캠핑 SITE", "cols": {"평일": "30,000원", "주말": "35,000원"}, "color": "#DF846D"},
                {"label": "일반 캠핑 SITE", "cols": {"평일": "20,000원", "주말": "25,000원"}, "color": "#AC81B4"},
            ],
        }
    elif key == "daejeo":
        price_table = {
            "columns": ["평일", "주말"],
            "rows": [
                {"label": "A구역 (5x8)",   "cols": {"평일": "23,000원", "주말": "28,000원"}, "color": "#DF846D"},
                {"label": "B구역 (12x12)","cols": {"평일": "35,000원", "주말": "40,000원"}, "color": "#73ABF7"},
                {"label": "C구역 (10x12)","cols": {"평일": "32,000원", "주말": "37,000원"}, "color": "#D47EF1"},
                {"label": "D구역 (10x10)","cols": {"평일": "30,000원", "주말": "35,000원"}, "color": "#ECEE4E"},
            ],
        }
    elif key == "hwamyeong":
        price_table = {
            "columns": ["평일", "주말"],
            "rows": [
                {"label": "전 구역", "cols": {"평일": "30,000원", "주말": "35,000원"}, "color": "#DF846D"},
            ],
        }
    elif key == "yeongdo":
        price_table = {
            "columns": ["평일", "주말", "성수기"],
            "rows": [
                {"label": "카라반 (6인용)", "cols": {"평일": "120,000원", "주말": "140,000원", "성수기": "160,000원"}, "color": "#623ECA"},
                {"label": "카라반 (4인용)", "cols": {"평일": "100,000원", "주말": "120,000원", "성수기": "140,000원"}, "color": "#8979E4"},
                {"label": "오토",           "cols": {"평일": "30,000원",  "주말": "35,000원",  "성수기": "40,000원"},  "color": "#DF846D"},
                {"label": "일반",           "cols": {"평일": "20,000원",  "주말": "25,000원",  "성수기": "30,000원"},  "color": "#AC81B4"},
            ],
        }
        price_note = "성수기: 7~8월 / 비수기: 그 외 기간"
    elif key == "busan_port":
        price_table = {
            "columns": ["요금"],
            "rows": [
                {"label": "데크", "cols": {"요금": "25,000원"}, "color": "#AC81B4"},
                {"label": "오토", "cols": {"요금": "30,000원"}, "color": "#DF846D"},
            ],
        }
    elif key == "gudeok":
        price_table = {
            "columns": ["요금"],
            "rows": [
                {"label": "4인 이하",   "cols": {"요금": "10,000원"}, "color": "#A47D5C"},
                {"label": "5~9인 이하", "cols": {"요금": "20,000원"}, "color": "#835A37"},
            ],
        }

    return {
        "image_url": image_url,
        "price_table": price_table,
        "price_note": price_note,
    }


# ===== 탭 정의 =====
CAMPING_TABS = {
    "all": { "name": "전체", "is_all": True },   # ✅ 추가 (맨 위에)
    "samnak": {
        "name": "삼락",
        "url_base": (
            "https://www.nakdongcamping.com/reservation/real_time?"
            "user_id=&site_id=&site_type=&site_name=&dis_rate=0&user_dis_rate=&reqcode=&reqname=&reqphone=&"
            "reservation_type=0&resdate={}&schGugun=1&price=0&bagprice=2000&allprice=0&percnt=0&g-recaptcha-response="
        ),
        "is_hwamyung": False,
    },
    "daejeo": {
        "name": "대저",
        "url_base": (
            "https://www.daejeocamping.com/reservation/real_time?"
            "user_id=&site_id=&site_type=&site_name=&dis_rate=0&user_dis_rate=&reqcode=&reqname=&reqphone=&"
            "reservation_type=0&resdate={}&schGugun=1&price=0&bagprice=2000&allprice=0&percnt=0&g-recaptcha-response="
        ),
        "is_hwamyung": False,
    },
    "hwamyeong": {
        "name": "화명",
        "url_base": (
            "https://hwamyungcamping.com/reservation/real_time?"
            "user_id=&site_id=&site_type=&site_name=&dis_rate=0&user_dis_rate=&reqcode=&reqname=&reqphone=&"
            "reservation_type=0&resdate={}&schGugun=1&price=0&bagprice=2000&allprice=0&percnt=0&g-recaptcha-response="
        ),
        "is_hwamyung": True,
    },
    "yeongdo": {
        "name": "영도",
        "url_page": "https://www.yeongdo.go.kr/marinocamping/00003/00015/00028.web",
        "is_yeongdo": True,
    },
    "busan_port": {
        "name": "부산항",
        "url_page": "https://www.busanpa.com/redevelopment/Board.do?mCode=MN0082",
        "is_busan_port": True,
    },
    'gudeok': {
        'name': '구덕',
        'url_page': "https://gudeok.go.kr/rent_camp01.php",
        'is_gudeok': True
    }
}

def _wait_until_interpark_main(driver, wait, max_secs: int = 25) -> bool:
    """인터파크 대기열/중간페이지를 거쳐 최종 BookMain.asp로 진입할 때까지 대기"""
    t0 = time.time()
    while time.time() - t0 < max_secs:
        url = driver.current_url
        if "PCampingBook/BookMain.asp" in url:
            return True
        # 대기열이거나 중간 redirect면 잠깐 쉼
        time.sleep(0.6)
    return "PCampingBook/BookMain.asp" in driver.current_url

def _interpark_close_notice(driver):
    """상단 공지 닫기: javascript:fnBookNoticeShowHide('') 호출"""
    try:
        driver.execute_script("if (typeof fnBookNoticeShowHide==='function') fnBookNoticeShowHide('');")
        time.sleep(0.2)
    except Exception:
        pass

def _interpark_pick_date(driver, wait, target: str):
    """
    달력에서 날짜 선택.
    target: 'YYYY-MM-DD'
    - 인터파크는 달력 셀들이 대부분 a/span으로 날짜 숫자만 들어있거나 data- 속성이 없음.
    - 월 이동 버튼: '다음달', '이전달' 등 다양한 케이스 대비.
    """
    y, m, d = map(int, target.split("-"))
    # 월 네비게이션을 18회 한도로 시도 (약 1.5년 범위)
    for _ in range(18):
        # 현재 달력이 target 년/월인지 텍스트로 유추
        head_texts = [e.text.strip() for e in driver.find_elements(By.CSS_SELECTOR, ".cal, .calendar, .date, .month, .ui-datepicker-title, .dateTit, .date_top")]
        head = " ".join(head_texts)
        # YYYY, MM 두 값이 머리글 어딘가 들어있는지 느슨하게 체크
        if str(y) in head and (f"{m}월" in head or f"{m:02d}" in head):
            # 해당 월에서 날짜 클릭
            # 1) data-date, data-day 같은 경우
            cells = driver.find_elements(By.CSS_SELECTOR, f'[data-date="{target}"], [data-day="{d}"]')
            # 2) 일반 td/a 안에 숫자만 있는 경우
            if not cells:
                cells = [el for el in driver.find_elements(By.CSS_SELECTOR, "td a, td, .cal a, .calendar a") if el.text.strip() == str(d)]
            if cells:
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cells[0])
                    cells[0].click()
                except Exception:
                    driver.execute_script("arguments[0].click();", cells[0])
                time.sleep(0.3)
                return True
        # 다음달 버튼 시도
        for sel in [
            "a[title*='다음']", "button[title*='다음']", ".ui-datepicker-next", ".month-next",
            ".btn.next", "a.next", "button.next", ".cal-next"
        ]:
            btns = driver.find_elements(By.CSS_SELECTOR, sel)
            if btns:
                try:
                    btns[0].click()
                except Exception:
                    driver.execute_script("arguments[0].click();", btns[0])
                time.sleep(0.35)
                break
        else:
            # 버튼 못 찾으면 JS 훅 시도
            driver.execute_script("""
                if (typeof goMonth==='function') { goMonth(1); }
                else if (typeof nextMonth==='function') { nextMonth(); }
            """)
            time.sleep(0.35)
    return False

def _interpark_select_period(driver, visible_text="1박 2일"):
    """이용기간 드롭다운에서 '1박 2일' 선택 (Select 태그/커스텀 UI 모두 대응 시도)"""
    try:
        # 표준 select 우선
        from selenium.webdriver.support.ui import Select
        selects = driver.find_elements(By.TAG_NAME, "select")
        for s in selects:
            try:
                Select(s).select_by_visible_text(visible_text)
                time.sleep(0.2)
                return True
            except Exception:
                continue
    except Exception:
        pass
    # 커스텀 드롭다운(버튼/LI) 케이스
    try:
        # 드롭다운 열기: '이용기간' 텍스트 인접 버튼 탐색
        triggers = driver.find_elements(By.XPATH, "//*[contains(text(),'이용기간')]/following::button[1] | //button[contains(.,'이용기간')]")
        if triggers:
            try:
                triggers[0].click()
            except Exception:
                driver.execute_script("arguments[0].click();", triggers[0])
            time.sleep(0.2)
        item = driver.find_elements(By.XPATH, f"//li[normalize-space()='{visible_text}'] | //a[normalize-space()='{visible_text}']")
        if item:
            try:
                item[0].click()
            except Exception:
                driver.execute_script("arguments[0].click();", item[0])
            time.sleep(0.2)
            return True
    except Exception:
        pass
    return False

def _interpark_click_block(driver, region_code: str):
    """
    블록(데크/오토) 버튼 클릭.
    데크: RGN001, 오토: RGN002
    """
    # onclick에 GetBlockSeatList 포함된 요소 찾기
    xp = f"//*[contains(@onclick, \"GetBlockSeatList\") and contains(@onclick, \"'{region_code}'\")]"
    btns = driver.find_elements(By.XPATH, xp)
    if btns:
        try:
            btns[0].click()
        except Exception:
            driver.execute_script("arguments[0].click();", btns[0])
        time.sleep(0.3)
        return True
    return False

def _interpark_parse_seats(driver):
    """
    좌석(사이트) 파싱.
    - 예약 가능: 초록색 아이콘(보통 '가능' 클래스/alt/title/aria-label로 구분)
    - 예약 불가: 흰색/회색 아이콘
    """
    avail, unavail = [], []

    # 1) title/aria-label에 'B-21' 같은 사이트명이 있는 경우
    nodes = driver.find_elements(By.CSS_SELECTOR, "[title], [aria-label], a, button, .seat, .unit, .block a")
    for el in nodes:
        t = (el.get_attribute("title") or el.get_attribute("aria-label") or el.text or "").strip()
        # [데크사이트] B-21 형태 → 마지막 토큰만 꺼냄
        if "B-" in t or "A-" in t:
            # 상태 추정: 클래스/disabled
            cls = (el.get_attribute("class") or "").lower()
            disabled = el.get_attribute("disabled") is not None
            # 초록/가능 키워드 힌트
            s = (t + " " + cls).lower()
            site = t.split()[-1]  # 'B-21'
            if any(k in s for k in ["가능", "able", "on", "green"]) and not disabled:
                avail.append(site)
            elif any(k in s for k in ["불가", "sold", "off", "gray", "grey"]) or disabled:
                unavail.append(site)
            else:
                # 이미지로 색 판단이 필요할 수도 → 부모 span/img alt 속성 검사
                try:
                    img = el.find_element(By.CSS_SELECTOR, "img")
                    alt = (img.get_attribute("alt") or "").lower()
                    if "가능" in alt or "green" in alt:
                        avail.append(site)
                    else:
                        unavail.append(site)
                except Exception:
                    # 모르면 불가 쪽으로 (보수적으로)
                    unavail.append(site)

    # 2) 중복 제거/정렬
    def key(x):
        # 'B-21' → ('B', 21)
        try:
            a, b = x.split("-")
            return (a, int(b))
        except Exception:
            return (x, 0)

    avail = sorted(sorted(set(avail)), key=key)
    unavail = sorted(sorted(set(unavail)), key=key)
    return avail, unavail

def fetch_busan_port(selected_date: str, headless: bool = True, wait_sec: int = 25):
    """
    부산항 힐링 야영장(인터파크) 파서:
      1) 부산항 페이지 → '예약 바로가기' 클릭 (또는 인터파크 직접 진입)
      2) 대기열/중간 redirect 통과 → BookMain.asp 도달
      3) 공지 닫기
      4) 날짜 선택(selected_date)
      5) '1박 2일' 선택
      6) 데크/오토 각각 클릭 후 좌석 파싱
    반환 구조:
      {
        "deck":  {...},
        "auto":  {...}
      }
    """
    driver = _new_driver(headless=headless, window="1440,1600")
    try:
        # 1) 부산항 공홈 → 예약 바로가기 버튼(내부 JS) 호출
        page_url = CAMPING_TABS["busan_port"]["url_page"]
        driver.get(page_url)
        _dismiss_alert_if_any(driver)
        wait = WebDriverWait(driver, wait_sec)

        # 예약 바로가기 버튼 시도 (fnTicketBooking)
        try:
            driver.execute_script("if (typeof fnTicketBooking==='function') fnTicketBooking();")
        except Exception:
            pass

        # 혹시 그냥 인터파크 메인으로 직접 이동
        if "PCampingBook/BookMain.asp" not in driver.current_url:
            try:
                driver.get("https://ticket.interpark.com/PCampingBook/BookMain.asp")
                # ... 여기서 작업 ...
            except UnexpectedAlertPresentException:
                msg = _accept_any_alert(driver, timeout=2)  # "먼저 로그인 하세요."가 들어옴
                raise RuntimeError(f"로그인 필요로 자동 수집을 중단했습니다. ({msg})")

        # 2) 대기열 통과 대기
        _wait_until_interpark_main(driver, wait, max_secs=35)

        # 3) 공지 닫기
        _interpark_close_notice(driver)

        # 4) 날짜 선택
        _interpark_pick_date(driver, wait, selected_date)

        # 5) 1박 2일 선택
        _interpark_select_period(driver, "1박 2일")

        # 6) 데크/오토 각각 파싱
        result = {}

        # 데크: RGN001
        _interpark_click_block(driver, "RGN001")
        time.sleep(0.4)
        deck_av, deck_un = _interpark_parse_seats(driver)
        result["deck"] = {
            "available": deck_av,
            "unavailable": deck_un,
            "num_available": len(deck_av),
            "num_unavailable": len(deck_un),
            "total": len(deck_av) + len(deck_un) if (deck_av or deck_un) else None,
        }

        # 오토: RGN002
        _interpark_click_block(driver, "RGN002")
        time.sleep(0.4)
        auto_av, auto_un = _interpark_parse_seats(driver)
        result["auto"] = {
            "available": auto_av,
            "unavailable": auto_un,
            "num_available": len(auto_av),
            "num_unavailable": len(auto_un),
            "total": len(auto_av) + len(auto_un) if (auto_av or auto_un) else None,
        }

        return result

    finally:
        try:
            driver.quit()
        except Exception:
            pass
        # 임시 프로필/캐시 정리 (충돌 예방, 용량 누수 방지)
        try:
            if hasattr(driver, "temp_profile_dir"):
                shutil.rmtree(driver.temp_profile_dir, ignore_errors=True)
        except Exception:
            pass



# ===== 영도 버튼 파서 =====
def parse_yeongdo_buttons(html_soup: BeautifulSoup):
    """
    영도 예약 영역에서 '카라반/오토/일반' 사이트 버튼/링크를 파싱.
    사이트 UI가 button뿐 아니라 a(링크), role=button 엘리먼트를 혼용할 수 있어 모두 대응.
    상태는 title/aria-label/텍스트/클래스/disabled/aria-disabled 등으로 추정.
    """
    result = {
        "caravan": {"available": [], "unavailable": []},
        "auto": {"available": [], "unavailable": []},
        "general": {"available": [], "unavailable": []},
    }

    # ✅ button + a + role=button 전부 긁기
    nodes = (html_soup.select("button[title], a[title], [role='button'][title]")
             or html_soup.select("button, a, [role='button']"))

    # 라벨/숫자 추출
    pat_main = re.compile(r"(카라반|오토사이트|일반사이트)\s*([0-9]+)")
    pat_alt  = re.compile(r"(카라반|오토사이트|오토|일반사이트|일반)\s*.*?([0-9]+)")

    def _get_text(el):
        # 텍스트 후보: innerText → aria-label → title
        txt = " ".join(el.stripped_strings)
        if not txt:
            txt = (el.get("aria-label") or el.get("title") or "")
        return (txt or "").strip()

    def _status_of(el, label_text):
        """
        예약가능/불가 상태 추정:
        - title/aria-label/텍스트에 '예약가능/불가' 포함
        - disabled / aria-disabled
        - class 힌트(on/off/possible/complete/able/sold/gray/green)
        - (이미지 alt) 보조
        """
        title = (el.get("title") or "").strip()
        aria  = (el.get("aria-label") or "").strip()
        cls   = (el.get("class") or [])
        cls_s = " ".join(cls).lower()

        blob = (title + " " + aria + " " + label_text).replace(" ", "")
        if "예약가능" in blob or "가능" in blob:
            return "available"
        if "예약불가" in blob or "불가" in blob:
            return "unavailable"

        # disabled류
        if el.has_attr("disabled"):
            return "unavailable"
        if (el.get("aria-disabled") or "").lower() in ("true", "1"):
            return "unavailable"

        # 클래스 힌트
        if any(k in cls_s for k in ["on", "able", "green", "possible", "avail"]):
            return "available"
        if any(k in cls_s for k in ["off", "sold", "complete", "gray", "grey", "unavail"]):
            return "unavailable"

        # 이미지 alt 힌트
        try:
            img = el.select_one("img")
            if img:
                alt = (img.get("alt") or "").lower()
                if "가능" in alt or "green" in alt or "able" in alt:
                    return "available"
                if "불가" in alt or "sold" in alt or "gray" in alt or "grey" in alt:
                    return "unavailable"
        except Exception:
            pass

        # 모르면 보수적으로 불가 처리
        return "unavailable"

    for el in nodes:
        label = _get_text(el)
        if not label:
            continue

        m = pat_main.search(label) or pat_alt.search(label)
        if not m:
            # title만 숫자가 있는 경우 대비
            t = (el.get("title") or "")
            m = pat_main.search(t) or pat_alt.search(t)
            if not m:
                continue

        area_ko, num_str = m.group(1), m.group(2)
        try:
            num = int(num_str)
        except ValueError:
            continue

        if area_ko == "카라반":
            key = "caravan"
        elif area_ko in ("오토사이트", "오토"):
            key = "auto"
        elif area_ko in ("일반사이트", "일반"):
            key = "general"
        else:
            continue

        status = _status_of(el, label)
        bucket = result[key]["available"] if status == "available" else result[key]["unavailable"]
        bucket.append(num)

    # 정렬/중복 제거
    for k in result:
        for kk in ("available", "unavailable"):
            vals = sorted(set(result[k][kk]))
            result[k][kk] = vals

    return result


def fetch_gudeok_sites_with_retry(selected_date: str, page_url: str | None = None) -> dict:
    try:
        return fetch_gudeok_sites(selected_date=selected_date, page_url=page_url, headless=True, wait_sec=25)
    except WebDriverException:
        # 첫 트라이 실패 시 짧게 쉬고 새 드라이버로 재시도
        time.sleep(1.5)
        return fetch_gudeok_sites(selected_date=selected_date, page_url=page_url, headless=True, wait_sec=30)

def fetch_gudeok_sites(
    selected_date: str,
    page_url: str | None = None,
    headless: bool = True,
    wait_sec: int = 25
):
    if not page_url:
        page_url = CAMPING_TABS['gudeok']['url_page']

    start_str = selected_date
    end_str = (datetime.strptime(selected_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    driver = _new_driver(headless=headless, window="1280,1600")

    def _switch_back(base_handle):
        for _ in range(10):
            try:
                if base_handle in driver.window_handles:
                    driver.switch_to.window(base_handle)
                    return
            except Exception:
                pass
            time.sleep(0.2)

    def try_js_set_dates() -> bool:
        """
        팝업 없이 직접 sdate/edate 주입 후 '다 음' 진행 시도.
        성공하면 True 반환.
        """
        try:
            # 입력 필드가 readonly여도 value 주입 + 이벤트 발생으로 먹히는 사이트가 많음
            driver.execute_script("""
                const s = document.getElementById('sdate');
                const e = document.getElementById('edate');
                if (!s || !e) return false;
                s.removeAttribute('readonly'); e.removeAttribute('readonly');
                s.value = arguments[0]; e.value = arguments[1];
                s.dispatchEvent(new Event('input', {bubbles:true}));
                s.dispatchEvent(new Event('change', {bubbles:true}));
                e.dispatchEvent(new Event('input', {bubbles:true}));
                e.dispatchEvent(new Event('change', {bubbles:true}));
                return true;
            """, start_str, end_str)

            # 전체동의 체크 (가능하면)
            try:
                agree = driver.find_element(By.CSS_SELECTOR, "input.selectAllC")
                driver.execute_script("arguments[0].click();", agree)
                time.sleep(0.2)
            except Exception:
                pass

            # '다 음' 클릭 트라이
            for xp in [
                "//span[contains(normalize-space(.),'다 음')]",
                "//button[contains(normalize-space(.),'다 음')]",
                "//a[contains(normalize-space(.),'다 음')]",
                "//input[@type='submit' and @value='다 음']",
            ]:
                btns = driver.find_elements(By.XPATH, xp)
                if btns:
                    try:
                        btns[0].click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", btns[0])
                    break
            # 다음 페이지 로딩 확인
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'select[name="camp_num"]'))
            )
            return True
        except Exception:
            return False

    try:
        driver.get(page_url)
        _dismiss_alert_if_any(driver)
        wait = WebDriverWait(driver, wait_sec)

        # 1) 먼저 JS로 날짜 주입 시도
        if not try_js_set_dates():
            # 2) 실패 시 팝업 방식 폴백
            try:
                agree = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input.selectAllC")))
                if not agree.is_selected():
                    driver.execute_script("arguments[0].click();", agree)
                time.sleep(0.2)
            except Exception:
                pass

            def pick_date(input_id: str, date_str: str):
                base = driver.current_window_handle
                before = set(driver.window_handles)

                field = wait.until(EC.element_to_be_clickable((By.ID, input_id)))
                driver.execute_script("arguments[0].click();", field)

                # 팝업 창 뜨는 것 확실히 기다림
                wait.until(lambda d: len(set(d.window_handles) - before) >= 1)
                new_handle = list(set(driver.window_handles) - before)[0]
                driver.switch_to.window(new_handle)

                # onclick="copy('YYYY-MM-DD')" 요소 클릭
                span = WebDriverWait(driver, 15).until(
                    EC.element_to_be_clickable((By.XPATH, f"//span[contains(@onclick, \"copy('{date_str}')\")]"))
                )
                driver.execute_script("arguments[0].click();", span)

                # 원창 복귀
                _switch_back(base)
                time.sleep(0.2)

            pick_date("sdate", start_str)
            pick_date("edate", end_str)

            # '다 음'
            clicked_next = False
            for xp in [
                "//span[contains(normalize-space(.),'다 음')]",
                "//button[contains(normalize-space(.),'다 음')]",
                "//a[contains(normalize-space(.),'다 음')]",
                "//input[@type='submit' and @value='다 음']",
            ]:
                try:
                    el = driver.find_element(By.XPATH, xp)
                    driver.execute_script("arguments[0].click();", el)
                    clicked_next = True
                    break
                except Exception:
                    continue
            if not clicked_next:
                raise RuntimeError("다음 버튼을 찾지 못했습니다.")

            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'select[name="camp_num"]')))

        # 옵션 파싱
        avail, unavail = [], []
        options = driver.find_elements(By.CSS_SELECTOR, 'select[name="camp_num"] option[value]')
        for op in options:
            val = (op.get_attribute("value") or "").strip()
            if not val:
                continue
            if op.get_attribute("disabled") is not None:
                unavail.append(val)
            else:
                avail.append(val)

        def sort_key(v: str):
            a, b = v.split("-")
            try:
                return (int(a), int(b))
            except Exception:
                return (a, b)

        avail.sort(key=sort_key)
        unavail.sort(key=sort_key)

        return {
            "deck": {
                "available": avail,
                "unavailable": unavail,
                "num_available": len(avail),
                "num_unavailable": len(unavail),
                "total": len(avail) + len(unavail),
            }
        }

    finally:
        try:
            driver.quit()
        except Exception:
            pass
        try:
            if hasattr(driver, "temp_profile_dir"):
                shutil.rmtree(driver.temp_profile_dir, ignore_errors=True)
        except Exception:
            pass


# ===== 영도: 셀레니움(날짜 클릭 → 라디오 전환) =====

# ===== 영도: 셀레니움(날짜 클릭 → 라디오 전환) =====
def fetch_yeongdo_via_selenium_dateclick(selected_date: str, page_url: str, headless: bool = True, wait_sec: int = 20, total_max_sec: int = 40):
    """
    라디오(카라반/오토/일반) 전환 직후 '현재 화면에 보이는 버튼들'만 긁는다.
    버튼 텍스트에 '카라반/오토/일반' 라벨이 없으면 현재 탭으로 귀속.
    """
    t0 = time.time()
    driver = _new_driver(headless=headless, window="1280,1600")
    def _extract_visible_items(_driver):
        """
        좌석 버튼만(.b1) 긁고, 가시성/상태/라벨+번호를 정확히 판정해서 반환.
        반환: [{area:'caravan|auto|general|unknown', num:int, state:'available|unavailable'}]
        """
        return _driver.execute_script("""
          const out = [];
          // 좌석 버튼만
          const nodes = Array.from(document.querySelectorAll('button.b1, a.b1'));
          for (const el of nodes) {
            // 가시성
            const cs = getComputedStyle(el);
            if (cs.display === 'none' || cs.visibility === 'hidden' || !el.offsetParent) continue;

            const inner = (el.innerText || '').trim();
            const title = (el.getAttribute('title') || '').trim();
            const aria  = (el.getAttribute('aria-label') || '').trim();
            const txt   = (inner + ' ' + aria).replace(/\\s+/g, ' ').trim();

            // 상태: title/disabled 최우선
            const disabled = !!el.disabled || String(el.getAttribute('aria-disabled')||'').toLowerCase()==='true';
            let state = 'unavailable';
            const blob = (title + ' ' + aria + ' ' + inner).replace(/\\s+/g,'');
            if (disabled || blob.includes('예약불가') || blob.includes('불가')) {
              state = 'unavailable';
            } else if (blob.includes('예약가능') || blob.includes('가능')) {
              state = 'available';
            } else {
              // 타이틀이 비어있는 특수 케이스 대비(그래도 disabled면 불가가 이미 잡힘)
              state = disabled ? 'unavailable' : 'available';
            }

            // 라벨+번호: "카라반 12" / "오토 3" / "일반 7"
            // 공백/개행이 섞이므로 normalize
            const norm = txt.replace(/\\s+/g, ' ').trim();
            let area = 'unknown';
            let num  = null;

            // 한글 라벨 + 번호만 허용 (다른 숫자 잡음 방지)
            let m = norm.match(/^(카라반|오토사이트|오토|일반사이트|일반)\\s*([0-9]{1,3})\\s*$/);
            if (m) {
              const label = m[1];
              num = parseInt(m[2], 10);
              if (label === '카라반') area = 'caravan';
              else if (label === '오토사이트' || label === '오토') area = 'auto';
              else if (label === '일반사이트' || label === '일반') area = 'general';
            } else {
              // 혹시 라벨이 빠졌으면 숫자만 추출 (탭 귀속용)
              const m2 = norm.match(/\\b([0-9]{1,3})\\b/);
              if (m2) num = parseInt(m2[1], 10);
            }

            if (!Number.isInteger(num)) continue;

            out.push({ area, num, state });
          }
          return out;
        """)
    def _extract_from_any_frame(_driver):
        """
        메인 문서 먼저 → 없으면 모든 iframe/frame을 순회해서 .b1 버튼을 추출.
        """
        def _safe_extract():
            try:
                return _extract_visible_items(_driver) or []
            except Exception:
                return []

        # 1) 메인 문서
        items = _safe_extract()
        if items:
            return items

        # 2) 프레임들
        frames = _driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        for fr in frames:
            try:
                _driver.switch_to.frame(fr)
                items = _safe_extract()
            finally:
                _driver.switch_to.default_content()
            if items:
                return items
        return []

    try:
        driver.get(page_url)
        _dismiss_alert_if_any(driver)
        wait = WebDriverWait(driver, wait_sec)

        # 달력에서 날짜 보이게 만들고 클릭
        def date_cell_exists():
            return len(driver.find_elements(By.CSS_SELECTOR, f'td.date-td[data-date-string="{selected_date}"]')) > 0

        jumps = 0
        while not date_cell_exists() and jumps < 24:
            clicked = False
            for sel in [
                ".ui-datepicker-next", ".ui-datepicker-next > a",
                ".btn.next", "button.next", "a.next",
                ".calendar .next", ".cal-next", ".month-next",
                'a[title="다음달"]', "button.cal-next",
            ]:
                btns = driver.find_elements(By.CSS_SELECTOR, sel)
                if btns:
                    try: btns[0].click()
                    except Exception: driver.execute_script("arguments[0].click();", btns[0])
                    clicked = True
                    time.sleep(0.35)
                    break
            if not clicked:
                driver.execute_script("""
                    if (typeof goMonth === 'function') { goMonth(1); }
                    else if (typeof nextMonth === 'function') { nextMonth(); }
                """)
                time.sleep(0.35)
            jumps += 1

        try:
            cell = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, f'td.date-td[data-date-string="{selected_date}"]'))
            )
            anchor = cell.find_element(By.CSS_SELECTOR, "a") if cell.find_elements(By.CSS_SELECTOR, "a") else cell
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", anchor)
            try: anchor.click()
            except Exception: driver.execute_script("arguments[0].click();", anchor)
            time.sleep(0.4)
        except TimeoutException:
            pass

        # 라디오 전환 util
        def click_radio_and_wait(value_value: str, keywords: list[str]):
            def is_target_checked():
                try:
                    return driver.execute_script("""
                        const v = arguments[0];
                        const r = document.querySelector('input[type="radio"][value="'+v+'"]');
                        return !!(r && r.checked);
                    """, value_value)
                except Exception:
                    return False

            if is_target_checked():
                return

            radios = driver.find_elements(By.CSS_SELECTOR, f'input[type="radio"][value="{value_value}"]')
            if radios:
                el = radios[0]
                try: driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                except Exception: pass
                try: el.click()
                except Exception: driver.execute_script("arguments[0].click();", el)
                try:
                    driver.execute_script("""
                        const el = arguments[0];
                        el.checked = true;
                        el.dispatchEvent(new Event('input', {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        el.dispatchEvent(new Event('click', {bubbles:true}));
                    """, el)
                except Exception:
                    pass

            if not is_target_checked():
                for kw in keywords:
                    els = driver.find_elements(By.XPATH, f'//label[contains(normalize-space(.),"{kw}")]')
                    if els:
                        el = els[0]
                        try: driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        except Exception: pass
                        try: el.click()
                        except Exception: driver.execute_script("arguments[0].click();", el)
                        break

            if not is_target_checked():
                try:
                    driver.execute_script("""
                      (function(v){
                        try{ if (typeof siteGubunChange==='function') siteGubunChange(v); }catch(e){}
                        try{ if (typeof fnSiteGubun==='function') fnSiteGubun(v); }catch(e){}
                        try{ if (typeof changeGubun==='function') changeGubun(v); }catch(e){}
                        try{ if (typeof fnSearch==='function') fnSearch(); }catch(e){}
                        const hid = document.querySelector('input[name*="Gubun" i], input[id*="Gubun" i]');
                        if (hid){ hid.value = v; hid.dispatchEvent(new Event('change', {bubbles:true})); }
                      })(arguments[0]);
                    """, value_value)
                except Exception:
                    pass

            try:
                WebDriverWait(driver, 8).until(lambda d: is_target_checked())
            except TimeoutException:
                time.sleep(0.5)

            try:
                const_before = len(driver.find_elements(By.CSS_SELECTOR, "#siteList button, button, a, [role='button']"))
                WebDriverWait(driver, 5).until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#siteList button, button, a, [role='button']")) != const_before
                )
            except TimeoutException:
                time.sleep(0.2)

        # 이용인원 드롭다운을 적당한 값으로 설정 (안 고르면 리스트가 안 뜨는 경우가 있음)
        def _pick_person_if_needed():
            try:
                # '이용인원' 근처 select 찾기(첫 번째 제대로 된 option 선택)
                sel = None
                # id/name 에 person, cnt 같은 키워드가 많은 편이라 느슨하게 탐색
                for q in [
                    'select[name*="person" i]', 'select[id*="person" i]',
                    'select[name*="cnt" i]',    'select[id*="cnt" i]',
                    'select'
                ]:
                    cands = driver.find_elements(By.CSS_SELECTOR, q)
                    for s in cands:
                        ops = s.find_elements(By.CSS_SELECTOR, 'option')
                        # 값 있는 옵션이 1개 이상 있으면 타깃으로 간주
                        if any((o.get_attribute("value") or "").strip() for o in ops):
                            sel = s
                            break
                    if sel: break
                if not sel:
                    return False

                # 첫 번째 "값 있는" 옵션으로 설정 (placeholder는 value가 빈 문자열일 가능성 큼)
                target = None
                for o in sel.find_elements(By.TAG_NAME, 'option'):
                    v = (o.get_attribute('value') or '').strip()
                    if v:
                        target = v
                        break
                if not target:
                    return False

                driver.execute_script(
                    "arguments[0].value = arguments[1];"
                    "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                    "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                    sel, target
                )
                time.sleep(0.4)  # 목록 갱신 대기
                return True
            except Exception:
                return False


        categories = [
            {"key": "caravan", "value": "G01", "kws": ["카라반"]},
            {"key": "auto",    "value": "G02", "kws": ["오토사이트", "오토"]},
            {"key": "general", "value": "G03", "kws": ["일반사이트", "일반"]},
        ]

        merged = {c["key"]: {"available": [], "unavailable": []} for c in categories}

        for cat in categories:
            if time.time() - t0 > total_max_sec:
                break
            # 라디오 전환 후 잠깐 대기
            click_radio_and_wait(cat["value"], cat["kws"])
            time.sleep(0.3)

            # 이용인원 선택(필요 시)
            _pick_person_if_needed()
            time.sleep(0.3)

            # 좌석 리스트 로드 재시도(메인/프레임 모두 탐색)
            items = []
            for _ in range(8):  # 최대 ~4초 정도 기다림
                items = _extract_from_any_frame(driver)
                if items:
                    break
                time.sleep(0.5)

            # 디버그 로그는 items 만든 '후'에 찍기 (순서 버그 방지)
            print("[yeongdo]", cat["key"], "items:", len(items), items[:8], flush=True)

            # 현재 탭으로 귀속 (라벨 없으면 unknown → 현재 탭)
            cur_av, cur_un = [], []
            for it in (items or []):
                area = (it.get('area') or 'unknown')
                num  = it.get('num')
                st   = it.get('state')
                if not isinstance(num, int):
                    continue
                if area == 'unknown':
                    area = cat['key']
                if area != cat['key']:
                    continue
                (cur_av if st == 'available' else cur_un).append(num)

            cur = {
                "available": sorted(set(cur_av)),
                "unavailable": sorted(set(cur_un)),
            }

            for kk in ("available", "unavailable"):
                if cur.get(kk):
                    merged[cat["key"]][kk] = sorted(set(merged[cat["key"]][kk] + cur[kk]))


        return merged

    finally:
        try: driver.quit()
        except Exception: pass
        try:
            if hasattr(driver, "temp_profile_dir"):
                shutil.rmtree(driver.temp_profile_dir, ignore_errors=True)
        except Exception:
            pass


def _run_with_timeout(fn, timeout_sec, *args, **kwargs):
    import queue, threading
    q = queue.Queue(1)
    def runner():
        try:
            q.put((True, fn(*args, **kwargs)))
        except Exception as e:
            q.put((False, e))
    th = threading.Thread(target=runner, daemon=True)
    th.start()
    th.join(timeout=timeout_sec)
    if th.is_alive():
        return ("timeout", None)
    ok, val = q.get()
    return ("ok", val) if ok else ("err", val)

# ===== 영도 크롤러 엔트리 (GET/POST → 실패 시 Selenium 폴백) =====
def fetch_yeongdo(selected_date: str, page_url: str):

    if not page_url:
        raise ValueError("yeongdo.url_page is empty")
    
    sess = requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": page_url,
    }

    soup = None
    # 1) GET
    try:
        r = sess.get(page_url, headers=headers, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        parsed_get = parse_yeongdo_buttons(soup)
    except requests.RequestException:
        parsed_get = {"caravan": {"available": [], "unavailable": []},
                      "auto": {"available": [], "unavailable": []},
                      "general": {"available": [], "unavailable": []}}

    # 2) (선택) POST
    parsed_post = None
    try:
        form = soup.find("form") if soup else None
        if form:
            payload = {}
            for inp in form.find_all(["input", "select", "textarea"]):
                name = inp.get("name")
                if not name:
                    continue
                itype = (inp.get("type") or "").lower()
                if itype in ("checkbox", "radio") and not inp.has_attr("checked"):
                    continue
                payload[name] = inp.get("value", "")

            # 날짜 필드 추정 (CSS4 [i] 대신 파이썬에서 소문자 비교)
            def _find_date_name():
                # 명시적 date 타입
                di = form.select_one('input[type="date"]')
                if di and di.get("name"):
                    return di.get("name")
                # name에 'date' 포함
                for el in form.find_all("input"):
                    nm = (el.get("name") or "")
                    if "date" in nm.lower():
                        return nm
                # name에 'ymd' 포함 (대소문자 무시)
                for el in form.find_all("input"):
                    nm = (el.get("name") or "")
                    if "ymd" in nm.lower():
                        return nm
                return None

            date_name = _find_date_name()
            if date_name:
                payload[date_name] = selected_date
            else:
                # 백업 키들
                for guess in ["resdate", "sDate", "useDate", "use_date", "ymd", "riYmd", "selYmd"]:
                    payload[guess] = selected_date

            action = form.get("action") or page_url
            post_url = urljoin(page_url, action)
            r2 = sess.post(post_url, data=payload, headers=headers, timeout=15)
            r2.raise_for_status()
            soup2 = BeautifulSoup(r2.text, "html.parser")
            parsed_post = parse_yeongdo_buttons(soup2)
    except Exception:
        parsed_post = None


    # ---- 여기부터 교체 ----
    def _empty_or_missing(parsed):
        if not parsed:
            return True
        for k in ("caravan","auto","general"):
            if k not in parsed:
                return True
            a = parsed[k].get("available", [])
            u = parsed[k].get("unavailable", [])
            if (len(a) + len(u)) == 0:
                return True
        return False

    def _merge_list(a, b):
        sa = set(a or [])
        sb = set(b or [])
        ss = sa | sb
        try:
            return sorted(ss, key=lambda x: int(x))
        except Exception:
            return sorted(ss)

    candidate = parsed_post or parsed_get or {
        "caravan": {"available": [], "unavailable": []},
        "auto":    {"available": [], "unavailable": []},
        "general": {"available": [], "unavailable": []},
    }

    if _empty_or_missing(candidate):
        try:
            # 타임아웃 래퍼 없이 직접 호출 (아래 3번의 '시간 예산' 보강을 같이 쓰면 안정적)
            parsed_click = fetch_yeongdo_via_selenium_dateclick(
                selected_date, page_url, headless=True, wait_sec=20
            )
        except Exception:
            parsed_click = None

        if parsed_click:
            def _merge_list(a, b):
                sa, sb = set(a or []), set(b or [])
                try:  return sorted(sa | sb, key=lambda x: int(x))
                except: return sorted(sa | sb)

            merged = {}
            for k in ("caravan", "auto", "general"):
                merged[k] = {
                    "available":  _merge_list(candidate.get(k, {}).get("available"),  parsed_click.get(k, {}).get("available")),
                    "unavailable":_merge_list(candidate.get(k, {}).get("unavailable"), parsed_click.get(k, {}).get("unavailable")),
                }
            return merged

        return candidate

    return candidate


# ===== Flask 라우트 =====
@app.route("/", methods=["GET", "POST"])
def home():
    today = date.today().strftime("%Y-%m-%d")

    if request.method == "POST":
        selected_date = request.form.get("resdate", today)
        selected_camp_key = request.form.get("camp_tab", "samnak")
        return redirect(url_for("home", resdate=selected_date, camp=selected_camp_key))

    selected_date = request.args.get("resdate", today)
    selected_camp_key = request.args.get("camp", "samnak")

    def build_one(camp_key: str):
        camp_info = CAMPING_TABS.get(camp_key)
        media = build_media(camp_key)
        # 아래 기존 분기 로직을 camp_info 기준으로 그대로 사용 (내용은 기존 코드에서 복붙)
        # ─────────────────────────────────────────
        # 0) 부산항
        if camp_info.get("is_busan_port"):
            areas = {
                "auto": {"available": [], "unavailable": [], "num_available": 0, "total": 16},
                "deck": {"available": [], "unavailable": [], "num_available": 0, "total": 24},
            }
            return {"key": camp_key, "name": camp_info["name"], "areas": areas, "media": media, "error": None}

        # 1) 구덕
        if camp_info.get("is_gudeok") and not DISABLE_SCRAPERS:
            # 즉시 비어있는 골격만 내려주고, 실제 데이터는 /api/gudeok에서 폴링
            area = {"deck": {"available": [], "unavailable": [], "num_available": 0, "num_unavailable": 0, "total": 18}}
            return {
                "key": camp_key,
                "name": camp_info["name"],
                "areas": area,
                "media": media,
                "error": None,
                "lazy_gudeok": True,   # ⬅️ 템플릿 트리거 플래그
            }

        # 2) 영도 — 서버에서는 즉시 빈 골격 + lazy 플래그만, 데이터는 /api/yeongdo로
        if camp_info.get("is_yeongdo") and not DISABLE_SCRAPERS:
            area_info = {
                "caravan": {"available": [], "unavailable": [], "num_available": 0, "num_unavailable": 0, "total": 15},
                "auto":    {"available": [], "unavailable": [], "num_available": 0, "num_unavailable": 0, "total": 40},
                "general": {"available": [], "unavailable": [], "num_available": 0, "num_unavailable": 0, "total": 12},
            }
            return {
                "key": camp_key,
                "name": camp_info["name"],
                "areas": area_info,
                "media": media,
                "error": None,
                "lazy_yeongdo": True,   # ⬅️ 템플릿에서 이 플래그로 JS 로딩 트리거
            }

        # 3) 삼락/대저/화명
        camping_url = (camp_info.get("url_base") or "").format(selected_date)
        is_hwamyung = camp_info.get("is_hwamyung", False)
        try:
            r = requests.get(camping_url, timeout=10)
            if r.status_code != 200:
                return {"key": camp_key, "name": camp_info["name"], "areas": {}, "media": media,
                        "error": f"웹사이트 접속 실패: {r.status_code}"}
            soup = BeautifulSoup(r.text, "html.parser")
            areas_to_process = ["area_a", "area_b", "area_c", "area_d"]
            area_info = {}
            if is_hwamyung:
                areas_to_process = ["area_a", "area_b", "area_c"]
                area_info["area_d"] = {"available": [], "unavailable": [], "num_available": 0, "num_unavailable": 0, "max_site_num": 0}
                area_info["area_e"] = {"available": [], "unavailable": [], "num_available": 0, "num_unavailable": 0, "max_site_num": 0}
                all_site_numbers_d, all_site_numbers_e = [], []

            for area in areas_to_process:
                available, unavailable, all_nums = [], [], []
                for a in soup.find_all("a", class_=[area]):
                    tag = a.find("input", class_="sitename")
                    site_str = tag.get("value") if tag else None
                    if not site_str:
                        continue
                    try:
                        all_nums.append(int(site_str))
                    except ValueError:
                        continue
                    cls = a.get("class", [])
                    if "cbtn_on" in cls:
                        available.append(site_str)
                    elif "cbtn_Pcomplete" in cls:
                        unavailable.append(site_str)
                area_info[area] = {
                    "available": available,
                    "unavailable": unavailable,
                    "num_available": len(available),
                    "num_unavailable": len(unavailable),
                    "max_site_num": max(all_nums) if all_nums else 0,
                }

            if is_hwamyung:
                all_d_sites = soup.find_all("a", class_="area_d")
                for a in all_d_sites:
                    nm = (a.contents[0].strip() if a.contents else "")
                    if not nm: continue
                    s = nm[1:]
                    try:
                        num = int(s)
                    except ValueError:
                        num = 0
                    cls = a.get("class", [])
                    if nm.startswith("D"):
                        target = "area_d"
                    elif nm.startswith("E"):
                        target = "area_e"
                    else:
                        continue
                    if "cbtn_on" in cls:
                        area_info[target]["available"].append(nm)
                    elif "cbtn_Pcomplete" in cls:
                        area_info[target]["unavailable"].append(nm)
                for k in ["area_d", "area_e"]:
                    area_info[k]["num_available"] = len(area_info[k]["available"])
                    area_info[k]["num_unavailable"] = len(area_info[k]["unavailable"])

            return {"key": camp_key, "name": camp_info["name"], "areas": area_info, "media": media, "error": None}
        except Exception as e:
            return {"key": camp_key, "name": camp_info["name"], "areas": {}, "media": media,
                    "error": f"데이터 수집 오류: {e}"}
        # ─────────────────────────────────────────

    # ✅ ‘전체’면 모두 순회, 아니면 해당 탭만
    if selected_camp_key == "all":
        keys_to_fetch = [k for k in CAMPING_TABS.keys() if k != "all"]
    else:
        keys_to_fetch = [selected_camp_key]

    camping_data = [build_one(k) for k in keys_to_fetch]

    return render_template(
        "index.html",
        all_camps=camping_data,
        selected_date=selected_date,
        camp_tabs=CAMPING_TABS,
        selected_camp_key=selected_camp_key,
    )


# app.py 맨 아래쯤에 추가
@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, threaded=False)
