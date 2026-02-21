"""Модуль fastrans.

Содержит прикладную логику и точки входа проекта.


САМОЕ ХАРДКОРНОЕ ЛОГГИРОВАНИЕ. ВРЕМЯ 3 УТРА, Я УСТАЛ
"""

from __future__ import annotations

import os
import re
import time
import uuid
import shutil
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Tuple
import threading

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
    ElementNotInteractableException,
    WebDriverException,
    NoSuchElementException,
)

from core.contracts import CalcParams, CalcResult, InvalidInputError, TemporaryError
from core.selenium_base import SyncSeleniumAdapter


log = logging.getLogger("parsers.fastrans")

SOURCE_URL = "https://fastrans.ru/logistics/calculate/"

banned = [
    "Ижевск",
]

_NO_CITY_LOCK = threading.Lock()
_NO_CITY_ROUTES: set[tuple[str, str]] = set()


def _route_key(from_city: str, to_city: str) -> tuple[str, str]:
    return (_norm_spaces(from_city).lower(), _norm_spaces(to_city).lower())


def _remember_no_city(from_city: str, to_city: str) -> None:
    with _NO_CITY_LOCK:
        _NO_CITY_ROUTES.add(_route_key(from_city, to_city))


def _is_no_city(from_city: str, to_city: str) -> bool:
    with _NO_CITY_LOCK:
        return _route_key(from_city, to_city) in _NO_CITY_ROUTES


def _textcontent(el) -> str:
    try:
        return (el.get_attribute("textContent") or "").strip()
    except Exception:
        return ""


def _norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def _parse_int_money(s: str) -> int:
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else 0


def _set_query_via_js(driver, input_el, text: str):
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


def _dismiss_overlays(driver):
    """Пытается закрыть попапы/оверлеи (cookie, чат, модалки)."""
    try:
        driver.execute_script(
            """
            const selectors = [
              "#cookies_accept",
              "button#cookie_accept",
              ".cookies button",
              ".cookie button",
              ".modal__close",
              ".popup__close",
              "[data-testid='close']",
              "button[aria-label='close']",
              "button[aria-label='закрыть']",
              "button[title='Закрыть']",
              ".bx-livechat-control-box .bx-livechat-control-btn-close"
            ];
            for (const s of selectors) {
              const el = document.querySelector(s);
              if (el) { try { el.click(); } catch(e) {} }
            }

            // Попытка скрыть виджет чата, если он перекрывает клики
            const chat = document.querySelector(".bx-livechat-wrapper, .bx-livechat");
            if (chat) { try { chat.style.display = "none"; } catch(e) {} }
            """
        )
    except Exception:
        pass


def _el_debug(el) -> str:
    """Собирает небольшой дебаг-слепок по элементу."""
    try:
        tag = el.tag_name
    except Exception:
        tag = "?"
    try:
        displayed = el.is_displayed()
    except Exception:
        displayed = None
    try:
        enabled = el.is_enabled()
    except Exception:
        enabled = None
    try:
        rect = el.rect
    except Exception:
        rect = None
    try:
        cls = el.get_attribute("class")
    except Exception:
        cls = None
    try:
        txt = _norm_spaces(_textcontent(el))[:120]
    except Exception:
        txt = ""
    return f"tag={tag} displayed={displayed} enabled={enabled} rect={rect} class={cls!r} text={txt!r}"


def _robust_click(driver, el, *, request_id: str, label: str) -> None:
    """
    Клик с несколькими стратегиями и ретраями:
    1) scroll + обычный click
    2) ActionChains move_to_element + click
    3) JS click
    При проблеме — лог и дамп (screenshot/html).
    """
    last_exc: Optional[Exception] = None

    for attempt in range(1, 6):
        try:
            _dismiss_overlays(driver)

            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            except Exception:
                pass
            time.sleep(0.15)

            # ПРОБУЮ ВСЕю
            # 1) Обычный click
            try:
                log.debug("fastrans click(%s) attempt=%s el=%s", label, attempt, _el_debug(el))
                el.click()
                return
            except (ElementClickInterceptedException, ElementNotInteractableException, WebDriverException) as e:
                last_exc = e

            # 2) ActionChains
            try:
                log.debug("fastrans click(%s) attempt=%s via ActionChains", label, attempt)
                ActionChains(driver).move_to_element(el).pause(0.05).click(el).perform()
                return
            except (ElementClickInterceptedException, ElementNotInteractableException, WebDriverException) as e:
                last_exc = e

            # 3) JS click
            try:
                log.debug("fastrans click(%s) attempt=%s via JS", label, attempt)
                driver.execute_script("arguments[0].click();", el)
                return
            except Exception as e:
                last_exc = e

            time.sleep(0.25)

        except StaleElementReferenceException as e:
            last_exc = e
            log.debug("fastrans click(%s) stale on attempt=%s", label, attempt)
            time.sleep(0.2)

    log.error("fastrans click(%s) FAILED request_id=%s last_error=%s", label, request_id, last_exc)
    raise WebDriverException(f"fastrans: element not clickable ({label}): {last_exc}") from last_exc


