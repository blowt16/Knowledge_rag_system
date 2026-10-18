"""自然句表格渲染（方案乙 Task 3）。"""
from __future__ import annotations

from app.ingestion.table_render import render_sentence

DOC12_H = ["课程类别", "课程项目", "课程内容", None, "参考学分", "开课单位", "获得学分方式"]


def test_render_cet6_sentence():
    raw = [
        "技能特长", "语言类证书", "大学英语及专业英语水平测试",
        "大学英语六级", "5分/项", "各学院",
        "大学英语四级、艺术类专业英语三级、大学英语六级由各学院通过系统统一导入赋予学分",
    ]
    assert render_sentence(raw, raw, DOC12_H) == (
        "技能特长 - 语言类证书 - 大学英语及专业英语水平测试：大学英语六级，"
        "参考学分 5分/项，各学院（大学英语四级、艺术类专业英语三级、"
        "大学英语六级由各学院通过系统统一导入赋予学分）。"
    )


def test_render_p8_without_unnamed_column():
    raw = [
        "思想政治与道德修养", "社会主义核心价值观教育", "完成相应培训课时",
        "5分/学期", "学工部（处）、各学院",
        "每学期结束，由各学院团委通过系统管理端统一导入赋予学分",
    ]
    h = ["课程类别", "课程项目", "课程内容", "参考学分", "开课单位", "获得学分方式"]
    assert render_sentence(raw, raw, h) == (
        "思想政治与道德修养 - 社会主义核心价值观教育：完成相应培训课时，"
        "参考学分 5分/学期，学工部（处）、各学院"
        "（每学期结束，由各学院团委通过系统管理端统一导入赋予学分）。"
    )


def test_render_uses_raw_value_for_unnamed_column():
    raw = ["文化艺术与体育", "文体竞赛", "获国家级奖项一、二、三等奖", "",
           "18、15、12分/项", "团委、体育部、各学院", "由活动主办单位通过系统统一导入赋予学分"]
    filled = ["文化艺术与体育", "文体竞赛", "获国家级奖项一、二、三等奖", "其他",
              "18、15、12分/项", "团委、体育部、各学院", "由活动主办单位通过系统统一导入赋予学分"]
    sentence = render_sentence(raw, filled, DOC12_H)
    assert "其他" not in sentence
    assert "获国家级奖项一、二、三等奖" in sentence


def test_render_no_leading_dash_when_category_and_project_empty():
    raw = ["", "", "院级", "学生会执行主席、团委兼职副书记",
           "8分/学年", "各学院团委", "由各学院团委通过系统统一导入赋予学分"]
    sentence = render_sentence(raw, raw, DOC12_H)
    assert not sentence.startswith(" - ")
    assert sentence.startswith("院级：学生会执行主席")


def test_render_generic_doc11():
    h = ["文章等级", "第1 作者或通信作者", "第2 作者", "第3 作者"]
    row = ["SCI", "100", "80", "60"]
    assert render_sentence(row, row, h) == (
        "文章等级是SCI，第1 作者或通信作者是100，第2 作者是80，第3 作者是60。"
    )


def test_render_escapes_table_markers():
    h = ["甲列", "乙列"]
    row = ["含｜竖线的值", "含【表头】标记的值"]
    sentence = render_sentence(row, row, h)
    assert "｜" not in sentence
    assert "【" not in sentence and "】" not in sentence


def test_render_empty_row():
    assert render_sentence([], [], []) == ""
