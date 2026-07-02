#!/usr/bin/env python3
"""
蛇口疫苗库存自动刷新脚本

通过 dws CLI 下载钉钉表格，解析并重新计算库存（B-C-D-E-F），
输出 stock-data.json。

用法:
    python3 scripts/refresh_stock.py [--commit]
    
    --commit  自动 git commit & push 到 GitHub
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── 配置 ───────────────────────────────────────────
REPO_DIR = Path(__file__).resolve().parent.parent
DENTRY_UUID = "jQPRqwxd3NLWjdeA535zJYK6lrGM4795"
SHEET_NAME = "同步在线编辑疫苗数"
OUTPUT_FILE = REPO_DIR / "stock-data.json"

# 表格行名称 → vaccineCatalog ID 映射
# 注意：同一种疫苗在表中可能有多行（如儿童乙肝 + 成人乙肝），需汇总
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
    "ACYW135（康希诺生物，结合）": "meningococcal",
}

# 需要汇总多行的疫苗（key = catalog ID, value = 汇总后 stockNote 格式）
MULTI_ROW = {
    "hepb": ["儿童乙肝", "成人乙肝"],   # 儿童 + 成人汇总
    "pcv13": ["13价肺炎（辉瑞进口）", "13价肺炎（国产玉溪沃森）"],
    "hepa": ["甲肝（国产  北京科兴", "甲肝（国产 艾美行动"],
    "dtap_dt": ["白破", "百白破"],
    "flu": ["华兰生物儿童流感", "儿童流感（三价/巴斯德", "成人流感（三价/巴斯德",
            "华兰生物成人流感", "冻干鼻喷流感", "巴斯德四价流感"],
}


def run_dws_download():
    """运行 dws doc download 获取 CDN 下载链接。"""
    print("[1/5] 运行 dws doc download...")
    result = subprocess.run(
        ["dws", "doc", "download", "--node", DENTRY_UUID, "-f", "json", "-y"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"  ❌ dws 失败: {result.stderr}")
        sys.exit(1)

    # 解析 JSON（dws 输出可能包含日志前缀）
    text = result.stdout
    start = text.find("{")
    if start == -1:
        print(f"  ❌ 无法解析 dws 输出: {text[:200]}")
        sys.exit(1)

    data = json.loads(text[start:])
    if not data.get("success"):
        print(f"  ❌ dws 返回错误: {data}")
        sys.exit(1)

    resource_url = data.get("resourceUrl", "")
    if not resource_url:
        print("  ❌ 未找到 resourceUrl")
        sys.exit(1)

    print(f"  ✅ 获取到下载链接")
    return resource_url


def download_xlsx(url):
    """下载 xlsx 文件到临时目录。"""
    print("[2/5] 下载 xlsx 文件...")
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    try:
        urllib.request.urlretrieve(url, tmp.name)
        size = os.path.getsize(tmp.name)
        print(f"  ✅ 已下载 {size:,} bytes")
        return tmp.name
    except Exception as e:
        print(f"  ❌ 下载失败: {e}")
        sys.exit(1)


def parse_and_calc(xlsx_path):
    """解析 xlsx，按 B-C-D-E-F 重算库存。"""
    print("[3/5] 解析表格并重算库存...")
    try:
        from python_calamine import CalamineWorkbook
    except ImportError:
        print("  ❌ 请先安装 python-calamine: pip install python-calamine")
        sys.exit(1)

    wb = CalamineWorkbook.from_path(xlsx_path)
    sheet = wb.get_sheet_by_name(SHEET_NAME)
    rows = sheet.to_python()

    # 按行名汇总库存
    stock_map = {}  # { 行名前缀: { B, C, D, E, F } }
    for i, row in enumerate(rows):
        if i == 0:
            continue
        name = str(row[0]).strip() if row[0] else ""
        if not name or name.startswith("另外") or name.startswith("【") or name == "A":
            continue
        try:
            B = float(row[1]) if len(row) > 1 and row[1] != "" else 0
            C = float(row[2]) if len(row) > 2 and row[2] != "" else 0
            D = float(row[3]) if len(row) > 3 and row[3] != "" else 0
            E = float(row[4]) if len(row) > 4 and row[4] != "" else 0
            F = float(row[5]) if len(row) > 5 and row[5] != "" else 0
        except (ValueError, TypeError):
            continue

        remaining = int(B - C - D - E - F)
        stock_map[name] = {
            "B": int(B), "C": int(C), "D": int(D), "E": int(E), "F": int(F),
            "remaining": remaining,
        }
        print(f"  {name[:45]:45s} | 库存={int(B):4d} 剩余={remaining:4d}")

    return stock_map


def map_to_vaccines(stock_map):
    """将表格行映射到 vaccineCatalog ID，处理多行汇总。"""
    print("[4/5] 映射到疫苗 ID...")
    vaccines = {}

    # 1. 前缀匹配（表行名包含厂家/价格后缀，用前缀匹配）
    for row_prefix, vid in ROW_TO_ID.items():
        matched = [k for k in stock_map if k.startswith(row_prefix)]
        if matched:
            # 取第一个匹配（通常只有一个）
            s = stock_map[matched[0]]
            vaccines[vid] = {"stock": s["remaining"]}
            print(f"  {vid:25s} ← '{matched[0][:40]}': {s['remaining']}")
        else:
            print(f"  ⚠️ {vid:25s} ← 未找到以 '{row_prefix}' 开头的行")

    # 2. 多行汇总
    for vid, prefixes in MULTI_ROW.items():
        total = 0
        parts = []
        for prefix in prefixes:
            matched = [k for k in stock_map if k.startswith(prefix)]
            for k in matched:
                s = stock_map[k]
                total += s["remaining"]
                # 提取简称
                short = k.split("（")[0].strip() if "（" in k else k.strip()
                parts.append(f"{short} {s['remaining']}")
        if total > 0 or vid == "flu":  # flu 汇总即使为0也记录
            vaccines[vid] = {"stock": total}
            if parts:
                # 生成 stockNote
                if vid == "pcv13":
                    pfizer = sum(stock_map[k]["remaining"] for k in stock_map if "辉瑞" in k)
                    wasen = sum(stock_map[k]["remaining"] for k in stock_map if "沃森" in k)
                    vaccines[vid]["stockNote"] = f"辉瑞 {pfizer} / 沃森 {wasen}"
                elif vid == "hepb":
                    child = sum(stock_map[k]["remaining"] for k in stock_map if "儿童" in k)
                    adult = sum(stock_map[k]["remaining"] for k in stock_map if "成人" in k)
                    vaccines[vid]["stockNote"] = f"儿童 {child} / 成人 {adult}"
                elif vid == "hepa":
                    kexing = sum(stock_map[k]["remaining"] for k in stock_map if "科兴" in k)
                    aimei = sum(stock_map[k]["remaining"] for k in stock_map if "艾美" in k)
                    vaccines[vid]["stockNote"] = f"北京科兴 {kexing} / 艾美行动 {aimei}"
                elif vid == "dtap_dt":
                    baipo = sum(stock_map[k]["remaining"] for k in stock_map if "白破" in k)
                    baibai = sum(stock_map[k]["remaining"] for k in stock_map if "百白破" in k)
                    vaccines[vid]["stockNote"] = f"白破 {baipo} / 百白破 {baibai}"
                elif vid == "flu":
                    vaccines[vid]["stockNote"] = "当前缺货，下批到苗约 9 月"
                else:
                    vaccines[vid]["stockNote"] = " / ".join(parts)
            print(f"  {vid:25s} ← 汇总 {len(prefixes)} 行: {total}")

    # 3. 补全不需要库存记录的疫苗（免费、缺货等）
    NO_STOCK_IDS = {
        "ac_conjugate": {"stock": 0, "stockNote": "绿竹暂时缺货"},
        "varicella_paid": {"stock": 0, "stockNote": "暂时缺货"},
    }
    for vid, default in NO_STOCK_IDS.items():
        if vid not in vaccines:
            vaccines[vid] = default

    return vaccines


def generate_json(vaccines):
    """生成 stock-data.json。"""
    print("[5/5] 生成 stock-data.json...")
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()

    output = {
        "updated": now,
        "source": "钉钉蛇口疫苗库存表",
        "vaccines": vaccines,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"  ✅ 已写入 {OUTPUT_FILE}")
    return output


def git_commit():
    """提交并推送到 GitHub。"""
    print("[可选] 提交到 GitHub...")
    os.chdir(REPO_DIR)

    # 检查是否有变更
    result = subprocess.run(["git", "status", "--porcelain", "stock-data.json"],
                            capture_output=True, text=True)
    if not result.stdout.strip():
        print("  ⚠️ 无变更，跳过提交")
        return

    subprocess.run(["git", "add", "stock-data.json"], check=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    subprocess.run(["git", "commit", "-m", f"🔄 库存刷新 {now}"], check=True)
    subprocess.run(["git", "push", "origin", "main"], check=True)
    print(f"  ✅ 已提交并推送 ({now})")


def main():
    print("🩺 蛇口疫苗库存自动刷新")
    print("=" * 50)

    # 1. 获取下载链接
    url = run_dws_download()

    # 2. 下载 xlsx
    xlsx_path = download_xlsx(url)

    try:
        # 3. 解析
        stock_map = parse_and_calc(xlsx_path)

        # 4. 映射
        vaccines = map_to_vaccines(stock_map)

        # 5. 生成 JSON
        generate_json(vaccines)

        # 6. 可选提交
        if "--commit" in sys.argv:
            git_commit()

    finally:
        # 清理临时文件
        if os.path.exists(xlsx_path):
            os.unlink(xlsx_path)
            print(f"  🧹 已清理临时文件")

    print("=" * 50)
    print("✅ 刷新完成")


if __name__ == "__main__":
    main()
