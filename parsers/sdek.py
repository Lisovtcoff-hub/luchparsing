
"""Модуль sdek.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import os
import time
from typing import Optional

import httpx
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
    ElementNotInteractableException,
    WebDriverException,
    InvalidSessionIdException,
    NoSuchElementException,
)

from core.contracts import CalcParams, CalcResult, TemporaryError, InvalidInputError
from core.selenium_base import SyncSeleniumAdapter, safe_click


CDEK_URL = "https://www.cdek.ru/ru/cabinet/calculate/"

RETRY_EXCEPTIONS = (
    TimeoutException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
    ElementNotInteractableException,
)


def dismiss_popups(driver: webdriver.Chrome) -> None:
    """Функция dismiss_popups.

    Параметры:
        driver: Описание параметра.
    """
    try:
        ok = driver.find_elements(By.XPATH, "//button[normalize-space()='Ok' or normalize-space()='Ок']")
        if ok:
            driver.execute_script("arguments[0].click();", ok[0])
    except Exception:
        pass


def safe_step(driver: webdriver.Chrome, name: str, fn, attempts: int = 3, pause: float = 0.4):
    """Функция safe_step.

    Параметры:
        driver: Описание параметра.
        name: Описание параметра.
        fn: Описание параметра.
        attempts: Описание параметра.
        pause: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            dismiss_popups(driver)
            return fn()
        except InvalidSessionIdException:
            raise
        except RETRY_EXCEPTIONS as e:
            last_exc = e
            time.sleep(pause)
        except WebDriverException:
            raise
    if last_exc:
        raise last_exc
    raise TimeoutException(f"cdek: step failed: {name}")


def safe_click(driver: webdriver.Chrome, wait: WebDriverWait, name: str, by, locator: str):
    """Функция safe_click.

    Параметры:
        driver: Описание параметра.
        wait: Описание параметра.
        name: Описание параметра.
        by: Описание параметра.
        locator: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    def _do():
        """Функция _do.

        Возвращает:
            Результат выполнения функции.
        """
        el = wait.until(EC.presence_of_element_located((by, locator)))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        try:
            ActionChains(driver).move_to_element(el).pause(0.1).click().perform()
        except Exception:
            driver.execute_script("arguments[0].click();", el)
        return el

    return safe_step(driver, f"click:{name}", _do)


def wait_modal_closed(driver: webdriver.Chrome, timeout: float = 12) -> bool:
    """Функция wait_modal_closed.

    Параметры:
        driver: Описание параметра.
        timeout: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    end = time.time() + timeout
    while time.time() < end:
        dialogs = driver.find_elements(By.CSS_SELECTOR, "div[role='dialog']")
        masks = driver.find_elements(By.CSS_SELECTOR, "div.p-dialog-mask")
        any_visible_dialog = any(d.is_displayed() for d in dialogs) if dialogs else False
        any_visible_mask = any(m.is_displayed() for m in masks) if masks else False
        if not any_visible_dialog and not any_visible_mask:
            return True
        time.sleep(0.2)
    return False


def get_active_dialog(driver: webdriver.Chrome, title_text: str):
    """Функция get_active_dialog.

    Параметры:
        driver: Описание параметра.
        title_text: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    dialogs = driver.find_elements(By.CSS_SELECTOR, "div[role='dialog']")
    candidates = []
    for d in dialogs:
        try:
            if d.is_displayed() and d.find_elements(By.XPATH, f".//*[contains(normalize-space(),'{title_text}')]"):
                candidates.append(d)
        except Exception:
            pass
    return candidates[-1] if candidates else None


def listbox_for_input(driver: webdriver.Chrome, wait: WebDriverWait, input_el):
    """Функция listbox_for_input.

    Параметры:
        driver: Описание параметра.
        wait: Описание параметра.
        input_el: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    list_id = input_el.get_attribute("aria-controls")
    if list_id:
        ul = wait.until(EC.visibility_of_element_located((By.ID, list_id)))
    else:
        ul = wait.until(EC.visibility_of_element_located((By.XPATH, "//ul[@role='listbox']")))
    wait.until(lambda d: len(ul.find_elements(By.CSS_SELECTOR, "li.p-autocomplete-item")) >= 1)
    return ul


