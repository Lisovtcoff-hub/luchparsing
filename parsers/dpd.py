"""
DPD selenium adapter.

Вариант с полным логированием и дампами
"""

from __future__ import annotations

import os
import re
import time
import uuid
import logging
from pathlib import Path
from contextlib import suppress

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import math
from dataclasses import dataclass

from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    NoSuchElementException,
    ElementClickInterceptedException,
)

from core.contracts import CalcParams, CalcResult, InvalidInputError, TemporaryError
from core.selenium_base import SyncSeleniumAdapter


@dataclass(frozen=True)
class BoxResult:
    L: int
    W: int
    H: int
    target_cm3: float
    actual_cm3: int
    abs_error_cm3: float
    rel_error: float

def best_dimensions_cm(total_m3: float, places: int,
                       margin_cm: int = 80,
                       min_side_cm: int = 5) -> BoxResult:
    """
    Подбирает целые L,W,H (см) для 1 места так, чтобы L*W*H был максимально близок
    к объёму на место, и при этом коробка была "нормальной", а не 83333x1x1.

    margin_cm  - запас к кубическому корню объёма, чтобы расширить поиск
    min_side_cm- минимальная сторона (см), чтобы исключить 1-2 см "иголки"
    """

    if total_m3 <= 0:
        raise ValueError("total_m3 должен быть > 0")
    if not (1 <= places <= 80):
        raise ValueError("places должен быть 1..80")

    target_cm3 = (total_m3 / places) * 1_000_000.0  # см^3 на 1 место
    c = target_cm3 ** (1/3)                          # оценка стороны куба (см)

    max_side = int(math.ceil(c + margin_cm))
    max_side = max(max_side, min_side_cm)

    best = None

    for L in range(min_side_cm, max_side + 1):
        for W in range(min_side_cm, L + 1):
            base = L * W
            h_real = target_cm3 / base
            H0 = int(round(h_real))

            # проверяем несколько ближайших H вокруг округления
            for H in (H0 - 2, H0 - 1, H0, H0 + 1, H0 + 2):
                if H < min_side_cm or H > max_side:
                    continue

                actual = base * H
                abs_err = abs(actual - target_cm3)
                rel_err = abs_err / target_cm3

                dims = sorted((L, W, H), reverse=True)
                L2, W2, H2 = dims

                # метрика "насколько это похоже на коробку", а не на иголку
                cube_score = (L2 - H2)  # меньше = более "квадратно"

                # 1) сначала точность по объёму
                # 2) потом "квадратность"
                # 3) потом меньшая максимальная сторона
                key = (rel_err, cube_score, L2, (L2 + W2 + H2))

                if best is None or key < best[0]:
                    best = (key, BoxResult(
                        L=L2, W=W2, H=H2,
                        target_cm3=target_cm3,
                        actual_cm3=actual,
                        abs_error_cm3=abs_err,
                        rel_error=rel_err
                    ))

    return best[1]

banned = ["Октябрьский", ]

# route-level caches for current run
_NO_ROUTE_ROUTES: set[tuple[str, str]] = set()
_TIMEOUT_ROUTE_COUNTS: dict[tuple[str, str], int] = {}


def _route_key(from_city: str, to_city: str) -> tuple[str, str]:
    return (from_city or "").strip().lower(), (to_city or "").strip().lower()


def _is_no_route(from_city: str, to_city: str) -> bool:
    return _route_key(from_city, to_city) in _NO_ROUTE_ROUTES


def _remember_no_route(from_city: str, to_city: str) -> None:
    _NO_ROUTE_ROUTES.add(_route_key(from_city, to_city))


def _bump_timeout(from_city: str, to_city: str) -> int:
    key = _route_key(from_city, to_city)
    cnt = _TIMEOUT_ROUTE_COUNTS.get(key, 0) + 1
    _TIMEOUT_ROUTE_COUNTS[key] = cnt
    return cnt

log = logging.getLogger("parsers.dpd")

URL = "https://dpd.ru/calc"

EXPORT_DIR = Path(os.getenv("EXPORT_DIR", "/app/exports"))
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

