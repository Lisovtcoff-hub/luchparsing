
"""Модуль fastrans.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, date

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from selenium.common.exceptions import (
    TimeoutException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
)

from core.contracts import CalcParams, CalcResult, InvalidInputError, TemporaryError
from core.selenium_base import SyncSeleniumAdapter


banned = [
    "Ижевск",
]

def _textcontent(el) -> str:
    """Функция _textcontent.

    Параметры:
        el: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    try:
        return (el.get_attribute("textContent") or "").strip()
    except Exception:
        return ""


def _norm_spaces(s: str) -> str:
    """Функция _norm_spaces.

    Параметры:
        s: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def _parse_int_money(s: str) -> int:
    """Функция _parse_int_money.

    Параметры:
        s: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else 0


def _set_query_via_js(driver, input_el, text: str):
    """Функция _set_query_via_js.

    Параметры:
        driver: Описание параметра.
        input_el: Описание параметра.
        text: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    driver.execute_script(
        """
      const el = arguments[0];
      el.value = '';
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.value = arguments[1];
      el.dispatchEvent(new Event('input', {bubbles: true}));
    """,
        input_el,
        text,
    )


def _open_multiselect_and_get_panel(driver, block_el, open_timeout=2.0):
    """Функция _open_multiselect_and_get_panel.

    Параметры:
        driver: Описание параметра.
        block_el: Описание параметра.
        open_timeout: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    ms = block_el.find_element(By.CSS_SELECTOR, "div.multiselect")
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", ms)
    ms.click()
    try:
        ms.find_element(By.CSS_SELECTOR, ".multiselect__select").click()
    except Exception:
        pass

    deadline = time.time() + open_timeout
    while time.time() < deadline:
        wrappers = block_el.find_elements(By.CSS_SELECTOR, "div.multiselect__content-wrapper")
        for w in wrappers:
            style = (w.get_attribute("style") or "").replace(" ", "").lower()
            if "display:none" not in style:
                return w
        time.sleep(0.05)
    raise TimeoutException("Не удалось открыть выпадающий список у multiselect")


def _pick_city_option(options, city_query: str):
    """Функция _pick_city_option.

    Параметры:
        options: Описание параметра.
        city_query: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    cq = _norm_spaces(city_query).lower()
    cq_tokens = [t for t in re.split(r"[^\wа-яё]+", cq, flags=re.I) if t]

    for opt in options:
        txt = _norm_spaces(_textcontent(opt)).lower()
        if txt and all(t in txt for t in cq_tokens):
            return opt

    for opt in options:
        cls = opt.get_attribute("class") or ""
        if "multiselect__option--highlight" in cls:
            return opt

    return options[0] if options else None


def _safe_click(driver, el):
    """Функция _safe_click.

    Параметры:
        driver: Описание параметра.
        el: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    try:
        el.click()
        return
    except (ElementClickInterceptedException, StaleElementReferenceException, Exception):
        pass

    try:
        ActionChains(driver).move_to_element(el).pause(0.05).click(el).perform()
        return
    except Exception:
        pass

    driver.execute_script("arguments[0].click();", el)


def _fill_num(wait, driver, css: str, value):
    """Функция _fill_num.

    Параметры:
        wait: Описание параметра.
        driver: Описание параметра.
        css: Описание параметра.
        value: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    el = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, css)))
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    el.click()
    el.send_keys(Keys.CONTROL, "a")
    el.send_keys(str(value))
    driver.execute_script(
        "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
        "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
        el,
    )


def _dismiss_overlays(driver):
    """Функция _dismiss_overlays.

    Параметры:
        driver: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    try:
        driver.execute_script(
            """
            const selectors = [
              '.b24-widget-button-wrapper',
              '.b24-widget-button-position-fixed',
              'div[class*="b24-widget-button"]',
              'div[class*="b24-widget"]',
              '.SmartCaptcha-Overlay',
              '.smart-captcha',
            ];
            for (const sel of selectors) {
              document.querySelectorAll(sel).forEach(n => {
                n.style.setProperty('display','none','important');
                n.style.setProperty('visibility','hidden','important');
                n.style.setProperty('pointer-events','none','important');
              });
            }
        """
        )
    except Exception:
        pass


