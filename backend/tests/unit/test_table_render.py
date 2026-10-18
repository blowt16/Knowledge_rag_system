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
    # 单值学分，避免走多值展开；专门验证未命名列用 raw 值，不把填充的“其他”写进句子。
    raw = ["文化艺术与体育", "文体竞赛", "获国家级奖项一、二、三等奖", "",
           "15分/个", "团委、体育部、各学院", "由活动主办单位通过系统统一导入赋予学分"]
    filled = ["文化艺术与体育", "文体竞赛", "获国家级奖项一、二、三等奖", "其他",
              "15分/个", "团委、体育部、各学院", "由活动主办单位通过系统统一导入赋予学分"]
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


# ---- 方案 1 Task 2：多值展开接进自然句 --------------------------------

P8_H = ["课程类别", "课程项目", "课程内容", "参考学分", "开课单位", "获得学分方式"]


def test_render_doc12_multi_value_p10():
    raw = ["文化艺术与体育", "文体竞赛", "获国家级奖项一、二、三等奖", "",
           "18、15、12分/项", "团委、体育部、各学院",
           "由活动主办单位通过系统统一导入赋予学分"]
    filled = ["文化艺术与体育", "文体竞赛", "获国家级奖项一、二、三等奖", "其他",
              "18、15、12分/项", "团委、体育部、各学院",
              "由活动主办单位通过系统统一导入赋予学分"]
    assert render_sentence(raw, filled, DOC12_H) == (
        "文化艺术与体育 - 文体竞赛："
        "国家级一等奖18分/项；国家级二等奖15分/项；国家级三等奖12分/项，"
        "团委、体育部、各学院（由活动主办单位通过系统统一导入赋予学分）。"
    )


def test_render_doc12_multi_value_p8():
    raw = ["", "入选思想政治类宣讲团", "区级、校级、院级",
           "15、10、5分/次", "团委、各学院团委", "方式"]
    assert render_sentence(raw, raw, P8_H) == (
        "入选思想政治类宣讲团：区级15分/次；校级10分/次；院级5分/次，"
        "团委、各学院团委（方式）。"
    )


def test_render_doc12_multi_value_p9():
    raw = ["科技学术与创新创业", "“挑战杯”…A类竞赛", "获国家级奖项",
           "特等奖（金奖）、一、二、三等奖", "30、20、15、10分/项", "各学院团委",
           "学生自主申请，由学院团委统一认定"]
    sentence = render_sentence(raw, raw, DOC12_H)
    assert "国家级特等奖（金奖）30分/项" in sentence
    assert "参考学分" not in sentence, "展开后不得重复分值字段"
    assert "特等奖（金奖）、一、二、三等奖" not in sentence, "展开后不得重复等级格"
