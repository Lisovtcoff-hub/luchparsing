"""Модуль jde.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import json
import re
import os
import time
import html as _html
from pathlib import Path
from typing import Dict, Tuple, Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException

from core.contracts import CalcParams, CalcResult, InvalidInputError, TemporaryError
from core.selenium_base import SyncSeleniumAdapter

_CITY_ID_CACHE: Optional[dict] = None


def _load_city_ids() -> dict:
    global _CITY_ID_CACHE
    if _CITY_ID_CACHE is not None:
        return _CITY_ID_CACHE
    city_ids_path = Path(__file__).resolve().parents[1] / "addings" / "jde_city_ids.json"
    with city_ids_path.open("r", encoding="utf-8") as f:
        _CITY_ID_CACHE = json.load(f)
    return _CITY_ID_CACHE


def jde_calc(from_city: str, to_city: str, places: int, weight: int, volume: float) -> Tuple[Optional[int], Optional[str], Dict[str, float]]:
    """
    Возвращает (итоговая_цена_руб, срок_текстом, надбавки).
    """
    city_to_id = _load_city_ids()

    if from_city not in city_to_id:
        raise ValueError(f"Неизвестный 'Откуда': {from_city!r}. Добавьте ID в addings/jde_city_ids.json.")
    if to_city not in city_to_id:
        raise ValueError(f"Неизвестный 'Куда': {to_city!r}. Добавьте ID в addings/jde_city_ids.json.")

    from_id = city_to_id[from_city]
    to_id = city_to_id[to_city]

    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1366,768")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-infobars")
    chrome_options.page_load_strategy = "eager"
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.geolocation": 2,
    }
    chrome_options.add_experimental_option("prefs", prefs)

    driver = webdriver.Chrome(
        service=Service(executable_path=os.getenv("CHROMEDRIVER", "/usr/bin/chromedriver")),
        options=chrome_options,
    )

    wait = WebDriverWait(driver, 20, poll_frequency=0.2)

    def click_best(el):
        """Функция click_best.

        Параметры:
            el: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        time.sleep(0.15)
        try:
            el.click()
        except ElementClickInterceptedException:
            driver.execute_script("arguments[0].click();", el)

    def norm_label(s: str) -> str:
        """Функция norm_label.

        Параметры:
            s: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        s = " ".join((s or "").split()).lower()
        s = s.replace("c", "с")
        return s
    
    def norm_city(s: str) -> str:
        s = (s or "").strip()
        # берём только "город" до скобок
        if "(" in s:
            s = s.split("(", 1)[0]
        s = " ".join(s.split()).lower()
        s = s.replace("ё", "е")
        s = s.replace("c", "с")  # на всякий, как в norm_label
        return s

    def extract_rub(text: str) -> int:
        """Функция extract_rub.

        Параметры:
            text: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        digits = re.sub(r"[^\d]", "", text or "")
        return int(digits) if digits else 0

    try:
        driver.get("https://www.jde.ru/")

        try:
            btn_cookie = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "#cookies_accept, button#cookie_accept, .cookies button"))
            )
            click_best(btn_cookie)
        except TimeoutException:
            pass

        form = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "form#Calculator")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", form)
        time.sleep(0.2)

        def set_select2_value(select_css: str, city_text: str, city_id: str):
            """Функция set_select2_value.

            Параметры:
                select_css: Описание параметра.
                city_text: Описание параметра.
                city_id: Описание параметра.

            Возвращает:
                Результат выполнения функции.
            """
            js = """
                const sel = document.querySelector(arguments[0]);
                const $sel = window.jQuery ? window.jQuery(sel) : null;
                const text = arguments[1];
                const val  = arguments[2];

                let opt = sel.querySelector(`option[value="${val}"]`);
                if (!opt) {
                    opt = new Option(text, val, true, true);
                    sel.appendChild(opt);
                } else {
                    opt.selected = true;
                }

                if ($sel && $sel.length && $sel.data('select2')) {
                    $sel.val(val).trigger('change');
                } else {
                    sel.value = val;
                    sel.dispatchEvent(new Event('change', {bubbles:true}));
                }
            """
            driver.execute_script(js, select_css, city_text, city_id)
            rendered_css = f"{select_css} + span.select2 .select2-selection__rendered"
            WebDriverWait(driver, 10).until(
                lambda d: (d.find_element(By.CSS_SELECTOR, rendered_css).get_attribute("title") or
                           d.find_element(By.CSS_SELECTOR, rendered_css).text or "").strip().endswith(city_text)
            )

        set_select2_value("form#Calculator select[name='from']", from_city, from_id)
        set_select2_value("form#Calculator select[name='to']", to_city, to_id)

        weight_input = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "form#Calculator input[name='weight']")))
        volume_input = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "form#Calculator input[name='volume']")))
        weight_input.clear()
        weight_input.send_keys(str(weight))
        volume_input.clear()
        volume_input.send_keys(str(round(float(volume), 3)))

        try:
            WebDriverWait(driver, 3).until(EC.invisibility_of_element_located((By.CSS_SELECTOR, ".select2-container--open")))
        except TimeoutException:
            driver.switch_to.active_element.send_keys(Keys.ESCAPE)

        calc_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "form#Calculator button[type='submit']")))
        click_best(calc_btn)

        before_time = ""
        try:
            before_time = driver.find_element(By.CSS_SELECTOR, ".calc-result .time b").text.strip()
        except Exception:
            before_time = ""

        def time_ready(drv):
            """Функция time_ready.

            Параметры:
                drv: Описание параметра.

            Возвращает:
                Результат выполнения функции.
            """
            try:
                t = drv.find_element(By.CSS_SELECTOR, ".calc-result .time b").text.strip()
                return bool(t) and t != before_time
            except Exception:
                return False

        try:
            WebDriverWait(driver, 15).until(time_ready)
        except TimeoutException:
            pass

        time_el = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".calc-result .time b")))
        time_text = time_el.text.strip()

        fulllink = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.fulllink")))
        WebDriverWait(driver, 15).until(
            lambda d: "i.jde.ru/rq/?" in (d.find_element(By.CSS_SELECTOR, "a.fulllink").get_attribute("href") or "")
        )

        old_handles = set(driver.window_handles)
        click_best(fulllink)
        WebDriverWait(driver, 15).until(lambda d: len(set(d.window_handles) - old_handles) == 1)
        new_handle = list(set(driver.window_handles) - old_handles)[0]
        driver.switch_to.window(new_handle)


                # --- ВАЖНО: проверка подмены терминала по storage (самый надежный источник) ---
        def get_storage_json(input_id: str) -> dict:
            try:
                raw = driver.find_element(By.CSS_SELECTOR, f"input#{input_id}").get_attribute("value") or ""
                raw = raw.strip()
                return json.loads(raw) if raw else {}
            except Exception:
                return {}

        def pick_name(d: dict) -> str:
            # В storages обычно есть label/value; берем что есть
            if not d:
                return ""
            return (d.get("label") or d.get("value") or "").strip()


        def storage_ready(input_id: str) -> bool:
            try:
                raw = driver.find_element(By.CSS_SELECTOR, f"input#{input_id}").get_attribute("value") or ""
                return bool(raw.strip())
            except Exception:
                return False

        try:
            WebDriverWait(driver, 10).until(lambda d: storage_ready("derival_point_storage"))
            WebDriverWait(driver, 10).until(lambda d: storage_ready("derival_terminal_list_storage"))
            WebDriverWait(driver, 10).until(lambda d: storage_ready("arrival_point_storage"))
            WebDriverWait(driver, 10).until(lambda d: storage_ready("arrival_terminal_list_storage"))
        except TimeoutException:
            pass

        derival_point = pick_name(get_storage_json("derival_point_storage"))
        arrival_point  = pick_name(get_storage_json("arrival_point_storage"))
        derival_term   = pick_name(get_storage_json("derival_terminal_list_storage"))
        arrival_term   = pick_name(get_storage_json("arrival_terminal_list_storage"))

        def city_match(user_city: str, parsed_city: str) -> bool:
            a = norm_city(user_city)
            b = norm_city(parsed_city)
            return bool(a and b and (a in b or b in a))

        if derival_term and not city_match(from_city, derival_term):
            return None, None, {}

        if arrival_term and not city_match(to_city, arrival_term):
            return None, None, {}





        try:
            acc_btn = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "a.accordion-title.js-accordionTrigger"))
            )
            click_best(acc_btn)
        except TimeoutException:
            try:
                acc_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//a[contains(@class,'js-accordionTrigger') and .//*[contains(normalize-space(),'ПРЕДВАРИТЕЛЬНЫЙ')]]"))
                )
                click_best(acc_btn)
            except TimeoutException:
                pass

        bill_total_el = WebDriverWait(driver, 25).until(EC.visibility_of_element_located((By.CSS_SELECTOR, "#bill-total")))
        total_price = extract_rub(bill_total_el.text)

        fees: Dict[str, float] = {"Оформление документов": 0, "Страхование груза": 0, "Маркировка груза": 0}
        wanted = {
            norm_label("Оформление документов"): "Оформление документов",
            norm_label("Страхование груза"): "Страхование груза",
            norm_label("Cтрахование груза"): "Страхование груза",
            norm_label("Маркировка груза"): "Маркировка груза",
        }

        try:
            rows = WebDriverWait(driver, 15).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "table.bill-details tr"))
            )
            for tr in rows:
                txt = " ".join((tr.text or "").split())
                if not txt:
                    continue
                n = norm_label(txt)
                for key_norm, out_key in wanted.items():
                    if key_norm in n:
                        fees[out_key] = float(max(fees[out_key], extract_rub(txt)))
        except TimeoutException:
            pass

        return total_price, time_text, fees

    finally:
        driver.quit()


class JdeAdapter(SyncSeleniumAdapter):
    """Класс JdeAdapter.

    Инкапсулирует связанную функциональность модуля.
    """
    code = "jde"
    TIMEOUT = 120.0
    HEADLESS = True

    def _calc_sync(self, p: CalcParams) -> CalcResult:
        """Функция _calc_sync.

        Параметры:
            p: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        try:
            price_rub, days_text, allowances = jde_calc(
                p.from_city,
                p.to_city,
                int(p.places),
                int(round(p.weight_kg)),
                float(p.volume_m3),
            )
        except ValueError as e:
            raise InvalidInputError(str(e)) from e
        except TemporaryError:
            raise
        except Exception as e:
            raise TemporaryError(f"jde: unexpected {type(e).__name__}: {e}") from e

        return CalcResult(
            price=float(price_rub) if price_rub is not None else None,
            days=days_text if days_text else None,
            currency="RUB",
            source="https://www.jde.ru/",
            name_tarif=None,
            allowances=allowances or {},
        )



__all__ = ["jde_calc", "JdeAdapter"]