def _wait_result_node(driver, timeout=35):
    """Функция _wait_result_node.

    Параметры:
        driver: Описание параметра.
        timeout: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    wait = WebDriverWait(driver, timeout)

    def _ready(d):
        """Функция _ready.

        Параметры:
            d: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        nodes = d.find_elements(By.CSS_SELECTOR, "div.result.calculatePageResult")
        if not nodes:
            nodes = d.find_elements(By.CSS_SELECTOR, "div.calculatePageResult")
        if not nodes:
            return False

        for node in nodes:
            for sp in node.find_elements(By.CSS_SELECTOR, "aside span"):
                if "Стоимость перевозки" in _textcontent(sp):
                    return node

            rows = node.find_elements(By.CSS_SELECTOR, "ul.result__of-calculations > li")
            for li in rows:
                try:
                    item = _textcontent(li.find_element(By.CSS_SELECTOR, "div.result__item"))
                    price = _textcontent(li.find_element(By.CSS_SELECTOR, "div.result__price"))
                    if _norm_spaces(item) and re.search(r"\d", price or ""):
                        return node
                except Exception:
                    continue

        return False

    return wait.until(_ready)


def _extract_breakdown(node) -> dict[str, int]:
    """Функция _extract_breakdown.

    Параметры:
        node: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    end = time.time() + 15
    last: dict[str, int] = {}

    while time.time() < end:
        rows = node.find_elements(By.CSS_SELECTOR, "ul.result__of-calculations > li")
        if not rows:
            time.sleep(0.1)
            continue

        breakdown: dict[str, int] = {}
        for li in rows:
            try:
                item_el = li.find_element(By.CSS_SELECTOR, "div.result__item")
                price_el = li.find_element(By.CSS_SELECTOR, "div.result__price")
                name = _norm_spaces(_textcontent(item_el))
                val = _parse_int_money(_textcontent(price_el))
                if name:
                    breakdown[name.lower()] = val
            except Exception:
                continue

        last = breakdown
        if breakdown.get("перевозка", 0) > 0 or any(k.startswith("страх") and v > 0 for k, v in breakdown.items()):
            return breakdown

        time.sleep(0.15)

    return last


def _extract_aside_price(node) -> int:
    """Функция _extract_aside_price.

    Параметры:
        node: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    for sp in node.find_elements(By.CSS_SELECTOR, "aside span"):
        txt = _textcontent(sp)
        if "Стоимость перевозки" in txt:
            return _parse_int_money(txt)
    return 0


