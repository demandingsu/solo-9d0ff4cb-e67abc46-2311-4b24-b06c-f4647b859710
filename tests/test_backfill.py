"""入仓明细来源行号补录的自测脚本。

运行方式（隔离模式）::

    python3 -I tests/test_backfill.py

覆盖：
  1. 入仓明细解析校验（表头 / 唯一性 / 同一单号 / 行号整数 / 上限）
  2. 候选路径：按拨款日 + 已有字段过滤；有效路径并集与 50 条上限
  3. 状态分类：人工已确认 / 可唯一推定 / 待核 / 无候选
  4. 一对一约束：同一对（归属行号, 归口行号）只能分给一条明细
  5. 确认导致整单无解 → 拒绝并保留原绑定；原本就无解的确认可接受
  6. 撤销确认后其余明细重判、恢复多解
  7. 唯一完整配法才可下载；多解 / 无解不可下载
  8. Flask 端到端：上传、确认、撤销、版本冲突 409、下载
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BACKFILL_JOB_DIR", tempfile.mkdtemp(prefix="backfill-test-"))

from app.backfill import (  # noqa: E402
    DETAIL_FILE,
    MAX_DETAILS,
    MAX_PATHS,
    STATUS_CONFIRMED,
    STATUS_NONE,
    STATUS_PENDING,
    STATUS_UNIQUE,
    ConfirmRejected,
    apply_confirm,
    apply_revoke,
    build_board,
    load_context,
    parse_details,
)
from app.reconcile import InputError  # noqa: E402

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
DETAIL_HEADER = ["入仓行号", "单号", "统计单位", "归属行号", "归口行号"]


def make_context(detail_rows, grant_no="G1"):
    """场景 A：1 归属 × 2 归口（同单位 U1）→ 路径 (1,10) / (1,11)。"""
    grants = csv_bytes([GRANT_HEADER, [grant_no, "P1", "2026-03-15", 1000]])
    attrs = csv_bytes([ATTR_HEADER, [1, "P1", "C1", "2026-01-01", "2027-01-01"]])
    jurs = csv_bytes([
        JUR_HEADER,
        [10, "C1", "U1", "2026-01-01", "2027-01-01"],
        [11, "C1", "U1", "2026-01-01", "2027-01-01"],
    ])
    details = csv_bytes([DETAIL_HEADER] + detail_rows)
    return load_context(grants.encode(), attrs.encode(), jurs.encode(), details.encode())


# --------------------------------------------------------------------------- #
# 1. 解析校验
# --------------------------------------------------------------------------- #
def expect_detail_error(raw, needle):
    try:
        parse_details(raw.encode())
    except InputError as exc:
        text = " | ".join(InputError.format(e) for e in exc.errors)
        check(needle in text, f"明细报错包含 [{needle}]；实际：{text}")
        return
    FAILURES.append(f"应报错但通过了：{needle}")
    print(f"  FAIL  应报错但通过了：{needle}")


def test_parse():
    print("[1] 入仓明细解析校验")
    good = csv_bytes([DETAIL_HEADER, ["E1", "G1", "U1", "", ""]]).encode()
    details = parse_details(good)
    check(details[0].attr_line is None and details[0].jur_line is None,
          "末两列留空解析为 None")

    filled = csv_bytes([DETAIL_HEADER, ["E1", "G1", "U1", 101, 901]]).encode()
    check(parse_details(filled)[0].attr_line == 101, "已填归属行号解析为整数")

    expect_detail_error(csv_bytes([["入仓行号", "单号", "统计单位"]]), f"{DETAIL_FILE} 第1行")
    expect_detail_error(
        csv_bytes([DETAIL_HEADER, ["E1", "G1", "U1", "abc", ""]]),
        f"{DETAIL_FILE} 第2行",
    )
    expect_detail_error(
        csv_bytes([DETAIL_HEADER, ["E1", "G1", "U1", "", ""], ["E1", "G2", "U1", "", ""]]),
        "重复",
    )
    expect_detail_error(
        csv_bytes([DETAIL_HEADER, ["E1", "G1", "U1", "", ""], ["E2", "G2", "U1", "", ""]]),
        "同一单号",
    )
    expect_detail_error(csv_bytes([DETAIL_HEADER, ["E1", "", "U1", "", ""]]), "单号 不能为空")
    expect_detail_error(csv_bytes([DETAIL_HEADER, ["E1", "G1", "", "", ""]]), "统计单位 不能为空")

    # 明细条数上限
    rows = [DETAIL_HEADER] + [[f"E{i}", "G1", "U1", "", ""] for i in range(MAX_DETAILS + 1)]
    expect_detail_error(csv_bytes(rows), f"最多核对 {MAX_DETAILS} 条明细")
    rows = [DETAIL_HEADER] + [[f"E{i}", "G1", "U1", "", ""] for i in range(MAX_DETAILS)]
    check(len(parse_details(csv_bytes(rows).encode())) == MAX_DETAILS,
          f"{MAX_DETAILS} 条明细为上限内，可正常解析")


def test_limits():
    print("[1b] 有效路径 50 条上限")
    grants = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", 100]])
    attrs = csv_bytes([ATTR_HEADER, [1, "P1", "C1", "2026-01-01", "2027-01-01"]])
    jur_rows = [JUR_HEADER] + [
        [100 + i, "C1", "U1", "2026-01-01", "2027-01-01"] for i in range(MAX_PATHS + 1)
    ]
    details = csv_bytes([DETAIL_HEADER, ["E1", "G1", "U1", "", ""]])
    try:
        load_context(grants.encode(), attrs.encode(), csv_bytes(jur_rows).encode(),
                     details.encode())
        check(False, f"有效路径 {MAX_PATHS + 1} 条应提示超上限")
    except InputError as exc:
        text = " | ".join(InputError.format(e) for e in exc.errors)
        check(f"最多 {MAX_PATHS} 条" in text, f"路径超上限提示；实际：{text}")

    # 单号在拨款表不存在
    bad_details = csv_bytes([DETAIL_HEADER, ["E1", "GX", "U1", "", ""]])
    try:
        load_context(grants.encode(), attrs.encode(),
                     csv_bytes([JUR_HEADER, [10, "C1", "U1", "2026-01-01", "2027-01-01"]]).encode(),
                     bad_details.encode())
        check(False, "单号不存在应报错")
    except InputError as exc:
        text = " | ".join(InputError.format(e) for e in exc.errors)
        check("GX" in text and "拨款表" in text, f"单号不存在提示；实际：{text}")


# --------------------------------------------------------------------------- #
# 2. 候选 / 状态分类 / 一对一
# --------------------------------------------------------------------------- #
def test_board():
    print("[2] 候选路径与状态分类")
    grant, details, paths = make_context([
        ["D1", "G1", "U1", "", ""],      # 候选 {(1,10),(1,11)} → 待核
        ["D2", "G1", "U1", "", 10],      # 候选 {(1,10)} → 可唯一推定
        ["D3", "G1", "U9", "", ""],      # 无候选
    ])
    check(len(paths) == 2, "有效路径 2 条（候选并集）")
    board = build_board(grant, details, paths, {})
    st = {db.detail.entry_no: db.status for db in board.details}
    check(st == {"D1": STATUS_PENDING, "D2": STATUS_UNIQUE, "D3": STATUS_NONE},
          f"状态分类 待核/唯一推定/无候选；实际 {st}")
    check(not board.solvable and any("D3" in r for r in board.reasons),
          "D3 无候选 → 整单无法配齐并说明原因")

    # 去掉 D3：可配齐且唯一（D2→(1,10)，D1→(1,11)）
    grant, details, paths = make_context([
        ["D1", "G1", "U1", "", ""],
        ["D2", "G1", "U1", "", 10],
    ])
    board = build_board(grant, details, paths, {})
    check(board.solvable and board.unique, "D2 唯一推定后整单唯一配齐")
    check(board.solution == {"D1": (1, 11), "D2": (1, 10)},
          f"唯一配法 D1→(1,11)、D2→(1,10)；实际 {board.solution}")

    # 已填归属行号过滤候选
    grant, details, paths = make_context([["D1", "G1", "U1", 1, 11]])
    board = build_board(grant, details, paths, {})
    check(board.details[0].status == STATUS_UNIQUE
          and board.details[0].bound.key == (1, 11),
          "已填归属+归口行号 → 候选收敛为唯一路径")


# --------------------------------------------------------------------------- #
# 3. 确认导致整单无解 → 拒绝并保留原绑定（题目指定试跑场景）
# --------------------------------------------------------------------------- #
def test_confirm_rejected_keeps_binding():
    print("[3] 确认能配但会让整单无解 → 拒绝，保留原绑定")
    grant, details, paths = make_context([
        ["D1", "G1", "U1", "", ""],      # {p1=(1,10), p2=(1,11)}
        ["D2", "G1", "U1", "", 10],      # {p1}
    ])
    confirmed = {}
    # D1 确认 p1：D2 将无路径可配 → 拒绝
    try:
        apply_confirm(grant, details, paths, confirmed, "D1", (1, 10))
        check(False, "确认 D1→(1,10) 应被拒绝")
    except ConfirmRejected as exc:
        check("保留原绑定" in str(exc) and "D2" in str(exc),
              f"拒绝并说明受影响的 D2；实际：{exc}")
    check(confirmed == {}, "拒绝后原绑定表未被改动")

    # D1 改确认 p2：整单唯一配齐 → 接受
    confirmed = apply_confirm(grant, details, paths, confirmed, "D1", (1, 11))
    check(confirmed == {"D1": (1, 11)}, "确认 D1→(1,11) 被接受")
    board = build_board(grant, details, paths, confirmed)
    check(board.solvable and board.unique, "确认后整单唯一配齐")
    st = {db.detail.entry_no: db.status for db in board.details}
    check(st["D1"] == STATUS_CONFIRMED and st["D2"] == STATUS_UNIQUE,
          "D1 人工已确认、D2 可唯一推定")

    # 重复确认 / 抢占同一路径 / 不符合字段，均应拒绝
    for eno, key, needle in (
        ("D1", (1, 11), "先撤销"),
        ("D2", (1, 11), "只能分给一条明细"),
        ("D2", (1, 11), "只能分给一条明细"),
    ):
        try:
            apply_confirm(grant, details, paths, confirmed, eno, key)
            check(False, f"确认 {eno}→{key} 应被拒绝")
        except ConfirmRejected as exc:
            check(needle in str(exc), f"拒绝 {eno}→{key}：{needle}")

    # 原本就无法配齐时，确认可接受（不是本次确认导致）
    grant, details, paths = make_context([
        ["D1", "G1", "U1", "", 10],      # {p1}
        ["D2", "G1", "U1", "", 10],      # {p1}，两条明细争同一路径
    ])
    before = build_board(grant, details, paths, {})
    check(not before.solvable, "两条明细争同一路径 → 原本无法配齐")
    confirmed = apply_confirm(grant, details, paths, {}, "D1", (1, 10))
    check(confirmed == {"D1": (1, 10)}, "原本无解时的确认可接受")
    after = build_board(grant, details, paths, confirmed)
    check(not after.solvable and any("占用" in r for r in after.reasons),
          "确认后仍无解，原因说明候选被确认绑定占用")


# --------------------------------------------------------------------------- #
# 4. 撤销恢复多解（题目指定试跑场景）
# --------------------------------------------------------------------------- #
def test_revoke_restores_multiple():
    print("[4] 撤销确认 → 其余明细重判，恢复多解")
    grant, details, paths = make_context([
        ["D1", "G1", "U1", "", ""],      # {p1, p2}
        ["D2", "G1", "U1", "", ""],      # {p1, p2}
    ])
    board = build_board(grant, details, paths, {})
    check(board.solvable and not board.unique, "初始两种完整配法（多解）")

    confirmed = apply_confirm(grant, details, paths, {}, "D1", (1, 10))
    board = build_board(grant, details, paths, confirmed)
    check(board.solvable and board.unique, "确认 D1→(1,10) 后唯一配齐")
    check(board.solution == {"D1": (1, 10), "D2": (1, 11)},
          "唯一配法 D1→(1,10)、D2→(1,11)")

    confirmed = apply_revoke(confirmed, "D1")
    board = build_board(grant, details, paths, confirmed)
    check(confirmed == {} and board.solvable and not board.unique,
          "撤销后恢复两种完整配法（多解）")
    check(all(db.status == STATUS_PENDING for db in board.details),
          "撤销后两条明细均回到待核")

    try:
        apply_revoke(confirmed, "D1")
        check(False, "重复撤销应被拒绝")
    except ConfirmRejected as exc:
        check("没有已确认" in str(exc), "重复撤销被拒绝")


# --------------------------------------------------------------------------- #
# 5. 多解计数：菱形 2×2 全空明细 → 2 种配法；3×2 → 不可唯一
# --------------------------------------------------------------------------- #
def test_uniqueness():
    print("[5] 完整配法唯一性判定")
    grants = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", 100]])
    attrs = csv_bytes([
        ATTR_HEADER,
        [1, "P1", "C1", "2026-01-01", "2027-01-01"],
        [2, "P1", "C1", "2026-01-01", "2027-01-01"],
    ])
    jurs = csv_bytes([
        JUR_HEADER,
        [10, "C1", "U1", "2026-01-01", "2027-01-01"],
        [11, "C1", "U1", "2026-01-01", "2027-01-01"],
    ])
    details = csv_bytes([
        DETAIL_HEADER,
        ["D1", "G1", "U1", "", ""],
        ["D2", "G1", "U1", "", ""],
    ])
    grant, details, paths = load_context(
        grants.encode(), attrs.encode(), jurs.encode(), details.encode()
    )
    check(len(paths) == 4, "菱形 2×2 → 4 条有效路径")
    board = build_board(grant, details, paths, {})
    check(board.solvable and not board.unique, "2 明细 × 4 路径 → 配法不唯一")

    confirmed = apply_confirm(grant, details, paths, {}, "D1", (2, 11))
    board = build_board(grant, details, paths, confirmed)
    check(board.solvable and not board.unique,
          "确认 D1 后 D2 仍有 3 条空闲候选 → 仍多解（交替路情形）")
    confirmed = apply_confirm(grant, details, paths, confirmed, "D2", (1, 10))
    board = build_board(grant, details, paths, confirmed)
    check(board.solvable and board.unique
          and board.solution == {"D1": (2, 11), "D2": (1, 10)},
          "两条都确认后收敛为唯一配法")

    # 候选集互不相交：无交替环，但各明细仍有空闲候选 → 多解（样例 G006 的情形）
    grants = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", 100]])
    attrs = csv_bytes([ATTR_HEADER, [1, "P1", "C1", "2026-01-01", "2027-01-01"]])
    jurs = csv_bytes([
        JUR_HEADER,
        [10, "C1", "U1", "2026-01-01", "2027-01-01"],
        [11, "C1", "U1", "2026-01-01", "2027-01-01"],
        [12, "C1", "U2", "2026-01-01", "2027-01-01"],
        [13, "C1", "U2", "2026-01-01", "2027-01-01"],
    ])
    details = csv_bytes([
        DETAIL_HEADER,
        ["D1", "G1", "U1", "", ""],      # {(1,10),(1,11)}
        ["D2", "G1", "U2", "", ""],      # {(1,12),(1,13)}，与 D1 不相交
    ])
    grant, details, paths = load_context(
        grants.encode(), attrs.encode(), jurs.encode(), details.encode()
    )
    board = build_board(grant, details, paths, {})
    check(board.solvable and not board.unique,
          "候选集互不相交但各有空闲候选 → 配法不唯一（交替路情形）")
    confirmed = apply_confirm(grant, details, paths, {}, "D1", (1, 10))
    board = build_board(grant, details, paths, confirmed)
    check(board.solvable and not board.unique,
          "只确认 D1 仍多解（D2 尚有两条候选）")
    confirmed = apply_confirm(grant, details, paths, confirmed, "D2", (1, 12))
    board = build_board(grant, details, paths, confirmed)
    check(board.solvable and board.unique, "两条都确认后才唯一")


# --------------------------------------------------------------------------- #
# 6. Flask 端到端
# --------------------------------------------------------------------------- #
def test_web():
    print("[6] Flask 端到端（上传 / 确认 / 撤销 / 版本冲突 / 下载）")
    try:
        from app.web import app
    except ImportError:
        print("  SKIP 未安装 Flask，跳过网页层测试")
        return

    client = app.test_client()
    grants = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", 1000]]).encode()
    attrs = csv_bytes([ATTR_HEADER, [1, "P1", "C1", "2026-01-01", "2027-01-01"]]).encode()
    jurs = csv_bytes([
        JUR_HEADER,
        [10, "C1", "U1", "2026-01-01", "2027-01-01"],
        [11, "C1", "U1", "2026-01-01", "2027-01-01"],
    ]).encode()
    details = csv_bytes([
        DETAIL_HEADER,
        ["D1", "G1", "U1", "", ""],
        ["D2", "G1", "U1", "", ""],
    ]).encode()

    def upload():
        return client.post(
            "/backfill",
            data={
                "grants": (io.BytesIO(grants), "grants.csv"),
                "attributions": (io.BytesIO(attrs), "attributions.csv"),
                "jurisdictions": (io.BytesIO(jurs), "jurisdictions.csv"),
                "details": (io.BytesIO(details), "details.csv"),
            },
            content_type="multipart/form-data",
        )

    check(client.get("/backfill").status_code == 200, "补录上传页 200")
    resp = upload()
    check(resp.status_code == 302 and "/backfill/" in resp.headers["Location"],
          "上传四份 CSV 后重定向到核对页")
    job_url = resp.headers["Location"]
    job_id = job_url.rstrip("/").split("/")[-1]

    resp = client.get(job_url)
    body = resp.get_data(as_text=True)
    check(resp.status_code == 200 and "待核" in body and "配法不唯一" in body,
          "核对页显示两条待核明细、配法不唯一")
    check("下载补齐来源行号" not in body, "多解时不提供下载入口")

    # 多解时直接请求下载 → 409
    resp = client.get(f"{job_url}/download")
    check(resp.status_code == 409 and "不唯一" in resp.get_data(as_text=True),
          "多解时下载被拒绝（409）")

    # 确认 D1→(1,10)：合法，版本 0 → 1
    resp = client.post(
        f"{job_url}/confirm",
        data={"version": "0", "entry_no": "D1", "attr_line": "1", "jur_line": "10"},
    )
    check(resp.status_code == 302, "确认 D1→(1,10) 成功并重定向")
    resp = client.get(job_url)
    body = resp.get_data(as_text=True)
    check("人工已确认" in body and "可唯一推定" in body and "已可唯一配齐" in body,
          "确认后 D1 人工已确认、D2 唯一推定、整单唯一配齐")

    # 旧页签用过期版本提交 → 409 提示刷新
    resp = client.post(
        f"{job_url}/confirm",
        data={"version": "0", "entry_no": "D2", "attr_line": "1", "jur_line": "11"},
    )
    check(resp.status_code == 409 and "绑定已变化" in resp.get_data(as_text=True),
          "旧版本提交被拒绝（409）并提示刷新")

    # 唯一配法 → 下载
    resp = client.get(f"{job_url}/download")
    check(resp.status_code == 200 and "attachment" in resp.headers.get("Content-Disposition", ""),
          "唯一配法时下载 200")
    text = resp.get_data(as_text=True).lstrip("﻿")
    lines = text.strip().splitlines()
    check(lines[0] == "入仓行号,单号,统计单位,归属行号,归口行号", "下载 CSV 表头正确")
    check("D1,G1,U1,1,10" in lines and "D2,G1,U1,1,11" in lines,
          f"下载 CSV 补齐了来源行号；实际：{lines}")

    # 撤销 D1 → 恢复多解
    resp = client.post(f"{job_url}/revoke", data={"version": "1", "entry_no": "D1"})
    check(resp.status_code == 302, "撤销 D1 成功")
    resp = client.get(job_url)
    body = resp.get_data(as_text=True)
    check("配法不唯一" in body and body.count("待核") >= 2, "撤销后恢复多解、两条待核")

    # 场景：确认会让整单无解 → 拒绝并保留原绑定
    details2 = csv_bytes([
        DETAIL_HEADER,
        ["D1", "G1", "U1", "", ""],
        ["D2", "G1", "U1", "", 10],
    ]).encode()
    resp = client.post(
        "/backfill",
        data={
            "grants": (io.BytesIO(grants), "grants.csv"),
            "attributions": (io.BytesIO(attrs), "attributions.csv"),
            "jurisdictions": (io.BytesIO(jurs), "jurisdictions.csv"),
            "details": (io.BytesIO(details2), "details.csv"),
        },
        content_type="multipart/form-data",
    )
    job2 = resp.headers["Location"]
    resp = client.post(
        f"{job2}/confirm",
        data={"version": "0", "entry_no": "D1", "attr_line": "1", "jur_line": "10"},
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    check("保留原绑定" in body and "待核" in body,
          "确认导致整单无解 → 页面提示保留原绑定，D1 仍待核")

    # 输入错误整次退回
    bad = csv_bytes([DETAIL_HEADER, ["D1", "G1", "U1", "x", ""]]).encode()
    resp = client.post(
        "/backfill",
        data={
            "grants": (io.BytesIO(grants), "grants.csv"),
            "attributions": (io.BytesIO(attrs), "attributions.csv"),
            "jurisdictions": (io.BytesIO(jurs), "jurisdictions.csv"),
            "details": (io.BytesIO(bad), "bad.csv"),
        },
        content_type="multipart/form-data",
    )
    check(resp.status_code == 400 and "入仓明细" in resp.get_data(as_text=True),
          "明细解析错误 400 整次退回并定位文件")

    # 不存在的会话
    check(client.get("/backfill/" + "0" * 32).status_code == 404, "未知会话 404")


def main():
    for fn in (test_parse, test_limits, test_board, test_confirm_rejected_keeps_binding,
               test_revoke_restores_multiple, test_uniqueness, test_web):
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
