
"""Модуль selenium_base.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from typing import Optional, Tuple

from core.contracts import CarrierAdapter, CalcParams, CalcResult, InvalidInputError, TemporaryError

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from webdriver_manager.chrome import ChromeDriverManager
except Exception:
    ChromeDriverManager = None


DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_SELENIUM_WAIT_SECONDS = 20.0
DEFAULT_MAX_BROWSERS = 2

SELENIUM_SEMAPHORE = asyncio.Semaphore(DEFAULT_MAX_BROWSERS)


def _chrome_options(headless: bool = True) -> webdriver.ChromeOptions:
    """Функция _chrome_options.

    Параметры:
        headless: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    opts = webdriver.ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--disable-extensions")

    ua = os.getenv("BROWSER_USER_AGENT")
    if ua:
        opts.add_argument(f"--user-agent={ua}")
    return opts


def _resolve_chromedriver_service() -> ChromeService:
    """Выбирает chromedriver: CHROMEDRIVER -> webdriver_manager -> PATH."""
    env_path = os.getenv("CHROMEDRIVER")
    if env_path and os.path.exists(env_path):
        return ChromeService(executable_path=env_path)

    if ChromeDriverManager is not None:
        return ChromeService(ChromeDriverManager().install())

    return ChromeService()


@contextmanager
def browser(headless: bool = True) -> webdriver.Chrome:
    """Контекстный менеджер ChromeDriver с гарантированным quit()."""
    driver: webdriver.Chrome | None = None
    try:
        driver = webdriver.Chrome(
            service=_resolve_chromedriver_service(),
            options=_chrome_options(headless=headless),
        )
        yield driver
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def wait_visible(
    driver: webdriver.Chrome,
    by: Tuple[str, str],
    timeout: float = DEFAULT_SELENIUM_WAIT_SECONDS,
):
    """Ждёт появления видимого элемента."""
    return WebDriverWait(driver, timeout).until(EC.visibility_of_element_located(by))


def wait_clickable(
    driver: webdriver.Chrome,
    by: Tuple[str, str],
    timeout: float = DEFAULT_SELENIUM_WAIT_SECONDS,
):
    """Ждёт кликабельности элемента."""
    return WebDriverWait(driver, timeout).until(EC.element_to_be_clickable(by))


def safe_click(
    driver: webdriver.Chrome,
    by: Tuple[str, str],
    attempts: int = 3,
    timeout: float = DEFAULT_SELENIUM_WAIT_SECONDS,
) -> None:
    """Пытается кликнуть по элементу с ретраями при transient DOM-ошибках."""
    last_err: Exception | None = None
    for _ in range(attempts):
        try:
            el = wait_clickable(driver, by, timeout=timeout)
            el.click()
            return
        except (StaleElementReferenceException, ElementClickInterceptedException) as e:
            last_err = e
    if last_err:
        raise last_err


def fill_input(
    driver: webdriver.Chrome,
    by: Tuple[str, str],
    value: str,
    clear_first: bool = True,
    timeout: float = DEFAULT_SELENIUM_WAIT_SECONDS,
):
    """Заполняет input, при необходимости предварительно очищая."""
    el = wait_visible(driver, by, timeout=timeout)
    if clear_first:
        el.clear()
    el.send_keys(value)
    return el


class SyncSeleniumAdapter(CarrierAdapter):
    """Базовый класс Selenium-адаптеров с async-обёрткой над sync-логикой."""

    TIMEOUT: float = DEFAULT_TIMEOUT_SECONDS
    HEADLESS: bool = True
    SEMAPHORE: Optional[asyncio.Semaphore] = None

    async def calc(self, client, p: CalcParams) -> CalcResult:
        """Запускает синхронный Selenium в пуле потоков с контролем параллелизма.

        Важный нюанс: asyncio.wait_for НЕ умеет «убивать» поток.
        Поэтому таймауты реализованы так, чтобы:
        - при истечении TIMEOUT мы отдавали TemporaryError;
        - но semaphore НЕ отпускался, пока поток реально не завершится
          (иначе получаем «фантомные» браузеры и лавину параллельности).
        """
        sem = self.SEMAPHORE or SELENIUM_SEMAPHORE

        await sem.acquire()
        task = asyncio.create_task(asyncio.to_thread(self._run_sync_with_mapped_errors, p))

        def _release(_t: asyncio.Task) -> None:
            try:
                sem.release()
            except ValueError:
                # на всякий случай: если release() вызван лишний раз
                pass

        task.add_done_callback(_release)

        try:
            # shield: чтобы timeout/cancel не отменял task (и не отпускал семафор преждевременно)
            return await asyncio.wait_for(asyncio.shield(task), timeout=self.TIMEOUT)
        except asyncio.TimeoutError as e:
            raise TemporaryError(f"{self.code}: timeout {self.TIMEOUT:.0f}s") from e


    def _calc_sync(self, p: CalcParams) -> CalcResult:
        """Синхронная реализация расчёта (реализуется в наследнике)."""
        raise NotImplementedError

    def _run_sync_with_mapped_errors(self, p: CalcParams) -> CalcResult:
        """Оборачивает типовые Selenium-ошибки в TemporaryError/InvalidInputError."""
        try:
            return self._calc_sync(p)
        except InvalidInputError:
            raise
        except TimeoutException as e:
            raise TemporaryError(f"{self.code}: selenium wait timeout") from e
        except NoSuchElementException as e:
            raise TemporaryError(f"{self.code}: element not found") from e
        except (StaleElementReferenceException, ElementClickInterceptedException) as e:
            raise TemporaryError(f"{self.code}: transient dom state") from e
        except WebDriverException as e:
            raise TemporaryError(f"{self.code}: webdriver error") from e
        except TemporaryError:
            raise
        except Exception as e:
            raise TemporaryError(f"{self.code}: unexpected {type(e).__name__}") from e

    @contextmanager
    def new_browser(self):
        """Открывает браузер с учётом HEADLESS, гарантируя закрытие."""
        with browser(headless=self.HEADLESS) as drv:
            # Вешаем базовые таймауты на сам драйвер (это снижает шанс вечных зависаний).
            try:
                drv.set_page_load_timeout(float(self.TIMEOUT))
                drv.set_script_timeout(float(self.TIMEOUT))
            except Exception:
                pass
            yield drv


    @staticmethod
    def by_css(css: str) -> Tuple[str, str]:
        """Удобный конструктор локатора CSS."""
        return (By.CSS_SELECTOR, css)

    @staticmethod
    def by_xpath(xpath: str) -> Tuple[str, str]:
        """Удобный конструктор локатора XPath."""
        return (By.XPATH, xpath)
