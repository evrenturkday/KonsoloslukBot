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

from dotenv import load_dotenv
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from twilio.rest import Client


# ============================================================
# 1) TEMEL AYARLAR
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

HOME_URL = "https://www.konsolosluk.gov.tr/"
APPOINTMENT_URL = (
    "https://www.konsolosluk.gov.tr/Appointment/Index/58"
)

COUNTRY_NAME = "Almanya"
MISSION_NAME = "Düsseldorf Başkonsolosluğu"

# Mevcut randevun.
# Yalnızca bu tarihten ÖNCE bir randevu bulunursa mesaj gönderilir.
CURRENT_APPOINTMENT_DATE = date(2026, 9, 15)

# İlk testlerde False bırak.
# Tarayıcının işlemlerini görürsün.
# Her şey çalıştıktan sonra True yapabilirsin.
HEADLESS = True

PAGE_TIMEOUT_MS = 60_000
MISSION_LOAD_TIMEOUT_SECONDS = 40
APPOINTMENT_LOAD_WAIT_MS = 7_000
ACCESS_RETRY_WAIT_SECONDS = (10, 30, 60)
MAX_ACCESS_ATTEMPTS = len(ACCESS_RETRY_WAIT_SECONDS) + 1

LOG_FILE = BASE_DIR / "konsolosluk_kontrol.log"
STATE_FILE = BASE_DIR / "konsolosluk_state.json"

PAGE_SCREENSHOT = BASE_DIR / "konsolosluk_sayfa.png"
PAGE_TEXT_FILE = BASE_DIR / "konsolosluk_sayfa_metni.txt"

ERROR_SCREENSHOT = BASE_DIR / "konsolosluk_hata.png"
ERROR_HTML_FILE = BASE_DIR / "konsolosluk_hata.html"
ERROR_TEXT_FILE = BASE_DIR / "konsolosluk_hata_metni.txt"


# ============================================================
# 2) .ENV DOSYASINI YÜKLE
# ============================================================

load_dotenv(ENV_FILE)


# ============================================================
# 3) LOGGER
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(
            LOG_FILE,
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger("konsolosluk_bot")


# ============================================================
# 4) GENEL YARDIMCI FONKSİYONLAR
# ============================================================

def normalize_text(value: str) -> str:
    """
    Fazla boşlukları ve görünmeyen boşluk karakterlerini temizler.
    """

    if not value:
        return ""

    return " ".join(
        value.replace("\xa0", " ").split()
    ).strip()


def required_env(name: str) -> str:
    """
    .env dosyasından zorunlu bir değeri okur.
    """

    value = os.getenv(name)

    if not value:
        raise ValueError(
            f"{name} değeri .env dosyasında bulunamadı."
        )

    return value.strip()


class ConsulateAccessError(RuntimeError):
    """Konsolosluk sitesi isteği engellediğinde veya erişilemediğinde kullanılır."""


def ensure_consulate_page_is_accessible(
    page: Page,
    http_status: Optional[int] = None,
) -> None:
    """403/Blist gibi engel sayfalarının normal sayfa sanılmasını önler."""

    try:
        body_text = normalize_text(page.locator("body").inner_text())
    except Exception:
        body_text = ""

    folded_text = body_text.casefold()
    block_markers = (
        "erişim engellendi",
        "erisim engellendi",
        "blist",
        "access denied",
        "forbidden",
    )

    if (
        http_status in {401, 403, 429, 503}
        or any(marker in folded_text for marker in block_markers)
    ):
        status_text = (
            str(http_status)
            if http_status is not None
            else "bilinmiyor"
        )
        raise ConsulateAccessError(
            "Konsolosluk sitesi erişimi engelledi "
            f"(HTTP {status_text}). Bu sayfadan tarih okunmadı "
            "ve bildirim gönderilmedi."
        )


def parse_date(value: str) -> Optional[date]:
    """
    DD.MM.YYYY biçimindeki metni date nesnesine dönüştürür.
    """

    try:
        return datetime.strptime(
            value,
            "%d.%m.%Y",
        ).date()

    except ValueError:
        return None


# ============================================================
# 5) STATE DOSYASI
# Aynı tarih için tekrar mesaj gönderilmesini engeller.
# ============================================================

def load_state() -> dict:
    if not STATE_FILE.exists():
        logger.info(
            "State dosyası henüz yok. İlk çalışma kabul ediliyor."
        )
        return {}

    try:
        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            return json.load(file)

    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "State dosyası okunamadı. "
            "Boş state kullanılacak: %s",
            exc,
        )
        return {}


