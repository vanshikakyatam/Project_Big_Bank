
import re
import sqlite3
import time
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt


# ============================================================
# ENTERPRISE DATA RECONCILIATION
# ============================================================

start_time = time.perf_counter()

PROJECT_FOLDER = Path(__file__).parent
DATABASE_PATH = PROJECT_FOLDER / "enterprise_database.db"
LOG_PATH = PROJECT_FOLDER / "server_logs (1).txt"
EXCHANGE_PATH = PROJECT_FOLDER / "daily_exchange_rates.csv"
FIGURE_FOLDER = PROJECT_FOLDER / "figure"

FIGURE_FOLDER.mkdir(exist_ok=True)


# ============================================================
# PHASE 1: IDENTIFY USERS WITH THEIR LATEST ACTIVE STATUS
# ============================================================

query = """
SELECT user_id, name, status, updated_at
FROM (
    SELECT
        user_id,
        name,
        status,
        updated_at,
        ROW_NUMBER() OVER (
            PARTITION BY user_id
            ORDER BY updated_at DESC
        ) AS row_num
    FROM users
)
WHERE row_num = 1
    AND status = 'Active';
"""

with sqlite3.connect(DATABASE_PATH) as connection:
    df_active_users = pd.read_sql_query(query, connection)

print("PHASE 1: ACTIVE USERS")
print("--------------------")
print(f"Active users: {len(df_active_users):,}")


# ============================================================
# PHASE 2: EXTRACT VALID TRANSACTIONS FROM SERVER LOGS
# ============================================================

patterns = {
    "Date": re.compile(r"\[(\d{4}-\d{2}-\d{2})"),
    "User ID": re.compile(r'""u"":""(U-\d+)""'),
    "Product ID": re.compile(r'""item_code"":""(PRD-\d+)""'),
    "Euro Value": re.compile(r'""eur_val"":([\d.]+)')
}

transactions = []
error_count = 0
unmatched_count = 0

with open(LOG_PATH, "r", encoding="utf-8") as file:
    next(file, None)  # Skip the header

    for line in file:
        if "ERROR" in line:
            error_count += 1
            continue

        matches = {
            column: pattern.search(line)
            for column, pattern in patterns.items()
        }

        if all(matches.values()):
            transactions.append({
                "Date": matches["Date"].group(1),
                "User ID": matches["User ID"].group(1),
                "Product ID": matches["Product ID"].group(1),
                "Euro Value": float(
                    matches["Euro Value"].group(1)
                )
            })
        else:
            unmatched_count += 1

extracted_data = pd.DataFrame(
    transactions,
    columns=["Date", "User ID", "Product ID", "Euro Value"]
)

if extracted_data.empty:
    raise ValueError("No valid transactions were extracted.")

extracted_data["Date"] = pd.to_datetime(extracted_data["Date"])

print("\nPHASE 2: TRANSACTION EXTRACTION")
print("-------------------------------")
print(f"Valid transactions: {len(extracted_data):,}")
print(f"ERROR entries skipped: {error_count:,}")
print(f"Unmatched entries: {unmatched_count:,}")


# ============================================================
# PHASE 3: CLEAN EXCHANGE RATES AND CALCULATE USD REVENUE
# ============================================================

exchange_rates = pd.read_csv(EXCHANGE_PATH)

exchange_rates["Date"] = pd.to_datetime(exchange_rates["Date"])
exchange_rates = exchange_rates.sort_values("Date").copy()

duplicate_rate_dates = exchange_rates["Date"].duplicated().sum()

if duplicate_rate_dates:
    raise ValueError(
        f"Exchange-rate file contains {duplicate_rate_dates} "
        "duplicate dates. Resolve them before reconciliation."
    )

exchange_rates["EUR_to_USD"] = (
    exchange_rates["EUR_to_USD"].ffill()
)

if exchange_rates["EUR_to_USD"].isna().any():
    raise ValueError(
        "Exchange rates still contain missing values after forward-fill."
    )

df_with_rates = extracted_data.merge(
    exchange_rates[["Date", "EUR_to_USD"]],
    on="Date",
    how="left",
    validate="many_to_one"
)

if df_with_rates["EUR_to_USD"].isna().any():
    raise ValueError(
        "Some transactions do not have a matching exchange rate."
    )

df_with_rates["USD_Revenue"] = (
    df_with_rates["Euro Value"] * df_with_rates["EUR_to_USD"]
)

print("\nPHASE 3: EXCHANGE-RATE RECONCILIATION")
print("-------------------------------------")
print(f"Exchange-rate records: {len(exchange_rates):,}")
print(
    "Missing rates after matching:",
    df_with_rates["EUR_to_USD"].isna().sum()
)


# ============================================================
# PHASE 4: RECONCILE TRANSACTIONS AGAINST ACTIVE USERS
# ============================================================