def _open_multiselect_and_get_panel(driver, block_el, request_id: str, label: str, open_timeout=3.0):
    ms = block_el.find_element(By.CSS_SELECTOR, "div.multiselect")
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", ms)
    except Exception:
        pass
    time.sleep(0.1)

    _robust_click(driver, ms, request_id=request_id, label=f"{label}.multiselect")

    # ну и наворотил я конечно, самому бы не забыть завтра
    t0 = time.time()
    while time.time() - t0 < open_timeout:
        panels = driver.find_elements(By.CSS_SELECTOR, "div.multiselect__content-wrapper")
        for p in panels:
            try:
                if p.is_displayed():
                    return p
            except Exception:
                pass
        time.sleep(0.05)

    panels = driver.find_elements(By.CSS_SELECTOR, "div.multiselect__content-wrapper")
    return panels[0] if panels else ms


def _pick_city_option(options, city: str):
    city_n = _norm_spaces(city).lower()
    best = None
    for opt in options:
        txt = _norm_spaces(_textcontent(opt)).lower()
        if not txt:
            continue
        if txt == city_n:
            return opt
        if txt.startswith(city_n) and best is None:
            best = opt
    return best or (options[0] if options else None)


def _fill_num(wait: WebDriverWait, driver, css: str, value):
    inp = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, css)))
    inp.send_keys(Keys.CONTROL, "a")
    inp.send_keys(Keys.BACKSPACE)
    inp.send_keys(str(value))
    driver.execute_script("arguments[0].dispatchEvent(new Event('input',{bubbles:true}));", inp)
    driver.execute_script("arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", inp)
    inp.send_keys(Keys.TAB)
    return inp


def _wait_result_node(driver, timeout=45):
    wait = WebDriverWait(driver, timeout, poll_frequency=0.2)

    def ready(drv):
        nodes = drv.find_elements(By.CSS_SELECTOR, "div.calculator-result, section.calculator-result, .calculator-result")
        for n in nodes:
            try:
                if n.is_displayed() and _norm_spaces(_textcontent(n)):
                    return n
            except Exception:
                continue
        nodes = drv.find_elements(By.CSS_SELECTOR, "aside")
        for n in nodes:
            try:
                if n.is_displayed() and _norm_spaces(_textcontent(n)):
                    return n
            except Exception:
                continue
        return False

    return wait.until(ready)


def _visible(el) -> bool:
    try:
        return bool(el.is_displayed())
    except Exception:
        return False


def _get_error_text(driver) -> str:
    selectors = [
        ".VueFormResultError",
        ".VueFormResult .message.error",
        ".message.error",
    ]
    for sel in selectors:
        for el in driver.find_elements(By.CSS_SELECTOR, sel):
            if _visible(el):
                txt = _norm_spaces(_textcontent(el))
                if txt:
                    return txt
    # modal messages (not terminal / not serving)
    for el in driver.find_elements(By.CSS_SELECTOR, ".modal-message-text"):
        try:
            if "hidden" in (el.get_attribute("class") or ""):
                continue
        except Exception:
            pass
        if _visible(el):
            txt = _norm_spaces(_textcontent(el))
            if txt:
                return txt
    return ""


def _wait_result_or_error(driver, timeout=45):
    t0 = time.time()
    while time.time() - t0 < timeout:
        err = _get_error_text(driver)
        if err:
            return None, err
        try:
            node = _wait_result_node(driver, timeout=0.5)
            if node:
                return node, ""
        except Exception:
            pass
        time.sleep(0.2)
    return None, ""


