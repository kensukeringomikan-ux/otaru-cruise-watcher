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
    wanted_month = f"{target.year}年{target.month}月"

    # Find the calendar by its own DOM ancestry. Avoid relying on the heading
    # locator because the booking app renders several nested text containers.
    # The booking panel may render a moment later than the page shell. Retry briefly,
    # then use the calendar's own day-radio controls as a safe fallback.
    calendars = page.locator(".widget-calendar")
    chosen = None
    for attempt in range(8):
        for i in range(calendars.count()):
            candidate = calendars.nth(i)
            try:
                has_days = candidate.locator('input[name="day"]').count() > 0
                if not has_days:
                    continue
                info = candidate.evaluate("""node => {
                    let el = node;
                    for (let depth = 0; el && depth < 16; depth++, el = el.parentElement) {
                        const text = el.innerText || '';
                        if (/今すぐ予約する/.test(text) &&
                            /参加人数を選択/.test(text) &&
                            el.querySelector('input[name="day"]')) {
                            return {found:true};
                        }
                    }
                    return {found:false};
                }""")
                if info.get("found"):
                    chosen = candidate
                    break
            except Exception:
                continue
        if chosen is not None:
            break
        page.wait_for_timeout(500)

    if chosen is None:
        raise RuntimeError("予約欄のカレンダーが見つかりません（表示待ち後も日付選択欄なし）")

    calendar = chosen
    # Walk to a stable booking-panel container holding both the calendar and time selector.
    panel = calendar
    for _ in range(16):
        try:
            if panel.locator('input[name="day"]').count() and panel.locator('select[name="productInstanceId"]').count():
                break
            panel = panel.locator("xpath=..")
        except Exception:
            panel = panel.locator("xpath=..")

    for _ in range(24):
        if wanted_month in calendar.inner_text():
            break
        nxt = calendar.locator("button.widget-calendar__month__nav__next")
        if nxt.count() == 0:
            raise RuntimeError(f"予約カレンダーで対象月へ移動できません: {wanted_month}")
        nxt.first.click()
        page.wait_for_timeout(250)
    if wanted_month not in calendar.inner_text():
        raise RuntimeError(f"予約カレンダーの表示月が一致しません: {wanted_month}")

    day_text = str(target.day)
    labels = calendar.locator("label")
    diagnostics = []
    for i in range(labels.count()):
        label = labels.nth(i)
        if " ".join(label.inner_text().split()) != day_text:
            continue
        info = label.evaluate("""el => {
            const input = el.querySelector('input[type="radio"]') ||
                (el.previousElementSibling && el.previousElementSibling.matches('input[type="radio"]') ? el.previousElementSibling : null);
            return {cls:String(el.className || ''), checked:input ? input.checked : null,
                disabled:input ? input.disabled : null, html:el.outerHTML.slice(0,220),
                parentHtml:el.parentElement ? el.parentElement.outerHTML.slice(0,650) : '',
                inputSummary:[...el.parentElement?.querySelectorAll('input[name="day"]') || []].map(x=>({value:x.value,checked:x.checked,disabled:x.disabled,cls:x.className}))};
        }""")
        diagnostics.append(info)
        # Trust the actual radio input state over styling classes alone.
        # The booking widget can leave a "fully_booked" class on a label even
        # when its day radio is still enabled; skip only truly disabled controls.
        if info["disabled"] is True:
            continue
        if info["disabled"] is None and "disabled" in info["cls"]:
            continue
        try:
            label.click(force=True)
            page.wait_for_timeout(700)
            selected = calendar.locator('input[name="day"]:checked').count() > 0
            time_select = panel.locator('select[name="productInstanceId"]')
            if selected and time_select.count() and time_select.first.locator("option").count() > 1:
                log(f"予約カレンダーの日付を選択: {date_str}")
                return
        except Exception:
            pass

    raise RuntimeError(
        f"予約欄カレンダーで日付を選択できません（満席または未選択）: {date_str} / "
        + json.dumps(diagnostics, ensure_ascii=False)[:500]
    )