df_final = df_with_rates.merge(
    df_active_users[["user_id", "name"]],
    left_on="User ID",
    right_on="user_id",
    how="inner",
    validate="many_to_one"
)

total_revenue_usd = df_final["USD_Revenue"].sum()

print("\nPHASE 4: FINAL RECONCILIATION")
print("-----------------------------")
print(f"Valid transactions: {len(extracted_data):,}")
print(f"Transactions with exchange rates: {len(df_with_rates):,}")
print(f"Transactions for active users: {len(df_final):,}")
print(f"Unique active customers: {df_final['user_id'].nunique():,}")
print(f"Total active-user revenue: ${total_revenue_usd:,.2f}")


# ============================================================
# PHASE 5: MONTHLY REVENUE AND TOP FIVE CUSTOMERS
# ============================================================

df_final["Month"] = df_final["Date"].dt.to_period("M")

monthly_revenue = (
    df_final.groupby("Month", as_index=False)["USD_Revenue"]
    .sum()
    .sort_values("Month")
)

monthly_revenue["Month"] = monthly_revenue["Month"].astype(str)

top_customers = (
    df_final.groupby(["user_id", "name"], as_index=False)["USD_Revenue"]
    .sum()
    .sort_values("USD_Revenue", ascending=False)
    .head(5)
)

print("\nMONTHLY USD REVENUE")
print("-------------------")
print(monthly_revenue.to_string(index=False))

print("\nTOP FIVE CUSTOMERS")
print("------------------")
print(
    top_customers.to_string(
        index=False,
        formatters={"USD_Revenue": "${:,.2f}".format}
    )
)


# ============================================================
# PHASE 6: SAVE CHARTS AND BUILD THE DASHBOARD
# ============================================================

# Monthly revenue chart
fig, ax = plt.subplots(figsize=(10, 5))
ax.bar(monthly_revenue["Month"], monthly_revenue["USD_Revenue"])
ax.set_title("Monthly USD Revenue from Active Users")
ax.set_xlabel("Month")
ax.set_ylabel("Revenue (USD)")
ax.tick_params(axis="x", rotation=45)
fig.tight_layout()
fig.savefig(
    FIGURE_FOLDER / "monthly_revenue.png",
    dpi=300,
    bbox_inches="tight"
)
plt.close(fig)

# Top five customers chart
fig, ax = plt.subplots(figsize=(8, 5))
ax.bar(top_customers["user_id"], top_customers["USD_Revenue"])
ax.set_title("Top 5 Most Valuable Customers")
ax.set_xlabel("Customer ID")
ax.set_ylabel("Revenue (USD)")
ax.tick_params(axis="x", rotation=45)
fig.tight_layout()
fig.savefig(
    FIGURE_FOLDER / "top_5_customers.png",
    dpi=300,
    bbox_inches="tight"
)
plt.close(fig)

# Combined dashboard
fig, axes = plt.subplots(1, 2, figsize=(16, 6))

axes[0].bar(
    monthly_revenue["Month"],
    monthly_revenue["USD_Revenue"]
)
axes[0].set_title("Monthly USD Revenue")
axes[0].set_xlabel("Month")
axes[0].set_ylabel("Revenue (USD)")
axes[0].tick_params(axis="x", rotation=45)

axes[1].bar(
    top_customers["user_id"],
    top_customers["USD_Revenue"]
)
axes[1].set_title("Top 5 Most Valuable Customers")
axes[1].set_xlabel("Customer ID")
axes[1].set_ylabel("Revenue (USD)")
axes[1].tick_params(axis="x", rotation=45)

fig.tight_layout()
fig.savefig(
    FIGURE_FOLDER / "reconciliation_dashboard.png",
    dpi=300,
    bbox_inches="tight"
)


# ============================================================
# FINAL VALIDATION
# ============================================================

print("\nFINAL VALIDATION")
print("----------------")
print(f"Valid transactions: {len(extracted_data):,}")
print(f"Active-user transactions: {len(df_final):,}")
print(f"Monthly revenue records: {len(monthly_revenue):,}")
print(f"Top customers returned: {len(top_customers):,}")
print(
    "Missing exchange rates:",
    df_with_rates["EUR_to_USD"].isna().sum()
)
print(
    "Missing USD revenue:",
    df_final["USD_Revenue"].isna().sum()
)
print(
    "Duplicate exchange-rate dates:",
    exchange_rates["Date"].duplicated().sum()
)

assert df_active_users["user_id"].is_unique
assert len(df_with_rates) == len(extracted_data)
assert df_final["USD_Revenue"].notna().all()
assert df_with_rates["EUR_to_USD"].notna().all()
assert len(top_customers) == min(5, df_final["user_id"].nunique())

print("\nAll validation checks passed.")
print(f"Charts saved in: {FIGURE_FOLDER}")
print(f"Total runtime: {time.perf_counter() - start_time:.2f} seconds")

plt.show()
