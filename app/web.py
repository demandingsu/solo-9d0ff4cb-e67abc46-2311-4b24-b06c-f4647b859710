"""Flask 网页：上传三份 CSV，展示核对结果；输入有误整次退回并定位文件与行号。

补录入口：上传三份 CSV + 同一单号的入仓明细，按拨款日取符合明细已有字段的
路径，人工确认 / 撤销后整单重判，唯一完整配法时可下载补齐来源行号的 CSV。
"""

from __future__ import annotations

import csv
import io
import os
import tempfile

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from .backfill import (
    DETAIL_COLUMNS,
    DETAIL_FILE,
    MAX_DETAILS,
    MAX_PATHS,
    STATUS_CONFIRMED,
    STATUS_NONE,
    STATUS_PENDING,
    STATUS_UNIQUE,
    ConfirmRejected,
    JobStore,
    apply_confirm,
    apply_revoke,
    board_from_payload,
    detail_from_dict,
    detail_to_dict,
    grant_from_dict,
    grant_to_dict,
    load_context,
    path_from_dict,
    path_to_dict,
)
from .reconcile import InputError, reconcile

MAX_UPLOAD_BYTES = 16 * 1024 * 1024
SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
JOB_DIR = os.environ.get(
    "BACKFILL_JOB_DIR",
    os.path.join(tempfile.gettempdir(), "backfill-jobs"),
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
# flash 消息签名用；生产环境可用 FLASK_SECRET_KEY 覆盖
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "grant-reconcile-dev-key")

job_store = JobStore(JOB_DIR)


@app.template_filter("fen")
def format_fen(value: int) -> str:
    """分 -> 元，千分位，保留两位小数。"""
    return f"{value / 100:,.2f}"


@app.errorhandler(413)
def too_large(_exc):
    return render_template(
        "error.html",
        errors=[(None, None, f"上传内容超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")],
    ), 413


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/check")
def check():
    files = {}
    missing = []
    for field, label in (
        ("grants", "拨款表"),
        ("attributions", "归属表"),
        ("jurisdictions", "归口表"),
    ):
        upload = request.files.get(field)
        if upload is None or upload.filename == "":
            missing.append((label, None, "未上传文件"))
            continue
        files[field] = (upload.filename, upload.read())
    if missing:
        return render_template("error.html", errors=missing), 400

    try:
        report = reconcile(
            files["grants"][1],
            files["attributions"][1],
            files["jurisdictions"][1],
        )
    except InputError as exc:
        # 本次核对整次退回，不落库、不产生部分结果
        return render_template("error.html", errors=exc.errors), 400

    return render_template(
        "report.html",
        report=report,
        file_names={
            "拨款表": files["grants"][0],
            "归属表": files["attributions"][0],
            "归口表": files["jurisdictions"][0],
        },
    )


@app.get("/samples/<path:filename>")
def sample(filename: str):
    return send_from_directory(SAMPLE_DIR, filename, as_attachment=True)


# --------------------------------------------------------------------------- #
# 补录来源行号
# --------------------------------------------------------------------------- #
def _read_uploads(fields):
    """读取上传文件，返回 (files, missing)：files[field] = (文件名, 内容)。"""
    files, missing = {}, []
    for field, label in fields:
        upload = request.files.get(field)
        if upload is None or upload.filename == "":
            missing.append((label, None, "未上传文件"))
            continue
        files[field] = (upload.filename, upload.read())
    return files, missing


@app.get("/backfill")
def backfill_upload():
    return render_template(
        "backfill_upload.html",
        max_details=MAX_DETAILS,
        max_paths=MAX_PATHS,
    )


@app.post("/backfill")
def backfill_create():
    files, missing = _read_uploads(
        (
            ("grants", "拨款表"),
            ("attributions", "归属表"),
            ("jurisdictions", "归口表"),
            ("details", DETAIL_FILE),
        )
    )
    if missing:
        return render_template("error.html", errors=missing), 400

    try:
        grant, details, paths = load_context(
            files["grants"][1],
            files["attributions"][1],
            files["jurisdictions"][1],
            files["details"][1],
        )
    except InputError as exc:
        return render_template("error.html", errors=exc.errors), 400

    job_id = job_store.create(
        {
            "grant": grant_to_dict(grant),
            "details": [detail_to_dict(d) for d in details],
            "paths": [path_to_dict(p) for p in paths],
            "confirmed": {},
            "files": {
                "拨款表": files["grants"][0],
                "归属表": files["attributions"][0],
                "归口表": files["jurisdictions"][0],
                DETAIL_FILE: files["details"][0],
            },
        }
    )
    return redirect(url_for("backfill_view", job_id=job_id))


def _render_board(job_id: str, payload: dict, **extra):
    board = board_from_payload(payload)
    return render_template(
        "backfill.html",
        job_id=job_id,
        version=payload["version"],
        board=board,
        file_names=payload.get("files", {}),
        statuses={
            "confirmed": STATUS_CONFIRMED,
            "unique": STATUS_UNIQUE,
            "pending": STATUS_PENDING,
            "none": STATUS_NONE,
        },
        **extra,
    )


@app.get("/backfill/<job_id>")
def backfill_view(job_id: str):
    payload = job_store.get(job_id)
    if payload is None:
        return render_template(
            "notice.html",
            title="会话不存在或已过期",
            message="该补录会话不存在或已超过保留时长，请重新上传四份 CSV。",
            back_url=url_for("backfill_upload"),
            back_label="← 返回补录上传",
        ), 404
    return _render_board(job_id, payload)