def _extract_arrival_days(node) -> int:
    """Функция _extract_arrival_days.

    Параметры:
        node: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    for sp in node.find_elements(By.CSS_SELECTOR, "aside span"):
        txt = _textcontent(sp)
        if "Ориентировочная дата прибытия" in txt:
            m = re.search(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}))?", txt)
            if not m:
                return 0
            dt_str = m.group(1) + (f" {m.group(2)}" if m.group(2) else "")
            fmt = "%d.%m.%Y %H:%M" if m.group(2) else "%d.%m.%Y"
            arr_dt = datetime.strptime(dt_str, fmt)
            return (arr_dt.date() - date.today()).days
    return 0


def fastrans(from_city: str, to_city: str, places: int, weight: int, volume: float, show_browser: bool = False):
    """Функция fastrans.

    Параметры:
        from_city: Описание параметра.
        to_city: Описание параметра.
        places: Описание параметра.
        weight: Описание параметра.
        volume: Описание параметра.
        show_browser: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    if from_city in banned or to_city in banned:
        return None, None, None
    opts = webdriver.ChromeOptions()
    opts.add_argument("--window-size=1600,1000")
    if not show_browser:
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    wait = WebDriverWait(driver, 25)

    try:
        driver.get("https://fastrans.ru/logistics/calculate/")

        from_block = wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//div[contains(@class,'VueFormInputLabelText')][normalize-space()='Город отправления']"
                    "/ancestor::div[contains(@class,'VueFormInput')][1]",
                )
            )
        )
        from_inp = from_block.find_element(By.CSS_SELECTOR, ".multiselect__tags input.multiselect__input")
        _set_query_via_js(driver, from_inp, from_city)

        from_panel = _open_multiselect_and_get_panel(driver, from_block)

        def _from_opts(_):
            """Функция _from_opts.

            Параметры:
                _: Описание параметра.

            Возвращает:
                Результат выполнения функции.
            """
            opts_ = from_panel.find_elements(By.CSS_SELECTOR, "li.multiselect__element .multiselect__option")
            return opts_ if opts_ else False

        from_options = wait.until(_from_opts)
        from_target = _pick_city_option(from_options, from_city)
        if not from_target:
            return None
        _safe_click(driver, from_target)

        to_block = wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//div[contains(@class,'VueFormInputLabelText')][normalize-space()='Город назначения']"
                    "/ancestor::div[contains(@class,'VueFormInput')][1]",
                )
            )
        )
        to_inp = to_block.find_element(By.CSS_SELECTOR, ".multiselect__tags input.multiselect__input")
        _set_query_via_js(driver, to_inp, to_city)

        to_panel = _open_multiselect_and_get_panel(driver, to_block)

        def _to_opts(_):
            """Функция _to_opts.

            Параметры:
                _: Описание параметра.

            Возвращает:
                Результат выполнения функции.
            """
            opts_ = to_panel.find_elements(By.CSS_SELECTOR, "li.multiselect__element .multiselect__option")
            return opts_ if opts_ else False

        to_options = wait.until(_to_opts)
        to_target = _pick_city_option(to_options, to_city)
        if not to_target:
            return None
        _safe_click(driver, to_target)

        _fill_num(wait, driver, "input.VueFormInputInput[name='amount']", places)
        _fill_num(wait, driver, "input.VueFormInputInput[name='weight']", weight)
        _fill_num(wait, driver, "input.VueFormInputInput[name='volume']", volume)

        calc_btn = wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//div[contains(@class,'VueForm-controls')]//button[.//span[normalize-space()='Расчёт']]",
                )
            )
        )
        _dismiss_overlays(driver)
        _safe_click(driver, calc_btn)

        node = _wait_result_node(driver, timeout=35)

        breakdown = _extract_breakdown(node)
        transport_price = breakdown.get("перевозка", 0)

        insurance = 0
        if "страхование" in breakdown:
            insurance = breakdown["страхование"]
        else:
            for k, v in breakdown.items():
                if k.startswith("страх"):
                    insurance = v
                    break

        if transport_price == 0:
            transport_price = _extract_aside_price(node)

        days = _extract_arrival_days(node)
        return transport_price, days, insurance

    finally:
        try:
            if show_browser:
                time.sleep(3)
        finally:
            driver.quit()


class FastransAdapter(SyncSeleniumAdapter):
    """Класс FastransAdapter.

    Инкапсулирует связанную функциональность модуля.
    """
    code = "fastrans"
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
            res = fastrans(
                p.from_city,
                p.to_city,
                int(p.places),
                int(round(p.weight_kg)),
                float(p.volume_m3),
                show_browser=False,
            )
        except ValueError as e:
            raise InvalidInputError(str(e)) from e
        except TemporaryError:
            raise
        except Exception as e:
            raise TemporaryError(f"fastrans: unexpected {type(e).__name__}: {e}") from e

        if not res:
            raise TemporaryError("fastrans: no result")

        price, days, insurance = res

        allowances = {}
        if insurance is not None:
            try:
                ins_val = float(insurance)
                if ins_val != 0:
                    allowances["Страхование"] = ins_val
            except Exception:
                pass

        return CalcResult(
            price=float(price) if price is not None else None,
            days=int(days) if days not in (None, 0, "0") else None,
            currency="RUB",
            source="https://fastrans.ru/logistics/calculate/",
            name_tarif=None,
            allowances=allowances,
        )


__all__ = ["fastrans", "FastransAdapter"]
