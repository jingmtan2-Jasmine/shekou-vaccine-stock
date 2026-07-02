#!/bin/bash
# 蛇口疫苗库存自动刷新
# 由 launchd 每日 10:00 触发
set -e
cd /Users/jasminetan/WorkBuddy/2026-06-16-17-32-41/shekou-vaccine-stock
/usr/bin/python3 scripts/refresh_stock.py --commit >> /tmp/vaccine-stock-refresh.log 2>&1
echo "$(date '+%Y-%m-%d %H:%M:%S') 刷新完成" >> /tmp/vaccine-stock-refresh.log
