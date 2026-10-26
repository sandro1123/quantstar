"""Report Calendar — 报告日历"""

from common.calendar import TradingCalendar


class ReportCalendar(TradingCalendar):
    def get_report_schedule(self, date: str) -> list[str]:
        reports = []
        if self.is_trading_day(date):
            reports.extend(["morning_brief", "midday_brief", "evening_report"])
        return reports