def click_city(driver: webdriver.Chrome, wait: WebDriverWait, input_el, city: str) -> None:
    """Функция click_city.

    Параметры:
        driver: Описание параметра.
        wait: Описание параметра.
        input_el: Описание параметра.
        city: Описание параметра.
    """
    ul = listbox_for_input(driver, wait, input_el)
    items = ul.find_elements(By.XPATH, f".//li[.//*[contains(normalize-space(),'{city}')]]")
    if not items:
        items = ul.find_elements(By.CSS_SELECTOR, "li.p-autocomplete-item")
    if not items:
        raise TimeoutException("autocomplete list is empty")
    driver.execute_script("arguments[0].click();", items[0])


def click_second_or_first(driver: webdriver.Chrome, wait: WebDriverWait, input_el) -> None:
    """Функция click_second_or_first.

    Параметры:
        driver: Описание параметра.
        wait: Описание параметра.
        input_el: Описание параметра.
    """
    ul = listbox_for_input(driver, wait, input_el)
    items = ul.find_elements(By.CSS_SELECTOR, "li.p-autocomplete-item")
    if not items:
        raise TimeoutException("autocomplete list is empty")
    target = items[1] if len(items) > 1 else items[0]
    driver.execute_script("arguments[0].click();", target)


def pick_point(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    prefix: str,
    open_btn_locator: str,
    title_text: str,
    city: str,
    confirm_text: str,
) -> None:
    """Функция pick_point.

    Параметры:
        driver: Описание параметра.
        wait: Описание параметра.
        prefix: Описание параметра.
        open_btn_locator: Описание параметра.
        title_text: Описание параметра.
        city: Описание параметра.
        confirm_text: Описание параметра.
    """
    wait_modal_closed(driver, timeout=6)

    safe_click(driver, wait, f"{prefix}:open_map", By.CSS_SELECTOR, open_btn_locator)

    safe_step(
        driver,
        f"{prefix}:wait_title",
        lambda: wait.until(
            EC.visibility_of_element_located(
                (By.XPATH, f"//div[@role='dialog']//*[contains(normalize-space(),'{title_text}')]")
            )
        ),
    )

    dialog = safe_step(driver, f"{prefix}:get_dialog", lambda: get_active_dialog(driver, title_text))
    if dialog is None:
        raise TimeoutException(f"{prefix}: dialog with title '{title_text}' not found")

    try:
        city_input = dialog.find_element(By.XPATH, ".//label[normalize-space()='Город, адрес']/preceding::input[1]")
    except NoSuchElementException:
        city_input = dialog.find_element(By.XPATH, ".//input")

    safe_step(driver, f"{prefix}:focus", lambda: driver.execute_script("arguments[0].focus();", city_input))
    safe_step(driver, f"{prefix}:clear", lambda: city_input.send_keys(Keys.CONTROL, "a"))
    safe_step(driver, f"{prefix}:type", lambda: city_input.send_keys(city))

    time.sleep(0.7)
    safe_step(driver, f"{prefix}:pick_city", lambda: click_city(driver, wait, city_input, city))
    time.sleep(0.4)
    safe_step(driver, f"{prefix}:pick_address", lambda: click_second_or_first(driver, wait, city_input))
    time.sleep(0.4)

    dialog2 = get_active_dialog(driver, title_text)
    if dialog2 is None:
        return

    def click_confirm():
        """Функция click_confirm.

        Возвращает:
            Результат выполнения функции.
        """
        btn = dialog2.find_element(
            By.XPATH,
            f".//button[.//span[normalize-space()='{confirm_text}'] or normalize-space()='{confirm_text}']",
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        try:
            ActionChains(driver).move_to_element(btn).pause(0.1).click().perform()
        except Exception:
            driver.execute_script("arguments[0].click();", btn)

    safe_step(driver, f"{prefix}:click_confirm", click_confirm, attempts=2)
    wait_modal_closed(driver, timeout=12)


def wait_place_dialog(wait: WebDriverWait, place_no: int):
    """Функция wait_place_dialog.

    Параметры:
        wait: Описание параметра.
        place_no: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    return wait.until(
        EC.visibility_of_element_located(
            (
                By.XPATH,
                f"//div[@role='dialog'][.//div[contains(@class,'p-dialog-title') and contains(normalize-space(),'Место {place_no}')]]",
            )
        )
    )


def click_own_packaging_in_dialog(driver: webdriver.Chrome, dlg, place_no: int) -> None:
    """Функция click_own_packaging_in_dialog.

    Параметры:
        driver: Описание параметра.
        dlg: Описание параметра.
        place_no: Описание параметра.
    """
    own_btn = safe_step(
        driver,
        f"place{place_no}:click_own_packaging",
        lambda: dlg.find_element(
            By.XPATH,
            ".//div[contains(@class,'p-togglebutton')]"
            "[.//span[contains(@class,'p-button-label') and normalize-space()='Своя упаковка']]",
        ),
    )
    driver.execute_script("arguments[0].click();", own_btn)


def set_value_in_dialog(driver: webdriver.Chrome, dlg, place_no: int, input_id: str, value: str) -> None:
    """Функция set_value_in_dialog.

    Параметры:
        driver: Описание параметра.
        dlg: Описание параметра.
        place_no: Описание параметра.
        input_id: Описание параметра.
        value: Описание параметра.
    """
    el = safe_step(driver, f"place{place_no}:find_{input_id}", lambda: dlg.find_element(By.ID, input_id))
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    driver.execute_script("arguments[0].focus();", el)
    el.send_keys(Keys.CONTROL, "a")
    el.send_keys(str(value))
    el.send_keys(Keys.TAB)


def click_button_in_dialog_by_text(driver: webdriver.Chrome, dlg, place_no: int, text: str) -> None:
    """Функция click_button_in_dialog_by_text.

    Параметры:
        driver: Описание параметра.
        dlg: Описание параметра.
        place_no: Описание параметра.
        text: Описание параметра.
    """
    btn = safe_step(
        driver,
        f"place{place_no}:find_btn_{text}",
        lambda: dlg.find_element(By.XPATH, f".//button[normalize-space()='{text}' or .//span[normalize-space()='{text}']]"),
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
    driver.execute_script("arguments[0].click();", btn)


def fill_place_and_continue(driver: webdriver.Chrome, wait: WebDriverWait, place_no: int, dims: dict, weight_kg: float, is_last: bool):
    """Функция fill_place_and_continue.

    Параметры:
        driver: Описание параметра.
        wait: Описание параметра.
        place_no: Описание параметра.
        dims: Описание параметра.
        weight_kg: Описание параметра.
        is_last: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    dlg = safe_step(driver, f"place{place_no}:wait_dialog", lambda: wait_place_dialog(wait, place_no))

    click_own_packaging_in_dialog(driver, dlg, place_no)

    safe_step(driver, f"place{place_no}:set_length", lambda: set_value_in_dialog(driver, dlg, place_no, "length", dims["l"]))
    safe_step(driver, f"place{place_no}:set_width", lambda: set_value_in_dialog(driver, dlg, place_no, "width", dims["w"]))
    safe_step(driver, f"place{place_no}:set_height", lambda: set_value_in_dialog(driver, dlg, place_no, "height", dims["h"]))
    safe_step(driver, f"place{place_no}:set_weight", lambda: set_value_in_dialog(driver, dlg, place_no, "weight", weight_kg))

    if is_last:
        def save_enabled():
            """Функция save_enabled.

            Возвращает:
                Результат выполнения функции.
            """
            btn = dlg.find_element(By.XPATH, ".//button[normalize-space()='Сохранить']")
            disabled_attr = btn.get_attribute("disabled")
            classes = btn.get_attribute("class") or ""
            return (disabled_attr is None) and ("p-disabled" not in classes)

        safe_step(driver, f"place{place_no}:wait_save_enabled", lambda: WebDriverWait(driver, 20).until(lambda d: save_enabled()))
        click_button_in_dialog_by_text(driver, dlg, place_no, "Сохранить")

        safe_step(
            driver,
            f"place{place_no}:wait_modal_close",
            lambda: WebDriverWait(driver, 25).until(
                EC.invisibility_of_element_located(
                    (
                        By.XPATH,
                        f"//div[@role='dialog'][.//div[contains(@class,'p-dialog-title') and contains(normalize-space(),'Место {place_no}')]]",
                    )
                )
            ),
        )
    else:
        click_button_in_dialog_by_text(driver, dlg, place_no, "Добавить место")
        safe_step(driver, f"place{place_no}:wait_next_place", lambda: wait_place_dialog(wait, place_no + 1))


def fill_multiple_places(driver: webdriver.Chrome, wait: WebDriverWait, places_count: int, total_weight: float, dims_one_place: dict) -> None:
    """Функция fill_multiple_places.

    Параметры:
        driver: Описание параметра.
        wait: Описание параметра.
        places_count: Описание параметра.
        total_weight: Описание параметра.
        dims_one_place: Описание параметра.
    """
    if places_count < 1:
        return

    per_place_weight = round(float(total_weight) / float(places_count), 6)

    for i in range(1, places_count + 1):
        fill_place_and_continue(
            driver=driver,
            wait=wait,
            place_no=i,
            dims=dims_one_place,
            weight_kg=per_place_weight,
            is_last=(i == places_count),
        )

        time.sleep(random.uniform(0.6, 1.2))
        if (i % 5 == 0) and (i != places_count):
            time.sleep(random.uniform(8, 12))


def parse_tariff_prices(driver: webdriver.Chrome, wait: WebDriverWait) -> dict[str, str]:
    """Функция parse_tariff_prices.

    Параметры:
        driver: Описание параметра.
        wait: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//div[@role='button'][.//p[contains(@class,'body-regular-lg-paragraph')]]",
            )
        )
    )

    prices: dict[str, str] = {}
    cards = driver.find_elements(By.XPATH, "//div[@role='button'][.//p[contains(@class,'body-regular-lg-paragraph')]]")
    for card in cards:
        try:
            if not card.is_displayed():
                continue
            name = card.find_element(By.CSS_SELECTOR, "p.body-regular-lg-paragraph").text.strip()
            price = card.find_element(By.CSS_SELECTOR, "p.body-bold-lg-paragraph").text.strip()
            if name and price:
                prices[name] = price
        except Exception:
            continue

    return prices


def _rub_to_float(s: str) -> Optional[float]:
    """Функция _rub_to_float.

    Параметры:
        s: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    if not s:
        return None
    digits = re.sub(r"[^\d]", "", s)
    return float(int(digits)) if digits else None

def _days_to_int(days_text: str) -> Optional[int]:
    """
    "~2 дня" / "~1 день" / "2 дня" -> 2 / 1 / None
    """
    if not days_text:
        return None
    m = re.search(r"\d+", days_text)
    return int(m.group(0)) if m else None


def _parse_tariffs_with_days(driver) -> list[dict]:
    """
    Возвращает список: [{"name": str, "price": float, "days": Optional[int]}, ...]
    """
    cards = driver.find_elements(
        By.XPATH,
        "//div[@role='button']"
        "[.//p[contains(@class,'body-regular-lg-paragraph')]"
        " and .//p[contains(@class,'body-regular-base-paragraph')]"
        " and .//p[contains(@class,'body-bold-lg-paragraph')]]"
    )

    out: list[dict] = []
    for card in cards:
        try:
            if not card.is_displayed():
                continue

            name = card.find_element(By.CSS_SELECTOR, "p.body-regular-lg-paragraph").text.strip()
            days_text = card.find_element(By.CSS_SELECTOR, "p.body-regular-base-paragraph").text.strip()
            price_text = card.find_element(By.CSS_SELECTOR, "p.body-bold-lg-paragraph").text.strip()

            price = _rub_to_float(price_text)
            days = _days_to_int(days_text)

            if not name or price is None:
                continue

            out.append({"name": name, "price": price, "days": days})
        except Exception:
            continue

    return out



def sdek_calc_sync(
    from_city: str,
    to_city: str,
    places: int,
    weight_kg: float,
    dims: dict,
    driver: webdriver.Chrome,
) -> tuple[Optional[float], Optional[str], Optional[int]]:
    """
    Возвращает:
      - price: стоимость "Стандарт" (если найден), иначе fallback (минимальная цена или None)
      - name_tarif_json: JSON-строка с альтернативными тарифами (кроме "Стандарт") в виде {"Тариф": 1234.0, ...}
      - days: количество дней у тарифа с минимальной ценой (если найден), иначе None
    """
    if driver is None:
        raise TemporaryError("cdek: driver is required")

    try:
        if places > 9:
            return  None, None, None
        driver.get(CDEK_URL)
        wait = WebDriverWait(driver, 40)

        pick_point(
            driver,
            wait,
            prefix="FROM",
            open_btn_locator='button[data-btn-open-map="from"]',
            title_text="Откуда отправить",
            city=from_city,
            confirm_text="Отправить отсюда",
        )

        pick_point(
            driver,
            wait,
            prefix="TO",
            open_btn_locator='button[data-btn-open-map="to"]',
            title_text="Куда доставить",
            city=to_city,
            confirm_text="Доставить сюда",
        )

        safe_click(
            driver,
            wait,
            name="parcel_size:open",
            by=By.XPATH,
            locator="//button[contains(@class,'p-button') and contains(normalize-space(.),'Указать размеры посылки')]",
        )

        safe_step(driver, "place1:wait_dialog", lambda: wait_place_dialog(wait, 1))

        fill_multiple_places(
            driver=driver,
            wait=wait,
            places_count=int(places),
            total_weight=float(weight_kg),
            dims_one_place=dims,
        )


        raw_prices = parse_tariff_prices(driver, wait)

        numeric_prices: dict[str, float] = {}
        for k, v in raw_prices.items():
            fv = _rub_to_float(v)
            if fv is not None:
                numeric_prices[k] = fv

        standard_price = numeric_prices.get("Стандарт")

        other_tariffs = {k: v for k, v in numeric_prices.items() if k != "Стандарт"}
        name_tarif_json = json.dumps(other_tariffs, ensure_ascii=False) if other_tariffs else None


        days_min_price: Optional[int] = None
        try:
            tariffs_with_days = _parse_tariffs_with_days(driver)
            if tariffs_with_days:
                min_card = min(tariffs_with_days, key=lambda x: x["price"])
                days_min_price = min_card.get("days")
        except Exception:
            days_min_price = None


        B2B_URL = "https://www.cdek.ru/ru/calculator_B2B/"
        driver.get(B2B_URL)
        wait_b2b = WebDriverWait(driver, 40)

        def _b2b_visible_input(label_ru: str):
            """Функция _b2b_visible_input.

            Параметры:
                label_ru: Описание параметра.

            Возвращает:
                Результат выполнения функции.
            """
            xp = (
                f"//div[contains(@class,'cdek-input__label') and normalize-space()='{label_ru}']"
                f"/ancestor::label[1]//input[contains(@class,'cdek-input__input')]"
            )

            def _pick(d):
                """Функция _pick.

                Параметры:
                    d: Описание параметра.

                Возвращает:
                    Результат выполнения функции.
                """
                els = d.find_elements(By.XPATH, xp)
                vis = [e for e in els if e.is_displayed() and e.is_enabled()]
                return vis[0] if vis else False

            return wait_b2b.until(_pick)

        def _pick_first_dropdown_item_near_input(inp):
            """Функция _pick_first_dropdown_item_near_input.

            Параметры:
                inp: Описание параметра.

            Возвращает:
                Результат выполнения функции.
            """
            def _first(d):
                """Функция _first.

                Параметры:
                    d: Описание параметра.

                Возвращает:
                    Результат выполнения функции.
                """
                root = inp.find_element(By.XPATH, "./ancestor::div[contains(@class,'cdek-autocomplete')][1]")
                box = root.find_element(By.CSS_SELECTOR, "div.cdek-dropdown-box")
                if not box.is_displayed():
                    return False
                items = box.find_elements(By.CSS_SELECTOR, "div.cdek-dropdown-item__content")
                return items[0] if items else False

            first_item = wait_b2b.until(_first)
            driver.execute_script("arguments[0].click();", first_item)

        def _fill_city_and_pick_first(label_ru: str, city: str):
            """Функция _fill_city_and_pick_first.

            Параметры:
                label_ru: Описание параметра.
                city: Описание параметра.

            Возвращает:
                Результат выполнения функции.
            """
            inp = _b2b_visible_input(label_ru)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", inp)

            try:
                ActionChains(driver).move_to_element(inp).pause(0.1).click().perform()
            except Exception:
                driver.execute_script("arguments[0].click();", inp)

            inp.send_keys(Keys.CONTROL, "a")
            inp.send_keys(city)

            _pick_first_dropdown_item_near_input(inp)

        _fill_city_and_pick_first("Откуда", from_city)
        time.sleep(0.2)
        _fill_city_and_pick_first("Куда", to_city)


        def _set_exact_field(label_text: str, value: float) -> None:
            """Функция _set_exact_field.

            Параметры:
                label_text: Описание параметра.
                value: Описание параметра.
            """
            inp = wait_b2b.until(
                EC.element_to_be_clickable((By.XPATH, f"//*[normalize-space()='{label_text}']/following::input[1]"))
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", inp)
            try:
                ActionChains(driver).move_to_element(inp).pause(0.1).click().perform()
            except Exception:
                driver.execute_script("arguments[0].click();", inp)

            inp.send_keys(Keys.CONTROL, "a")
            inp.send_keys(str(value).replace(",", "."))
            inp.send_keys(Keys.TAB)

        size_trigger = wait_b2b.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//div[contains(@class,'cdek-dropdown-trigger__control')][.//label[normalize-space()='Размер посылки']]",
                )
            )
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", size_trigger)
        try:
            ActionChains(driver).move_to_element(size_trigger).pause(0.1).click().perform()
        except Exception:
            driver.execute_script("arguments[0].click();", size_trigger)

        tab_exact = wait_b2b.until(
            EC.element_to_be_clickable((By.XPATH, "//div[contains(@class,'segment-control__tab')][normalize-space()='Точные']"))
        )
        try:
            ActionChains(driver).move_to_element(tab_exact).pause(0.1).click().perform()
        except Exception:
            driver.execute_script("arguments[0].click();", tab_exact)

        _set_exact_field("Длина", int(places*dims["l"]))
        _set_exact_field("Ширина", int(dims["w"]))
        _set_exact_field("Высота", int(dims["h"]))
        _set_exact_field("Вес", int(weight_kg))

        confirm_btn = wait_b2b.until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Подтвердить']")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", confirm_btn)
        try:
            ActionChains(driver).move_to_element(confirm_btn).pause(0.1).click().perform()
        except Exception:
            driver.execute_script("arguments[0].click();", confirm_btn)



        calc_btn = wait_b2b.until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Рассчитать']")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", calc_btn)
        driver.execute_script("arguments[0].click();", calc_btn)


        wait_b2b.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "div.tariffs, div.totals, div.mobile-total")
            )
        )

        b2b_selected_price = None


        try:
            el = wait_b2b.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.totals__all_price"))
            )
            txt = (el.text or "").strip()
            b2b_selected_price = _rub_to_float(txt)
        except Exception:
            pass


        if b2b_selected_price is None:
            try:
                el = wait_b2b.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.mobile-total__price p"))
                )
                txt = (el.text or "").strip()
                b2b_selected_price = _rub_to_float(txt)
            except Exception:
                pass



        if b2b_selected_price is None:
            try:

                selected = wait_b2b.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR,
                         "div.tariff-card_selected, div.tariff-card--selected, div.tariff-card__selected")
                    )
                )

                price_el = selected.find_element(By.XPATH, ".//*[contains(normalize-space(),'₽')][1]")
                txt = (price_el.text or "").strip()
                b2b_selected_price = _rub_to_float(txt)
            except Exception:
                pass


        if standard_price is not None:
            if b2b_selected_price is not None:
                data = json.loads(name_tarif_json) if name_tarif_json else {}
                data["Юр. лицо"] = b2b_selected_price
                name_tarif_json = json.dumps(data, ensure_ascii=False)
            return standard_price, name_tarif_json, days_min_price

        if numeric_prices:
            data = dict(numeric_prices)
            if b2b_selected_price is not None:
                data["Юр. лицо"] = b2b_selected_price
            return min(numeric_prices.values()), json.dumps(data, ensure_ascii=False), days_min_price

        return None, None, None

    finally:
        pass