DPD_DEBUG = os.getenv("DPD_DEBUG", "0").strip() in {"1", "true", "True", "yes", "YES"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", s).strip("_")


def build_driver() -> webdriver.Chrome:
    """
    Драйвер под Docker
    """
    opts = Options()
    opts.set_capability("pageLoadStrategy", "eager")

    # В контейнере обычно chromium лежит здесь
    chromium_bin = os.getenv("CHROME_BIN", "/usr/bin/chromium")
    if os.path.exists(chromium_bin):
        opts.binary_location = chromium_bin

    
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1366,768")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--lang=ru-RU")
    prefs = {
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.geolocation": 2,
    }
    opts.add_experimental_option("prefs", prefs)

    chromedriver_path = os.getenv("CHROMEDRIVER", "/usr/bin/chromedriver")
    service = Service(chromedriver_path)

    raise TemporaryError("dpd: driver must be provided by pool")

    # Таймауты в Docker лучше держать больше(((
    driver.set_page_load_timeout(30)
    driver.set_script_timeout(30)

    return driver


def js_set_value(driver: webdriver.Chrome, el, value) -> None:
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


def set_number_field(wait: WebDriverWait, name: str, value) -> None:
    driver = wait._driver
    el = wait.until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, f"div.calc-sizes__exact input.number-field__input[name='{name}']")
        )
    )
    js_set_value(driver, el, value)


def set_places(wait: WebDriverWait, count: int) -> None:
    driver = wait._driver
    el = wait.until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, "div.calc-sizes__exact div.count-field input.count-field__input")
        )
    )
    js_set_value(driver, el, count)


def set_declared_value(wait: WebDriverWait, value) -> None:
    driver = wait._driver
    el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input.text-field__input[name='DeclaredValue']")))
    js_set_value(driver, el, value)


def type_city(wait: WebDriverWait, legend_text: str, placeholder_text: str, query: str, option_substr: str) -> None:
    driver = wait._driver

    inp = wait.until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, f"div[legend='{legend_text}'] input.multiselect__input[placeholder='{placeholder_text}']")
        )
    )

    # раскрыть ли
    inp.find_element(By.XPATH, "./ancestor::div[contains(@class,'multiselect')]").click()

    # очистить и ввести
    inp.send_keys(Keys.CONTROL, "a")
    inp.send_keys(Keys.DELETE)
    inp.send_keys(query)

    opt = WebDriverWait(driver, 20, 0.1).until(
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


def click_proceed(wait: WebDriverWait) -> None:
    driver = wait._driver
    btn = wait.until(
        EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button.calc-form__button.ui-button_OqgQF.button_Tj27p.full-width_NJ54H")
        )
    )
    try:
        btn.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", btn)

def get_first_tariff_price_and_time(driver: webdriver.Chrome) -> dict[str, list]:
    """
    Возвращает словарь вида:
      {
        "DPD ECONOMY": [price_text, days_token],
        "DPD 18:00":   [price_text, days_token],
      }

    days_token:
      - "3-5" / "3–5" если на странице диапазон
      - "2" если одно число
      - "" если не удалось извлечь
    """
    wait = WebDriverWait(driver, 35, 0.2)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table.calc-tariffs__table")))

    table = driver.find_element(By.CSS_SELECTOR, "table.calc-tariffs__table")
    rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")

    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip()).upper()

    def _extract_days_token(time_text: str) -> str:
        t = (time_text or "").strip()
        # диапазон типа 3-5 / 3–5
        m = re.search(r"(\d+\s*[–-]\s*\d+)", t)
        if m:
            return re.sub(r"\s+", "", m.group(1))
        # одно число
        m = re.search(r"(\d+)", t)
        return m.group(1) if m else ""

    targets = ("DPD ECONOMY", "DPD 18:00")
    out: dict[str, list] = {}

    for row in rows:
        tariff_name = _norm(row.find_element(By.CSS_SELECTOR, "td:first-child").text)

        for target in targets:
            if target in tariff_name:
                price_text = row.find_element(
                    By.CSS_SELECTOR, "td div.calc-tariffs__table-cell_cost-inner"
                ).text.strip()
                time_text = row.find_element(
                    By.CSS_SELECTOR, "td.calc-tariffs__table-cell_right"
                ).text.strip()

                out[target] = [price_text, _extract_days_token(time_text)]
                break

    return out