def save_state(state: dict) -> None:
    with STATE_FILE.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=4,
        )

    logger.info(
        "State dosyası güncellendi: %s",
        STATE_FILE,
    )


# ============================================================
# 6) WHATSAPP MESAJI
# ============================================================

def send_whatsapp_message(
    appointment_date: date,
) -> str:
    """
    Twilio Content SID üzerinden WhatsApp mesajı gönderir.
    """

    account_sid = required_env(
        "TWILIO_ACCOUNT_SID"
    )
    auth_token = required_env(
        "TWILIO_AUTH_TOKEN"
    )
    whatsapp_to = required_env(
        "TWILIO_WHATSAPP_TO"
    )
    whatsapp_from = required_env(
        "TWILIO_WHATSAPP_FROM"
    )
    content_sid = required_env(
        "TWILIO_CONTENT_SID"
    )

    appointment_text = appointment_date.strftime(
        "%d.%m.%Y"
    )

    logger.warning(
        "Erken randevu bulundu. "
        "WhatsApp mesajı gönderiliyor: %s",
        appointment_text,
    )

    client = Client(
        account_sid,
        auth_token,
    )

    message = client.messages.create(
        to=whatsapp_to,
        from_=whatsapp_from,
        content_sid=content_sid,
    )

    logger.warning(
        "WhatsApp mesajı Twilio sistemine iletildi. "
        "SID: %s | Durum: %s",
        message.sid,
        message.status,
    )

    return message.sid


# ============================================================
# 7) DEBUG DOSYALARI
# ============================================================

def save_page_debug_files(
    page: Page,
) -> None:
    """
    Normal sayfa ekran görüntüsünü ve metnini kaydeder.
    """

    try:
        page.screenshot(
            path=str(PAGE_SCREENSHOT),
            full_page=True,
        )

        logger.info(
            "Sayfa ekran görüntüsü kaydedildi: %s",
            PAGE_SCREENSHOT,
        )

    except Exception as exc:
        logger.warning(
            "Sayfa ekran görüntüsü alınamadı: %s",
            exc,
        )

    try:
        body_text = page.locator(
            "body"
        ).inner_text()

        PAGE_TEXT_FILE.write_text(
            body_text,
            encoding="utf-8",
        )

        logger.info(
            "Sayfa metni kaydedildi: %s",
            PAGE_TEXT_FILE,
        )

    except Exception as exc:
        logger.warning(
            "Sayfa metni kaydedilemedi: %s",
            exc,
        )


def save_error_files(
    page: Page,
) -> None:
    """
    Hata durumunda ekran görüntüsü, HTML ve sayfa metni kaydeder.
    """

    try:
        page.screenshot(
            path=str(ERROR_SCREENSHOT),
            full_page=True,
        )

        logger.error(
            "Hata ekran görüntüsü kaydedildi: %s",
            ERROR_SCREENSHOT,
        )

    except Exception as exc:
        logger.error(
            "Hata ekran görüntüsü alınamadı: %s",
            exc,
        )

    try:
        ERROR_HTML_FILE.write_text(
            page.content(),
            encoding="utf-8",
        )

        logger.error(
            "Hata HTML dosyası kaydedildi: %s",
            ERROR_HTML_FILE,
        )

    except Exception as exc:
        logger.error(
            "Hata HTML dosyası kaydedilemedi: %s",
            exc,
        )

    try:
        body_text = page.locator(
            "body"
        ).inner_text()

        ERROR_TEXT_FILE.write_text(
            body_text,
            encoding="utf-8",
        )

        logger.error(
            "Hata sayfa metni kaydedildi: %s",
            ERROR_TEXT_FILE,
        )

    except Exception as exc:
        logger.error(
            "Hata sayfa metni kaydedilemedi: %s",
            exc,
        )


# ============================================================
# 8) SELECT OPTION BİLGİLERİNİ OKUMA
# ============================================================

def get_select_options(
    select: Locator,
) -> list[dict]:
    """
    Tek bir select elementinin option metinlerini ve değerlerini okur.
    """

    return select.evaluate(
        """
        select => Array.from(select.options).map(option => ({
            text: (option.textContent || "").trim(),
            value: option.value
        }))
        """
    )


