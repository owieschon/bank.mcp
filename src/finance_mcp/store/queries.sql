-- queries.sql — analytical read-models over the canonical transactions store.
--
-- These are the *descriptive reporting* rollups (what the report shows): monthly
-- cash flow, category mix, top merchants. They are intentionally expressed in SQL
-- rather than Python because they are set-based aggregations over a relational
-- store — the right tool for the job. (The *forecasting* and recurring-cadence
-- logic is algorithmic and lives in tested Python; it is deliberately NOT here.)
--
-- Schema notes the queries rely on (see db.py):
--   transactions(date TEXT 'YYYY-MM-DD', amount REAL  -- magnitude, always >= 0,
--                direction TEXT 'debit'|'credit', owner TEXT, merchant_name TEXT,
--                description TEXT, category_raw TEXT)
-- `amount` is an unsigned magnitude; `direction` carries the sign, so spend is
-- `direction='debit'` and income is `direction='credit'`.
--
-- Every query takes a :owner bind (pass NULL for "all owners"). Requires SQLite
-- >= 3.25 for window functions.

-- name: monthly_cashflow
-- Per-calendar-month income, spend, and net, plus a running cumulative net and the
-- month-over-month change in net. The running total answers "where did the balance
-- trend?" and the MoM delta answers "is it getting better or worse?" — both are
-- window functions over the month series, which is exactly what windows are for.
WITH monthly AS (
    SELECT
        substr(date, 1, 7)                                            AS month,  -- 'YYYY-MM' bucket
        SUM(CASE WHEN direction = 'credit' THEN amount ELSE 0 END)    AS income,
        SUM(CASE WHEN direction = 'debit'  THEN amount ELSE 0 END)    AS spend
    FROM transactions
    WHERE date <> ''
      AND (:owner IS NULL OR owner = :owner)
    GROUP BY month
)
SELECT
    month,
    ROUND(income, 2)                                                  AS income,
    ROUND(spend, 2)                                                   AS spend,
    ROUND(income - spend, 2)                                          AS net,
    -- cumulative net to date: a classic running total
    ROUND(SUM(income - spend) OVER (ORDER BY month
              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 2)   AS running_net,
    -- change in net vs the prior month (NULL for the first month)
    ROUND((income - spend) - LAG(income - spend) OVER (ORDER BY month), 2)
                                                                      AS net_mom_change
FROM monthly
ORDER BY month;

-- name: category_breakdown
-- Spend grouped by category, with each category's share of total spend. The
-- ratio-to-total is computed with an un-partitioned window (SUM(...) OVER ()), so
-- the per-row total and the grand total come from one pass instead of a subquery.
WITH spend AS (
    SELECT
        COALESCE(NULLIF(category_raw, ''), 'UNCATEGORIZED')           AS category,
        SUM(amount)                                                   AS total,
        COUNT(*)                                                      AS txns
    FROM transactions
    WHERE direction = 'debit'
      AND (:owner IS NULL OR owner = :owner)
    GROUP BY category
)
SELECT
    category,
    ROUND(total, 2)                                                   AS total,
    txns,
    ROUND(100.0 * total / SUM(total) OVER (), 1)                      AS pct_of_spend
FROM spend
ORDER BY total DESC;

-- name: top_merchants
-- The biggest spend destinations, ranked. RANK() over the spend ordering gives a
-- stable position even when two merchants tie, which a plain ORDER BY + row number
-- would not. :limit caps the list for the report.
WITH by_merchant AS (
    SELECT
        COALESCE(NULLIF(merchant_name, ''), description, '(unknown)') AS merchant,
        SUM(amount)                                                   AS total,
        COUNT(*)                                                      AS txns
    FROM transactions
    WHERE direction = 'debit'
      AND (:owner IS NULL OR owner = :owner)
    GROUP BY merchant
)
SELECT
    RANK() OVER (ORDER BY total DESC)                                 AS rank,
    merchant,
    ROUND(total, 2)                                                   AS total,
    txns
FROM by_merchant
ORDER BY total DESC
LIMIT :limit;
