"""核对引擎与网页层自测脚本。

运行方式（隔离模式，避免从数据目录加载到同名模块）::

    python3 -I tests/test_reconcile.py

覆盖：
  1. 基础两层内连接 + 半开区间边界（含起日、不含止日）
  2. 漏计：归属层断裂 / 归口层断裂
  3. 多计：重叠归属、重叠归口、菱形连接（同一统计单位不同路径分别计入）
  4. 大量重复匹配：路径数爆炸（30 万级）下的线性性能与不枚举校验
  5. 输入校验：表头、列数、日期、金额、唯一性、有效期、空文件、NUL 字节
  6. Flask 端到端：上传成功、整次退回并带文件与行号、缺文件
"""

from __future__ import annotations

import io
import os
import sys
import time
import traceback
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.reconcile import (  # noqa: E402
    ATTRIBUTION_FILE,
    GRANT_FILE,
    InputError,
    JURISDICTION_FILE,
    reconcile,
)

FAILURES = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  PASS  {msg}")
    else:
        FAILURES.append(msg)
        print(f"  FAIL  {msg}")


def csv_bytes(rows):
    buf = io.StringIO()
    for row in rows:
        buf.write(",".join(str(c) for c in row) + "\n")
    return buf.getvalue()


GRANT_HEADER = ["单号", "项目号", "拨款日", "金额_分"]
ATTR_HEADER = ["行号", "项目号", "学院码", "起日", "止日"]
JUR_HEADER = ["行号", "学院码", "统计单位", "起日", "止日"]


# --------------------------------------------------------------------------- #
# 1. 基础连接 + 边界 + 漏计 + 多计 + 菱形
# --------------------------------------------------------------------------- #
def test_basic():
    print("[1] 基础两层连接 / 漏计 / 多计 / 菱形")
    grants = csv_bytes([
        GRANT_HEADER,
        ["G1", "P1", "2026-03-15", 1000],          # 正常单路径 -> U1
        ["G2", "P2", "2026-03-15", 2000],          # 归口层断裂（C2 无归口，漏计）
        ["G3", "P3", "2026-03-15", 3000],          # 归属层断裂（无归属，漏计）
        ["G5", "P5", "2026-03-15", 5000],          # 跨学院：C4→U2、C5→U3，共 2 路径
        ["G6", "P6", "2026-03-15", 6000],          # 菱形但两条路径都到同一单位 U9
        ["G7", "P7", "2026-03-15", 7000],          # 1 归属 × 2 归口 = 2 路径，U4/U5
    ])
    attrs = csv_bytes([
        ATTR_HEADER,
        [1, "P1", "C1", "2026-01-01", "2027-01-01"],
        [2, "P2", "C2", "2026-01-01", "2027-01-01"],   # C2 在归口表不存在
        [5, "P5", "C4", "2026-01-01", "2027-01-01"],
        [6, "P5", "C5", "2026-01-01", "2027-01-01"],
        [7, "P6", "C6", "2026-01-01", "2027-01-01"],
        [8, "P6", "C7", "2026-01-01", "2027-01-01"],
        [9, "P7", "C8", "2026-01-01", "2027-01-01"],
    ])
    jurs = csv_bytes([
        JUR_HEADER,
        [10, "C1", "U1", "2026-01-01", "2027-01-01"],
        [12, "C4", "U2", "2026-01-01", "2027-01-01"],
        [13, "C5", "U3", "2026-01-01", "2027-01-01"],
        [14, "C6", "U9", "2026-01-01", "2027-01-01"],
        [15, "C7", "U9", "2026-01-01", "2027-01-01"],
        [16, "C8", "U4", "2026-01-01", "2027-01-01"],
        [17, "C8", "U5", "2026-01-01", "2027-01-01"],
    ])
    report = reconcile(grants.encode(), attrs.encode(), jurs.encode())

    check(report.total_grants == 6, "拨款总数 6")
    check(report.normal_grants == 1, "正常笔数 1（G1）")

    missed = {m.grant_no: m for m in report.missed}
    check(set(missed) == {"G2", "G3"}, "漏计单号为 G2/G3")
    check(missed["G2"].broken_layer == "归口层" and missed["G2"].attribution_hits == 1,
          "G2 断在归口层，且命中 1 条归属")
    check(missed["G3"].broken_layer == "归属层", "G3 断在归属层")

    over = {o.grant_no: o for o in report.overcounted}
    check(set(over) == {"G5", "G6", "G7"}, "多计单号为 G5/G6/G7")
    check(over["G5"].path_count == 2, "G5 实际入表次数 2（跨学院各一条）")
    check(over["G5"].unit_counts == {"U2": 1, "U3": 1},
          "G5 单位分布 U2×1/U3×1（不同路径分别计入）")
    check(len(over["G5"].sample_paths) == 2, "G5 给出 2 条示例路径")
    sp = over["G5"].sample_paths
    check(all(p.attribution_line in (5, 6) and p.jurisdiction_line in (12, 13)
              for p in sp), "示例路径带归属/归口行号")
    check(over["G6"].path_count == 2 and over["G6"].unit_counts == {"U9": 2},
          "G6 两条不同路径汇入同一单位 U9，分别计入（2 次）")
    check(over["G7"].path_count == 2 and over["G7"].unit_counts == {"U4": 1, "U5": 1},
          "G7 一条归属对两条归口，2 条路径分别入 U4/U5")

    # 合计：G1=1000→U1×1；G5=5000→U2/U3 各 1；G6=6000→U9×2；G7=7000→U4/U5 各 1
    check(report.unit_path_counts == {"U1": 1, "U2": 1, "U3": 1, "U4": 1, "U5": 1, "U9": 2},
          "各单位实际入表次数正确")
    check(report.unit_totals ==
          {"U1": 1000, "U2": 5000, "U3": 5000, "U4": 7000, "U5": 7000, "U9": 12000},
          "各单位实际合计正确（多路径按路径倍乘）")
    check(report.grand_total == 37000, "总合计 37000 分")

    entry = {e.grant_no: e for e in report.entries}
    check([entry[g].path_count for g in ("G1", "G2", "G3", "G5", "G6", "G7")]
          == [1, 0, 0, 2, 2, 2], "每笔拨款实际入表次数与状态齐全")
    check([entry[g].status for g in ("G1", "G2", "G3", "G5", "G6", "G7")]
          == ["正常", "漏计", "漏计", "多计", "多计", "多计"], "每笔拨款状态正确")