def _stale_response(job_id: str):
    return render_template(
        "notice.html",
        title="绑定已变化，请刷新",
        message="在你操作之前，该单的确认绑定已被其他提交修改（可能来自另一个页签）。"
        "本次提交未生效，请刷新页面查看最新状态后再操作。",
        back_url=url_for("backfill_view", job_id=job_id),
        back_label="刷新查看最新状态",
    ), 409


@app.post("/backfill/<job_id>/confirm")
def backfill_confirm(job_id: str):
    form = request.form
    try:
        version = int(form.get("version", ""))
        attr_line = int(form.get("attr_line", ""))
        jur_line = int(form.get("jur_line", ""))
        entry_no = form["entry_no"]
    except (KeyError, ValueError):
        return render_template(
            "notice.html", title="提交参数有误",
            message="确认请求缺少必要参数或格式不正确，请刷新页面后重试。",
            back_url=url_for("backfill_view", job_id=job_id),
            back_label="← 返回核对页",
        ), 400

    def txn(payload):
        if payload["version"] != version:
            return None, "stale"
        grant = grant_from_dict(payload["grant"])
        details = [detail_from_dict(d) for d in payload["details"]]
        paths = [path_from_dict(p) for p in payload["paths"]]
        confirmed = {eno: tuple(k) for eno, k in payload["confirmed"].items()}
        try:
            new_confirmed = apply_confirm(
                grant, details, paths, confirmed, entry_no, (attr_line, jur_line)
            )
        except ConfirmRejected as exc:
            return None, ("rejected", str(exc))
        payload["confirmed"] = {k: list(v) for k, v in new_confirmed.items()}
        payload["version"] += 1
        return payload, ("ok", entry_no)

    result = job_store.update(job_id, txn)
    if result is None:
        return render_template(
            "notice.html", title="会话不存在或已过期",
            message="该补录会话不存在或已超过保留时长，请重新上传四份 CSV。",
            back_url=url_for("backfill_upload"),
            back_label="← 返回补录上传",
        ), 404
    if result == "stale":
        return _stale_response(job_id)
    kind, payload_msg = result
    if kind == "rejected":
        flash(payload_msg, "err")
    else:
        flash(f"已确认入仓行号 {payload_msg} 的绑定，其余明细已随之重判。", "ok")
    return redirect(url_for("backfill_view", job_id=job_id))


@app.post("/backfill/<job_id>/revoke")
def backfill_revoke(job_id: str):
    form = request.form
    try:
        version = int(form.get("version", ""))
        entry_no = form["entry_no"]
    except (KeyError, ValueError):
        return render_template(
            "notice.html", title="提交参数有误",
            message="撤销请求缺少必要参数或格式不正确，请刷新页面后重试。",
            back_url=url_for("backfill_view", job_id=job_id),
            back_label="← 返回核对页",
        ), 400

    def txn(payload):
        if payload["version"] != version:
            return None, "stale"
        confirmed = {eno: tuple(k) for eno, k in payload["confirmed"].items()}
        try:
            new_confirmed = apply_revoke(confirmed, entry_no)
        except ConfirmRejected as exc:
            return None, ("rejected", str(exc))
        payload["confirmed"] = {k: list(v) for k, v in new_confirmed.items()}
        payload["version"] += 1
        return payload, ("ok", entry_no)

    result = job_store.update(job_id, txn)
    if result is None:
        return render_template(
            "notice.html", title="会话不存在或已过期",
            message="该补录会话不存在或已超过保留时长，请重新上传四份 CSV。",
            back_url=url_for("backfill_upload"),
            back_label="← 返回补录上传",
        ), 404
    if result == "stale":
        return _stale_response(job_id)
    kind, payload_msg = result
    if kind == "rejected":
        flash(payload_msg, "err")
    else:
        flash(f"已撤销入仓行号 {payload_msg} 的确认绑定，其余明细已随之重判。", "ok")
    return redirect(url_for("backfill_view", job_id=job_id))


@app.get("/backfill/<job_id>/download")
def backfill_download(job_id: str):
    payload = job_store.get(job_id)
    if payload is None:
        return render_template(
            "notice.html", title="会话不存在或已过期",
            message="该补录会话不存在或已超过保留时长，请重新上传四份 CSV。",
            back_url=url_for("backfill_upload"),
            back_label="← 返回补录上传",
        ), 404
    board = board_from_payload(payload)
    if not (board.solvable and board.unique):
        return render_template(
            "notice.html",
            title="暂不能下载",
            message="按当前绑定，整单的完整配法不唯一（或无法配齐）。"
            "请继续确认待核明细，直至存在唯一完整配法后再下载。",
            back_url=url_for("backfill_view", job_id=job_id),
            back_label="← 返回核对页",
        ), 409

    details = [detail_from_dict(d) for d in payload["details"]]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(DETAIL_COLUMNS)
    for d in details:
        attr_line, jur_line = board.solution[d.entry_no]
        writer.writerow([d.entry_no, d.grant_no, d.unit, attr_line, jur_line])
    data = buf.getvalue().encode("utf-8-sig")
    filename = f"backfilled_{board.grant.grant_no}.csv"
    return Response(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
