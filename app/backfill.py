"""入仓明细来源行号补录引擎。

旧入仓明细缺归属行号 / 归口行号，按单号取拨款日有效路径补齐：

- 每条明细的候选路径 = 该单号按拨款日两层连接（与核对引擎同一规则）得到的
  完整路径中，符合明细已有字段者：统计单位必须相等；归属行号、归口行号
  已填则必须相等，留空不约束；
- 同一对（归属行号, 归口行号）只能分给一条明细（一对一匹配）；
- 人工确认的绑定优先固定，其余明细扣除已确认占用后随之重判；
- 确认若导致整单无法配齐则拒绝并保留原绑定；
- 仅当当前绑定下存在**唯一**完整配法时才允许导出补齐结果。

规模上限：每次最多 20 条明细、50 条有效路径（候选路径并集），超出即提示。
"""

from __future__ import annotations

import fcntl  # Unix 文件锁；部署目标为 Linux 容器
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

# 复用核对引擎的解析与校验工具（同包内部复用，保持同一套输入规则）
from .reconcile import (
    GRANT_FILE,
    AttributionRow,
    Grant,
    InputError,
    JurisdictionRow,
    _parse_csv,
    _parse_int,
    _require_text,
    parse_attributions,
    parse_grants,
    parse_jurisdictions,
)

DETAIL_FILE = "入仓明细"
DETAIL_COLUMNS = ["入仓行号", "单号", "统计单位", "归属行号", "归口行号"]

MAX_DETAILS = 20          # 每次最多核对的明细条数
MAX_PATHS = 50            # 每次最多核对的有效路径条数（候选并集）
JOB_TTL_SECONDS = 6 * 3600  # 补录会话保留时长

# 明细状态
STATUS_CONFIRMED = "confirmed"   # 人工已确认
STATUS_UNIQUE = "unique"         # 可唯一推定（仅剩 1 条候选）
STATUS_PENDING = "pending"       # 待核（候选 ≥ 2）
STATUS_NONE = "none"             # 无候选路径


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DetailRow:
    entry_no: str                 # 入仓行号（业务标识，唯一）
    grant_no: str                 # 单号
    unit: str                     # 统计单位
    attr_line: Optional[int]      # 已填归属行号（可空）
    jur_line: Optional[int]       # 已填归口行号（可空）
    csv_line: int                 # CSV 物理行号，用于报错定位


@dataclass(frozen=True)
class CandidatePath:
    """一条有效路径：归属行 + 归口行。"""

    attr_line: int
    jur_line: int
    college: str
    unit: str

    @property
    def key(self) -> Tuple[int, int]:
        return (self.attr_line, self.jur_line)


@dataclass
class DetailBoard:
    """一条明细的当前核对状态。"""

    detail: DetailRow
    status: str                            # confirmed / unique / pending / none
    candidates: List[CandidatePath]        # 扣除他人确认占用后的可选路径
    bound: Optional[CandidatePath]         # 已确认（或唯一推定）的路径
    raw_count: int                         # 不扣占用时的候选数（区分“本就没有”与“被占光”）


@dataclass
class Board:
    """整单核对盘面。"""

    grant: Grant
    details: List[DetailBoard]
    path_total: int                        # 有效路径总数（候选并集）
    solvable: bool                         # 当前绑定下整单能否配齐
    unique: bool                           # 完整配法是否唯一
    solution: Optional[Dict[str, Tuple[int, int]]]  # 入仓行号 -> (归属行号, 归口行号)
    reasons: List[str]                     # 无法配齐时的原因说明