# --------------------------------------------------------------------------- #
# 2. 半开区间边界（两层各自验证含起日、不含止日）
# --------------------------------------------------------------------------- #
def test_half_open_boundary():
    print("[1b] 半开区间 [起日, 止日)：起日当天命中、止日当天失效")
    # 第一层边界走 P1/C1；第二层边界走 P2/C2（归属始终有效，归口窗口短）
    attrs = csv_bytes([
        ATTR_HEADER,
        [1, "P1", "C1", "2026-01-10", "2026-01-20"],
        [2, "P2", "C2", "2026-01-01", "2027-01-01"],
    ])
    jurs = csv_bytes([
        JUR_HEADER,
        [9, "C1", "U0", "2026-01-01", "2027-01-01"],   # C1 归口长期有效，不干扰第一层边界
        [10, "C2", "U1", "2026-01-10", "2026-01-20"],
    ])

    def run(day, project="P1"):
        g = csv_bytes([GRANT_HEADER, ["G1", project, day, 100]]).encode()
        return reconcile(g, attrs.encode(), jurs.encode())

    # 第一层：起日当天有效；止日当天失效
    check(run("2026-01-10").entries[0].path_count == 1, "归属起日当天（1-10）命中")
    check(run("2026-01-19").entries[0].path_count == 1, "归属止日前一天（1-19）命中")
    r = run("2026-01-20")
    check(r.entries[0].status == "漏计" and r.missed[0].broken_layer == "归属层",
          "归属止日当天（1-20）失效，断在归属层")

    # 第二层：起日当天有效；止日当天失效（归属始终有效）
    check(run("2026-01-10", "P2").entries[0].path_count == 1, "归口起日当天（1-10）命中")
    check(run("2026-01-19", "P2").entries[0].path_count == 1, "归口止日前一天（1-19）命中")
    r = run("2026-01-20", "P2")
    check(r.entries[0].status == "漏计" and r.missed[0].broken_layer == "归口层",
          "归口止日当天（1-20）失效，断在归口层")


# --------------------------------------------------------------------------- #
# 3. 重叠归属（同一项目多条并行有效归属行）
# --------------------------------------------------------------------------- #
def test_overlapping_attribution():
    print("[2] 重叠归属：同项目同时期多条归属行")
    grants = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-05-01", 100]])
    attrs = csv_bytes([
        ATTR_HEADER,
        [1, "P1", "C1", "2026-01-01", "2027-01-01"],
        [2, "P1", "C1", "2026-03-01", "2027-01-01"],   # 与行 1 重叠、同学院
        [3, "P1", "C2", "2026-04-01", "2027-01-01"],   # 重叠、跨学院
    ])
    jurs = csv_bytes([
        JUR_HEADER,
        [10, "C1", "U1", "2026-01-01", "2027-01-01"],
        [20, "C2", "U2", "2026-01-01", "2027-01-01"],
    ])
    report = reconcile(grants.encode(), attrs.encode(), jurs.encode())
    over = report.overcounted[0]
    check(over.path_count == 3, "3 条并行归属 → 3 条路径")
    check(over.unit_counts == {"U1": 2, "U2": 1}, "同学院两条归属各成路径：U1×2, U2×1")
    check(report.unit_totals == {"U1": 200, "U2": 100}, "重叠归属按路径倍乘金额")
    check({p.attribution_line for p in over.sample_paths} == {1, 2},
          "多计两条示例路径标注不同归属行号")