def select_contains_option(
    select: Locator,
    option_text: str,
) -> bool:
    """
    Select içinde istenen metni taşıyan bir option var mı kontrol eder.
    """

    wanted = normalize_text(
        option_text
    ).casefold()

    try:
        options = get_select_options(
            select
        )

    except Exception:
        return False

    for option in options:
        visible_text = normalize_text(
            option.get("text", "")
        ).casefold()

        if wanted == visible_text or wanted in visible_text:
            return True

    return False


# ============================================================
# 9) GİZLİ SELECT ÜZERİNDE JAVASCRIPT İLE SEÇİM
# ============================================================

def select_hidden_option_by_text(
    select: Locator,
    option_text: str,
    selection_type: str,
) -> str:
    """
    Bootstrap Select tarafından gizlenmiş select üzerinde
    JavaScript ile seçim yapar.
    """

    wanted = normalize_text(
        option_text
    ).casefold()

    options = get_select_options(
        select
    )

    selected_option = None

    # Önce tam eşleşme.
    for option in options:
        visible_text = normalize_text(
            option.get("text", "")
        )

        if visible_text.casefold() == wanted:
            selected_option = option
            break

    # Tam eşleşme bulunamazsa kısmi eşleşme.
    if selected_option is None:
        for option in options:
            visible_text = normalize_text(
                option.get("text", "")
            )

            if wanted in visible_text.casefold():
                selected_option = option
                break

    if selected_option is None:
        available_options = [
            normalize_text(
                option.get("text", "")
            )
            for option in options
        ]

        raise RuntimeError(
            f"'{option_text}' seçeneği bulunamadı. "
            f"Mevcut seçenekler: {available_options}"
        )

    selected_value = str(
        selected_option.get(
            "value",
            "",
        )
    )

    selected_text = normalize_text(
        selected_option.get(
            "text",
            "",
        )
    )

    logger.info(
        "JavaScript ile seçim hazırlanıyor: "
        "%s | value=%s",
        selected_text,
        selected_value,
    )

    select.evaluate(
        """
        (select, value) => {
            select.value = value;

            const inputEvent = new Event(
                "input",
                {
                    bubbles: true
                }
            );

            const changeEvent = new Event(
                "change",
                {
                    bubbles: true
                }
            );

            select.dispatchEvent(inputEvent);
            select.dispatchEvent(changeEvent);

            if (
                window.jQuery &&
                window.jQuery.fn &&
                window.jQuery.fn.selectpicker
            ) {
                try {
                    window.jQuery(select).selectpicker("refresh");
                    window.jQuery(select).selectpicker("render");
                } catch (error) {
                    console.log(error);
                }
            }
        }
        """,
        selected_value,
    )

    # Sitedeki inline JavaScript fonksiyonlarını ayrıca çağır.
    if selection_type == "country":
        select.evaluate(
            """
            select => {
                if (typeof window.setMissions === "function") {
                    window.setMissions(select.value);
                }
            }
            """
        )

    elif selection_type == "mission":
        select.evaluate(
            """
            select => {
                if (
                    typeof window.setSelectedMission === "function"
                ) {
                    window.setSelectedMission(select.value);
                }
            }
            """
        )

    select.page.wait_for_timeout(
        2_000
    )

    actual_value = select.input_value()

    logger.info(
        "Seçim tamamlandı: %s | Seçili value=%s",
        selected_text,
        actual_value,
    )

    return actual_value


# ============================================================
# 10) ÜLKE SELECT'İNİ BULMA
# ============================================================

def find_country_select(
    page: Page,
) -> Locator:
    """
    Almanya seçeneğini içeren ülke select'ini bulur.
    """

    preferred_selectors = [
        "#ddlCountries1",
        "#ddlCountries",
    ]

    for selector in preferred_selectors:
        locator = page.locator(
            selector
        )

        if locator.count() != 1:
            continue

        select = locator.first

        if select_contains_option(
            select,
            COUNTRY_NAME,
        ):
            logger.info(
                "Ülke select'i bulundu: %s",
                selector,
            )
            return select

    all_selects = page.locator(
        "select"
    )

    for index in range(
        all_selects.count()
    ):
        select = all_selects.nth(
            index
        )

        if select_contains_option(
            select,
            COUNTRY_NAME,
        ):
            select_id = select.get_attribute(
                "id"
            )

            logger.info(
                "Ülke select'i seçeneklerden bulundu. "
                "ID: %s",
                select_id,
            )

            return select

    raise RuntimeError(
        "Almanya seçeneğini içeren ülke select'i bulunamadı."
    )


# ============================================================
# 11) TEMSİLCİLİK SELECT'İNİ BULMA
# ============================================================

