"""Sweep capture-supported automatic holdings for leveraged products (#839).

python -m app.scripts.check_leveraged_holdings
"""

from app.core.database import SessionLocal
from app.models.holding import Holding
from app.services.holdings_write import leverage_check_candidates
from app.services.ticker_leverage import detect_leveraged
from app.tasks.admin_tasks import alert_detected_products


def main() -> None:
    with SessionLocal() as session:
        tickers = leverage_check_candidates(
            session, session.query(Holding).all(), ignore_redis=True
        )
    products = detect_leveraged(tickers)
    for p in products:
        print(f"{p.ticker}: {p.long_name} | {p.category} | {p.fund_family}")
    alert_detected_products(products)


if __name__ == "__main__":
    main()
