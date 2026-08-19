#!/usr/bin/env python3
"""
蛇口疫苗库存自动刷新脚本（v2：数据源切换为钉钉表格 axls）

旧版从「钉钉文档(doc) 节点」下载 xlsx 解析；新数据源是「钉钉表格(sheet / axls)」
节点 93NwLYZXWyg4LPvLIG7qlAB5JkyEqBQm，主表「深圳疫苗库存」(sheetId st-7fdbb3db-47926)。
axls 不支持直接下载，改用 `dws sheet range read` 读取单元格，再按 B-C-D-E-F 重算剩余库存。

表结构（深圳疫苗库存）：
  A=疫苗名称  B=库存数量  C=远程客服  D=会员管家  E=商保管家  F=诊所现场
  G=剩余库存(=B-C-D-E-F)  H=备注  I=活性

用法:
    python3 scripts/refresh_stock.py [--commit]

    --commit  自动 git commit & push 到 GitHub（push 绕代理直连）
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── 配置 ───────────────────────────────────────────
REPO_DIR = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_DIR / "stock-data.json"

# 新数据源：钉钉表格（axls）节点 + 主表 sheetId
SHEET_NODE = "93NwLYZXWyg4LPvLIG7qlAB5JkyEqBQm"
SHEET_ID = "st-7fdbb3db-47926"
SHEET_NAME = "深圳疫苗库存"  # 仅用于日志提示

# 表格行名称 → vaccineCatalog ID 映射（单行）
# 注意：同名疫苗可能有多个子行（如乙肝含大连/华北/GSK），单行映射只取第一个匹配；
#       多行汇总在 MULTI_ROW 中处理。
ROW_TO_ID = {
    "五联": "pentavalent",
    "轮状": "rotavirus",
    "手足口": "hfmd",
    "（带状疱疹": "shingles",
    "HPV9": "hpv",
    "23价肺炎": "ppv23",
    "麻腮风": "mmr",
    "乙脑": "je",
    "免费水痘": "varicella_free",
    "结合A+C": "ac_conjugate",
    "自费水痘": "varicella_paid",
    "ACYW135（绿竹，多糖）": "meningococcal_ps",
    "康希诺": "meningococcal",
}

# 需要汇总多行的疫苗（key = catalog ID, value = 汇总时匹配的行名前缀列表）
MULTI_ROW = {
    "hepb": ["儿童乙肝", "成人乙肝"],   # 儿童 + 成人汇总（含 GSK，余量为 0 不影响）
    "pcv13": ["13价肺炎（辉瑞进口）"],   # 只保留辉瑞，不汇总国产玉溪沃森
    "hepa": ["甲肝（国产  北京科兴", "甲肝（国产 艾美行动"],  # 北京科兴 + 艾美行动
    "dtap_dt": ["白破", "百白破"],       # 白破 + 百白破
    "flu": ["华兰生物儿童流感", "儿童流感（三价/巴斯德", "成人流感（三价/巴斯德",
            "华兰生物成人流感", "冻干鼻喷流感", "巴斯德四价流感"],  # 流感系列汇总
}

# 不需要库存记录的疫苗（缺货默认值），仅当未在表中匹配到时填充
NO_STOCK_IDS = {
    "ac_conjugate": {"stock": 0, "stockNote": "绿竹暂时缺货"},
    "varicella_paid": {"stock": 0, "stockNote": "暂时缺货"},
}


def run_dws_sheet_read():
    """通过 dws sheet 读取「深圳疫苗库存」全部数据。

    先 sheet info 取有效数据区域，再 sheet range read 读取。
    """
    print("[1/5] 读取钉钉表格（axls）数据...")

    # 1) 取有效数据区域
    info = subprocess.run(
        ["dws", "sheet", "info", "--node", SHEET_NODE,
         "--sheet-id", SHEET_ID, "--format", "json"],
        capture_output=True, text=True, timeout=60,
    )
    if info.returncode != 0:
        print(f"  ❌ dws sheet info 失败: {info.stderr}")
        sys.exit(1)
    info_text = info.stdout
    info_start = info_text.find("{")
    if info_start == -1:
        print(f"  ❌ 无法解析 sheet info: {info_text[:200]}")
        sys.exit(1)
    info_data = json.loads(info_text[info_start:])
    rng = info_data.get("nonEmptyRange", {}).get("range", "A1:I39")
    print(f"  ✅ 数据区域: {rng}（表：{SHEET_NAME}）")

    # 2) 读取区域
    read = subprocess.run(
        ["dws", "sheet", "range", "read", "--node", SHEET_NODE,
         "--sheet-id", SHEET_ID, "--range", rng, "--format", "json"],
        capture_output=True, text=True, timeout=60,
    )
    if read.returncode != 0:
        print(f"  ❌ dws sheet range read 失败: {read.stderr}")
        sys.exit(1)
    read_text = read.stdout
    read_start = read_text.find("{")
    if read_start == -1:
        print(f"  ❌ 无法解析 sheet range read: {read_text[:200]}")
        sys.exit(1)
    read_data = json.loads(read_text[read_start:])
    rows = read_data.get("values", read_data.get("rows", []))
    if not rows:
        print("  ❌ 未读取到任何行")
        sys.exit(1)
    print(f"  ✅ 读取到 {len(rows)} 行")
    return rows


def parse_and_calc(rows):
    """解析表格，按 B-C-D-E-F 重算剩余库存。"""
    print("[2/5] 解析表格并重算库存...")
    stock_map = {}  # { 行名: { B, C, D, E, F, remaining } }

    for i, row in enumerate(rows):
        if i == 0:
            continue  # 表头
        name = str(row[0]).strip() if len(row) > 0 and row[0] else ""
        if not name or name == "疫苗名称":
            continue
        # 跳过非疫苗说明行
        if name.startswith("另外") or name.startswith("【") or name.startswith("("):
            continue

        def _num(v):
            try:
                return float(v) if v not in (None, "") else 0.0
            except (ValueError, TypeError):
                return 0.0

        B = _num(row[1]) if len(row) > 1 else 0
        C = _num(row[2]) if len(row) > 2 else 0
        D = _num(row[3]) if len(row) > 3 else 0
        E = _num(row[4]) if len(row) > 4 else 0
        F = _num(row[5]) if len(row) > 5 else 0
        remaining = int(B - C - D - E - F)

        stock_map[name] = {
            "B": int(B), "C": int(C), "D": int(D), "E": int(E), "F": int(F),
            "remaining": remaining,
        }
        print(f"  {name[:42]:42s} | 库存={int(B):4d} 剩余={remaining:4d}")

    return stock_map


def map_to_vaccines(stock_map):
    """将表格行映射到 vaccineCatalog ID，处理多行汇总。"""
    print("[3/5] 映射到疫苗 ID...")
    vaccines = {}

    # 1. 单行前缀匹配
    for row_prefix, vid in ROW_TO_ID.items():
        matched = [k for k in stock_map if k.startswith(row_prefix) or row_prefix in k]
        if matched:
            s = stock_map[matched[0]]
            vaccines[vid] = {"stock": s["remaining"]}
            print(f"  {vid:25s} ← '{matched[0][:38]}': {s['remaining']}")
        else:
            print(f"  ⚠️ {vid:25s} ← 未找到匹配行（前缀 '{row_prefix}'）")

    # 2. 多行汇总
    for vid, prefixes in MULTI_ROW.items():
        total = 0
        parts = []
        for prefix in prefixes:
            matched = [k for k in stock_map if k.startswith(prefix)]
            for k in matched:
                s = stock_map[k]
                total += s["remaining"]
                short = k.split("（")[0].strip() if "（" in k else k.strip()
                parts.append(f"{short} {s['remaining']}")
        if total > 0 or vid == "flu":
            vaccines[vid] = {"stock": total}
            if parts:
                if vid == "pcv13":
                    vaccines[vid]["stockNote"] = f"辉瑞 {total} 支"
                elif vid == "hepb":
                    child = sum(stock_map[k]["remaining"] for k in stock_map if "儿童" in k)
                    adult = sum(stock_map[k]["remaining"] for k in stock_map if "成人" in k)
                    vaccines[vid]["stockNote"] = f"儿童 {child} / 成人 {adult}"
                elif vid == "hepa":
                    # 注意：「儿童乙肝（大连艾美诚信）」也含"艾美"，必须同时限定"甲肝"
                    kexing = sum(stock_map[k]["remaining"] for k in stock_map
                                if "甲肝" in k and "科兴" in k)
                    aimei = sum(stock_map[k]["remaining"] for k in stock_map
                                if "甲肝" in k and "艾美" in k)
                    vaccines[vid]["stockNote"] = f"北京科兴 {kexing} / 艾美行动 {aimei}"
                elif vid == "dtap_dt":
                    baibai = sum(stock_map[k]["remaining"] for k in stock_map if "百白破" in k)
                    baipo = sum(stock_map[k]["remaining"] for k in stock_map
                                if ("白破" in k and "百白破" not in k))
                    vaccines[vid]["stockNote"] = f"白破 {baipo} / 百白破 {baibai}"
                elif vid == "flu":
                    vaccines[vid]["stockNote"] = "当前缺货，下批到苗约 9 月"
                else:
                    vaccines[vid]["stockNote"] = " / ".join(parts)
            print(f"  {vid:25s} ← 汇总 {len(prefixes)} 行: {total}")

    # 3. 补全缺货默认值
    for vid, default in NO_STOCK_IDS.items():
        if vid not in vaccines:
            vaccines[vid] = default
            print(f"  {vid:25s} ← 未匹配，使用默认值 {default}")

    return vaccines


def generate_json(vaccines):
    """生成 stock-data.json。"""
    print("[4/5] 生成 stock-data.json...")
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    output = {
        "updated": now,
        "source": "钉钉表格·深圳疫苗库存(axls)",
        "vaccines": vaccines,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"  ✅ 已写入 {OUTPUT_FILE}")
    return output


def git_commit():
    """提交并推送到 GitHub（push 绕代理直连）。"""
    print("[5/5] 提交到 GitHub...")
    os.chdir(REPO_DIR)
    result = subprocess.run(["git", "status", "--porcelain", "stock-data.json"],
                            capture_output=True, text=True)
    if not result.stdout.strip():
        print("  ⚠️ 无变更，跳过提交")
        return
    subprocess.run(["git", "add", "stock-data.json"], check=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    subprocess.run(["git", "commit", "-m", f"🔄 库存刷新 {now}"], check=True)
    # pull --rebase 也绕代理，避免内网代理拦截 GitHub
    subprocess.run(["git", "-c", "http.proxy=", "-c", "https.proxy=",
                    "pull", "--rebase", "origin", "main"], check=True)
    subprocess.run(["git", "-c", "http.proxy=", "-c", "https.proxy=",
                    "push", "origin", "main"], check=True)
    print(f"  ✅ 已提交并推送 ({now})")


def main():
    print("🩺 蛇口疫苗库存自动刷新（axls 数据源）")
    print("=" * 50)

    rows = run_dws_sheet_read()
    stock_map = parse_and_calc(rows)
    vaccines = map_to_vaccines(stock_map)
    generate_json(vaccines)

    if "--commit" in sys.argv:
        git_commit()

    print("=" * 50)
    print("✅ 刷新完成")


if __name__ == "__main__":
    main()