def find_mission_select(
    page: Page,
) -> Optional[Locator]:
    """
    Sayfadaki iki olası temsilcilik select'ini ayrı ayrı kontrol eder.

    #ddlMission1
    #ddlMission
    """

    preferred_selectors = [
        "#ddlMission1",
        "#ddlMission",
    ]

    for selector in preferred_selectors:
        locator = page.locator(
            selector
        )

        if locator.count() != 1:
            continue

        select = locator.first

        if select_contains_option(
            select,
            MISSION_NAME,
        ):
            logger.info(
                "Düsseldorf seçeneğini içeren "
                "temsilcilik select'i bulundu: %s",
                selector,
            )

            return select

    # ID'ler değişirse tüm select alanlarını kontrol et.
    all_selects = page.locator(
        "select"
    )

    for index in range(
        all_selects.count()
    ):
        select = all_selects.nth(
            index
        )

        if select_contains_option(
            select,
            MISSION_NAME,
        ):
            select_id = select.get_attribute(
                "id"
            )

            logger.info(
                "Düsseldorf seçeneğini içeren temsilcilik "
                "select'i genel taramada bulundu. ID: %s",
                select_id,
            )

            return select

    return None


def wait_for_mission_select(
    page: Page,
) -> Locator:
    """
    Almanya seçildikten sonra Düsseldorf seçeneğinin
    temsilcilik listesine yüklenmesini bekler.
    """

    logger.info(
        "Düsseldorf temsilcilik seçeneğinin yüklenmesi bekleniyor."
    )

    start_time = time.monotonic()

    while True:
        mission_select = find_mission_select(
            page
        )

        if mission_select is not None:
            return mission_select

        elapsed_seconds = (
            time.monotonic() - start_time
        )

        if (
            elapsed_seconds
            >= MISSION_LOAD_TIMEOUT_SECONDS
        ):
            raise RuntimeError(
                "Almanya seçildi ancak Düsseldorf Başkonsolosluğu "
                "seçeneği temsilcilik listesine yüklenmedi."
            )

        page.wait_for_timeout(
            500
        )


# ============================================================
# 12) ONAY BUTONU
# ============================================================

def click_confirmation_button(
    page: Page,
) -> None:
    """
    Temsilcilik seçimi sonrası açılabilecek onay düğmesine tıklar.
    """

    possible_texts = [
        "Evet, bu temsilcilikte işlem yapmak istiyorum",
        "İşlemlere burada devam et",
        "Burada devam et",
    ]

    for text in possible_texts:
        locator = page.get_by_text(
            text,
            exact=False,
        )

        for index in range(
            locator.count()
        ):
            item = locator.nth(
                index
            )

            try:
                if not item.is_visible():
                    continue

                item.click()

                logger.info(
                    "Onay butonuna tıklandı: %s",
                    text,
                )

                page.wait_for_timeout(
                    3_000
                )
                return

            except Exception:
                continue

    possible_selectors = [
        "button:has-text('Evet')",
        "a:has-text('Evet')",
        "button:has-text('Devam')",
        "a:has-text('Devam')",
    ]

    for selector in possible_selectors:
        locator = page.locator(
            selector
        )

        for index in range(
            locator.count()
        ):
            item = locator.nth(
                index
            )

            try:
                if not item.is_visible():
                    continue

                item.click()

                logger.info(
                    "Genel onay/devam butonuna tıklandı."
                )

                page.wait_for_timeout(
                    3_000
                )
                return

            except Exception:
                continue

    logger.info(
        "Ek onay butonu görünmedi."
    )


# ============================================================
# 13) ÜLKE VE TEMSİLCİLİK SEÇİMİ
# ============================================================

def prepare_consulate_session(
    page: Page,
) -> None:
    """
    Ana sayfada Almanya ve Düsseldorf Başkonsolosluğu seçer.
    """

    logger.info(
        "Konsolosluk ana sayfası açılıyor: %s",
        HOME_URL,
    )

    response = page.goto(
        HOME_URL,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )

    page.wait_for_timeout(
        4_000
    )

    ensure_consulate_page_is_accessible(
        page,
        response.status if response is not None else None,
    )

    logger.info(
        "Ana sayfa açıldı. URL: %s",
        page.url,
    )

    # --------------------------------------------------------
    # Almanya seçimi
    # --------------------------------------------------------

    country_select = find_country_select(
        page
    )

    logger.info(
        "Ülke JavaScript ile seçiliyor: %s",
        COUNTRY_NAME,
    )

    select_hidden_option_by_text(
        select=country_select,
        option_text=COUNTRY_NAME,
        selection_type="country",
    )

    # --------------------------------------------------------
    # Düsseldorf seçeneğinin yüklenmesini bekle
    # --------------------------------------------------------

    mission_select = wait_for_mission_select(
        page
    )

    # --------------------------------------------------------
    # Düsseldorf seçimi
    # --------------------------------------------------------

    logger.info(
        "Temsilcilik JavaScript ile seçiliyor: %s",
        MISSION_NAME,
    )

    select_hidden_option_by_text(
        select=mission_select,
        option_text=MISSION_NAME,
        selection_type="mission",
    )

    page.wait_for_timeout(
        2_000
    )

    click_confirmation_button(
        page
    )

    logger.info(
        "Ülke ve temsilcilik seçimi tamamlandı. URL: %s",
        page.url,
    )


