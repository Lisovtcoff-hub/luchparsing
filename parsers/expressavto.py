"""Модуль expressavto.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import os
import re
import time
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

from core.contracts import CalcParams, CalcResult, InvalidInputError, TemporaryError
from core.selenium_base import SyncSeleniumAdapter


CITY_TO_VALUE = {
    "Абакан": "405",
    "Березники": "3",
    "Екатеринбург": "1",
    "Ижевск": "4",
    "Иркутск": "407",
    "Казань": "5",
    "Красноярск": "182",
    "Курган": "6",
    "Магнитогорск": "7",
    "Миасс": "8",
    "Москва": "2",
    "Набережные Челны": "9",
    "Нефтеюганск": "10",
    "Нижневартовск": "11",
    "Нижний Новгород": "12",
    "Нижний Тагил": "13",
    "Новосибирск": "14",
    "Новый Уренгой": "15",
    "Ноябрьск": "16",
    "Омск": "17",
    "Оренбург": "18",
    "Орск": "19",
    "Пермь": "20",
    "Самара": "21",
    "Санкт-Петербург": "22",
    "Сургут": "23",
    "Тюмень": "24",
    "Уфа": "25",
    "Ханты-Мансийск": "26",
    "Челябинск": "27",
}

EXPRESSAVTO_SOURCE = "https://expressauto.ru/"


def _wait_stable_total(
    driver: webdriver.Chrome,
    css: str,
    timeout: float = 45.0,
    stable_for: float = 2.0,
    poll: float = 0.1,
) -> int:
    end = time.time() + timeout
    last_val: Optional[int] = None
    last_change = time.time()
    seen_change = False
    max_seen: Optional[int] = None

    while time.time() < end:
        try:
            text = driver.find_element(By.CSS_SELECTOR, css).text.strip()
        except Exception:
            time.sleep(poll)
            continue

        digits = re.sub(r"[^\d]", "", text)
        val = int(digits) if digits else None

        if val is None:
            time.sleep(poll)
            continue

        if max_seen is None or val > max_seen:
            max_seen = val

        if last_val is None:
            last_val = val
            last_change = time.time()
        elif val != last_val:
            last_val = val
            last_change = time.time()
            seen_change = True
        else:
            if seen_change and max_seen is not None and val == max_seen and (time.time() - last_change) >= stable_for:
                return max_seen

        time.sleep(poll)

    if max_seen is not None:
        return max_seen
    raise TimeoutException("calc_total did not become numeric in time")


def expressavto(from_city: str, to_city: str, places: int, weight: int, volume: float, driver: webdriver.Chrome) -> int:
    try:
        from_value = CITY_TO_VALUE[from_city]
    except KeyError:
        raise ValueError(f"Неизвестный город 'откуда': {from_city!r}. Добавьте его в CITY_TO_VALUE.")
    try:
        to_value = CITY_TO_VALUE[to_city]
    except KeyError:
        raise ValueError(f"Неизвестный город 'куда': {to_city!r}. Добавьте его в CITY_TO_VALUE.")

    service = Service(executable_path=os.getenv("CHROMEDRIVER", "/usr/bin/chromedriver"))

    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.page_load_strategy = "eager"
    chrome_options.add_argument("--window-size=1366,768")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-infobars")
    chrome_options.add_argument("--lang=ru-RU")

    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.geolocation": 2,
    }
    chrome_options.add_experimental_option("prefs", prefs)

    if driver is None:
        raise TemporaryError("expressavto: driver is required")

    try:
        driver.set_page_load_timeout(25)
        wait = WebDriverWait(driver, 15, poll_frequency=0.2)

        driver.get(EXPRESSAVTO_SOURCE)

        # Иногда калькулятор появляется не сразу, ждееееееееем
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".calc_place")))
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".calc_place .calculator")))

        def select_visible_option(css_selector: str, value: str) -> None:
            sel = wait.until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, f"{css_selector}:not([style*='display:none'])"))
            )
            Select(sel).select_by_value(value)
            driver.execute_script(
                "var el=arguments[0]; var ev=new Event('change',{bubbles:true}); el.dispatchEvent(ev);",
                sel,
            )

        select_visible_option("select.c_from_select", from_value)
        select_visible_option("select.c_to_select", to_value)

        weight_input = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input.ci_weight")))
        volume_input = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input.ci_volume")))

        # Вес
        weight_input.click()
        weight_input.send_keys(Keys.CONTROL, "a")
        weight_input.send_keys(Keys.BACKSPACE)
        weight_input.send_keys(f"{weight}")
        driver.execute_script("arguments[0].dispatchEvent(new Event('input',{bubbles:true}));", weight_input)
        driver.execute_script("arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", weight_input)
        weight_input.send_keys(Keys.TAB)

        # Объём
        volume_input.click()
        volume_input.send_keys(Keys.CONTROL, "a")
        volume_input.send_keys(Keys.BACKSPACE)
        volume_input.send_keys(f"{round(float(volume), 3)}")
        driver.execute_script("arguments[0].dispatchEvent(new Event('input',{bubbles:true}));", volume_input)
        driver.execute_script("arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", volume_input)
        volume_input.send_keys(Keys.TAB)

        wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".calc_place .calc_total")))
        return _wait_stable_total(driver, ".calc_place .calc_total", timeout=45.0, stable_for=2.0, poll=0.1)

    except TimeoutException as e:
        raise TemporaryError(f"expressavto: timeout waiting for price: {e}") from e
    except WebDriverException as e:
        raise TemporaryError(f"expressavto: webdriver error: {type(e).__name__}: {e}") from e
    finally:
        pass


class ExpressAvtoAdapter(SyncSeleniumAdapter):
    code = "expressavto"
    TIMEOUT = 60.0
    HEADLESS = True

    def _calc_sync(self, p: CalcParams, driver: webdriver.Chrome) -> CalcResult:
        try:
            price = expressavto(p.from_city, p.to_city, int(p.places), int(round(p.weight_kg)), float(p.volume_m3), driver=driver)
        except ValueError as e:
            raise InvalidInputError(str(e)) from e
        except TemporaryError:
            raise
        except Exception as e:
            raise TemporaryError(f"expressavto: unexpected {type(e).__name__}: {e}") from e

        return CalcResult(
            price=float(price) if price is not None else None,
            currency="RUB",
            days=None,
            source=EXPRESSAVTO_SOURCE,
            name_tarif=None,
            allowances=None,
        )


__all__ = ["expressavto", "ExpressAvtoAdapter", "CITY_TO_VALUE"]