# --------------------------------------------------------------------------- #
# 3. 大量重复匹配（路径数爆炸，验证不枚举 + 线性性能）
# --------------------------------------------------------------------------- #
def test_massive():
    print("[3] 大量重复匹配：600 归属 × 800 归口 = 480,000 条路径")
    n_attr, n_jur = 600, 800
    grants = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-06-01", 1000]])
    attr_rows = [ATTR_HEADER]
    for i in range(1, n_attr + 1):
        attr_rows.append([i, "P1", "C1", "2026-01-01", "2027-01-01"])
    jur_rows = [JUR_HEADER]
    for j in range(1, n_jur + 1):
        jur_rows.append([1000 + j, "C1", "U1", "2026-01-01", "2027-01-01"])

    started = time.perf_counter()
    report = reconcile(
        grants.encode(),
        csv_bytes(attr_rows).encode(),
        csv_bytes(jur_rows).encode(),
    )
    elapsed = time.perf_counter() - started

    expected = n_attr * n_jur
    check(report.overcounted[0].path_count == expected,
          f"实际入表次数 = {expected}（未枚举也能算出）")
    check(report.unit_path_counts == {"U1": expected}, "单位次数 48 万")
    check(report.unit_totals == {"U1": expected * 1000}, "单位合计 = 次数 × 金额")
    check(len(report.overcounted[0].sample_paths) == 2, "示例路径仍只取 2 条（有界）")
    check(elapsed < 2.0, f"耗时 {elapsed*1000:.1f}ms < 2s（与路径总数无关的线性复杂度）")
    print(f"       （480,000 路径，引擎实际构造的路径对象仅 2 个，用时 {elapsed*1000:.1f}ms）")


# --------------------------------------------------------------------------- #
# 4. 输入校验：错误必须带文件与行号，且整次退回
# --------------------------------------------------------------------------- #
def expect_error(grants, attrs, jurs, needle):
    try:
        reconcile(grants, attrs, jurs)
    except InputError as exc:
        text = " | ".join(InputError.format(e) for e in exc.errors)
        check(needle in text, f"报错包含 [{needle}]；实际：{text}")
        return exc
    FAILURES.append(f"应报错但通过了：{needle}")
    print(f"  FAIL  应报错但通过了：{needle}")
    return None


def test_validation():
    print("[4] 输入校验（文件 + 行号定位，整次退回）")
    good_g = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", 100]]).encode()
    good_a = csv_bytes([ATTR_HEADER, [1, "P1", "C1", "2026-01-01", "2027-01-01"]]).encode()
    good_j = csv_bytes([JUR_HEADER, [1, "C1", "U1", "2026-01-01", "2027-01-01"]]).encode()

    # 表头错误（定位到第 1 行）
    bad_header = csv_bytes([["单号", "项目号", "拨款日", "金额"]]).encode()
    expect_error(bad_header, good_a, good_j, f"{GRANT_FILE} 第1行")

    # 列数错误（定位物理行号 3）
    bad_cols = csv_bytes([
        GRANT_HEADER,
        ["G1", "P1", "2026-03-15", 100],
        ["G2", "P2", "2026-03-15"],
    ]).encode()
    expect_error(bad_cols, good_a, good_j, f"{GRANT_FILE} 第3行")

    # 日期格式
    bad_date = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026/03/15", 100]]).encode()
    expect_error(bad_date, good_a, good_j, f"{GRANT_FILE} 第2行")

    # 金额非整数 / 负数
    bad_amount = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", "12.5"]]).encode()
    expect_error(bad_amount, good_a, good_j, f"{GRANT_FILE} 第2行")
    neg_amount = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", -1]]).encode()
    expect_error(neg_amount, good_a, good_j, f"{GRANT_FILE} 第2行")

    # 单号重复（第二次出现的物理行号）
    dup_grant = csv_bytes([
        GRANT_HEADER,
        ["G1", "P1", "2026-03-15", 100],
        ["G1", "P2", "2026-03-15", 200],
    ]).encode()
    expect_error(dup_grant, good_a, good_j, f"{GRANT_FILE} 第3行")

    # 归属表行号重复
    dup_attr = csv_bytes([
        ATTR_HEADER,
        [1, "P1", "C1", "2026-01-01", "2027-01-01"],
        [1, "P2", "C2", "2026-01-01", "2027-01-01"],
    ]).encode()
    expect_error(good_g, dup_attr, good_j, f"{ATTRIBUTION_FILE} 第3行")

    # 归口表有效期非法（起日 >= 止日）
    bad_interval = csv_bytes([
        JUR_HEADER, [1, "C1", "U1", "2026-01-01", "2026-01-01"],
    ]).encode()
    expect_error(good_g, good_a, bad_interval, f"{JURISDICTION_FILE} 第2行")

    # 空字段
    empty_field = csv_bytes([GRANT_HEADER, ["G1", "", "2026-03-15", 100]]).encode()
    expect_error(empty_field, good_a, good_j, f"{GRANT_FILE} 第2行")

    # 空文件 / NUL 字节（只定位到文件）
    expect_error(b"", good_a, good_j, GRANT_FILE)
    expect_error(b"header\x00\n", good_a, good_j, GRANT_FILE)

    # 多文件同时有错：错误一次性全部返回，且都带文件名
    exc = expect_error(bad_date, dup_attr, bad_interval, GRANT_FILE)
    if exc is not None:
        labels = {e[0] for e in exc.errors}
        check(labels == {GRANT_FILE, ATTRIBUTION_FILE, JURISDICTION_FILE},
              "三个文件的错误一次性全部返回（整次退回，不留部分结果）")

    # 外键不存在不算输入错误，按漏计处理
    orphan = csv_bytes([GRANT_HEADER, ["G1", "PX", "2026-03-15", 100]]).encode()
    report = reconcile(orphan, good_a, good_j)
    check(report.missed[0].broken_layer == "归属层", "项目号在归属表不存在 → 归属层漏计而非报错")


