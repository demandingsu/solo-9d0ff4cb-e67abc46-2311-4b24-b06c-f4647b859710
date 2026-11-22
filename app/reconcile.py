"""拨款核对核心引擎。

数据流（按拨款日做两层内连接）::

    拨款表 grant ──(项目号, 拨款日 ∈ 归属有效期)──> 归属表 attribution
                 ──(学院码, 拨款日 ∈ 归口有效期)──> 归口表 jurisdiction

一条完整匹配路径 = (一条归属行, 一条归口行)。
- 路径数为 0：漏计，标明断在归属层还是归口层；
- 路径数 > 1：多计，实际入表次数即路径数；
- 同一统计单位的不同路径分别计入（菱形连接也要按路径数倍乘金额）。

关键约束：计算合计与次数时**不枚举全部匹配路径**。
归属有效期为 [起日, 止日)，归口同理（含起日、不含止日）。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

# 各文件期望列名（顺序固定，首行表头必须完全一致）
GRANT_COLUMNS = ["单号", "项目号", "拨款日", "金额_分"]
ATTRIBUTION_COLUMNS = ["行号", "项目号", "学院码", "起日", "止日"]
JURISDICTION_COLUMNS = ["行号", "学院码", "统计单位", "起日", "止日"]

GRANT_FILE = "拨款表"
ATTRIBUTION_FILE = "归属表"
JURISDICTION_FILE = "归口表"


class InputError(Exception):
    """输入数据错误，errors 中每项为 (文件标识, 行号或 None, 说明)。"""

    def __init__(self, errors: List[Tuple[str, Optional[int], str]]):
        self.errors = errors
        super().__init__("; ".join(self.format(e) for e in errors))

    @staticmethod
    def format(item: Tuple[str, Optional[int], str]) -> str:
        label, line, msg = item
        return f"{label} 第{line}行: {msg}" if line else f"{label}: {msg}"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AttributionRow:
    line_no: int            # 数据中的“行号”（业务行号，非 CSV 物理行）
    project: str
    college: str
    start: date
    end: date


@dataclass(frozen=True)
class JurisdictionRow:
    line_no: int
    college: str
    unit: str
    start: date
    end: date


@dataclass(frozen=True)
class Grant:
    grant_no: str
    project: str
    grant_date: date
    amount: int             # 分
    csv_line: int           # CSV 物理行号（含表头偏移），用于报错定位


@dataclass(frozen=True)
class MatchPath:
    """一条匹配路径：归属行 + 归口行。"""

    attribution_line: int
    jurisdiction_line: int
    college: str
    unit: str


@dataclass
class MissedGrant:
    grant_no: str
    project: str
    grant_date: date
    amount: int
    broken_layer: str       # "归属层" | "归口层"
    attribution_hits: int   # 命中的归属行数（归口层断裂时 > 0）


@dataclass
class OvercountedGrant:
    grant_no: str
    project: str
    grant_date: date
    amount: int
    path_count: int                       # 实际入表次数（完整路径总数）
    sample_paths: List[MatchPath]         # 至多 2 条示例路径
    unit_counts: Dict[str, int]           # 各统计单位的路径数


@dataclass
class GrantEntry:
    """每笔拨款的实际入表情况。"""

    grant_no: str
    project: str
    grant_date: date
    amount: int
    path_count: int
    status: str                           # 正常 / 漏计 / 多计


@dataclass
class Report:
    total_grants: int
    normal_grants: int
    unit_totals: Dict[str, int]           # 统计单位 -> 实际合计（分）
    unit_path_counts: Dict[str, int]      # 统计单位 -> 实际入表次数
    missed: List[MissedGrant]
    overcounted: List[OvercountedGrant]
    entries: List[GrantEntry]
    grand_total: int                      # 所有统计单位合计（分）


# --------------------------------------------------------------------------- #
# CSV 读取与校验
# --------------------------------------------------------------------------- #
def _decode(raw: bytes, label: str) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError([(label, None, f"文件不是合法的 UTF-8 编码（{exc.reason}）")])


def _parse_csv(raw: bytes, label: str, expected: List[str]) -> List[Dict[str, str]]:
    """通用 CSV 解析：校验表头、列数，返回带物理行号的字典列表。"""
    text = _decode(raw, label)
    if "\x00" in text:
        raise InputError([(label, None, "文件包含 NUL 字节，疑似不是 CSV 文本")])
    reader = csv.reader(io.StringIO(text))
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise InputError([(label, None, f"CSV 解析失败: {exc}")])

    if not rows or all(not any(cell.strip() for cell in r) for r in rows):
        raise InputError([(label, None, "文件为空或没有数据行")])

    header = [c.strip() for c in rows[0]]
    if header != expected:
        raise InputError(
            [(label, 1, f"表头应为 {','.join(expected)}，实际为 {','.join(header) or '(空)'}")]
        )

    records: List[Dict[str, str]] = []
    for physical, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue  # 跳过纯空行
        if len(row) != len(expected):
            raise InputError(
                [(label, physical, f"列数应为 {len(expected)}，实际为 {len(row)}")]
            )
        record = {col: row[i].strip() for i, col in enumerate(expected)}
        record["_csv_line"] = physical  # type: ignore[assignment]
        records.append(record)
    if not records:
        raise InputError([(label, None, "只有表头，没有数据行")])
    return records


def _parse_date(value: str, label: str, line: int, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InputError(
            [(label, line, f"{field_name} 日期格式应为 YYYY-MM-DD，实际为 '{value}'")]
        )


def _parse_int(value: str, label: str, line: int, field_name: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise InputError(
            [(label, line, f"{field_name} 应为整数，实际为 '{value}'")]
        )


def _require_text(value: str, label: str, line: int, field_name: str) -> str:
    if value == "":
        raise InputError([(label, line, f"{field_name} 不能为空")])
    return value


def parse_grants(raw: bytes, label: str = GRANT_FILE) -> List[Grant]:
    records = _parse_csv(raw, label, GRANT_COLUMNS)
    grants: List[Grant] = []
    seen: Dict[str, int] = {}
    errors: List[Tuple[str, Optional[int], str]] = []
    for rec in records:
        line = rec["_csv_line"]  # type: ignore[assignment]
        try:
            grant_no = _require_text(rec["单号"], label, line, "单号")
            project = _require_text(rec["项目号"], label, line, "项目号")
            grant_date = _parse_date(rec["拨款日"], label, line, "拨款日")
            amount = _parse_int(rec["金额_分"], label, line, "金额_分")
            if amount < 0:
                errors.append((label, line, f"金额_分不能为负数，实际为 {amount}"))
                continue
            if grant_no in seen:
                errors.append(
                    (label, line, f"单号 '{grant_no}' 重复，首次出现在第 {seen[grant_no]} 行")
                )
                continue
            seen[grant_no] = line
            grants.append(Grant(grant_no, project, grant_date, amount, line))
        except InputError as exc:
            errors.extend(exc.errors)
    if errors:
        raise InputError(errors)
    return grants


def _parse_interval_table(
    raw: bytes,
    label: str,
    columns: List[str],
) -> Tuple[List[dict], List[Tuple[str, Optional[int], str]]]:
    """解析归属/归口表，返回 (记录, 错误)，行号唯一性由调用方按表校验。"""
    records = _parse_csv(raw, label, columns)
    errors: List[Tuple[str, Optional[int], str]] = []
    parsed: List[dict] = []
    seen: Dict[int, int] = {}
    for rec in records:
        line = rec["_csv_line"]  # type: ignore[assignment]
        try:
            line_no = _parse_int(rec["行号"], label, line, "行号")
            if line_no in seen:
                errors.append(
                    (label, line, f"行号 {line_no} 重复，首次出现在第 {seen[line_no]} 行")
                )
                continue
            seen[line_no] = line
            start = _parse_date(rec["起日"], label, line, "起日")
            end = _parse_date(rec["止日"], label, line, "止日")
            if not start < end:
                errors.append(
                    (label, line, f"有效期非法：起日 {start} 必须早于止日 {end}（含起不含止）")
                )
                continue
            parsed.append({"rec": rec, "line": line, "line_no": line_no,
                           "start": start, "end": end})
        except InputError as exc:
            errors.extend(exc.errors)
    return parsed, errors


def parse_attributions(raw: bytes, label: str = ATTRIBUTION_FILE) -> List[AttributionRow]:
    parsed, errors = _parse_interval_table(raw, label, ATTRIBUTION_COLUMNS)
    rows: List[AttributionRow] = []
    for item in parsed:
        rec, line = item["rec"], item["line"]
        try:
            project = _require_text(rec["项目号"], label, line, "项目号")
            college = _require_text(rec["学院码"], label, line, "学院码")
            rows.append(
                AttributionRow(item["line_no"], project, college, item["start"], item["end"])
            )
        except InputError as exc:
            errors.extend(exc.errors)
    if errors:
        raise InputError(errors)
    return rows


def parse_jurisdictions(raw: bytes, label: str = JURISDICTION_FILE) -> List[JurisdictionRow]:
    parsed, errors = _parse_interval_table(raw, label, JURISDICTION_COLUMNS)
    rows: List[JurisdictionRow] = []
    for item in parsed:
        rec, line = item["rec"], item["line"]
        try:
            college = _require_text(rec["学院码"], label, line, "学院码")
            unit = _require_text(rec["统计单位"], label, line, "统计单位")
            rows.append(
                JurisdictionRow(item["line_no"], college, unit, item["start"], item["end"])
            )
        except InputError as exc:
            errors.extend(exc.errors)
    if errors:
        raise InputError(errors)
    return rows


# --------------------------------------------------------------------------- #
# 索引与两层连接
# --------------------------------------------------------------------------- #
def _active(rows: List[Tuple[date, date, object]], day: date) -> List[object]:
    """有效期 [start, end) 覆盖 day 的行（线性扫描，不展开任何组合）。"""
    return [obj for start, end, obj in rows if start <= day < end]


def reconcile(
    grants_raw: bytes,
    attributions_raw: bytes,
    jurisdictions_raw: bytes,
) -> Report:
    """执行两层内连接核对。

    合计与次数的复杂度为 O(匹配到的归属行数 + 这些学院下的归口行数)，
    与完整路径总数（两者乘积）无关，因此不枚举任何匹配路径。
    """
    # 三个文件一起解析，汇总全部错误后一次性退回
    errors: List[Tuple[str, Optional[int], str]] = []
    grants: List[Grant] = []
    attributions: List[AttributionRow] = []
    jurisdictions: List[JurisdictionRow] = []
    for raw, parser, sink in (
        (grants_raw, parse_grants, grants),
        (attributions_raw, parse_attributions, attributions),
        (jurisdictions_raw, parse_jurisdictions, jurisdictions),
    ):
        try:
            sink.extend(parser(raw))
        except InputError as exc:
            errors.extend(exc.errors)
    if errors:
        raise InputError(errors)

    # 归属：项目号 -> [(起日, 止日, 行)]；归口：学院码 -> [(起日, 止日, 行)]
    attr_index: Dict[str, List[Tuple[date, date, AttributionRow]]] = {}
    for row in attributions:
        attr_index.setdefault(row.project, []).append((row.start, row.end, row))

    jur_index: Dict[str, List[Tuple[date, date, JurisdictionRow]]] = {}
    for row in jurisdictions:
        jur_index.setdefault(row.college, []).append((row.start, row.end, row))

    unit_totals: Dict[str, int] = {}
    unit_path_counts: Dict[str, int] = {}
    missed: List[MissedGrant] = []
    overcounted: List[OvercountedGrant] = []
    entries: List[GrantEntry] = []
    normal = 0

    for grant in grants:
        # ---- 第一层：项目号 + 拨款日落入归属有效期 ----
        attr_hits: List[AttributionRow] = _active(
            attr_index.get(grant.project, []), grant.grant_date
        )
        if not attr_hits:
            missed.append(
                MissedGrant(
                    grant.grant_no, grant.project, grant.grant_date,
                    grant.amount, "归属层", 0,
                )
            )
            entries.append(
                GrantEntry(
                    grant.grant_no, grant.project, grant.grant_date,
                    grant.amount, 0, "漏计",
                )
            )
            continue

        # ---- 第二层：按命中归属行的学院码，找拨款日有效的归口行 ----
        # 不构造 (归属行 × 归口行) 的笛卡尔积：
        # 先按学院聚合归口行数，再用 归属行数 × 学院归口行数 做加权计数。
        college_jur: Dict[str, List[JurisdictionRow]] = {}
        for attr in attr_hits:
            if attr.college not in college_jur:
                college_jur[attr.college] = _active(
                    jur_index.get(attr.college, []), grant.grant_date
                )

        # 学院 -> 拨款日有效的归属行数（同学院多条归属行各自是独立路径）
        attr_count_by_college: Dict[str, int] = {}
        for attr in attr_hits:
            attr_count_by_college[attr.college] = (
                attr_count_by_college.get(attr.college, 0) + 1
            )

        total_paths = 0
        per_unit: Dict[str, int] = {}
        for college, jur_rows in college_jur.items():
            n_attr = attr_count_by_college[college]
            for jur in jur_rows:
                paths = n_attr  # 该归口行与 n_attr 条归属行各组成一条路径
                total_paths += paths
                per_unit[jur.unit] = per_unit.get(jur.unit, 0) + paths
                unit_path_counts[jur.unit] = unit_path_counts.get(jur.unit, 0) + paths
                unit_totals[jur.unit] = (
                    unit_totals.get(jur.unit, 0) + paths * grant.amount
                )

        if total_paths == 0:
            missed.append(
                MissedGrant(
                    grant.grant_no, grant.project, grant.grant_date,
                    grant.amount, "归口层", len(attr_hits),
                )
            )
            entries.append(
                GrantEntry(
                    grant.grant_no, grant.project, grant.grant_date,
                    grant.amount, 0, "漏计",
                )
            )
            continue

        entries.append(
            GrantEntry(
                grant.grant_no, grant.project, grant.grant_date,
                grant.amount, total_paths,
                "正常" if total_paths == 1 else "多计",
            )
        )
        if total_paths == 1:
            normal += 1
            continue

        # 多计：仅为展示取至多 2 条示例路径（有界采样，不枚举全部路径）
        sample: List[MatchPath] = []
        for attr in attr_hits:
            for jur in college_jur.get(attr.college, []):
                sample.append(
                    MatchPath(attr.line_no, jur.line_no, attr.college, jur.unit)
                )
                if len(sample) == 2:
                    break
            if len(sample) == 2:
                break
        overcounted.append(
            OvercountedGrant(
                grant.grant_no, grant.project, grant.grant_date,
                grant.amount, total_paths, sample, dict(sorted(per_unit.items())),
            )
        )

    missed.sort(key=lambda m: m.grant_no)
    overcounted.sort(key=lambda o: o.grant_no)
    entries.sort(key=lambda e: e.grant_no)
    return Report(
        total_grants=len(grants),
        normal_grants=normal,
        unit_totals=dict(sorted(unit_totals.items())),
        unit_path_counts=dict(sorted(unit_path_counts.items())),
        missed=missed,
        overcounted=overcounted,
        entries=entries,
        grand_total=sum(unit_totals.values()),
    )
