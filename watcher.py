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
    # The status badge is a sibling/nearby element of the time <select>,
    # not necessarily inside an ancestor that contains the select.
    wanted = set(target.get("preferred_times", []))
    time_select = None
    candidates = []

    selects = page.locator("select")
    for i in range(selects.count()):
        select = selects.nth(i)
        try:
            labels = [s.strip() for s in select.locator("option").all_text_contents()]
            matches = []
            for label in labels:
                match = re.search(r"\b(?:[01]\d|2[0-3]):[0-5]\d\b", label)
                if match and (not wanted or match.group(0) in wanted):
                    matches.append((match.group(0), label))
            if matches:
                time_select = select
                candidates = matches
                break
        except Exception:
            continue

    if time_select is None:
        log(f"{target['label']}: 時刻選択プルダウンが見つかりません。誤通知防止のため空席なし扱い")
        return []

    available = []
    seen = set()
    for time_text, option_label in candidates:
        if time_text in seen:
            continue
        seen.add(time_text)
        try:
            time_select.select_option(label=option_label)
            page.wait_for_timeout(600)

            status = time_select.evaluate("""node => {
                const terms = /即時予約|予約不可|リクエスト予約|満席|空席なし/;
                const found = [];
                let current = node;
                // Collect the nearest booking-panel text, and inspect nearby
                // siblings because the badge is shown beside the time selector.
                for (let depth = 0; current && depth < 5; depth++, current = current.parentElement) {
                    const text = (current.innerText || current.textContent || '').trim();
                    if (text && terms.test(text)) {
                        found.push({depth, text: text.slice(0, 1600)});
                    }
                }
                const parent = node.parentElement;
                if (parent) {
                    for (const sibling of parent.parentElement ? Array.from(parent.parentElement.children) : []) {
                        const text = (sibling.innerText || sibling.textContent || '').trim();
                        if (text && terms.test(text)) found.push({depth: 'sibling', text: text.slice(0, 500)});
                    }
                }
                return found.sort((a,b) => String(a.depth).length - String(b.depth).length)[0]?.text || '';
            }""")

            # Require the explicit positive badge; absence of a readable badge
            # is unknown, never availability.
            if re.search(r"即時予約", status):
                available.append(time_text)
                log(f"{target['label']}: {time_text} 「即時予約」を検出")
            elif re.search(r"予約不可|リクエスト予約|満席|空席なし", status):
                log(f"{target['label']}: {time_text} 即時予約ではない表示を検出")
            else:
                log(f"{target['label']}: {time_text} 判定できる予約ステータスなし。誤通知防止のため除外")
        except Exception as e:
            log(f"{target['label']}: {time_text} 判定失敗 ({type(e).__name__}); 誤通知防止のため除外")

    result = sorted(set(available))
    log(f"{target['label']}: 時刻候補={','.join(sorted(seen)) if seen else 'なし'} / 即時予約確認済み={','.join(result) if result else 'なし'}")
    return result

def check_target(page, target):
    page.goto(URLS[target["course"]], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    set_people(page, target)
    select_date(page, target["date"])
    page.wait_for_timeout(1000)
    return get_times(page, target)


def test_line():
    send_line("✅ 小樽運河クルーズ監視ツールのLINE通知テストです。")
    log("LINEテスト通知を送信しました。")


def main():
    if os.environ.get("TEST_LINE") == "1":
        test_line()
        return

    log("小樽運河クルーズ空席チェックを開始")
    state = load_state()
    changed = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP", timezone_id="Asia/Tokyo")
        try:
            for target in TARGETS:
                key = target_key(target)
                try:
                    slots = check_target(page, target)
                    current = sorted(set(slots))
                    old = set(state.get(key, []))
                    new_slots = sorted(set(current) - old)

                    if new_slots:
                        notify(target, new_slots)

                    if current != sorted(old):
                        state[key] = current
                        changed = True

                    log(f"{target['label']}: {', '.join(current) if current else '空きなし'}")
                except PlaywrightTimeoutError:
                    log(f"{target['label']}: タイムアウト")
                except Exception as e:
                    log(f"{target['label']}: チェック失敗: {type(e).__name__}: {e}")
        finally:
            browser.close()

    if changed:
        save_state(state)
        log("通知済み状態を保存しました。")


if __name__ == "__main__":
    main()