def dpd_calc(
    from_city: str,
    to_city: str,
    places: int,
    weight_kg: float,
    volume_m3: float,
    driver: webdriver.Chrome,
) -> tuple[Optional[float], Optional[str], dict]:
    if to_city in banned or from_city in banned:
        return None, None, {}
    request_id = uuid.uuid4().hex[:10]
    if driver is None:
        raise TemporaryError("dpd: driver is required")

    # основной wait для формы
    wait = WebDriverWait(driver, 20, 0.1)

    try:

        driver.get(URL)
        log.debug("dpd current_url request_id=%s url=%s", request_id, driver.current_url)

        type_city(wait, "Отправление", "Откуда", from_city, f"г. {from_city}")
        type_city(wait, "Получение", "Куда", to_city, f"г. {to_city}")

        r = best_dimensions_cm(volume_m3, places)
        
        set_number_field(wait, "length", r.L)
        set_number_field(wait, "width", r.W)
        set_number_field(wait, "height", r.H)

        per_place_weight = round(float(weight_kg) / max(1, int(places)), 3)
        set_number_field(wait, "weight", per_place_weight)
        set_places(wait, int(places))
        set_declared_value(wait, 0)
        

        click_proceed(wait)

        # иногда страница “дозревает” после клмка, небольшая пауза допустима
        time.sleep(0.3)

        results = get_first_tariff_price_and_time(driver)
        final_res = {}
        for key in results:
            price = results[key][0]
            price_num = float(re.sub(r"[^\d.,]", "", price).replace(",", "."))
            days = results[key][1]
            days_text = (days or "").strip()
            final_res[key] = [price_num, days_text]

        log.debug(f"{places}, {per_place_weight}, {volume_m3}, {r.L}, {r.W}, {r.H}")
        log.debug(next(iter(final_res.values()))[0], next(iter(final_res.values()))[1], final_res)
        
        return next(iter(final_res.values()))[0], next(iter(final_res.values()))[1], final_res

        # price_num = float(re.sub(r"[^\d.,]", "", price_text).replace(",", "."))
        # days_text = (time_text or "").strip()
        # return min(prices), (days_text.split()[0] if days_text else "")

    except TimeoutException as e:
        log.error("dpd timeout request_id=%s url=%s err=%s", request_id, driver.current_url, e)
        raise TemporaryError("dpd: timeout while waiting for page elements/result") from e

    except (NoSuchElementException, ElementClickInterceptedException) as e:
        log.error("dpd dom error request_id=%s url=%s err=%s", request_id, driver.current_url, e)
        raise TemporaryError("dpd: page layout changed / element not found") from e

    except WebDriverException as e:
        log.error("dpd webdriver error request_id=%s url=%s err=%s", request_id, driver.current_url, e)
        raise TemporaryError("dpd: webdriver error") from e

    finally:
        pass


class DpdAdapter(SyncSeleniumAdapter):
    code = "dpd"
    TIMEOUT = 120.0
    HEADLESS = True

    def _calc_sync(self, p: CalcParams, driver: webdriver.Chrome) -> CalcResult:
        if _is_no_route(p.from_city, p.to_city):
            raise InvalidInputError(f"dpd: no terminal for route (cached): {p.from_city} -> {p.to_city}")
        try:
            price, days, results = dpd_calc(
                p.from_city,
                p.to_city,
                int(p.places),
                float(p.weight_kg),
                round(float(p.volume_m3), 3),
                driver=driver,
            )
        except ValueError as e:
            raise InvalidInputError(str(e)) from e
        except TemporaryError as e:
            msg = str(e)
            if "dpd: timeout while waiting for page elements/result" in msg:
                cnt = _bump_timeout(p.from_city, p.to_city)
                if cnt >= 2:
                    _remember_no_route(p.from_city, p.to_city)
                    raise InvalidInputError(
                        f"dpd: no terminal for route (timeout x{cnt}): {p.from_city} -> {p.to_city}"
                    ) from e
            raise
        except Exception as e:
            log.exception("dpd unexpected error: %s", e)
            raise TemporaryError(f"dpd: unexpected {type(e).__name__}: {e}") from e

        return CalcResult(
            price=price,
            currency="RUB",
            days=days,
            source=URL,
            name_tarif=results,
            allowances={},
        )


__all__ = ["dpd_calc", "DpdAdapter", "build_driver"]