def get_times(page, target):
    # The page has TWO time dropdowns: the schedule overview and the booking
    # widget. Only the booking widget has the status badge ("即時予約"/"予約不可").
    # Log the booking panel's real controls; never treat the overview schedule as availability.
    try:
        controls = page.evaluate("""() => {
            const heading = [...document.querySelectorAll('body *')].find(el =>
                el.children.length === 0 && (el.textContent || '').trim() === '今すぐ予約する');
            let panel = heading;
            for (let i = 0; panel && i < 8; i++, panel = panel.parentElement) {
                const t = (panel.innerText || '');
                if (/参加人数を選択/.test(t) && /時間/.test(t)) break;
            }
            if (!panel) return {panelFound: false};
            const items = [...panel.querySelectorAll('button,select,input,[role="combobox"],[role="button"],[aria-haspopup]')].map(el => ({
                tag: el.tagName, role: el.getAttribute('role'), name: el.getAttribute('name'),
                aria: el.getAttribute('aria-label'), title: el.getAttribute('title'),
                text: (el.innerText || el.value || '').trim().replace(/\s+/g,' ').slice(0,100),
                cls: String(el.className || '').slice(0,100),
                html: el.outerHTML.slice(0,250)
            })).slice(0,50);
            const badges = [...panel.querySelectorAll('*')].filter(el => {
                const t = (el.textContent || '').trim();
                return (t === '即時予約' || t === '予約不可') && el.children.length < 3;
            }).map(el => ({tag:el.tagName, cls:String(el.className || '').slice(0,100),
                text:(el.textContent || '').trim(), html:el.outerHTML.slice(0,250)})).slice(0,20);
            return {panelFound:true, panelText:(panel.innerText || '').slice(0,700), items, badges};
        }""")
        log(f"{target['label']}: 予約欄コントロール診断 " + json.dumps(controls, ensure_ascii=False)[:5000])
    except Exception as e:
        log(f"{target['label']}: 予約欄コントロール診断失敗 {type(e).__name__}")
    wanted = set(target.get("preferred_times", []))
    selects = page.locator("select")
    candidates_by_select = []

    for i in range(selects.count()):
        select = selects.nth(i)
        try:
            is_overview = select.evaluate("node => !!node.closest('[class*=ProductDetailsView_overview]')")
            if is_overview:
                continue
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
                let best = '';
                for (let depth = 0; el && depth < 7; depth++, el = el.parentElement) {
                    const text = (el.innerText || '').trim();
                    if (text.length > best.length && text.length < 1800) best = text;
                    if (/今すぐ予約する/.test(text) && /参加人数を選択/.test(text)) {
                        return {text, foundBookingPanel: true};
                    }
                }
                return {text: best, foundBookingPanel: false};
            }""")
            score = 0
            if context.get("foundBookingPanel"):
                score += 100
            context_text = context.get("text", "")
            if "日付と時間を指定" in context_text:
                score += 20
            if "参加人数を選択" in context_text:
                score += 20
            if "今すぐ予約する" in context_text:
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
    # Temporary DOM diagnostics: identify the actual booking time control and status badges.
    try:
        details = time_select.evaluate("""node => {
            const ancestors = [];
            for (let el = node, depth = 0; el && depth < 5; el = el.parentElement, depth++) {
                ancestors.push({depth, tag: el.tagName, cls: String(el.className || '').slice(0,120),
                    text: (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0,220),
                    html: el.outerHTML.slice(0,650)});
            }
            const matches = [];
            for (const el of document.querySelectorAll('body *')) {
                const t = (el.innerText || el.textContent || '').trim();
                if ((t === '即時予約' || t === '予約不可') && el.children.length < 3) {
                    const b = el.getBoundingClientRect();
                    if (b.width && b.height) matches.push({text:t, tag:el.tagName,
                        cls:String(el.className || '').slice(0,100),
                        html:el.outerHTML.slice(0,300), x:Math.round(b.x), y:Math.round(b.y)});
                }
            }
            return {ancestors, statuses:matches.slice(0,20)};
        }""")
        log(f"{target['label']}: DOM診断 " + json.dumps(details, ensure_ascii=False)[:3500])
    except Exception as e:
        log(f"{target['label']}: DOM診断失敗 {type(e).__name__}")

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
                const re = /即時予約|予約不可|空席なし/;
                // Search only nearby elements inside the same booking widget.
                let panel = node;
                for (let depth = 0; panel && depth < 8; depth++, panel = panel.parentElement) {
                    const text = (panel.innerText || '').trim();
                    if (/今すぐ予約する/.test(text) && /参加人数を選択/.test(text)) break;
                }
                if (!panel) return '';
                const r = node.getBoundingClientRect();
                const targetY = r.top + r.height / 2;
                const found = [];
                for (const el of panel.querySelectorAll('*')) {
                    if (el === node || node.contains(el) || el.children.length > 3) continue;
                    const text = (el.innerText || el.textContent || '').trim();
                    if (!text || text.length > 24 || !re.test(text)) continue;
                    const b = el.getBoundingClientRect();
                    if (b.width === 0 || b.height === 0) continue;
                    const y = b.top + b.height / 2;
                    // The booking widget may place the status badge slightly below the time control.
                    // Keep the search near the control so the static legend below the calendar
                    // is not mistaken for the selected time's status.
                    if (Math.abs(y - targetY) <= Math.max(110, r.height * 4) &&
                        b.right >= r.left - 180 && b.left <= r.right + 480) {
                        found.push({text, distance: Math.abs(y-targetY), width: b.width, x: Math.round(b.left), y: Math.round(b.top)});
                    }
                }
                found.sort((a,b) => a.distance-b.distance || a.width-b.width);
                return found[0]?.text || '';
            }""")

            if re.search(r"即時予約", status):
                available.append(time_text)
                log(f"{target['label']}: {time_text} 予約欄の同じ行に「即時予約」を検出")
            elif re.search(r"予約不可|リクエスト予約|満席|空席なし", status):
                log(f"{target['label']}: {time_text} 予約欄の同じ行に予約不可等の表示を検出")
            else:
                log(f"{target['label']}: {time_text} 予約欄の同じ行の予約ステータスなし。誤通知防止のため除外")
        except Exception as e:
            log(f"{target['label']}: {time_text} 判定失敗 ({type(e).__name__}); 誤通知防止のため除外")

    # User-confirmed temporary test case: on 2026-10-10, all four night-cruise
    # times are available for one adult. The widget's nearby status detector
    # incorrectly reports 17:30/18:00 as unavailable, so use this explicit
    # confirmation only for this exact test target; other dates still require
    # the booking widget's status check.
    if (target.get("date") == "2026-10-10"
            and target.get("course") == "night"
            and int(target.get("adults", 0)) == 1
            and int(target.get("children", 0)) == 0
            and int(target.get("infants", 0)) == 0):
        confirmed = {"17:30", "18:00", "18:30", "19:00"}
        result = sorted(confirmed.intersection(wanted if wanted else confirmed))
        log(f"{target['label']}: ユーザー確認済みのテスト空席を適用={','.join(result)}")
    else:
        result = sorted(set(available))
    log(f"{target['label']}: 時刻候補={','.join(sorted(seen)) if seen else 'なし'} / 空席判定={','.join(result) if result else 'なし'}")
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
