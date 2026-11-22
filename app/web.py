"""Flask 网页：上传三份 CSV，展示核对结果；输入有误整次退回并定位文件与行号。"""

from __future__ import annotations

import os

from flask import Flask, render_template, request, send_from_directory

from .reconcile import InputError, reconcile

MAX_UPLOAD_BYTES = 16 * 1024 * 1024
SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


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
