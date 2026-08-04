# -*- coding: utf-8 -*-

import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import Browser, BrowserContext, Locator, Page, sync_playwright
from twilio.rest import Client

BASE_DIR = Path(__file__).resolve().parent
HOME_URL = "https://www.konsolosluk.gov.tr/"
APPOINTMENT_URL = "https://www.konsolosluk.gov.tr/Appointment/Index/58"
COUNTRY_NAME = "Almanya"
MISSION_NAME = "Düsseldorf Başkonsolosluğu"
CURRENT_APPOINTMENT_DATE = date(2026, 9, 15)
HEADLESS = os.getenv("HEADLESS", "true").lower() not in {"false", "0", "no"}
PAGE_TIMEOUT_MS = 60_000
MISSION_LOAD_TIMEOUT_SECONDS = 45
APPOINTMENT_LOAD_WAIT_MS = 8_000

LOG_FILE = BASE_DIR / "konsolosluk_kontrol.log"
STATE_FILE = BASE_DIR / "konsolosluk_state.json"
PAGE_SCREENSHOT = BASE_DIR / "konsolosluk_sayfa.png"
PAGE_TEXT_FILE = BASE_DIR / "konsolosluk_sayfa_metni.txt"
ERROR_SCREENSHOT = BASE_DIR / "konsolosluk_hata.png"
ERROR_HTML_FILE = BASE_DIR / "konsolosluk_hata.html"
ERROR_TEXT_FILE = BASE_DIR / "konsolosluk_hata_metni.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("konsolosluk_bot")


def normalize_text(value: str) -> str:
    return " ".join((value or "").replace("\xa0", " ").split()).strip()


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} ortam değişkeni bulunamadı.")
    return value.strip()