class CdekAdapter(SyncSeleniumAdapter):
    """Класс CdekAdapter.

    Инкапсулирует связанную функциональность модуля.
    """
    code = "cdek"

    def _calc_sync(self, p: CalcParams, driver: webdriver.Chrome) -> CalcResult:
        """??????? _calc_sync.

        ?????????:
            p: ???????? ?????????.
        """
        d = getattr(p, "dims", None)
        if d is None:
            raise InvalidInputError("cdek: CalcParams.dims is None")

        l = getattr(d, "length_cm", None)
        w = getattr(d, "width_cm", None)
        h = getattr(d, "height_cm", None)
        if not l or not w or not h:
            raise InvalidInputError(f"cdek: invalid dims: {d!r}")

        dims = {"l": float(l), "w": float(w), "h": float(h)}

        try:
            price, name_tarif_json, days = sdek_calc_sync(
                p.from_city,
                p.to_city,
                int(p.places),
                float(p.weight_kg),
                dims,
                driver,
            )
        except InvalidInputError:
            raise
        except Exception as e:
            raise TemporaryError(f"cdek: {type(e).__name__}: {e}") from e

        return CalcResult(
            price=float(price) if price is not None else None,
            days=days,
            currency="RUB",
            source=CDEK_URL,
            name_tarif=name_tarif_json,
            allowances={},
        )


__all__ = ["CdekAdapter", "sdek_calc_sync"]