def _extract_breakdown(node) -> dict:
    out = {}
    rows = node.find_elements(By.CSS_SELECTOR, "div, li, p, span")
    for r in rows:
        txt = _norm_spaces(_textcontent(r))
        if not txt:
            continue
        if re.search(r"\d", txt) and re.search(r"[А-Яа-яA-Za-z]", txt):
            m = re.match(r"^(.+?)\s+([\d\s]+)\s*(₽|руб\.?|RUB)?$", txt, flags=re.IGNORECASE)
            if m:
                label = _norm_spaces(m.group(1)).lower()
                value = _parse_int_money(m.group(2))
                if label and value is not None:
                    out[label] = value
    return out


def _extract_aside_price(node) -> int:
    txt = _norm_spaces(_textcontent(node))
    m = re.search(r"(\d[\d\s]{2,})\s*(₽|руб\.?|RUB)", txt, flags=re.IGNORECASE)
    if not m:
        return 0
    return _parse_int_money(m.group(1))


def _extract_arrival_days(node) -> int:
    for sp in node.find_elements(By.CSS_SELECTOR, "aside span, span, p, div"):
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


def _build_driver(request_id: str, show_browser: bool) -> webdriver.Chrome:
    opts = webdriver.ChromeOptions()
    opts.add_argument("--window-size=1366,768")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--lang=ru-RU")

    if not show_browser:
        opts.add_argument("--headless=new")

    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.geolocation": 2,
    }
    opts.add_experimental_option("prefs", prefs)

    chromedriver_path = os.getenv("CHROMEDRIVER") or shutil.which("chromedriver") or "/usr/bin/chromedriver"

    allow_wdm = os.getenv("FASTRANS_ALLOW_WDM", "0").strip() in {"1", "true", "True", "yes", "YES"}

    if chromedriver_path and Path(chromedriver_path).exists():
        log.debug("fastrans webdriver config request_id=%s chromedriver=%s", request_id, chromedriver_path)
        service = Service(executable_path=chromedriver_path)
    elif allow_wdm:
        path = ChromeDriverManager().install()
        log.debug("fastrans webdriver config request_id=%s chromedriver(downloaded)=%s", request_id, path)
        service = Service(executable_path=path)
    else:
        raise TemporaryError("fastrans: chromedriver not found and webdriver_manager disabled")

    raise TemporaryError("fastrans: driver must be provided by pool")