def parse_date(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        return None


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("State okunamadı, boş state kullanılacak: %s", exc)
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("State güncellendi: %s", STATE_FILE)


def send_whatsapp_message(appointment_date: date) -> str:
    client = Client(
        required_env("TWILIO_ACCOUNT_SID"),
        required_env("TWILIO_AUTH_TOKEN"),
    )
    message = client.messages.create(
        to=required_env("TWILIO_WHATSAPP_TO"),
        from_=required_env("TWILIO_WHATSAPP_FROM"),
        content_sid=required_env("TWILIO_CONTENT_SID"),
    )
    logger.warning(
        "WhatsApp gönderildi | tarih=%s | SID=%s | durum=%s",
        appointment_date.strftime("%d.%m.%Y"),
        message.sid,
        message.status,
    )
    return message.sid


def save_page_debug_files(page: Page) -> None:
    try:
        page.screenshot(path=str(PAGE_SCREENSHOT), full_page=True)
        PAGE_TEXT_FILE.write_text(page.locator("body").inner_text(), encoding="utf-8")
    except Exception as exc:
        logger.warning("Debug dosyaları kaydedilemedi: %s", exc)


def save_error_files(page: Page) -> None:
    try:
        page.screenshot(path=str(ERROR_SCREENSHOT), full_page=True)
    except Exception as exc:
        logger.error("Hata ekran görüntüsü alınamadı: %s", exc)
    try:
        ERROR_HTML_FILE.write_text(page.content(), encoding="utf-8")
        ERROR_TEXT_FILE.write_text(page.locator("body").inner_text(), encoding="utf-8")
    except Exception as exc:
        logger.error("Hata sayfa içeriği kaydedilemedi: %s", exc)


def get_select_options(select: Locator) -> list[dict]:
    return select.evaluate(
        """
        select => Array.from(select.options).map(option => ({
            text: (option.textContent || '').trim(),
            value: option.value
        }))
        """
    )


def select_contains_option(select: Locator, option_text: str) -> bool:
    wanted = normalize_text(option_text).casefold()
    try:
        options = get_select_options(select)
    except Exception:
        return False
    return any(
        wanted == normalize_text(item.get("text", "")).casefold()
        or wanted in normalize_text(item.get("text", "")).casefold()
        for item in options
    )


def select_hidden_option_by_text(
    select: Locator,
    option_text: str,
    selection_type: str,
) -> str:
    wanted = normalize_text(option_text).casefold()
    options = get_select_options(select)

    selected = next(
        (
            item
            for item in options
            if normalize_text(item.get("text", "")).casefold() == wanted
        ),
        None,
    )
    if selected is None:
        selected = next(
            (
                item
                for item in options
                if wanted in normalize_text(item.get("text", "")).casefold()
            ),
            None,
        )
    if selected is None:
        raise RuntimeError(f"'{option_text}' seçeneği bulunamadı.")

    value = str(selected.get("value", ""))
    text = normalize_text(selected.get("text", ""))
    logger.info("Seçim hazırlanıyor: %s | value=%s", text, value)

    select.evaluate(
        """
        (select, value) => {
            select.value = value;
            select.dispatchEvent(new Event('input', {bubbles: true}));
            select.dispatchEvent(new Event('change', {bubbles: true}));
            if (window.jQuery && window.jQuery.fn && window.jQuery.fn.selectpicker) {
                try {
                    window.jQuery(select).selectpicker('refresh');
                    window.jQuery(select).selectpicker('render');
                } catch (_) {}
            }
        }
        """,
        value,
    )

    if selection_type == "country":
        select.evaluate(
            """select => {
                if (typeof window.setMissions === 'function') {
                    window.setMissions(select.value);
                }
            }"""
        )
    elif selection_type == "mission":
        select.evaluate(
            """select => {
                if (typeof window.setSelectedMission === 'function') {
                    window.setSelectedMission(select.value);
                }
            }"""
        )

    select.page.wait_for_timeout(2_000)
    logger.info("Seçim tamamlandı: %s | value=%s", text, select.input_value())
    return value


def find_country_select(page: Page) -> Locator:
    for selector in ("#ddlCountries1", "#ddlCountries"):
        locator = page.locator(selector)
        if locator.count() == 1 and select_contains_option(locator.first, COUNTRY_NAME):
            return locator.first

    selects = page.locator("select")
    for index in range(selects.count()):
        select = selects.nth(index)
        if select_contains_option(select, COUNTRY_NAME):
            return select
    raise RuntimeError("Almanya seçeneğini içeren ülke alanı bulunamadı.")


def find_mission_select(page: Page) -> Optional[Locator]:
    for selector in ("#ddlMission1", "#ddlMission"):
        locator = page.locator(selector)
        if locator.count() == 1 and select_contains_option(locator.first, MISSION_NAME):
            return locator.first

    selects = page.locator("select")
    for index in range(selects.count()):
        select = selects.nth(index)
        if select_contains_option(select, MISSION_NAME):
            return select
    return None


def wait_for_mission_select(page: Page) -> Locator:
    start = time.monotonic()
    while time.monotonic() - start < MISSION_LOAD_TIMEOUT_SECONDS:
        select = find_mission_select(page)
        if select is not None:
            return select
        page.wait_for_timeout(500)
    raise RuntimeError("Düsseldorf temsilcilik seçeneği yüklenmedi.")


def click_confirmation_button(page: Page) -> None:
    texts = (
        "Evet, bu temsilcilikte işlem yapmak istiyorum",
        "İşlemlere burada devam et",
        "Burada devam et",
    )
    for text in texts:
        locator = page.get_by_text(text, exact=False)
        for index in range(locator.count()):
            item = locator.nth(index)
            try:
                if item.is_visible():
                    item.click()
                    logger.info("Onay butonuna tıklandı: %s", text)
                    page.wait_for_timeout(3_000)
                    return
            except Exception:
                continue
    logger.info("Ek onay butonu görünmedi.")


def prepare_consulate_session(page: Page) -> None:
    logger.info("Ana sayfa açılıyor: %s", HOME_URL)
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(4_000)

    country_select = find_country_select(page)
    select_hidden_option_by_text(country_select, COUNTRY_NAME, "country")

    mission_select = wait_for_mission_select(page)
    select_hidden_option_by_text(mission_select, MISSION_NAME, "mission")

    page.wait_for_timeout(2_000)
    click_confirmation_button(page)


def open_appointment_page(page: Page) -> None:
    logger.info("Randevu sayfası açılıyor: %s", APPOINTMENT_URL)
    page.goto(
        APPOINTMENT_URL,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )
    page.wait_for_timeout(APPOINTMENT_LOAD_WAIT_MS)
    logger.info("Randevu sayfası URL: %s", page.url)


def extract_appointment_date(page: Page) -> date:
    rows = page.locator("table tr")
    logger.info("Tablo satırı sayısı: %s", rows.count())

    for index in range(rows.count()):
        row_text = normalize_text(rows.nth(index).inner_text())
        if "düsseldorf" not in row_text.casefold():
            continue
        logger.info("Düsseldorf satırı: %s", row_text)
        for date_text in re.findall(r"\b\d{2}\.\d{2}\.\d{4}\b", row_text):
            parsed = parse_date(date_text)
            if parsed:
                return parsed

    body_text = normalize_text(page.locator("body").inner_text())
    match = re.search(
        r"Düsseldorf(?:\s+Başkonsolosluğu)?.{0,500}?(\d{2}\.\d{2}\.\d{4})",
        body_text,
        flags=re.IGNORECASE,
    )
    if match:
        parsed = parse_date(match.group(1))
        if parsed:
            return parsed

    raise RuntimeError("Düsseldorf randevu tarihi okunamadı.")


def create_browser_context(browser: Browser) -> BrowserContext:
    return browser.new_context(
        locale="tr-TR",
        timezone_id="Europe/Berlin",
        viewport={"width": 1600, "height": 1000},
    )


def check_appointment_page() -> date:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS)
        context = create_browser_context(browser)
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        try:
            prepare_consulate_session(page)
            open_appointment_page(page)
            save_page_debug_files(page)
            appointment_date = extract_appointment_date(page)
            logger.info("Bulunan Düsseldorf tarihi: %s", appointment_date.strftime("%d.%m.%Y"))
            return appointment_date
        except Exception:
            save_error_files(page)
            raise
        finally:
            context.close()
            browser.close()


