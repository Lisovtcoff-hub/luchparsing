
"""Модуль wb.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Dict, Optional, Tuple

import httpx
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from webdriver_manager.chrome import ChromeDriverManager

from core.contracts import CarrierAdapter, CalcParams, CalcResult, TemporaryError, InvalidInputError


URL = "https://track.wildberries.ru/"


def choose_safe_pack_variant(dims_cm_json: dict, places: int = 1, gap_cm: int = 0) -> int | None:
    l = float(dims_cm_json["l"])
    w = float(dims_cm_json["w"])
    h = float(dims_cm_json["h"])

    a, b, c = sorted([l, w, h])
    x, y = b*places + gap_cm, c + gap_cm

    packs = [
        (1, (16, 25)),  # S
        (2, (25, 40)),  # M
        (3, (45, 65)),  # L
    ]

    for variant, (p1, p2) in packs:
        if (x <= p1 and y <= p2) or (x <= p2 and y <= p1):
            return variant

    return None


def pick_city(driver, city: str, type: int = 1, timeout: int = 30) -> str:
    """
    Выбирает пункт вида '..., {city}' в поле:
      type=1 -> 'Город отправления'
      type=2 -> 'Город доставки'
    Скроллит виртуализированный список до конца. Возвращает полный title выбранного пункта.
    """
    if type == 1:
        label_text = "Город отправления"
    elif type == 2:
        label_text = "Город доставки"
    else:
        raise ValueError("type должен быть 1 (отправления) или 2 (доставки)")

    wait = WebDriverWait(driver, timeout)

    field = wait.until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                f"//div[contains(@class,'main-form-entry')]//div[contains(@class,'input') and contains(@class,'dropdown')][.//div[contains(@class,'input__title')]/span[normalize-space()='{label_text}']]",
            )
        )
    )
    inp = field.find_element(By.CSS_SELECTOR, "input[type='text']")
    inp.click()
    inp.send_keys(Keys.CONTROL, "a")
    inp.send_keys(city)

    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, ".//div[contains(@class,'input__drop-contents')]//div[contains(@class,'drop-menu')]")
        )
    )
    scroll_box = wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, ".//div[contains(@class,'input__drop-contents')]//div[contains(@class,'input__drop-scroll')]")
        )
    )

    wanted = city.strip().casefold()
    end = time.time() + timeout

    last_top = -1
    last_height = -1

    def iter_items():
        """Функция iter_items.

        Возвращает:
            Результат выполнения функции.
        """
        return field.find_elements(By.CSS_SELECTOR, ".drop-menu .drop-menu__item div.package__name")

    while time.time() < end:
        try:
            for name_el in iter_items():
                try:
                    title = (name_el.get_attribute("title") or name_el.text or "").strip()
                    if not title:
                        continue
                    parts = [p.strip() for p in title.split(",")]
                    city_part = parts[1] if len(parts) > 1 else ""
                    if city_part.casefold() == wanted:
                        item = name_el.find_element(By.XPATH, "./ancestor::div[contains(@class,'drop-menu__item')]")
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", item)
                        try:
                            driver.execute_script("arguments[0].click();", item)
                        except ElementClickInterceptedException:
                            driver.execute_script("arguments[0].click();", name_el)
                        return title
                except (NoSuchElementException, StaleElementReferenceException):
                    continue

            top = driver.execute_script("return arguments[0].scrollTop;", scroll_box)
            height = driver.execute_script("return arguments[0].scrollHeight;", scroll_box)
            client = driver.execute_script("return arguments[0].clientHeight;", scroll_box)

            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight;", scroll_box
            )
            time.sleep(0.15)

            new_top = driver.execute_script("return arguments[0].scrollTop;", scroll_box)
            new_height = driver.execute_script("return arguments[0].scrollHeight;", scroll_box)

            no_progress = (new_top == top and new_height == height) or (new_top == last_top and new_height == last_height)
            last_top, last_height = new_top, new_height

            if no_progress and (height - new_top) <= (client + 2):
                break
        except StaleElementReferenceException:
            continue

    try:
        target_name = wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    f".//div[contains(@class,'drop-menu__item')]//div[contains(@class,'package__name')][contains(@title, ', {city}')]",
                )
            )
        )
        target_item = target_name.find_element(By.XPATH, "./ancestor::div[contains(@class,'drop-menu__item')]")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target_item)
        driver.execute_script("arguments[0].click();", target_item)
        return (target_name.get_attribute("title") or target_name.text or "").strip()
    except TimeoutException:
        raise TimeoutException(f"[{label_text}] Не найден пункт дропдауна с городом: '{city}'.")


def pick_parcel_size(driver, variant: int = 1, timeout: int = 15) -> str:
    if variant not in (1, 2, 3):
        raise ValueError("variant должен быть 1, 2 или 3")

    target_text = {1: "Сейф-пакет S", 2: "Сейф-пакет M", 3: "Сейф-пакет L"}[variant]
    wait = WebDriverWait(driver, timeout)

    field = wait.until(
        EC.presence_of_element_located(
            (By.XPATH, "//div[contains(@class,'input') and contains(@class,'dropdown')][.//span[normalize-space()='Размер посылки']]")
        )
    )
    inp = field.find_element(By.CSS_SELECTOR, "input[readonly]")

    def _open_dropdown():
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field)
        except Exception:
            pass
        try:
            field.click()
        except Exception:
            driver.execute_script("arguments[0].click();", field)

        wait.until(lambda d: field.find_element(By.CSS_SELECTOR, ".input__drop").size["height"] >= 0)
        wait.until(lambda d: field.find_element(By.CSS_SELECTOR, ".drop-menu").is_displayed())

    end = time.time() + timeout
    last_exc = None

    while time.time() < end:
        try:
            current = (inp.get_attribute("value") or "").strip()
            if current == target_text:
                return target_text

            _open_dropdown()

            items = field.find_elements(By.CSS_SELECTOR, ".drop-menu__item")
            if not items:
                raise TimeoutException("drop-menu__item not found")

            target_item = None
            for it in items:
                try:
                    name_el = it.find_element(By.CSS_SELECTOR, ".package__name span")
                    if name_el.text.strip() == target_text:
                        target_item = it
                        break
                except Exception:
                    continue

            if target_item is None:
                raise TimeoutException(f"item '{target_text}' not found")

            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target_item)
            try:
                target_item.click()
            except Exception:
                driver.execute_script("arguments[0].click();", target_item)

            WebDriverWait(driver, 3).until(lambda d: (inp.get_attribute("value") or "").strip() == target_text)
            return target_text

        except (TimeoutException, StaleElementReferenceException, ElementClickInterceptedException) as e:
            last_exc = e
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except Exception:
                pass
            time.sleep(0.15)

    raise TimeoutException(f"Не удалось выбрать '{target_text}' в дропдауне 'Размер посылки': {type(last_exc).__name__}")


def submit_and_get_results(driver, timeout: int = 30):
    """
    Нажимает кнопку 'Рассчитать' (id='main-form-submit') и парсит результаты:
      - values: список текстов из .form-result__value
      - labeled: словарь {имя -> значение}, если есть .form-result__name рядом
    Возвращает (values, labeled).
    """
    wait = WebDriverWait(driver, timeout)

    btn = wait.until(EC.element_to_be_clickable((By.ID, "main-form-submit")))
    try:
        btn.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", btn)

    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".form-result__value")))

    end = time.time() + timeout
    values = []
    while time.time() < end:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, ".form-result__value")
            values = [e.text.strip() for e in els if (e.text or "").strip()]
            if values:
                break
        except StaleElementReferenceException:
            pass
        time.sleep(0.1)

    if not values:
        raise TimeoutException("Результаты не загрузились: .form-result__value пусты.")

    labeled = {}
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, ".form-result__row")
        for row in rows:
            try:
                name_el = row.find_element(By.CSS_SELECTOR, ".form-result__name")
                value_el = row.find_element(By.CSS_SELECTOR, ".form-result__value")
                name = (name_el.text or "").strip()
                val = (value_el.text or "").strip()
                if name and val:
                    labeled[name] = val
            except Exception:
                continue
    except Exception:
        pass

    return values, labeled


def wb(from_city, to_city, places, weight_kg, volume_m3, dims_cm_json):
    """Функция wb.

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
    variant = choose_safe_pack_variant(dims_cm_json, places, gap_cm=0)
    if variant is None:
        return None

    chrome_options = Options()

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    try:
        driver.get(URL)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//span[normalize-space()='Город отправления']")))

        pick_city(driver, from_city, type=1, timeout=30)
        pick_city(driver, city=to_city, type=2, timeout=30)
        pick_parcel_size(driver, variant=variant, timeout=15)
        res = submit_and_get_results(driver, timeout=30)

        price_token = res[0][0].split()[1] if res and res[0] and len(res[0]) >= 1 else None
        days_token = res[0][1].split()[1] if res and res[0] and len(res[0]) >= 2 else None
        return price_token, days_token
    finally:
        driver.quit()


