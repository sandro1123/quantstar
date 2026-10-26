"""回补 factor_scores 历史数据 — 为 ML 训练准备 67 个 z-score 因子"""
import json
import time
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from common.database import Database
from data_layer import DataLayer
from strategy_layer import StrategyLayer
from strategy_layer.aggregation.aggregator import Aggregator, DEFAULT_WEIGHTS

BATCH_SIZE = 5  # 每批处理天数
STOCKS_PER_BATCH = 500
TARGET_DATES = 100  # 处理 100 天 ≈ 50K 行


def process_date_batch(dates):
    db = Database("data/qs.db")
    dl = DataLayer(db)
    sl = StrategyLayer(data_layer=dl, mode="backtest")
    agg = Aggregator()

    codes = sl._get_stock_codes(dates[-1])[:STOCKS_PER_BATCH]
    conn = db.get_connection()
    total = 0
    for date in dates:
        matrix = sl.factor_engine.compute_all(codes, date)
        factor_daily = agg.aggregate(matrix, weights=DEFAULT_WEIGHTS)
        for fd in factor_daily:
            conn.execute(
                "UPDATE factor_daily SET factor_scores = ? WHERE code = ? AND date = ?",
                (json.dumps(fd.factor_scores), fd.code, fd.date),
            )
            total += 1
        conn.commit()
    return total


def main():
    db = Database("data/qs.db")
    conn = db.get_connection()

    # 获取所有需要回补的日期
    rows = conn.execute(
        "SELECT DISTINCT date FROM factor_daily WHERE fwd_return_20d IS NOT NULL AND factor_scores IS NULL ORDER BY date"
    ).fetchall()
    all_dates = [r[0] for r in rows]
    total_dates = len(all_dates)
    print(f"待回补日期: {total_dates} 个 ({all_dates[0]} ~ {all_dates[-1]})")

    # 取最后 TARGET_DATES 天
    target_dates = all_dates[-TARGET_DATES:]
    print(f"目标: 处理最后 {len(target_dates)} 天 ({target_dates[0]} ~ {target_dates[-1]})")

    # 分批处理
    batches = [target_dates[i : i + BATCH_SIZE] for i in range(0, len(target_dates), BATCH_SIZE)]
    print(f"分批: {len(batches)} 批, 每批 {BATCH_SIZE} 天")

    t0 = time.time()
    total_rows = 0
    for i, batch in enumerate(batches):
        bt = time.time()
        n = process_date_batch(batch)
        total_rows += n
        elapsed = time.time() - bt
        rate = n / elapsed
        eta = (len(batches) - i - 1) * elapsed
        print(f"  [{i+1}/{len(batches)}] {batch[0]}~{batch[-1]}: {n} 行, {elapsed:.0f}s, {rate:.0f}行/秒, 预计剩余 {eta/60:.0f}分")
        sys.stdout.flush()

    total_time = time.time() - t0
    print(f"\n完成! 总计 {total_rows} 行, {total_time/60:.1f} 分钟")

    # 验证
    n = conn.execute("SELECT COUNT(*) FROM factor_daily WHERE factor_scores IS NOT NULL AND fwd_return_20d IS NOT NULL").fetchone()[0]
    print(f"可用训练数据: {n} 行")


if __name__ == "__main__":
    main()