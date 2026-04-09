#!/bin/bash
DB="/home/cleblanc/projects/wethr/data/wethr.db"
OUT="/home/cleblanc/n8n-wethr/wethr-output/settled_trades.json"

RESULT=$(sqlite3 -cmd ".timeout 5000" -json "$DB" <<'EOF'
SELECT 
    t.id as trade_id,
    t.city,
    t.target_date,
    t.bracket_label,
    t.bracket_lower,
    t.bracket_upper,
    t.bracket_unit,
    t.side,
    t.entry_price,
    t.size_usd,
    t.pnl,
    t.model_prob,
    t.market_prob,
    t.edge,
    t.outcome,
    t.settled_at,
    t.market_volume
FROM trades t
WHERE t.settled = 1
  AND t.settled_at > datetime('now', '-1 days')
ORDER BY t.target_date, t.city;
EOF
)

if [ -z "$RESULT" ]; then
  echo "[]" > "$OUT"
else
  echo "$RESULT" > "$OUT"
fi

echo "$(date -Iseconds) Exported $(jq length "$OUT") trades to $OUT"
