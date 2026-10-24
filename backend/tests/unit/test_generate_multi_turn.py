"""多轮用例生成：窗口构建、逐句原文校验、多轮结构校验（§7.3）。"""

from __future__ import annotations

from app.eval import generate

CHUNKS = [
    {"chunk_id": f"c{i}", "text": f"第{i}段原文内容。",
     "metadata": {"chunk_index": i, "page": i + 1,
                  "char_start": i * 8, "char_end": (i + 1) * 8}}
    for i in range(5)
]
TEXT = "第0段原文内容。第1段原文内容。第2段原文内容。第3段原文内容。第4段原文内容。"

WINDOW = ("退役复学学生可以申请免修课程。免修成绩按85分记录。"
          "退役复学后所修课程成绩可乘以1.5，最高不超过85分。学校应当审核申请材料。")


def test_windows_are_adjacent_and_merge_by_offsets():
    windows = generate.build_windows(CHUNKS, 3, 2, normalized_text=TEXT)
    assert len(windows) == 2
    assert windows[0]["text"] == TEXT[0:24]
    assert windows[1]["text"] == TEXT[16:40]


def test_windows_overlap_when_not_enough_starts():
    windows = generate.build_windows(CHUNKS, 3, 5, normalized_text=TEXT)
    assert len(windows) == 5
    assert all(w["text"] for w in windows)


def test_window_falls_back_to_overlap_merge_without_offsets():
    chunks = [{"chunk_id": "c1", "text": "ABCDE", "metadata": {"chunk_index": 0}},
              {"chunk_id": "c2", "text": "CDEFG", "metadata": {"chunk_index": 1}}]
    windows = generate.build_windows(chunks, 2, 1)
    assert windows[0]["text"] == "ABCDEFG"


def test_validate_accepts_single_clause_and_splicing():
    assert generate.validate_ground_truth("免修成绩按85分记录。", WINDOW)[0]
    assert generate.validate_ground_truth(
        "退役复学学生可以申请免修课程；免修成绩按85分记录。", WINDOW)[0]


def test_validate_rejects_rewrites_and_numbers():
    assert not generate.validate_ground_truth(
        "退役复学学生能申请不用上课。", WINDOW)[0]
    assert not generate.validate_ground_truth(
        "免修成绩按90分记录。", WINDOW)[0]


def test_validate_rejects_connector_not_in_window():
    assert not generate.validate_ground_truth(
        "此外，免修成绩按85分记录。", WINDOW)[0]


def test_validate_rejects_more_than_three_substantive_clauses():
    answer = ("退役复学学生可以申请免修课程。免修成绩按85分记录。"
              "退役复学后所修课程成绩可乘以1.5，最高不超过85分。"
              "学校应当审核申请材料。")
    assert not generate.validate_ground_truth(answer, WINDOW)[0]


def test_validate_rejects_thin_generalization():
    assert not generate.validate_ground_truth("可以申请。", WINDOW)[0]


def test_validate_multi_accepts_two_to_four_turns():
    data = {"turns": [
        {"question": "退役复学可以申请什么？",
         "ground_truth": "退役复学学生可以申请免修课程。"},
        {"question": "那成绩怎么记？", "ground_truth": "免修成绩按85分记录。"},
    ]}
    got = generate.validate_multi_dialogue(
        data, "退役复学学生可以申请免修课程。免修成绩按85分记录。", "某办法", 2)
    assert got is not None
    assert got["turns"][0]["expected_route"] == "knowledge"
    assert got["turns"][0]["should_clarify"] == 0
    assert got["turns"][0]["expected_document"] == "某办法"


def test_validate_multi_rejects_first_turn_pronoun():
    data = {"turns": [
        {"question": "这个可以申请什么？",
         "ground_truth": "退役复学学生可以申请免修课程。"},
        {"question": "那成绩怎么记？", "ground_truth": "免修成绩按85分记录。"},
    ]}
    assert generate.validate_multi_dialogue(
        data, "退役复学学生可以申请免修课程。免修成绩按85分记录。",
        "某办法", 2) is None


def test_validate_multi_rejects_duplicate_answers():
    data = {"turns": [
        {"question": "第一问", "ground_truth": "免修成绩按85分记录。"},
        {"question": "第二问", "ground_truth": "免修成绩按85分记录。"},
    ]}
    assert generate.validate_multi_dialogue(
        data, "免修成绩按85分记录。", "某办法", 2) is None