# --------------------------------------------------------------------------- #
# 入仓明细解析
# --------------------------------------------------------------------------- #
def parse_details(raw: bytes, label: str = DETAIL_FILE) -> List[DetailRow]:
    records = _parse_csv(raw, label, DETAIL_COLUMNS)
    errors = []
    details: List[DetailRow] = []
    seen: Dict[str, int] = {}
    grant_nos = set()
    for rec in records:
        line = rec["_csv_line"]  # type: ignore[assignment]
        try:
            entry_no = _require_text(rec["入仓行号"], label, line, "入仓行号")
            grant_no = _require_text(rec["单号"], label, line, "单号")
            unit = _require_text(rec["统计单位"], label, line, "统计单位")
            attr_raw, jur_raw = rec["归属行号"], rec["归口行号"]
            attr_line = _parse_int(attr_raw, label, line, "归属行号") if attr_raw else None
            jur_line = _parse_int(jur_raw, label, line, "归口行号") if jur_raw else None
            if entry_no in seen:
                errors.append(
                    (label, line, f"入仓行号 '{entry_no}' 重复，首次出现在第 {seen[entry_no]} 行")
                )
                continue
            seen[entry_no] = line
            grant_nos.add(grant_no)
            details.append(DetailRow(entry_no, grant_no, unit, attr_line, jur_line, line))
        except InputError as exc:
            errors.extend(exc.errors)
    if not errors and len(grant_nos) != 1:
        errors.append(
            (label, None,
             f"入仓明细应只含同一单号，实际出现 {len(grant_nos)} 个："
             f"{', '.join(sorted(grant_nos))}")
        )
    if not errors and len(details) > MAX_DETAILS:
        errors.append(
            (label, None, f"每次最多核对 {MAX_DETAILS} 条明细，实际 {len(details)} 条")
        )
    if errors:
        raise InputError(errors)
    return details


# --------------------------------------------------------------------------- #
# 路径枚举与候选
# --------------------------------------------------------------------------- #
def _enum_paths(
    grant: Grant,
    attributions: List[AttributionRow],
    jurisdictions: List[JurisdictionRow],
) -> List[CandidatePath]:
    """按拨款日做两层内连接，枚举该单号的全部完整路径（有上限保护，规模有界）。"""
    attr_index: Dict[str, List[AttributionRow]] = {}
    for row in attributions:
        attr_index.setdefault(row.project, []).append(row)
    jur_index: Dict[str, List[JurisdictionRow]] = {}
    for row in jurisdictions:
        jur_index.setdefault(row.college, []).append(row)

    day = grant.grant_date
    paths: List[CandidatePath] = []
    for attr in attr_index.get(grant.project, []):
        if not (attr.start <= day < attr.end):
            continue
        for jur in jur_index.get(attr.college, []):
            if jur.start <= day < jur.end:
                paths.append(
                    CandidatePath(attr.line_no, jur.line_no, attr.college, jur.unit)
                )
    paths.sort(key=lambda p: (p.attr_line, p.jur_line))
    return paths


def detail_candidates(detail: DetailRow, paths: List[CandidatePath]) -> List[CandidatePath]:
    """符合明细已有字段的路径：统计单位必等；归属/归口行号已填则必等。"""
    return [
        p for p in paths
        if p.unit == detail.unit
        and (detail.attr_line is None or p.attr_line == detail.attr_line)
        and (detail.jur_line is None or p.jur_line == detail.jur_line)
    ]


def effective_paths(details: List[DetailRow], paths: List[CandidatePath]) -> List[CandidatePath]:
    """有效路径 = 各明细候选路径的并集（页面实际核对的对象）。"""
    seen: Dict[Tuple[int, int], CandidatePath] = {}
    for d in details:
        for p in detail_candidates(d, paths):
            seen[p.key] = p
    return [seen[k] for k in sorted(seen)]


def load_context(
    grants_raw: bytes,
    attributions_raw: bytes,
    jurisdictions_raw: bytes,
    details_raw: bytes,
) -> Tuple[Grant, List[DetailRow], List[CandidatePath]]:
    """解析四份 CSV，返回 (该单号拨款, 明细列表, 有效路径列表)。

    任一文件有误即整次退回（InputError 汇总全部错误）。
    """
    errors = []
    grants: List[Grant] = []
    attributions: List[AttributionRow] = []
    jurisdictions: List[JurisdictionRow] = []
    details: List[DetailRow] = []
    for raw, parser, sink in (
        (grants_raw, parse_grants, grants),
        (attributions_raw, parse_attributions, attributions),
        (jurisdictions_raw, parse_jurisdictions, jurisdictions),
        (details_raw, parse_details, details),
    ):
        try:
            sink.extend(parser(raw))
        except InputError as exc:
            errors.extend(exc.errors)
    if errors:
        raise InputError(errors)

    grant_no = details[0].grant_no
    grant = next((g for g in grants if g.grant_no == grant_no), None)
    if grant is None:
        raise InputError(
            [(DETAIL_FILE, None, f"单号 '{grant_no}' 在{GRANT_FILE}中不存在")]
        )

    paths = effective_paths(details, _enum_paths(grant, attributions, jurisdictions))
    if len(paths) > MAX_PATHS:
        raise InputError(
            [(DETAIL_FILE, None,
              f"本次有效路径共 {len(paths)} 条，超过每次最多 {MAX_PATHS} 条的上限；"
              f"请先在明细中补填归属/归口行号以缩小范围")]
        )
    return grant, details, paths


