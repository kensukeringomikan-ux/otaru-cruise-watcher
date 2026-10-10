import json
import os
import re
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

CONFIG = json.loads(Path("config.json").read_text(encoding="utf-8"))
STATE_PATH = Path("state.json")

URLS = {
    "day": "https://book.otaru.cc/products/1ebc1dee-2e41-5a6e-b866-dd1e1df95281?lng=ja-JP",
    "night": "https://book.otaru.cc/products/e79640ab-93d7-584a-be79-95a05658e187?lng=ja-JP",
}

TARGETS = sorted(CONFIG["targets"], key=lambda x: x.get("priority", 99))
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_USER_ID = os.environ.get("LINE_USER_ID", "").strip()


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def target_key(target):
    return "|".join([
        target["date"], target["course"], str(target["adults"]),
        str(target.get("children", 0)), str(target.get("infants", 0))
    ])


def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def send_line(text):
    if not LINE_TOKEN or not LINE_USER_ID:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID が設定されていません")

    response = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "to": LINE_USER_ID,
            "messages": [{"type": "text", "text": text}],
        },
        timeout=20,
    )
    response.raise_for_status()


def notify(target, slots):
    course_name = "ナイトクルーズ" if target["course"] == "night" else "デイクルーズ"
    text = (
        "🚢 小樽運河クルーズに空きが出ました！\n\n"
        f"⭐ {target['label']}\n"
        f"📅 {target['date']}\n"
        f"🛥️ {course_name}\n"
        f"👥 大人 {target['adults']}名 / 小学生 {target.get('children', 0)}名 / 幼児 {target.get('infants', 0)}名\n"
        f"🕐 空き: {', '.join(slots)}\n\n"
        "空席は変動するので、早めの予約がおすすめです。\n"
        f"🎟️ {URLS[target['course']]}"
    )
    send_line(text)
    log("LINE通知を送信: " + target["label"] + " / " + ", ".join(slots))


def set_people(page, target):
    values = [
        ("大人", int(target.get("adults", 0))),
        ("小学生", int(target.get("children", 0))),
        ("幼児・乳児", int(target.get("infants", 0))),
    ]

    selects = page.locator("select")
    select_count = selects.count()

    for label, value in values:
        if value == 0:
            continue

        changed = False
        loc = page.get_by_text(label, exact=False)
        for i in range(min(loc.count(), 10)):
            try:
                parent = loc.nth(i).locator("xpath=..")
                s = parent.locator("select")
                if s.count():
                    s.first.select_option(str(value))
                    changed = True
                    break
            except Exception:
                pass

        if not changed:
            for i in range(select_count):
                try:
                    opts = [x.strip() for x in selects.nth(i).locator("option").all_text_contents()]
                    if str(value) in opts:
                        selects.nth(i).select_option(str(value))
                        changed = True
                        break
                except Exception:
                    pass

        if not changed:
            raise RuntimeError(f"人数設定失敗: {label}={value}")


def select_date(page, date_str):
    target = datetime.strptime(date_str, "%Y-%m-%d")
    ym = f"{target.year}年{target.month}月"
    day = str(target.day)

    for _ in range(24):
        body = page.locator("body").inner_text()
        if ym in body:
            break

        clicked = False
        buttons = page.locator("button")
        for i in range(buttons.count()):
            try:
                b = buttons.nth(i)
                attrs = " ".join(filter(None, [
                    b.get_attribute("aria-label"),
                    b.get_attribute("title"),
                    b.inner_text(),
                ])).lower()
                if any(x in attrs for x in ["next", "次", "翌"]):
                    b.click()
                    page.wait_for_timeout(300)
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            break

    candidates = page.get_by_text(day, exact=True)
    for i in range(candidates.count()):
        try:
            el = candidates.nth(i)
            if el.is_visible():
                el.click()
                page.wait_for_timeout(700)
                return
        except Exception:
            pass

    raise RuntimeError(f"日付を選択できません: {date_str}")


