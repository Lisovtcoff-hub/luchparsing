
"""Модуль baykal.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import re
import time
from typing import Optional, Dict, Tuple

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

from core.contracts import CalcParams, CalcResult, InvalidInputError, TemporaryError
from core.selenium_base import SyncSeleniumAdapter


def _make_driver(show_browser: bool = False):
    """Функция _make_driver.

    Параметры:
        show_browser: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    opts = webdriver.ChromeOptions()
    if not show_browser:
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")

    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")

    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts,
    )


def baykal_calc(
    from_city: str,
    to_city: str,
    places: int,
    weight: int,
    volume: float,
) -> Tuple[Optional[float], Optional[str], Dict[str, float]]:
    """Функция baykal_calc.

    Параметры:
        from_city: Описание параметра.
        to_city: Описание параметра.
        places: Описание параметра.
        weight: Описание параметра.
        volume: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    driver = _make_driver(show_browser=False)
    wait = WebDriverWait(driver, 20)

    try:
        driver.get("https://request.baikalsr.ru/calculator")

        def pick_from_typeahead(input_css: str, query_text: str, target_text: str):
            """Функция pick_from_typeahead.

            Параметры:
                input_css: Описание параметра.
                query_text: Описание параметра.
                target_text: Описание параметра.

            Возвращает:
                Результат выполнения функции.
            """
            inp = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, input_css)))
            inp.click()
            inp.clear()
            inp.send_keys(query_text)
            time.sleep(0.4)

            menu_id = inp.get_attribute("aria-owns")
            if menu_id:
                menu_sel = f"ul#{menu_id}:not(.ng-hide)"
            else:
                menu_sel = "ul[id^='typeahead-'][role='listbox']:not(.ng-hide)"

            menu = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, menu_sel)))

            items = menu.find_elements(By.CSS_SELECTOR, "li a, li")
            if not items:
                raise RuntimeError("Нет пунктов выпадающего списка")

            target_el = None
            for it in items:
                txt = (it.text or "").strip()
                base = txt.split("(")[0].strip()
                if base == target_text or target_text in base:
                    target_el = it
                    break
            if target_el is None:
                target_el = items[0]

            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target_el)
            target_el.click()

            driver.execute_script(
                "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                inp
            )


        pick_from_typeahead("input[ng-model='request.departure.cityname']", from_city, from_city)


        pick_from_typeahead("input[ng-model='request.destination.cityname']", to_city, to_city)

        tab_total = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a[ng-click='setOnePlace(0)']")))
        driver.execute_script("arguments[0].click();", tab_total)

        cube_input = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, ".form-control__wrapper_meter_cubic input.form-control")
        ))
        driver.execute_script(
            "arguments[0].value = arguments[1];"
            "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
            cube_input, f"{volume:.3f}".replace(".", ",")
        )

        mass_input = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, ".form-control__wrapper_kilogram input.form-control")
        ))
        mass_input.clear()
        mass_input.send_keys(Keys.CONTROL, "a")
        mass_input.send_keys(str(weight))

        places_input = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "div.spinbox[number-spinbox*='summarycargo'][number-spinbox*='units'] input.spinbox-input")
        ))
        places_input.send_keys(Keys.CONTROL, "a")
        places_input.send_keys(str(places))

        def rub_to_float(s: str) -> float:
            """Функция rub_to_float.

            Параметры:
                s: Описание параметра.

            Возвращает:
                Результат выполнения функции.
            """
            s = (s or "").replace("\xa0", " ").replace("₽", "").strip()


            m = re.search(r"(\d[\d\s]*([.,]\d+)?)", s)
            if not m:
                raise ValueError(f"Не удалось распарсить цену из: {s!r}")

            num = m.group(1).replace(" ", "").replace(",", ".")
            return float(num)

        price_el = wait.until(EC.visibility_of_element_located(
            (By.CSS_SELECTOR, "#side-block .side-result__inner [ng-bind-html='totalSumHtml()']")
        ))
        wait.until(lambda d: re.search(r"\d", price_el.text or ""))
        price = rub_to_float(re.search(r"([\d\s]+[.,]\d+)", price_el.text).group(1))

        days_el = wait.until(EC.visibility_of_element_located(
            (By.CSS_SELECTOR, "#side-block .side-result__inner .btn-link_icon_time")
        ))
        m = re.search(r"(\d+)", days_el.text)
        days = m.group(1) if m else None

        rows = wait.until(EC.presence_of_all_elements_located(
            (By.CSS_SELECTOR, "#side-block .side-result__table tbody tr")
        ))

        allowances: Dict[str, float] = {}
        for r in rows:
            name = r.find_element(By.CSS_SELECTOR, "td:first-child").text.lower()
            cost = rub_to_float(r.find_element(By.CSS_SELECTOR, "td.text-nowrap").text)
            if "страхов" in name:
                allowances["Страхование"] = cost
            elif "информ" in name:
                allowances["Информирование"] = cost

        return price, days, allowances

    finally:
        try:
            driver.quit()
        except Exception:
            pass


class BaykalAdapter(SyncSeleniumAdapter):
    """Класс BaykalAdapter.

    Инкапсулирует связанную функциональность модуля.
    """
    code = "baykal"
    TIMEOUT = 80.0
    HEADLESS = False

    def _calc_sync(self, p: CalcParams) -> CalcResult:
        """Функция _calc_sync.

        Параметры:
            p: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        try:
            price, days, allowances = baykal_calc(
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
            raise TemporaryError(f"baykal: unexpected {type(e).__name__}: {e}") from e

        return CalcResult(
            price=price,
            days=days,
            currency="RUB",
            source="https://request.baikalsr.ru/calculator",
            name_tarif=None,
            allowances=allowances or {},
        )


__all__ = ["baykal_calc", "BaykalAdapter"]
