# 蛇口疫苗库存自动刷新

每日从钉钉「蛇口疫苗库存+问题转交登记」表自动拉取最新库存数据。

## 工作原理

```
┌──────────────┐     ┌──────────────────┐     ┌────────────────┐
│ 钉钉表格      │ ──→ │ refresh_stock.py │ ──→ │ stock-data.json │
│ (每日最新)    │     │ (解析+重算库存)   │     │ (GitHub Pages)  │
└──────────────┘     └──────────────────┘     └────────────────┘
                                                       │
                                              ┌────────▼────────┐
                                              │ vaccine-checker │
                                              │ (自动加载JSON)   │
                                              └─────────────────┘
```

## 文件说明

- `index.html` — 疫苗查询器页面（启动时自动从 stock-data.json 加载库存）
- `stock-data.json` — 库存数据（由 refresh_stock.py 自动更新）
- `scripts/refresh_stock.py` — 库存刷新脚本
- `scripts/com.shekou.vaccine-stock.plist` — macOS launchd 配置（每日 8:00 AM 自动运行）

## 本地刷新

```bash
# 手动刷新一次
python3 scripts/refresh_stock.py

# 刷新并提交到 GitHub
python3 scripts/refresh_stock.py --commit

# 安装 launchd 定时任务（每日 8:00 AM）
cp scripts/com.shekou.vaccine-stock.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.shekou.vaccine-stock.plist
```

## 依赖

```bash
pip install python-calamine
```

## 页面地址

GitHub Pages: https://jingmtan2-Jasmine.github.io/shekou-vaccine-stock/

兼容旧链接（指向同一页面）:
- `https://jingmtan2-Jasmine.github.io/shekou-vaccine-stock/vaccine-checker.html`
- `https://jingmtan2-Jasmine.github.io/shekou-vaccine-stock/index.html`