def get_times(page, target):
    # The page has TWO time dropdowns: the schedule overview and the booking
    # widget. Only the booking widget has the status badge ("即時予約"/"予約不可").
    wanted = set(target.get("preferred_times", []))
    selects = page.locator("select")
    candidates_by_select = []

    for i in range(selects.count()):
        select = selects.nth(i)
        try:
            labels = [s.strip() for s in select.locator("option").all_text_contents()]
            matches = []
            for label in labels:
                match = re.search(r"\b(?:[01]\d|2[0-3]):[0-5]\d\b", label)
                if match and (not wanted or match.group(0) in wanted):
                    matches.append((match.group(0), label))
            if not matches:
                continue

            # Score the dropdown by the surrounding booking-panel text.
            # The schedule dropdown elsewhere on the page must not be used.
            context = select.evaluate("""node => {
                let el = node;
                for (let depth = 0; el && depth < 10; depth++, el = el.parentElement) {
                    const text = (el.innerText || '').trim();
                    const hasTime = /(?:17:30|18:00|18:30|19:00)/.test(text);
                    if (hasTime && /参加人数を選択/.test(text) && /今すぐ予約する/.test(text)) {
                        return {text, foundBookingPanel: true, depth};
                    }
                }
                return {text: '', foundBookingPanel: false, depth: -1};
            }""")
            score = 100 if context.get("foundBookingPanel") else 0
            context_text = context.get("text", "")
            if context.get("depth", 99) <= 5:
                score += 20
            candidates_by_select.append((score, select, matches, context_text[:180]))
        except Exception:
            continue

    if not candidates_by_select:
        log(f"{target['label']}: 時刻選択プルダウンが見つかりません。誤通知防止のため空席なし扱い")
        return []

    candidates_by_select.sort(key=lambda x: x[0], reverse=True)
    score, time_select, candidates, context_preview = candidates_by_select[0]
    log(f"{target['label']}: 時刻プルダウン選択 score={score} / 周辺={context_preview.replace(chr(10), ' ')[:100]}")
    if score < 20:
        log(f"{target['label']}: 予約欄の時刻プルダウンと確認できないため、誤通知防止で除外")
        return []

    available = []
    seen = set()
    for time_text, option_label in candidates:
        if time_text in seen:
            continue
        seen.add(time_text)
        try:
            time_select.select_option(label=option_label)
            page.wait_for_timeout(800)

            status = time_select.evaluate("""node => {
                const re = /即時予約|予約不可|リクエスト予約|満席|空席なし/;
                let panel = null;
                // Use the smallest ancestor containing the booking heading and this time control.
                for (let el = node; el && el.parentElement; el = el.parentElement) {
                    const text = (el.innerText || '').trim();
                    if (/今すぐ予約する/.test(text) && /参加人数を選択/.test(text) &&
                        /(?:17:30|18:00|18:30|19:00)/.test(text)) {
                        panel = el;
                        break;
                    }
                }
                if (!panel) return '';
                const r = node.getBoundingClientRect();
                const targetY = r.top + r.height / 2;
                const found = [];
                for (const el of panel.querySelectorAll('*')) {
                    if (el === node || node.contains(el) || el.children.length > 2) continue;
                    const text = (el.innerText || el.textContent || '').trim();
                    if (!text || text.length > 24 || !re.test(text)) continue;
                    const b = el.getBoundingClientRect();
                    if (b.width === 0 || b.height === 0) continue;
                    const y = b.top + b.height / 2;
                    if (Math.abs(y - targetY) <= Math.max(40, r.height * 3) &&
                        b.right >= r.left - 100 && b.left <= r.right + 350) {
                        found.push({text, distance: Math.abs(y-targetY), width: b.width});
                    }
                }
                found.sort((a,b) => a.distance-b.distance || a.width-b.width);
                return found[0]?.text || '';
            }""")