class WbAdapter(CarrierAdapter):
    """Класс WbAdapter.

    Инкапсулирует связанную функциональность модуля.
    """
    code = "wb"

    async def calc(self, client: httpx.AsyncClient, p: CalcParams) -> CalcResult:
        """Функция calc.

        Параметры:
            client: Описание параметра.
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

            result = await asyncio.to_thread(
                wb,
                p.from_city,
                p.to_city,
                int(p.places),
                float(p.weight_kg),
                float(p.volume_m3),
                dims,
            )

            if result is None:
                raise InvalidInputError("wb: текущий тариф доступен только для ширины посылки до 35 см")

            if isinstance(result, tuple) and len(result) == 2:
                price_token, days_token = result
            else:
                price_token, days_token = None, None

        except InvalidInputError:
            raise
        except TemporaryError:
            raise
        except Exception as e:
            raise TemporaryError(f"wb: {type(e).__name__}: {e}") from e

        price: Optional[float] = None
        if isinstance(price_token, str):
            digits = re.sub(r"[^\d]", "", price_token)
            if digits:
                try:
                    price = float(digits)
                except Exception:
                    price = None

        days: Optional[int] = None
        if isinstance(days_token, str):
            m = re.search(r"\d+", days_token)
            if m:
                try:
                    days = int(m.group())
                except Exception:
                    days = None

        return CalcResult(
            price=price,
            days=days,
            currency="RUB",
            source=URL,
            name_tarif=None,
            allowances={},
        )


__all__ = ["wb", "WbAdapter", "pick_city", "pick_parcel_size", "submit_and_get_results"]