# --------------------------------------------------------------------------- #
# 一对一匹配：完美匹配存在性 + 唯一性
# --------------------------------------------------------------------------- #
def _max_matching(
    adj: Dict[str, List[Tuple[int, int]]]
) -> Tuple[Dict[str, Tuple[int, int]], bool]:
    """匈牙利增广。返回 (匹配, 是否完美)。已确认明细的邻接表只有一条边，自然固定。"""
    match_p: Dict[Tuple[int, int], str] = {}
    match_d: Dict[str, Tuple[int, int]] = {}

    def aug(d: str, seen: set) -> bool:
        for p in adj[d]:
            if p in seen:
                continue
            seen.add(p)
            if p not in match_p or aug(match_p[p], seen):
                match_d[d] = p
                match_p[p] = d
                return True
        return False

    for d in sorted(adj, key=lambda x: (len(adj[x]), x)):
        aug(d, set())
    return match_d, len(match_d) == len(adj)


def _is_unique(
    adj: Dict[str, List[Tuple[int, int]]],
    match_d: Dict[str, Tuple[int, int]],
) -> bool:
    """完美配法唯一，当且仅当以下两条同时成立：

    1. 任何“候选但未匹配”的边指向的路径都已被其他明细占用——否则把该明细
       改配到这条空闲路径即得另一套完整配法（交替路情形）；
    2. 交替有向图（明细 -> 路径 为候选非匹配边，路径 -> 明细 为匹配边）无环——
       有环则沿环交换匹配/非匹配边即得另一套完整配法（交替环情形）。
    """
    match_p = {p: d for d, p in match_d.items()}
    for d, ps in adj.items():
        for p in ps:
            if match_d.get(d) != p and p not in match_p:
                return False

    graph: Dict[Tuple[str, object], List[Tuple[str, object]]] = {}
    for d, ps in adj.items():
        graph[("d", d)] = [("p", p) for p in ps if match_d.get(d) != p]
    for d, p in match_d.items():
        graph.setdefault(("p", p), []).append(("d", d))

    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[Tuple[str, object], int] = {}

    def has_cycle(u: Tuple[str, object]) -> bool:
        color[u] = GRAY
        for v in graph.get(u, []):
            c = color.get(v, WHITE)
            if c == GRAY:
                return True
            if c == WHITE and has_cycle(v):
                return True
        color[u] = BLACK
        return False

    return not any(
        has_cycle(u) for u in list(graph) if color.get(u, WHITE) == WHITE
    )


def _diagnose(boards: List[DetailBoard], match_d: Dict[str, Tuple[int, int]]) -> List[str]:
    """整单无法配齐时，逐条说明原因。"""
    reasons: List[str] = []
    for db in boards:
        d = db.detail
        if db.candidates:
            continue
        filled = []
        if d.attr_line is not None:
            filled.append(f"已填归属行号 {d.attr_line}")
        if d.jur_line is not None:
            filled.append(f"已填归口行号 {d.jur_line}")
        cond = "，".join(filled)
        if db.raw_count == 0:
            reasons.append(
                f"入仓行号 {d.entry_no}：按拨款日没有符合已有字段的有效路径"
                f"（统计单位 {d.unit}{'，' + cond if cond else ''}）"
            )
        else:
            reasons.append(
                f"入仓行号 {d.entry_no}：{db.raw_count} 条候选路径均被其他明细的"
                f"已确认绑定占用"
            )
    for db in boards:
        if db.candidates and db.detail.entry_no not in match_d:
            reasons.append(
                f"入仓行号 {db.detail.entry_no}：{len(db.candidates)} 条候选路径与"
                f"其他明细冲突（同一对归属/归口行只能分给一条明细），整单配不齐"
            )
    return reasons


