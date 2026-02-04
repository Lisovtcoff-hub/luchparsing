
"""Модуль magic_trans.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import re
import time
import os
import logging
import threading
from typing import Dict, Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException, StaleElementReferenceException

from core.contracts import CalcParams, CalcResult, InvalidInputError, TemporaryError
from core.selenium_base import SyncSeleniumAdapter


_NON_TERMINAL_CITIES: dict[str, float] = {}
_NON_TERMINAL_TTL_S = 6 * 60 * 60
logger = logging.getLogger(__name__)
_NO_ROUTE_LOCK = threading.Lock()
_NO_ROUTE_ROUTES: set[tuple[str, str]] = set()
_NO_RESULT_LOCK = threading.Lock()
_NO_RESULT_ROUTES: set[tuple[str, str]] = set()
MAGIC_TRANS_STALE_RETRIES = int(os.getenv("MAGIC_TRANS_STALE_RETRIES", "3") or 3)



def _norm_city(value: str) -> str:
    return (value or "").strip().lower()


def _is_non_terminal(city_key: str) -> bool:
    ts = _NON_TERMINAL_CITIES.get(city_key)
    if not ts:
        return False
    if time.time() - ts > _NON_TERMINAL_TTL_S:
        _NON_TERMINAL_CITIES.pop(city_key, None)
        return False
    return True


def _route_key(from_city: str, to_city: str) -> tuple[str, str]:
    return (_norm_city(from_city), _norm_city(to_city))


def _remember_no_route(from_city: str, to_city: str) -> None:
    with _NO_ROUTE_LOCK:
        _NO_ROUTE_ROUTES.add(_route_key(from_city, to_city))


def _is_no_route(from_city: str, to_city: str) -> bool:
    with _NO_ROUTE_LOCK:
        return _route_key(from_city, to_city) in _NO_ROUTE_ROUTES


def _remember_no_result(from_city: str, to_city: str) -> None:
    with _NO_RESULT_LOCK:
        _NO_RESULT_ROUTES.add(_route_key(from_city, to_city))


def _is_no_result(from_city: str, to_city: str) -> bool:
    with _NO_RESULT_LOCK:
        return _route_key(from_city, to_city) in _NO_RESULT_ROUTES


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
    inp.send_keys(Keys.ARROW_DOWN)
    inp.send_keys(Keys.ENTER)
    time.sleep(0.1)
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
    _stale_attempt: int = 0,
) -> tuple[Optional[float], Optional[int], Dict[str, float]]:
    """Возвращает (price, days, allowances)."""
    started_ts = time.time()
    logger.info("magic-trans start", extra={"from_city": from_city, "to_city": to_city, "places": places, "weight": weight, "volume": volume, "stale_attempt": _stale_attempt})
    if _is_no_route(from_city, to_city):
        raise InvalidInputError(f"magic-trans: no terminal for route (cached): {from_city} -> {to_city}")
    if _is_no_result(from_city, to_city):
        raise InvalidInputError(f"magic-trans: no terminal for route (timeout cached): {from_city} -> {to_city}")

    from_key = _norm_city(from_city)
    to_key = _norm_city(to_city)
    if _is_non_terminal(from_key) or _is_non_terminal(to_key):
        raise InvalidInputError(f"magic-trans: город без терминала (cached): {from_city} -> {to_city}")
        logger.info(
            "magic-trans: skipped by cache",
            extra={"from_city": from_city, "to_city": to_city, "cache_size": len(_NON_TERMINAL_CITIES)},
        )
        return None, None, {}
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

    driver = webdriver.Chrome(
        service=Service(executable_path=os.getenv("CHROMEDRIVER", "/usr/bin/chromedriver")),
        options=chrome_options,
    )
    driver.set_page_load_timeout(20)
    wait = WebDriverWait(driver, 15, poll_frequency=0.2)

    def _wait_city_applied(selector: str, expected: str, timeout_s: float = 3.0) -> None:
        expected_low = (expected or "").strip().lower()
        if not expected_low:
            return
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            val = driver.execute_script(
                "const el=document.querySelector(arguments[0]); return el ? el.value : '';",
                selector,
            )
            if expected_low in (val or "").lower():
                return
            time.sleep(0.1)

    def _city_applied_any(expected: str, timeout_s: float = 3.0) -> bool:
        expected_low = (expected or "").strip().lower()
        if not expected_low:
            return True
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            val = driver.execute_script(
                """
                const norm = (v) => (v || "").toString().trim().toLowerCase();
                const v1 = norm(document.querySelector("input[data-name='city-from']")?.value);
                const v2 = norm(document.querySelector("input[data-name='city-to']")?.value);
                const v3 = norm(document.querySelector("#input-city-from")?.value);
                const v4 = norm(document.querySelector("#input-city-to")?.value);
                return [v1, v2, v3, v4].filter(Boolean).join(" | ");
                """
            )
            if expected_low in (val or "").lower():
                return True
            time.sleep(0.1)
        return False

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
        logger.info("magic-trans opened", extra={"url": driver.current_url})

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

        logger.info("magic-trans set city-from", extra={"from_city": from_city})
        set_city(driver, wait, "input[data-name='city-from'], #input-city-from", from_city)
        time.sleep(0.5)
        logger.info("magic-trans set city-to", extra={"to_city": to_city})
        set_city(driver, wait, "input[data-name='city-to'], #input-city-to", to_city)
        _wait_city_applied("input[data-name='city-from']", from_city)
        _wait_city_applied("input[data-name='city-to']", to_city)
        if not _city_applied_any(from_city):
            logger.info("magic-trans set city-from", extra={"from_city": from_city})
            set_city(driver, wait, "input[data-name='city-from'], #input-city-from", from_city)
        if not _city_applied_any(to_city):
            logger.info("magic-trans set city-to", extra={"to_city": to_city})
            set_city(driver, wait, "input[data-name='city-to'], #input-city-to", to_city)
        # === ПРОВЕРКА: если город не терминальный ЗАЕБАЛА
        def _get_terminal_state() -> dict:
            info = driver.execute_script(
                """
                const norm = (v) => (v || "").toString().trim().toLowerCase();
                const cityFrom = norm(
                    document.querySelector("input[data-name='city-from']")?.value
                    || document.querySelector("#input-city-from")?.value
                );
                const cityTo = norm(
                    document.querySelector("input[data-name='city-to']")?.value
                    || document.querySelector("#input-city-to")?.value
                );
                const tf = document.querySelector("input[data-name='type-from']");
                const tt = document.querySelector("input[data-name='type-to']");
                const typeFrom = norm(tf && tf.value);
                const typeTo = norm(tt && tt.value);

                const fromBlock = document.querySelector("div[data-name='direction-from-terminal']");
                const toBlock = document.querySelector("div[data-name='direction-to-terminal']");
                const fromBlockDisabled = !!(fromBlock && fromBlock.classList && fromBlock.classList.contains("k1-disabled-radio"));
                const toBlockDisabled = !!(toBlock && toBlock.classList && toBlock.classList.contains("k1-disabled-radio"));

                const fromRadio = document.querySelector("input[data-name='mt-radio-from-terminal']");
                const toRadio = document.querySelector("input[data-name='mt-radio-to-terminal']");
                const fromRadioDisabled = !!(fromRadio && fromRadio.disabled);
                const toRadioDisabled = !!(toRadio && toRadio.disabled);

                const selectFrom = document.querySelector("select[data-name='address-terminal-from']");
                const selectTo = document.querySelector("select[data-name='address-terminal-to'], #address-terminal-to");
                const terminalFromVal = norm(document.querySelector("input[data-name='terminal-from']")?.value);
                const terminalToVal = norm(document.querySelector("input[data-name='terminal-to']")?.value);
                const routeFromVal = norm(document.querySelector("[data-name='rout-terminal-from']")?.textContent);
                const routeToVal = norm(document.querySelector("[data-name='rout-terminal-to']")?.textContent);
                const fromTerminalSelected = !!terminalFromVal || !!routeFromVal;
                const toTerminalSelected = !!terminalToVal || !!routeToVal;
                const fromOptionsLen = selectFrom && selectFrom.options ? selectFrom.options.length : 0;
                const toOptionsLen = selectTo && selectTo.options ? selectTo.options.length : 0;
                const fromPlaceholder = fromOptionsLen === 1;
                const toPlaceholder = toOptionsLen === 1;

                const countTerminals = (rootSel, selectEl) => {
                    const root = rootSel ? document.querySelector(rootSel) : null;
                    const byClass = root ? root.querySelectorAll(".selecter-options .k1-regime-terminal").length : 0;
                    if (byClass > 0) return byClass;
                    if (!selectEl || !selectEl.options) return 0;
                    let count = 0;
                    for (const opt of selectEl.options) {
                        const v = (opt.value || "").toString().trim();
                        const t = (opt.text || "").toString().trim();
                        if (v && t) count += 1;
                    }
                    return count;
                };
                const fromTerminalOptions = countTerminals("#selected_terminal_from, [data-name='direction-from-terminal']", selectFrom);
                const toTerminalOptions = countTerminals("#selected_terminal_to, [data-name='direction-to-terminal']", selectTo);
                const fromNoTerminals = fromOptionsLen > 0 && fromTerminalOptions === 0;
                const toNoTerminals = toOptionsLen > 0 && toTerminalOptions === 0;

                const fromNonTerminal = typeFrom === "city" || fromBlockDisabled || fromRadioDisabled || fromPlaceholder || fromNoTerminals;
                const toNonTerminal = typeTo === "city" || toBlockDisabled || toRadioDisabled || toPlaceholder || toNoTerminals;
                const anyNonTerminal = fromNonTerminal || toNonTerminal;
                return {
                    cityFrom,
                    cityTo,
                    typeFrom,
                    typeTo,
                    fromBlockDisabled,
                    toBlockDisabled,
                    fromRadioDisabled,
                    toRadioDisabled,
                    terminalFromVal,
                    terminalToVal,
                    routeFromVal,
                    routeToVal,
                    fromTerminalSelected,
                    toTerminalSelected,
                    fromOptionsLen,
                    toOptionsLen,
                    fromPlaceholder,
                    toPlaceholder,
                    fromTerminalOptions,
                    toTerminalOptions,
                    fromNoTerminals,
                    toNoTerminals,
                    fromNonTerminal,
                    toNonTerminal,
                    anyNonTerminal,
                };
                """
            )
            return info if isinstance(info, dict) else {}

        def _get_terminal_state_safe(attempts: int = 4, delay_s: float = 0.15) -> dict:
            last_info: dict = {}
            for _ in range(max(1, attempts)):
                try:
                    last_info = _get_terminal_state()
                    return last_info
                except StaleElementReferenceException:
                    time.sleep(delay_s)
                except Exception:
                    # Keep last_info and break on unexpected errors
                    break
            return last_info

        def _terminal_state_ready(info: Optional[dict]) -> bool:
            if not info:
                return False
            if info.get("typeFrom") or info.get("typeTo"):
                return True
            if info.get("fromBlockDisabled") or info.get("toBlockDisabled"):
                return True
            if info.get("fromRadioDisabled") or info.get("toRadioDisabled"):
                return True
            if info.get("fromTerminalSelected") or info.get("toTerminalSelected"):
                return True
            if (info.get("fromTerminalOptions") or 0) > 0 or (info.get("toTerminalOptions") or 0) > 0:
                return True
            if (info.get("fromOptionsLen") or 0) > 0 or (info.get("toOptionsLen") or 0) > 0:
                return True
            return False

        def _wait_terminal_state(timeout_s: float = 6.0) -> Optional[dict]:
            deadline = time.time() + timeout_s
            last_info = None
            while time.time() < deadline:
                last_info = _get_terminal_state_safe()
                if _terminal_state_ready(last_info):
                    return last_info
                time.sleep(0.15)
            return last_info

        def _remember_non_terminal_cities(info: Optional[dict] = None) -> None:
            info = info or _get_terminal_state_safe()
            if not info:
                return
            cached = []
            now_ts = time.time()
            if info.get("fromNonTerminal"):
                _NON_TERMINAL_CITIES[from_key] = now_ts
                cached.append(from_city)
            if info.get("toNonTerminal"):
                _NON_TERMINAL_CITIES[to_key] = now_ts
                cached.append(to_city)
            if info.get("anyNonTerminal"):
                _remember_no_route(from_city, to_city)
            logger.info(
                "magic-trans: non-terminal detected",
                extra={
                    "from_city": from_city,
                    "to_city": to_city,
                    "ui_city_from": info.get("cityFrom"),
                    "ui_city_to": info.get("cityTo"),
                    "type_from": info.get("typeFrom"),
                    "type_to": info.get("typeTo"),
                    "from_block_disabled": info.get("fromBlockDisabled"),
                    "to_block_disabled": info.get("toBlockDisabled"),
                    "from_radio_disabled": info.get("fromRadioDisabled"),
                    "to_radio_disabled": info.get("toRadioDisabled"),
                    "terminal_from": info.get("terminalFromVal"),
                    "terminal_to": info.get("terminalToVal"),
                    "route_from": info.get("routeFromVal"),
                    "route_to": info.get("routeToVal"),
                    "from_terminal_selected": info.get("fromTerminalSelected"),
                    "to_terminal_selected": info.get("toTerminalSelected"),
                    "from_options_len": info.get("fromOptionsLen"),
                    "to_options_len": info.get("toOptionsLen"),
                    "from_placeholder": info.get("fromPlaceholder"),
                    "to_placeholder": info.get("toPlaceholder"),
                    "from_terminal_options": info.get("fromTerminalOptions"),
                    "to_terminal_options": info.get("toTerminalOptions"),
                    "from_no_terminals": info.get("fromNoTerminals"),
                    "to_no_terminals": info.get("toNoTerminals"),
                    "cached": cached,
                },
            )

        info = _wait_terminal_state()
        logger.info("magic-trans terminal state", extra={"from_city": from_city, "to_city": to_city, "state": info})
        if _terminal_state_ready(info) and info.get("anyNonTerminal"):
            _remember_non_terminal_cities(info)
            raise InvalidInputError(f"magic-trans: город без терминала: {from_city} -> {to_city}")
        if not _terminal_state_ready(info):
            logger.warning(
                "magic-trans: terminal state not ready",
                extra={
                    "from_city": from_city,
                    "to_city": to_city,
                    "ui_city_from": info.get("cityFrom") if info else None,
                    "ui_city_to": info.get("cityTo") if info else None,
                    "type_from": info.get("typeFrom") if info else None,
                    "type_to": info.get("typeTo") if info else None,
                    "from_block_disabled": info.get("fromBlockDisabled") if info else None,
                    "to_block_disabled": info.get("toBlockDisabled") if info else None,
                    "from_radio_disabled": info.get("fromRadioDisabled") if info else None,
                    "to_radio_disabled": info.get("toRadioDisabled") if info else None,
                    "terminal_from": info.get("terminalFromVal") if info else None,
                    "terminal_to": info.get("terminalToVal") if info else None,
                    "route_from": info.get("routeFromVal") if info else None,
                    "route_to": info.get("routeToVal") if info else None,
                    "from_terminal_selected": info.get("fromTerminalSelected") if info else None,
                    "to_terminal_selected": info.get("toTerminalSelected") if info else None,
                    "from_options_len": info.get("fromOptionsLen") if info else None,
                    "to_options_len": info.get("toOptionsLen") if info else None,
                    "from_placeholder": info.get("fromPlaceholder") if info else None,
                    "to_placeholder": info.get("toPlaceholder") if info else None,
                    "from_terminal_options": info.get("fromTerminalOptions") if info else None,
                    "to_terminal_options": info.get("toTerminalOptions") if info else None,
                },
            )
        else:
            logger.info(
                "magic-trans: terminal state",
                extra={
                    "from_city": from_city,
                    "to_city": to_city,
                    "ui_city_from": info.get("cityFrom"),
                    "ui_city_to": info.get("cityTo"),
                    "type_from": info.get("typeFrom"),
                    "type_to": info.get("typeTo"),
                    "from_block_disabled": info.get("fromBlockDisabled"),
                    "to_block_disabled": info.get("toBlockDisabled"),
                    "from_radio_disabled": info.get("fromRadioDisabled"),
                    "to_radio_disabled": info.get("toRadioDisabled"),
                    "terminal_from": info.get("terminalFromVal"),
                    "terminal_to": info.get("terminalToVal"),
                    "route_from": info.get("routeFromVal"),
                    "route_to": info.get("routeToVal"),
                    "from_terminal_selected": info.get("fromTerminalSelected"),
                    "to_terminal_selected": info.get("toTerminalSelected"),
                    "from_options_len": info.get("fromOptionsLen"),
                    "to_options_len": info.get("toOptionsLen"),
                    "from_placeholder": info.get("fromPlaceholder"),
                    "to_placeholder": info.get("toPlaceholder"),
                    "from_terminal_options": info.get("fromTerminalOptions"),
                    "to_terminal_options": info.get("toTerminalOptions"),
                    "from_no_terminals": info.get("fromNoTerminals"),
                    "to_no_terminals": info.get("toNoTerminals"),
                },
            )

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
        logger.info("magic-trans clicked detailed calc", extra={"from_city": from_city, "to_city": to_city})

        qty = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[data-name='quantity-of-cargo']")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", qty)
        qty.click()
        qty.send_keys(Keys.CONTROL, "a")
        qty.send_keys(Keys.BACKSPACE)
        qty.send_keys(str(places))
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
            qty,
        )
        qty.send_keys(Keys.TAB)

        time.sleep(5)

        def _wait_result_ready(timeout_s: float = 20.0) -> bool:
            def _has_value(d) -> bool:
                return bool(
                    d.execute_script(
                        """
                        const pickText = (sel) => {
                            const el = document.querySelector(sel);
                            return el ? (el.textContent || "").trim() : "";
                        };
                        const pickAttr = (sel, attr) => {
                            const el = document.querySelector(sel);
                            return el ? (el.getAttribute(attr) || "").trim() : "";
                        };
                        return (
                            pickAttr("#res-calculation #total-delivery", "value")
                            || pickText("#res-calculation .k1-tot")
                        );
                        """
                    )
                )

            WebDriverWait(driver, timeout_s, poll_frequency=0.2).until(_has_value)
            return True

        def _refetch_btn_detail():
            return wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='knopka_podrobniy_raschet'].submit-main-calculate"))
            )

        res = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "#res-calculation")))
        logger.info("magic-trans result container visible", extra={"from_city": from_city, "to_city": to_city})

        result_ready = False
        try:
            logger.info("magic-trans wait result ready", extra={"from_city": from_city, "to_city": to_city, "attempt": 1})
            _wait_result_ready(20.0)
            result_ready = True
        except TimeoutException:
            logger.warning("magic-trans result not ready (attempt 1)", extra={"from_city": from_city, "to_city": to_city})
        if not result_ready:
            _remember_no_result(from_city, to_city)
            raise TemporaryError("magic-trans: result not ready after timeout [no-retry]")

        info_after = _wait_terminal_state(timeout_s=6.0)
        logger.info("magic-trans terminal state after calc", extra={"from_city": from_city, "to_city": to_city, "state": info_after})
        if _terminal_state_ready(info_after) and info_after.get("anyNonTerminal"):
            _remember_non_terminal_cities(info_after)
            raise InvalidInputError(f"magic-trans: город без терминала: {from_city} -> {to_city}")
        if _terminal_state_ready(info_after):
            logger.info(
                "magic-trans: terminal state after calc",
                extra={
                    "from_city": from_city,
                    "to_city": to_city,
                    "ui_city_from": info_after.get("cityFrom"),
                    "ui_city_to": info_after.get("cityTo"),
                    "type_from": info_after.get("typeFrom"),
                    "type_to": info_after.get("typeTo"),
                    "from_block_disabled": info_after.get("fromBlockDisabled"),
                    "to_block_disabled": info_after.get("toBlockDisabled"),
                    "from_radio_disabled": info_after.get("fromRadioDisabled"),
                    "to_radio_disabled": info_after.get("toRadioDisabled"),
                    "terminal_from": info_after.get("terminalFromVal"),
                    "terminal_to": info_after.get("terminalToVal"),
                    "route_from": info_after.get("routeFromVal"),
                    "route_to": info_after.get("routeToVal"),
                    "from_terminal_selected": info_after.get("fromTerminalSelected"),
                    "to_terminal_selected": info_after.get("toTerminalSelected"),
                    "from_options_len": info_after.get("fromOptionsLen"),
                    "to_options_len": info_after.get("toOptionsLen"),
                    "from_placeholder": info_after.get("fromPlaceholder"),
                    "to_placeholder": info_after.get("toPlaceholder"),
                    "from_terminal_options": info_after.get("fromTerminalOptions"),
                    "to_terminal_options": info_after.get("toTerminalOptions"),
                    "from_no_terminals": info_after.get("fromNoTerminals"),
                    "to_no_terminals": info_after.get("toNoTerminals"),
                },
            )

        try:
            logger.info("magic-trans wait price text", extra={"from_city": from_city, "to_city": to_city})
            price_text = WebDriverWait(driver, 60, poll_frequency=0.2).until(
                lambda d: next(
                    (
                        t.strip()
                        for t in [
                            el.text
                            for el in d.find_elements(By.CSS_SELECTOR, "#res-calculation table.table-summ-first td:nth-child(2)")
                        ]
                        if _rub_to_int(t) not in (None, 0)
                    ),
                    None,
                )
            )
        except TimeoutException:
            logger.warning("magic-trans price text timeout", extra={"from_city": from_city, "to_city": to_city})
            price_text = None

        if not price_text or _rub_to_int(price_text) in (None, 0):
            price_text = driver.execute_script(
                """
                const pickText = (sel) => {
                    const el = document.querySelector(sel);
                    return el ? (el.textContent || "").trim() : "";
                };
                const pickAttr = (sel, attr) => {
                    const el = document.querySelector(sel);
                    return el ? (el.getAttribute(attr) || "").trim() : "";
                };
                return (
                    pickAttr("#res-calculation #total-delivery", "value")
                    || pickText("#res-calculation .k1-tot")
                    || pickText("#res-calculation table.table-summ-first td.k1-tot")
                );
                """
            )
            if price_text:
                logger.info(
                    "magic-trans: price fallback",
                    extra={"from_city": from_city, "to_city": to_city, "price_text": price_text},
                )
        if not price_text or _rub_to_int(price_text) in (None, 0):
            raise TemporaryError("magic-trans: price not available after retries")

        try:
            additional_text = res.find_element(By.CSS_SELECTOR, "table.k-calc-table-toggle tbody tr td:nth-child(2)").text.strip()
        except Exception:
            additional_text = ""

        try:
            days_text = res.find_element(By.CSS_SELECTOR, "table.table-summ-first tbody tr td.totals span").text.strip()
        except Exception:
            days_text = ""
        m = re.search(r"\d+", days_text or "")
        days = int(m.group()) if m else None

        price_val = _to_float(price_text or "")
        allowances: Dict[str, float] = {}
        add_val = _to_float(additional_text or "")
        total = price_val
        if add_val is not None:
            allowances["Страхование"] = float(add_val)

        if add_val is not None and price_val is not None:
            total = price_val + add_val
        elif add_val is not None and price_val is None:
            total = add_val

        # print(from_city, to_city, total, days, allowances)
        logger.info("magic-trans done", extra={"from_city": from_city, "to_city": to_city, "total": total, "days": days, "allowances": allowances, "ms": int((time.time() - started_ts) * 1000)})
        return total, days, allowances

    except StaleElementReferenceException as e:
        if _stale_attempt < max(0, MAGIC_TRANS_STALE_RETRIES):
            logger.warning(
                "magic-trans stale retry",
                extra={"from_city": from_city, "to_city": to_city, "attempt": _stale_attempt + 1},
            )
            return magic_trans_calc(
                from_city,
                to_city,
                places,
                weight,
                volume,
                dims,
                show_browser=show_browser,
                _stale_attempt=_stale_attempt + 1,
            )
        raise TemporaryError("magic-trans: stale element reference") from e

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
                round(float(p.volume_m3), 3),
                dims,
                show_browser=False,
            )
        except InvalidInputError:
            raise
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
