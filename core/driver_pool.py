import asyncio
import logging
from typing import Callable, Dict, Set

from selenium.webdriver.remote.webdriver import WebDriver


logger = logging.getLogger(__name__)


class DriverPool:
    def __init__(self, size: int, driver_factory: Callable[[], WebDriver], recycle_every: int = 30) -> None:
        if size <= 0:
            raise ValueError("size must be > 0")
        if recycle_every <= 0:
            raise ValueError("recycle_every must be > 0")

        self._size = size
        self._driver_factory = driver_factory
        self._recycle_every = recycle_every

        self._queue: asyncio.Queue[WebDriver] = asyncio.Queue(maxsize=size)
        self._create_lock = asyncio.Lock()

        self._created = 0
        self._use_count: Dict[WebDriver, int] = {}
        self._all_drivers: Set[WebDriver] = set()
        self._closed = False

    async def acquire(self) -> WebDriver:
        if self._closed:
            raise RuntimeError("DriverPool is closed")

        driver = self._try_get_nowait()
        if driver is not None:
            logger.debug("driver_pool acquire: reuse driver=%s", id(driver))
            return driver

        async with self._create_lock:
            if self._closed:
                raise RuntimeError("DriverPool is closed")

            if self._created < self._size:
                driver = await self._create_driver()
                logger.info("driver_pool acquire: created driver=%s total=%s", id(driver), self._created)
                return driver

        driver = await self._queue.get()
        if self._closed:
            # pool закрывается пока ждет, лучшее что я успел придумать
            self._safe_quit(driver)
            raise RuntimeError("DriverPool is closed")

        logger.debug("driver_pool acquire: waited reuse driver=%s", id(driver))
        return driver

    def release(self, driver: WebDriver, *, broken: bool = False) -> None:
        if driver is None:
            return

        if driver not in self._all_drivers:
            logger.warning("driver_pool release: unknown driver=%s", id(driver))
            self._safe_quit(driver)
            return

        if self._closed:
            logger.info("driver_pool release: pool closed, quitting driver=%s", id(driver))
            self._remove_driver(driver)
            self._safe_quit(driver)
            return

        if broken:
            logger.warning("driver_pool release: broken driver=%s", id(driver))
            self._remove_driver(driver)
            self._safe_quit(driver)
            return

        uses = self._use_count.get(driver, 0) + 1
        self._use_count[driver] = uses
        if uses >= self._recycle_every:
            logger.info(
                "driver_pool release: recycle driver=%s uses=%s", id(driver), uses
            )
            self._remove_driver(driver)
            self._safe_quit(driver)
            return

        try:
            self._queue.put_nowait(driver)
        except asyncio.QueueFull:
            logger.warning("driver_pool release: queue full, quitting driver=%s", id(driver))
            self._remove_driver(driver)
            self._safe_quit(driver)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        async with self._create_lock:
            drained: list[WebDriver] = []
            while True:
                try:
                    drained.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            for driver in drained:
                self._remove_driver(driver)
                self._safe_quit(driver)

            # Best-effort: quit any drivers still tracked (may be in-use)
            for driver in list(self._all_drivers):
                self._remove_driver(driver)
                self._safe_quit(driver)

        logger.info("driver_pool close: completed")

    def _try_get_nowait(self) -> WebDriver | None:
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def _create_driver(self) -> WebDriver:
        driver = await asyncio.to_thread(self._driver_factory)
        self._created += 1
        self._all_drivers.add(driver)
        self._use_count[driver] = 0
        return driver

    def _remove_driver(self, driver: WebDriver) -> None:
        if driver in self._all_drivers:
            self._all_drivers.discard(driver)
            self._use_count.pop(driver, None)
            self._created = max(0, self._created - 1)

    def _safe_quit(self, driver: WebDriver) -> None:
        try:
            driver.quit()
        except Exception:
            logger.exception("driver_pool quit failed for driver=%s", id(driver))