# ============================================================
# 14) RANDEVU SAYFASINI AÇMA
# ============================================================

def open_appointment_page(
    page: Page,
) -> None:
    """
    Aynı tarayıcı oturumunda randevu sayfasını açar.
    """

    logger.info(
        "Randevu sayfası açılıyor: %s",
        APPOINTMENT_URL,
    )

    page.goto(
        APPOINTMENT_URL,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )

    page.wait_for_timeout(
        APPOINTMENT_LOAD_WAIT_MS
    )

    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=15_000,
        )

    except PlaywrightTimeoutError:
        logger.warning(
            "Networkidle bekleme süresi doldu. "
            "Sayfa mevcut haliyle kontrol edilecek."
        )

    logger.info(
        "Randevu sayfası açıldı. Gerçek URL: %s",
        page.url,
    )


# ============================================================
# 15) RANDEVU TARİHİNİ OKUMA
# ============================================================

def extract_date_from_table(
    page: Page,
) -> Optional[date]:
    """
    Tablo satırında Düsseldorf ve tarihi arar.
    """

    rows = page.locator(
        "table tr"
    )

    row_count = rows.count()

    logger.info(
        "Tablo satırı sayısı: %s",
        row_count,
    )

    for index in range(
        row_count
    ):
        row = rows.nth(
            index
        )

        try:
            row_text = normalize_text(
                row.inner_text()
            )

        except Exception:
            continue

        if (
            "düsseldorf"
            not in row_text.casefold()
        ):
            continue

        logger.info(
            "Düsseldorf satırı bulundu: %s",
            row_text,
        )

        date_strings = re.findall(
            r"(?<!\d)\d{2}\.\d{2}\.\d{4}(?!\d)",
            row_text,
        )

        for date_string in date_strings:
            appointment_date = parse_date(
                date_string
            )

            if appointment_date:
                logger.info(
                    "Randevu tarihi tablo satırından okundu: %s",
                    appointment_date.strftime(
                        "%d.%m.%Y"
                    ),
                )

                return appointment_date

    return None


def extract_appointment_date(
    page: Page,
) -> date:
    """
    Tarihi yalnızca doğrulanmış Düsseldorf tablo satırından okur.
    Yanlış bildirim riskine karşı genel sayfa metni kullanılmaz.
    """

    appointment_date = extract_date_from_table(
        page
    )

    if appointment_date:
        return appointment_date

    raise RuntimeError(
        "Randevu tarihi doğrulanmış Düsseldorf tablo satırından "
        "okunamadı. Yanlış bildirim riskini önlemek için işlem "
        "durduruldu."
    )


# ============================================================
# 16) TARAYICI BAĞLAMI
# ============================================================

def create_browser_context(
    browser: Browser,
) -> BrowserContext:
    """
    Türkçe ve Europe/Berlin saat diliminde tarayıcı bağlamı oluşturur.
    """

    return browser.new_context(
        locale="tr-TR",
        timezone_id="Europe/Berlin",
        viewport={
            "width": 1600,
            "height": 1000,
        },
    )


# ============================================================
# 17) RANDEVU SAYFASINI KONTROL ET
# ============================================================

