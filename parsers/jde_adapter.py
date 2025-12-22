# parsers/jde_adapter.py
from __future__ import annotations
import re

from core.contracts import CalcParams, CalcResult, InvalidInputError, TemporaryError
from core.selenium_base import SyncSeleniumAdapter

# ВАЖНО: импортируем вашу реализацию без правок
# убедитесь, что файл parsers/jde.py существует и содержит функцию jde_calc(...)
from parsers.jde import jde_calc

class JdeAdapter(SyncSeleniumAdapter):
    code = "jde"
    TIMEOUT = 60.0     # общий таймаут одной попытки расчёта
    HEADLESS = True    # можно поставить False для локальной отладки

    def _calc_sync(self, p: CalcParams) -> CalcResult:
        """
        Никакой логики из jde_calc не меняем: просто вызываем,
        затем нормализуем в CalcResult.
        jde_calc -> (price:int, time_text:str)
        """
        try:
            price, time_text = jde_calc(
                p.from_city, p.to_city, int(round(p.weight_kg)), float(p.volume_m3)
            )
        except ValueError as e:
            # В вашем jde_calc ValueError — это, по сути, невалидные входные (неизвестный город)
            raise InvalidInputError(str(e)) from e
        except TemporaryError:
            # если внутри уже кинули TemporaryError — пробрасываем
            raise
        except Exception as e:
            # Любой прочий сбой Selenium/DOM/network трактуем как временный
            raise TemporaryError(f"jde: unexpected {type(e).__name__}") from e

        # Нормализация срока: берём первое целое число, если есть
        days = None
        if isinstance(time_text, str):
            m = re.search(r"\d+", time_text)
            if m:
                try:
                    days = int(m.group())
                except Exception:
                    days = None

        return CalcResult(
            price=float(price) if price is not None else None,
            currency="RUB",
            days=days,
            source="https://www.jde.ru/",
        )
