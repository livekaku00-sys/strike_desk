from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import Optional, Set

from models.instrument import Exchange

logger = logging.getLogger(__name__)

# Indian Standard Time (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))


class MarketSession:
    """
    NSE/BSE Indian equities and derivatives market session validator.
    Trading hours: 09:15 to 15:30 IST, Monday to Friday.
    """

    OPEN_TIME = time(9, 15)
    CLOSE_TIME = time(15, 30)

    # Standard exchange holidays (YYYY-MM-DD)
    DEFAULT_HOLIDAYS_2026: Set[date] = {
        date(2026, 1, 26),  # Republic Day
        date(2026, 3, 6),   # Mahashivratri
        date(2026, 3, 25),  # Holi
        date(2026, 4, 3),   # Good Friday
        date(2026, 4, 14),  # Dr. Ambedkar Jayanti
        date(2026, 5, 1),   # Maharashtra Day
        date(2026, 8, 15),  # Independence Day
        date(2026, 10, 2),  # Mahatma Gandhi Jayanti
        date(2026, 10, 20), # Dussehra
        date(2026, 11, 10), # Diwali Laxmi Pujan
        date(2026, 12, 25), # Christmas
    }

    def __init__(
        self,
        holidays: Optional[Set[date]] = None,
        bypass_market_hours: bool = False,
    ) -> None:
        self.holidays = holidays if holidays is not None else set(self.DEFAULT_HOLIDAYS_2026)
        self.bypass_market_hours = bypass_market_hours

    def is_market_open(
        self,
        exchange: Exchange = Exchange.NSE,
        timestamp: Optional[datetime] = None,
    ) -> bool:
        """
        Determines whether the exchange is currently open for trading.
        """
        if self.bypass_market_hours:
            return True

        if timestamp is None:
            ts = datetime.now(timezone.utc)
        else:
            ts = timestamp

        # Convert to IST
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ist_time = ts.astimezone(IST)

        # Check weekend: Monday is 0, Sunday is 6
        if ist_time.weekday() >= 5:
            return False

        # Check holiday
        if ist_time.date() in self.holidays:
            return False

        # Check hours: 09:15 to 15:30 IST
        t = ist_time.time()
        return self.OPEN_TIME <= t <= self.CLOSE_TIME

    def get_next_market_open(
        self,
        exchange: Exchange = Exchange.NSE,
        from_time: Optional[datetime] = None,
    ) -> datetime:
        """
        Returns the next market opening datetime in UTC.
        """
        if from_time is None:
            ts = datetime.now(timezone.utc)
        else:
            ts = from_time
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        ist_now = ts.astimezone(IST)
        candidate_date = ist_now.date()

        # If today is open and before 09:15, open is today at 09:15
        if (
            ist_now.weekday() < 5
            and candidate_date not in self.holidays
            and ist_now.time() < self.OPEN_TIME
        ):
            open_ist = datetime.combine(candidate_date, self.OPEN_TIME, tzinfo=IST)
            return open_ist.astimezone(timezone.utc)

        # Otherwise look forward for next trading day
        candidate_date += timedelta(days=1)
        while candidate_date.weekday() >= 5 or candidate_date in self.holidays:
            candidate_date += timedelta(days=1)

        open_ist = datetime.combine(candidate_date, self.OPEN_TIME, tzinfo=IST)
        return open_ist.astimezone(timezone.utc)

    def get_next_market_close(
        self,
        exchange: Exchange = Exchange.NSE,
        from_time: Optional[datetime] = None,
    ) -> datetime:
        """
        Returns the next market close datetime in UTC.
        """
        if from_time is None:
            ts = datetime.now(timezone.utc)
        else:
            ts = from_time
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        ist_now = ts.astimezone(IST)
        candidate_date = ist_now.date()

        # If market is currently open today, close is today at 15:30
        if (
            ist_now.weekday() < 5
            and candidate_date not in self.holidays
            and ist_now.time() < self.CLOSE_TIME
        ):
            close_ist = datetime.combine(candidate_date, self.CLOSE_TIME, tzinfo=IST)
            return close_ist.astimezone(timezone.utc)

        next_open_utc = self.get_next_market_open(exchange, from_time=ts)
        next_open_ist = next_open_utc.astimezone(IST)
        close_ist = datetime.combine(next_open_ist.date(), self.CLOSE_TIME, tzinfo=IST)
        return close_ist.astimezone(timezone.utc)


market_session = MarketSession()