def check_appointment_page() -> date:
    """
    Almanya ve Düsseldorf seçimini yapar.
    Erişim engeli/zaman aşımında temiz oturumla yeniden dener.
    Tarihi yalnızca doğrulanmış tablodan okur.
    """

    last_error: Optional[Exception] = None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=HEADLESS,
        )

        try:
            for attempt in range(1, MAX_ACCESS_ATTEMPTS + 1):
                context = create_browser_context(
                    browser
                )
                page = context.new_page()
                page.set_default_timeout(
                    PAGE_TIMEOUT_MS
                )

                logger.info(
                    "Konsolosluk erişim denemesi: %s/%s",
                    attempt,
                    MAX_ACCESS_ATTEMPTS,
                )

                try:
                    prepare_consulate_session(
                        page
                    )
                    open_appointment_page(
                        page
                    )
                    ensure_consulate_page_is_accessible(
                        page
                    )
                    save_page_debug_files(
                        page
                    )

                    appointment_date = extract_appointment_date(
                        page
                    )

                    logger.info(
                        "Düsseldorf için bulunan randevu tarihi: %s",
                        appointment_date.strftime("%d.%m.%Y"),
                    )
                    return appointment_date

                except (ConsulateAccessError, PlaywrightTimeoutError) as exc:
                    last_error = exc
                    save_error_files(
                        page
                    )

                    if attempt >= MAX_ACCESS_ATTEMPTS:
                        break

                    wait_seconds = ACCESS_RETRY_WAIT_SECONDS[
                        attempt - 1
                    ]
                    logger.warning(
                        "Konsolosluk sitesine erişilemedi: %s "
                        "Temiz oturumla %s saniye sonra yeniden denenecek.",
                        exc,
                        wait_seconds,
                    )
                    time.sleep(
                        wait_seconds
                    )

                except Exception:
                    save_error_files(
                        page
                    )
                    raise

                finally:
                    context.close()

        finally:
            browser.close()

    raise ConsulateAccessError(
        f"Konsolosluk sitesine {MAX_ACCESS_ATTEMPTS} denemede erişilemedi. "
        "Tarih okunmadı ve WhatsApp bildirimi gönderilmedi. "
        f"Son hata: {last_error}"
    )


# ============================================================
# 18) TARİH KARŞILAŞTIRMASI VE MESAJ KARARI
# ============================================================

def process_appointment_date(
    appointment_date: date,
) -> None:
    """
    Sadece bulunan tarih 15.09.2026'dan önceyse mesaj gönderir.
    """

    found_text = appointment_date.strftime(
        "%d.%m.%Y"
    )

    current_text = CURRENT_APPOINTMENT_DATE.strftime(
        "%d.%m.%Y"
    )

    logger.info(
        "Bulunan tarih: %s | Mevcut randevun: %s",
        found_text,
        current_text,
    )

    # 15.09.2026 veya daha sonraki bir tarih için mesaj gönderme.
    if appointment_date >= CURRENT_APPOINTMENT_DATE:
        logger.info(
            "Bulunan tarih mevcut randevundan daha erken değil. "
            "WhatsApp mesajı gönderilmedi."
        )
        return

    logger.warning(
        "ERKEN RANDEVU BULUNDU: %s",
        found_text,
    )

    state = load_state()

    last_notified_date = state.get(
        "last_notified_date"
    )

    if last_notified_date == found_text:
        logger.info(
            "%s tarihi daha önce bildirildi. "
            "Mesaj hakkını korumak için tekrar mesaj gönderilmedi.",
            found_text,
        )
        return

    message_sid = send_whatsapp_message(
        appointment_date
    )

    state.update(
        {
            "last_notified_date": found_text,
            "last_notification_time": (
                datetime.now().isoformat(
                    timespec="seconds"
                )
            ),
            "twilio_message_sid": message_sid,
            "current_appointment_date": current_text,
        }
    )

    save_state(
        state
    )


# ============================================================
# 19) MAIN
# ============================================================

def main() -> None:
    logger.info("=" * 80)

    logger.info(
        "Düsseldorf konsolosluk randevu kontrolü başladı."
    )

    logger.info(
        "Mesaj yalnızca %s tarihinden önceki "
        "randevular için gönderilecek.",
        CURRENT_APPOINTMENT_DATE.strftime(
            "%d.%m.%Y"
        ),
    )

    logger.info(
        "Headless modu: %s",
        HEADLESS,
    )

    appointment_date = check_appointment_page()

    process_appointment_date(
        appointment_date
    )

    logger.info(
        "Kontrol başarıyla tamamlandı."
    )

    logger.info("=" * 80)


if __name__ == "__main__":
    try:
        main()

    except PlaywrightTimeoutError as exc:
        logger.exception(
            "Konsolosluk sitesi zaman aşımına uğradı: %s",
            exc,
        )
        sys.exit(1)

    except Exception as exc:
        logger.exception(
            "Program hata ile tamamlandı: %s",
            exc,
        )
        sys.exit(1)