# --------------------------------------------------------------------------- #
# 5. Flask 端到端
# --------------------------------------------------------------------------- #
def test_web():
    print("[5] Flask 端到端（上传成功 / 整次退回 / 缺文件）")
    try:
        from app.web import app
    except ImportError:
        print("  SKIP 未安装 Flask，跳过网页层测试")
        return

    client = app.test_client()
    sample_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "app", "samples")
    with open(os.path.join(sample_dir, "grants.csv"), "rb") as f1, \
         open(os.path.join(sample_dir, "attributions.csv"), "rb") as f2, \
         open(os.path.join(sample_dir, "jurisdictions.csv"), "rb") as f3:
        resp = client.post(
            "/check",
            data={
                "grants": (io.BytesIO(f1.read()), "grants.csv"),
                "attributions": (io.BytesIO(f2.read()), "attributions.csv"),
                "jurisdictions": (io.BytesIO(f3.read()), "jurisdictions.csv"),
            },
            content_type="multipart/form-data",
        )
    check(resp.status_code == 200, "样例三表上传返回 200")
    body = resp.get_data(as_text=True)
    for needle in ("各统计单位实际合计", "漏计单号", "多计单号", "G003", "G001", "行号 201"):
        check(needle in body, f"结果页包含 {needle}")

    # 错误输入：HTTP 400 + 文件行号定位 + 退回提示
    bad = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", "abc"]]).encode()
    with open(os.path.join(sample_dir, "attributions.csv"), "rb") as f2, \
         open(os.path.join(sample_dir, "jurisdictions.csv"), "rb") as f3:
        resp = client.post(
            "/check",
            data={
                "grants": (io.BytesIO(bad), "bad.csv"),
                "attributions": (io.BytesIO(f2.read()), "attributions.csv"),
                "jurisdictions": (io.BytesIO(f3.read()), "jurisdictions.csv"),
            },
            content_type="multipart/form-data",
        )
    check(resp.status_code == 400, "错误输入返回 400")
    text = resp.get_data(as_text=True)
    check("本次核对已整次退回" in text and "拨款表" in text and "第 2 行" in text,
          "错误页提示整次退回并定位到拨款表第 2 行")

    # 缺文件
    resp = client.post("/check", data={}, content_type="multipart/form-data")
    check(resp.status_code == 400 and "未上传文件" in resp.get_data(as_text=True),
          "缺少上传文件时 400 并提示")

    # 首页与样例下载
    check(client.get("/").status_code == 200, "首页 200")
    check(client.get("/samples/grants.csv").status_code == 200, "样例下载 200")


def main():
    for fn in (test_basic, test_half_open_boundary, test_overlapping_attribution,
               test_massive, test_validation, test_web):
        try:
            fn()
        except Exception:  # noqa: BLE001
            FAILURES.append(f"{fn.__name__} 抛异常")
            traceback.print_exc()
    print()
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for msg in FAILURES:
            print(f"  - {msg}")
        sys.exit(1)
    print("全部自测通过。")


if __name__ == "__main__":
    main()