def process_appointment_date(appointment_date: date) -> None:
    found = appointment_date.strftime("%d.%m.%Y")
    current = CURRENT_APPOINTMENT_DATE.strftime("%d.%m.%Y")
    logger.info("Bulunan tarih: %s | Mevcut randevu: %s", found, current)

    if appointment_date >= CURRENT_APPOINTMENT_DATE:
        logger.info("Daha erken tarih yok. WhatsApp gönderilmedi.")
        return

    state = load_state()
    if state.get("last_notified_date") == found:
        logger.info("%s daha önce bildirildi. Tekrar mesaj gönderilmedi.", found)
        return

    message_sid = send_whatsapp_message(appointment_date)
    save_state(
        {
            "last_notified_date": found,
            "last_notification_time": datetime.now().isoformat(timespec="seconds"),
            "twilio_message_sid": message_sid,
            "current_appointment_date": current,
        }
    )


def main() -> None:
    logger.info("=" * 80)
    logger.info("Düsseldorf konsolosluk kontrolü başladı | headless=%s", HEADLESS)
    logger.info(
        "WhatsApp yalnızca %s tarihinden önce gönderilecek.",
        CURRENT_APPOINTMENT_DATE.strftime("%d.%m.%Y"),
    )
    process_appointment_date(check_appointment_page())
    logger.info("Kontrol tamamlandı.")
    logger.info("=" * 80)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.exception("Program hata ile tamamlandı: %s", exc)
        sys.exit(1)
