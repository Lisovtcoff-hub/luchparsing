
"""Модуль magic_trans.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import re
import time
from typing import Dict, Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException

from core.contracts import CalcParams, CalcResult, InvalidInputError, TemporaryError
from core.selenium_base import SyncSeleniumAdapter


def set_city(driver, wait, selector: str, text: str) -> None:
    """Функция set_city.

    Параметры:
        driver: Описание параметра.
        wait: Описание параметра.
        selector: Описание параметра.
        text: Описание параметра.
    """
    inp = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
    inp.click()
    inp.clear()
    inp.send_keys(text)
    time.sleep(0.25)
    inp.send_keys(Keys.TAB)
    driver.execute_script(
        "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
        "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
        inp,
    )
    time.sleep(0.1)


def magic_trans_calc(
    from_city: str,
    to_city: str,
    places: int,
    weight: int,
    volume: float,
    dims: dict,
    show_browser: bool = False,
) -> tuple[Optional[float], Optional[int], Dict[str, float]]:
    """Возвращает (price, days, allowances)."""
    chrome_options = webdriver.ChromeOptions()
    if not show_browser:
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--disable-gpu")

    chrome_options.page_load_strategy = "eager"
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-infobars")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")

    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.geolocation": 2,
        "profile.default_content_setting_values.media_stream_mic": 2,
        "profile.default_content_setting_values.media_stream_camera": 2,
        "intl.accept_languages": "ru-RU,ru",
    }
    chrome_options.add_experimental_option("prefs", prefs)

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    driver.set_page_load_timeout(20)
    wait = WebDriverWait(driver, 15, poll_frequency=0.2)

    def _to_float(s: str) -> Optional[float]:
        """Функция _to_float.

        Параметры:
            s: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        s = (s or "").replace("\xa0", " ").strip()
        digits = re.sub(r"[^\d,.\-]", "", s).replace(" ", "").replace(",", ".")
        try:
            return float(digits) if digits else None
        except Exception:
            return None

    def _rub_to_int(s: str) -> Optional[int]:
        """Функция _rub_to_int.

        Параметры:
            s: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        s = (s or "").replace("\xa0", " ").strip()
        m = re.search(r"(\d[\d\s]*)\s*руб", s)
        if not m:
            return None
        try:
            return int(m.group(1).replace(" ", ""))
        except Exception:
            return None

    try:
        driver.get("https://magic-trans.ru/")

        try:
            btn_cookie = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//button[contains(.,'Принять') or contains(.,'Согласен') or contains(.,'Ок')]")
                )
            )
            try:
                btn_cookie.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", btn_cookie)
        except TimeoutException:
            pass

        set_city(driver, wait, "#input-city-from", from_city)
        time.sleep(0.5)
        set_city(driver, wait, "#input-city-to", to_city)

        mass_in = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input#input-mass")))
        mass_in.click()
        mass_in.send_keys(Keys.CONTROL, "a")
        mass_in.send_keys(str(weight))
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
            mass_in,
        )

        cube_in = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input#input-cube")))
        cube_in.click()
        cube_in.send_keys(Keys.CONTROL, "a")
        cube_in.send_keys(str(volume))
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
            cube_in,
        )

        btn_detail = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='knopka_podrobniy_raschet'].submit-main-calculate"))
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn_detail)
        try:
            btn_detail.click()
        except Exception:
            driver.execute_script("arguments[0].click();", btn_detail)

        qty = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[data-name='quantity-of-cargo']")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", qty)
        qty.click()
        qty.send_keys(Keys.CONTROL, "a")
        qty.send_keys(str(places))
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
            qty,
        )
        qty.send_keys(Keys.TAB)

        res = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "#res-calculation")))

        price_text = WebDriverWait(driver, 60, poll_frequency=0.2).until(
            lambda d: next(
                (
                    t.strip()
                    for t in [el.text for el in d.find_elements(By.CSS_SELECTOR, "#res-calculation table.table-summ-first td:nth-child(2)")]
                    if _rub_to_int(t) not in (None, 0)
                ),
                None,
            )
        )

        additional_text = res.find_element(By.CSS_SELECTOR, "table.k-calc-table-toggle tbody tr td:nth-child(2)").text.strip()

        days_text = res.find_element(By.CSS_SELECTOR, "table.table-summ-first tbody tr td.totals span").text.strip()
        m = re.search(r"\d+", days_text or "")
        days = int(m.group()) if m else None

        price_val = _to_float(price_text or "")
        allowances: Dict[str, float] = {}
        add_val = _to_float(additional_text or "")
        if add_val is not None:
            allowances["Доп. услуги"] = float(add_val)

        return price_val, days, allowances

    finally:
        try:
            driver.quit()
        except Exception:
            pass


class MagicTransAdapter(SyncSeleniumAdapter):
    """Класс MagicTransAdapter.

    Инкапсулирует связанную функциональность модуля.
    """
    code = "magic-trans"
    TIMEOUT = 80.0
    HEADLESS = True

    def _calc_sync(self, p: CalcParams) -> CalcResult:
        """Функция _calc_sync.

        Параметры:
            p: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        try:
            dims = {"l": float(p.dims.length_cm), "w": float(p.dims.width_cm), "h": float(p.dims.height_cm)}
            price, days, allowances = magic_trans_calc(
                p.from_city,
                p.to_city,
                int(p.places),
                int(round(p.weight_kg)),
                float(p.volume_m3),
                dims,
                show_browser=False,
            )
        except ValueError as e:
            raise InvalidInputError(str(e)) from e
        except TemporaryError:
            raise
        except Exception as e:
            raise TemporaryError(f"magic-trans: unexpected {type(e).__name__}: {e}") from e

        return CalcResult(
            price=float(price) if price is not None else None,
            currency="RUB",
            days=days,
            source="https://magic-trans.ru/",
            name_tarif=None,
            allowances=allowances or {},
        )


__all__ = ["set_city", "magic_trans_calc", "MagicTransAdapter"]