def build_board(
    grant: Grant,
    details: List[DetailRow],
    paths: List[CandidatePath],
    confirmed: Dict[str, Tuple[int, int]],
) -> Board:
    """按当前已确认绑定重判整单盘面。"""
    taken = {key: eno for eno, key in confirmed.items()}
    path_by_key = {p.key: p for p in paths}

    boards: List[DetailBoard] = []
    adj: Dict[str, List[Tuple[int, int]]] = {}
    for d in details:
        raw = detail_candidates(d, paths)
        if d.entry_no in confirmed:
            key = confirmed[d.entry_no]
            bound = path_by_key.get(key)
            cands = [bound] if bound is not None else []
            boards.append(DetailBoard(d, STATUS_CONFIRMED, cands, bound, len(raw)))
            adj[d.entry_no] = [key]
        else:
            cands = [p for p in raw if p.key not in taken]
            if not cands:
                status, bound = STATUS_NONE, None
            elif len(cands) == 1:
                status, bound = STATUS_UNIQUE, cands[0]
            else:
                status, bound = STATUS_PENDING, None
            boards.append(DetailBoard(d, status, cands, bound, len(raw)))
            adj[d.entry_no] = [p.key for p in cands]

    match, perfect = _max_matching(adj)
    reasons = [] if perfect else _diagnose(boards, match)
    unique = perfect and _is_unique(adj, match)
    return Board(
        grant=grant,
        details=boards,
        path_total=len(paths),
        solvable=perfect,
        unique=unique,
        solution=match if perfect else None,
        reasons=reasons,
    )


# --------------------------------------------------------------------------- #
# 确认 / 撤销（事务性：导致整单无解的确认不生效）
# --------------------------------------------------------------------------- #
class ConfirmRejected(Exception):
    """确认被拒绝（导致整单无法配齐 / 路径被占 / 不符合已有字段等）。"""


def apply_confirm(
    grant: Grant,
    details: List[DetailRow],
    paths: List[CandidatePath],
    confirmed: Dict[str, Tuple[int, int]],
    entry_no: str,
    key: Tuple[int, int],
) -> Dict[str, Tuple[int, int]]:
    """尝试确认一条绑定，返回新的绑定表；不合法则抛 ConfirmRejected。"""
    detail = next((d for d in details if d.entry_no == entry_no), None)
    if detail is None:
        raise ConfirmRejected(f"入仓行号 '{entry_no}' 不存在")
    if entry_no in confirmed:
        raise ConfirmRejected(f"入仓行号 {entry_no} 已有确认绑定，请先撤销")
    holder = next((eno for eno, k in confirmed.items() if k == key), None)
    if holder is not None:
        raise ConfirmRejected(
            f"路径（归属行号 {key[0]}，归口行号 {key[1]}）已确认给入仓行号 {holder}，"
            f"同一对归属和归口行只能分给一条明细"
        )
    path = next((p for p in paths if p.key == key), None)
    if path is None or path not in detail_candidates(detail, paths):
        raise ConfirmRejected(
            f"路径（归属行号 {key[0]}，归口行号 {key[1]}）不符合入仓行号 {entry_no} "
            f"的已有字段，不能确认"
        )

    new_confirmed = dict(confirmed)
    new_confirmed[entry_no] = key
    new_board = build_board(grant, details, paths, new_confirmed)
    if new_board.solvable:
        return new_confirmed

    old_board = build_board(grant, details, paths, confirmed)
    reason = new_board.reasons[0] if new_board.reasons else "整单无法配齐"
    if old_board.solvable:
        raise ConfirmRejected(f"该确认将导致整单无法配齐，已保留原绑定。{reason}")
    # 原本就无法配齐：不是本次确认导致，接受确认但如实提示
    return new_confirmed


