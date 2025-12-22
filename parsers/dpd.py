"""Модуль dpd.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import time, re

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import ElementClickInterceptedException
from webdriver_manager.chrome import ChromeDriverManager
from core.contracts import CalcParams, CalcResult, InvalidInputError, TemporaryError
from core.selenium_base import SyncSeleniumAdapter

URL = "https://dpd.ru/calc"


def build_driver():
    """Функция build_driver.

    Возвращает:
        Результат выполнения функции.
    """
    opts = Options()
    opts.set_capability("pageLoadStrategy", "eager")
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--no-first-run")
    opts.add_argument("--mute-audio")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--no-sandbox")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    driver.set_page_load_timeout(12)

    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd(
        "Network.setBlockedURLs",
        {
            "urls": [
                "*.mp4",
                "*.webm",
                "*.avi",
                "*doubleclick.net/*",
                "*googletagmanager.com/*",
                "*google-analytics.com/*",
            ]
        },
    )
    return driver


def js_set_value(driver, el, value):
    """Функция js_set_value.

    Параметры:
        driver: Описание параметра.
        el: Описание параметра.
        value: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    driver.execute_script(
        """
        const el = arguments[0], val = arguments[1].toString();
        el.value = val;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    """,
        el,
        str(value),
    )


def set_number_field(wait: WebDriverWait, name, value):
    """Функция set_number_field.

    Параметры:
        wait: Описание параметра.
        name: Описание параметра.
        value: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    driver = wait._driver
    el = WebDriverWait(driver, 8, 0.1).until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, f"div.calc-sizes__exact input.number-field__input[name='{name}']")
        )
    )
    js_set_value(driver, el, value)


def set_places(wait: WebDriverWait, count: int):
    """Функция set_places.

    Параметры:
        wait: Описание параметра.
        count: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    driver = wait._driver
    el = WebDriverWait(driver, 8, 0.1).until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, "div.calc-sizes__exact div.count-field input.count-field__input")
        )
    )
    js_set_value(driver, el, count)


def set_declared_value(wait: WebDriverWait, value):
    """Функция set_declared_value.

    Параметры:
        wait: Описание параметра.
        value: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    driver = wait._driver
    el = WebDriverWait(driver, 8, 0.1).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input.text-field__input[name='DeclaredValue']"))
    )
    js_set_value(driver, el, value)


def type_city(wait: WebDriverWait, legend_text, placeholder_text, query, option_substr):
    """Функция type_city.

    Параметры:
        wait: Описание параметра.
        legend_text: Описание параметра.
        placeholder_text: Описание параметра.
        query: Описание параметра.
        option_substr: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    driver = wait._driver
    inp = WebDriverWait(driver, 8, 0.1).until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, f"div[legend='{legend_text}'] input.multiselect__input[placeholder='{placeholder_text}']")
        )
    )
    inp.find_element(By.XPATH, "./ancestor::div[contains(@class,'multiselect')]").click()
    inp.send_keys(Keys.CONTROL, "a")
    inp.send_keys(Keys.DELETE)
    inp.send_keys(query)
    opt = WebDriverWait(driver, 12, 0.1).until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                f"//div[@legend='{legend_text}']//span[contains(@class,'multiselect__option')]/span[contains(normalize-space(),\"{option_substr}\")]",
            )
        )
    )
    try:
        opt.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", opt)


def click_proceed(wait: WebDriverWait):
    """Функция click_proceed.

    Параметры:
        wait: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    driver = wait._driver
    btn = WebDriverWait(driver, 15, 0.1).until(
        EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button.calc-form__button.ui-button_OqgQF.button_Tj27p.full-width_NJ54H")
        )
    )
    try:
        btn.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", btn)


def get_first_tariff_price_and_time(wait: WebDriverWait):
    """Функция get_first_tariff_price_and_time.

    Параметры:
        wait: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    driver = wait._driver
    WebDriverWait(driver, 25, 0.2).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table.calc-tariffs__table")))
    row = driver.find_element(By.CSS_SELECTOR, "table.calc-tariffs__table tbody tr")
    price = row.find_element(By.CSS_SELECTOR, "td div.calc-tariffs__table-cell_cost-inner").text.strip()
    time_ = row.find_element(By.CSS_SELECTOR, "td.calc-tariffs__table-cell_right").text.strip()
    return price, time_


def dpd_calc(
    from_city: str, to_city: str, places: int, weight_kg: float, volume_m3: float, dims_cm_json: dict
) -> tuple[float, str]:
    """Функция dpd_calc.

    Параметры:
        from_city: Описание параметра.
        to_city: Описание параметра.
        places: Описание параметра.
        weight_kg: Описание параметра.
        volume_m3: Описание параметра.
        dims_cm_json: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    driver = build_driver()
    wait = WebDriverWait(driver, 6, 0.1)
    try:
        driver.get(URL)

        type_city(wait, "Отправление", "Откуда", from_city, f"г. {from_city}")
        type_city(wait, "Получение", "Куда", to_city, f"г. {to_city}")

        set_number_field(wait, "length", float(dims_cm_json["l"]))
        set_number_field(wait, "width", float(dims_cm_json["w"]))
        set_number_field(wait, "height", float(dims_cm_json["h"]))
        set_number_field(wait, "weight", float(weight_kg) / max(1, int(places)))
        set_places(wait, int(places))

        set_declared_value(wait, 0)

        click_proceed(wait)

        price_text, time_text = get_first_tariff_price_and_time(wait)

        days_text = time_text or ""
        time.sleep(5)
        return float(re.sub(r"[^\d.,]", "", price_text).replace(",", ".")), days_text.split()[0]

    finally:
        try:
            driver.quit()
        except Exception:
            pass


class DpdAdapter(SyncSeleniumAdapter):
    """Класс DpdAdapter.

    Инкапсулирует связанную функциональность модуля.
    """
    code = "dpd"
    TIMEOUT = 90.0
    HEADLESS = True

    def _calc_sync(self, p: CalcParams) -> CalcResult:
        """Функция _calc_sync.

        Параметры:
            p: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        try:
            dims = {
                "l": float(p.dims.length_cm),
                "w": float(p.dims.width_cm),
                "h": float(p.dims.height_cm),
            }
            price, days_text = dpd_calc(
                p.from_city,
                p.to_city,
                int(p.places),
                float(p.weight_kg),
                float(p.volume_m3),
                dims,
            )
        except ValueError as e:
            raise InvalidInputError(str(e)) from e
        except TemporaryError:
            raise
        except Exception as e:
            raise TemporaryError(f"dpd: unexpected {type(e).__name__}") from e

        return CalcResult(
            price=float(price) if price is not None else None,
            currency="RUB",
            days=days_text,
            source=URL,
            name_tarif=None,
            allowances={},
        )


__all__ = ["dpd_calc", "DpdAdapter", "build_driver"]