def fastrans(
    from_city: str,
    to_city: str,
    places: int,
    weight: int,
    volume: float,
    show_browser: bool = False,
    driver: webdriver.Chrome | None = None,
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    request_id = uuid.uuid4().hex[:12]

    log.info(
        "fastrans start request_id=%s from=%r to=%r places=%s weight=%s volume=%s show_browser=%s",
        request_id, from_city, to_city, places, weight, volume, show_browser
    )

    if _is_no_city(from_city, to_city):
        raise InvalidInputError(f"fastrans: no terminal for route {from_city!r} -> {to_city!r} (cached)")

    if from_city in banned or to_city in banned:
        log.warning("fastrans banned route request_id=%s from=%r to=%r", request_id, from_city, to_city)
        raise InvalidInputError(f"fastrans: banned route {from_city!r} -> {to_city!r}")

    driver: Optional[webdriver.Chrome] = driver
    if driver is None:
        raise TemporaryError("fastrans: driver is required")
    try:
        wait = WebDriverWait(driver, 30, poll_frequency=0.2)

        driver.get(SOURCE_URL)
        log.debug("fastrans opened request_id=%s url=%s", request_id, driver.current_url)

        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.multiselect")))
        _dismiss_overlays(driver)

        # ТУту "Город отправления"
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
        log.debug("fastrans typed from_city request_id=%s", request_id)

        from_panel = _open_multiselect_and_get_panel(driver, from_block, request_id=request_id, label="from")

        def _from_opts(_):
            opts_ = from_panel.find_elements(By.CSS_SELECTOR, "li.multiselect__element .multiselect__option")
            # только видимые берем, время 5 утра)
            vis = []
            for o in opts_:
                try:
                    if o.is_displayed():
                        vis.append(o)
                except Exception:
                    pass
            return vis if vis else False

        try:
            from_options = wait.until(_from_opts)
        except TimeoutException:
            log.error("fastrans from_city options empty request_id=%s from=%r", request_id, from_city)
            _remember_no_city(from_city, to_city)
            raise InvalidInputError(f"fastrans: city not found: {from_city!r}")
        log.debug("fastrans from_options count=%s request_id=%s", len(from_options), request_id)

        from_target = _pick_city_option(from_options, from_city)
        if not from_target:
            log.error("fastrans from_city not found request_id=%s from=%r", request_id, from_city)
            _remember_no_city(from_city, to_city)
            raise InvalidInputError(f"fastrans: city not found: {from_city!r}")

        _robust_click(driver, from_target, request_id=request_id, label="from.option")

        # Блок "Город назначения"
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
        log.debug("fastrans typed to_city request_id=%s", request_id)

        to_panel = _open_multiselect_and_get_panel(driver, to_block, request_id=request_id, label="to")

        def _to_opts(_):
            opts_ = to_panel.find_elements(By.CSS_SELECTOR, "li.multiselect__element .multiselect__option")
            vis = []
            for o in opts_:
                try:
                    if o.is_displayed():
                        vis.append(o)
                except Exception:
                    pass
            return vis if vis else False

        try:
            to_options = wait.until(_to_opts)
        except TimeoutException:
            log.error("fastrans to_city options empty request_id=%s to=%r", request_id, to_city)
            _remember_no_city(from_city, to_city)
            raise InvalidInputError(f"fastrans: city not found: {to_city!r}")
        log.debug("fastrans to_options count=%s request_id=%s", len(to_options), request_id)

        to_target = _pick_city_option(to_options, to_city)
        if not to_target:
            log.error("fastrans to_city not found request_id=%s to=%r", request_id, to_city)
            _remember_no_city(from_city, to_city)
            raise InvalidInputError(f"fastrans: city not found: {to_city!r}")

        _robust_click(driver, to_target, request_id=request_id, label="to.option")

        _fill_num(wait, driver, "input.VueFormInputInput[name='amount']", places)
        _fill_num(wait, driver, "input.VueFormInputInput[name='weight']", weight)
        _fill_num(wait, driver, "input.VueFormInputInput[name='volume']", str(round(float(volume), 3)))

        calc_btn = wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//div[contains(@class,'VueForm-controls')]//button[.//span[normalize-space()='Расчёт']]",
                )
            )
        )
        _robust_click(driver, calc_btn, request_id=request_id, label="calc.button")

        node, err_text = _wait_result_or_error(driver, timeout=60)
        if err_text:
            err_low = err_text.lower()
            if any(s in err_low for s in ["не обслуживается", "нет терминал", "город отправления", "город назначения", "адрес в городе"]):
                _remember_no_city(from_city, to_city)
                raise InvalidInputError(f"fastrans: city not served: {err_text}")
            raise InvalidInputError(f"fastrans: invalid form: {err_text}")
        if node is None:
            raise TemporaryError("fastrans: selenium wait timeout")
        breakdown = _extract_breakdown(node)
        log.debug("fastrans breakdown request_id=%s data=%s", request_id, breakdown)

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
            log.debug("fastrans extracted aside price request_id=%s price=%s", request_id, transport_price)

        days = _extract_arrival_days(node)

        log.info("fastrans done request_id=%s price=%s days=%s insurance=%s", request_id, transport_price, days, insurance)
        return transport_price+float(insurance), days, insurance

    except TimeoutException as e:
        log.exception("fastrans timeout request_id=%s: %s", request_id, e)
        raise TemporaryError("fastrans: selenium wait timeout") from e

    except NoSuchElementException as e:
        log.exception("fastrans element not found request_id=%s: %s", request_id, e)
        raise TemporaryError("fastrans: element not found") from e

    except WebDriverException as e:
        log.exception("fastrans webdriver error request_id=%s: %s", request_id, e)
        raise TemporaryError(f"fastrans: webdriver error: {e}") from e

    except Exception as e:
        log.exception("fastrans unexpected error request_id=%s: %s", request_id, e)
        raise

    finally:
        try:
            if show_browser:
                time.sleep(2)
        finally:
            pass


class FastransAdapter(SyncSeleniumAdapter):
    """Класс FastransAdapter."""
    code = "fastrans"
    TIMEOUT = 90.0
    HEADLESS = True

    def _calc_sync(self, p: CalcParams, driver: webdriver.Chrome) -> CalcResult:
        try:
            res = fastrans(
                p.from_city,
                p.to_city,
                int(p.places),
                int(round(p.weight_kg)),
                float(p.volume_m3),
                show_browser=False,
                driver=driver,
            )
        except InvalidInputError:
            raise
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
            source=SOURCE_URL,
            name_tarif=None,
            allowances=allowances,
        )


__all__ = ["fastrans", "FastransAdapter"]