def apply_revoke(
    confirmed: Dict[str, Tuple[int, int]],
    entry_no: str,
) -> Dict[str, Tuple[int, int]]:
    """撤销一条确认绑定。"""
    if entry_no not in confirmed:
        raise ConfirmRejected(f"入仓行号 {entry_no} 没有已确认的绑定")
    new_confirmed = dict(confirmed)
    del new_confirmed[entry_no]
    return new_confirmed


# --------------------------------------------------------------------------- #
# 会话（job）存储：文件 + 排他锁，多 worker / 多线程下读改写原子
# --------------------------------------------------------------------------- #
def grant_to_dict(g: Grant) -> dict:
    return {
        "grant_no": g.grant_no,
        "project": g.project,
        "grant_date": g.grant_date.isoformat(),
        "amount": g.amount,
    }


def grant_from_dict(d: dict) -> Grant:
    return Grant(
        d["grant_no"], d["project"], date.fromisoformat(d["grant_date"]),
        d["amount"], 0,
    )


def detail_to_dict(d: DetailRow) -> dict:
    return {
        "entry_no": d.entry_no,
        "grant_no": d.grant_no,
        "unit": d.unit,
        "attr_line": d.attr_line,
        "jur_line": d.jur_line,
        "csv_line": d.csv_line,
    }


def detail_from_dict(d: dict) -> DetailRow:
    return DetailRow(
        d["entry_no"], d["grant_no"], d["unit"],
        d["attr_line"], d["jur_line"], d["csv_line"],
    )


def path_to_dict(p: CandidatePath) -> dict:
    return {
        "attr_line": p.attr_line,
        "jur_line": p.jur_line,
        "college": p.college,
        "unit": p.unit,
    }


def path_from_dict(d: dict) -> CandidatePath:
    return CandidatePath(d["attr_line"], d["jur_line"], d["college"], d["unit"])


def board_from_payload(payload: dict) -> Board:
    """从会话快照重建盘面。"""
    grant = grant_from_dict(payload["grant"])
    details = [detail_from_dict(d) for d in payload["details"]]
    paths = [path_from_dict(p) for p in payload["paths"]]
    confirmed = {eno: tuple(k) for eno, k in payload["confirmed"].items()}
    return build_board(grant, details, paths, confirmed)


class JobStore:
    """文件型会话存储：每个补录会话一个 JSON 文件，fcntl 排他锁保证原子。"""

    def __init__(self, directory: str, ttl_seconds: int = JOB_TTL_SECONDS):
        self.dir = directory
        self.ttl = ttl_seconds
        os.makedirs(directory, exist_ok=True)

    def _path(self, job_id: str) -> str:
        if not job_id or not all(c in "0123456789abcdef" for c in job_id):
            raise ValueError("非法会话标识")
        return os.path.join(self.dir, f"{job_id}.json")

    def _sweep(self) -> None:
        cutoff = time.time() - self.ttl
        try:
            for name in os.listdir(self.dir):
                full = os.path.join(self.dir, name)
                try:
                    if name.endswith(".json") and os.path.getmtime(full) < cutoff:
                        os.unlink(full)
                except OSError:
                    pass
        except OSError:
            pass

    def create(self, payload: dict) -> str:
        self._sweep()
        job_id = uuid.uuid4().hex
        payload = dict(payload, version=0, created=time.time())
        tmp = self._path(job_id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, self._path(job_id))
        return job_id

    def get(self, job_id: str) -> Optional[dict]:
        try:
            with open(self._path(job_id), "r", encoding="utf-8") as fh:
                fcntl.flock(fh, fcntl.LOCK_SH)
                try:
                    return json.load(fh)
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
        except (OSError, ValueError):
            return None

    def update(self, job_id: str, fn):
        """在排他锁内读-改-写：fn(payload) -> (新 payload 或 None, 结果)。"""
        try:
            fh = open(self._path(job_id), "r+", encoding="utf-8")
        except (OSError, ValueError):
            return None
        with fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                payload = json.load(fh)
            except ValueError:
                return None
            new_payload, result = fn(payload)
            if new_payload is not None:
                fh.seek(0)
                fh.truncate()
                json.dump(new_payload, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            return result
