"""空白申请表判定（方案 3 Task 1）。"""
from __future__ import annotations

from pathlib import Path

import fitz

from app.ingestion.tables import extract_page_tables
from app.ingestion.table_rules import is_application_form

ROOT = Path(__file__).resolve().parents[3]
DOC11 = ROOT / "corpus/guet/11_创新创业拓展课程学分认定管理办法_桂电教2026-13号.pdf"
DOC12 = ROOT / "corpus/guet/12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf"


def test_form_table_detected():
    rows = [
        ["学号", "", "", "", "专业", "", "", ""],
        ["姓名", "", "", "", "课号", "", "", ""],
        ["序号", "类别", "", "认定内容", "", "成绩", "", "审核人签字"],
        ["例1", "科研成果", "", "论文", "", "25", "", ""],
        ["例2", "科研成果", "", "专利", "", "10", "", ""],
        ["例3", "科研训练", "", "大创", "", "20", "", ""],
        ["合计", "", "", "", "", "77", "", ""],
    ]
    assert is_application_form(rows) is True


def test_normal_table_with_name_and_major_not_dropped():
    rows = [
        ["姓名", "专业", "成绩"],
        ["张三", "计算机", "90"],
    ]
    assert is_application_form(rows) is False


def test_form_words_without_structure_not_dropped():
    rows = [
        ["学号", "姓名", "备注"],
        ["001", "张三", "正常表"],
    ]
    assert is_application_form(rows) is False


def test_only_doc11_p8_is_form():
    for path, expected_pages in ((DOC11, {8}), (DOC12, set())):
        if not path.exists():
            continue
        doc = fitz.open(path)
        try:
            for page_no in range(1, doc.page_count + 1):
                page = doc.load_page(page_no - 1)
                for t in extract_page_tables(page):
                    assert is_application_form(t.rows) == (page_no in expected_pages), (
                        f"{path.name} p{page_no} 判定不符")
        finally:
            doc.close()
