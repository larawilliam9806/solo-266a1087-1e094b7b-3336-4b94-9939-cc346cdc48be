"""人口行拼配与重叠校验的单元测试。"""
import pytest

from app.population import (
    PopRow,
    find_overlap,
    match_group,
    row_area,
    zip_intersects,
    zip_set_size,
)


def pr(lo, hi, prefix, pop):
    return PopRow(lo, hi, prefix, pop)


# --------------------------------------------------------------- 基础几何工具
def test_zip_set_size_and_area():
    assert zip_set_size("") == 100_000
    assert zip_set_size("123") == 100
    assert zip_set_size("12345") == 1
    assert row_area(pr(0, 4, "", 1)) == 5 * 100_000
    assert row_area(pr(20, 24, "111", 1)) == 5 * 100


def test_zip_prefix_intersection():
    assert zip_intersects("", "12345")
    assert zip_intersects("123", "12345")   # 包含关系
    assert zip_intersects("12345", "123")
    assert not zip_intersects("123", "124")  # 同长度不同值
    assert not zip_intersects("12", "13")


def test_find_overlap():
    # 年龄不相交 -> 不算重叠（即使邮编相同）
    assert find_overlap([pr(0, 9, "", 1), pr(10, 19, "", 1)]) is None
    # 邮编不相交 -> 不算重叠（即使年龄相同）
    assert find_overlap([pr(0, 9, "1", 1), pr(0, 9, "2", 1)]) is None
    # 年龄相交 + 邮编前缀包含 -> 重叠
    pair = find_overlap([pr(0, 10, "", 1), pr(5, 15, "123", 1)])
    assert pair == (0, 1)
    # 空前缀与任何前缀在年龄相交时重叠
    pair = find_overlap([pr(0, 100, "", 1), pr(0, 100, "1", 1)])
    assert pair == (0, 1)
    # 相邻闭区间共享端点年龄也算重叠
    pair = find_overlap([pr(0, 5, "1", 1), pr(5, 9, "1", 1)])
    assert pair == (0, 1)


# ----------------------------------------------------------- 单组精确拼配
def test_match_single_full_row_exact():
    # 整组一行恰好覆盖：年龄 20-24 × 前缀 111
    m = match_group([pr(20, 24, "111", 500)], 20, 24, "111")
    assert m.ok and m.population == 500


def test_match_tiling_by_age():
    # 两个年龄分片 + 一个补片，恰好拼满 20-24 × 111
    rows = [pr(20, 22, "111", 300), pr(23, 24, "111", 200)]
    m = match_group(rows, 20, 24, "111")
    assert m.ok and m.population == 500


def test_match_tiling_by_zip():
    # 邮编分片：11100..11149 与 11150..11199 恰好拼成前缀 111
    rows = [pr(20, 24, "1110", 250), pr(20, 24, "1111", 250),
            pr(20, 24, "1112", 250), pr(20, 24, "1113", 250),
            pr(20, 24, "1114", 250), pr(20, 24, "1115", 250),
            pr(20, 24, "1116", 250), pr(20, 24, "1117", 250),
            pr(20, 24, "1118", 250), pr(20, 24, "1119", 250)]
    m = match_group(rows, 20, 24, "111")
    assert m.ok and m.population == 2500


def test_match_empty_prefix_group():
    # 分组邮区为全部（前缀 0 位）：单条全量行恰好拼满
    m = match_group([pr(30, 34, "", 999)], 30, 34, "")
    assert m.ok and m.population == 999


def test_match_unrelated_rows_are_ignored():
    # 与本组完全不相交的行不影响结果
    rows = [pr(0, 19, "999", 10 ** 9), pr(20, 24, "111", 500)]
    m = match_group(rows, 20, 24, "111")
    assert m.ok and m.population == 500


def test_match_coverage_insufficient_no_intersection():
    m = match_group([pr(0, 19, "111", 500)], 20, 24, "111")
    assert not m.ok and m.reason_code == "coverage_insufficient"


def test_match_coverage_insufficient_age_gap():
    # 只覆盖了 20-22，缺 23-24
    m = match_group([pr(20, 22, "111", 300)], 20, 24, "111")
    assert not m.ok and m.reason_code == "coverage_insufficient"


def test_match_coverage_insufficient_zip_gap():
    # 只覆盖 1110 段（一半邮区），缺另一半
    m = match_group([pr(20, 24, "1110", 100)], 20, 24, "111")
    assert not m.ok and m.reason_code == "coverage_insufficient"


def test_match_spans_age_boundary():
    # 人口行年龄超出组边界 -> 口径跨组
    m = match_group([pr(19, 24, "111", 500)], 20, 24, "111")
    assert not m.ok and m.reason_code == "population_spans_groups"


def test_match_spans_zip_boundary():
    # 人口行邮编前缀更粗 -> 口径跨组（不能拆行估算）
    m = match_group([pr(20, 24, "11", 500)], 20, 24, "111")
    assert not m.ok and m.reason_code == "population_spans_groups"


def test_match_spans_both_but_partial_tiling_exists():
    # 即使组内另有正确补片，只要存在相交的跨行即判跨组
    rows = [pr(20, 22, "111", 300), pr(23, 24, "11", 200)]
    m = match_group(rows, 20, 24, "111")
    assert not m.ok and m.reason_code == "population_spans_groups"


def test_match_zero_population():
    rows = [pr(20, 22, "111", 0), pr(23, 24, "111", 0)]
    m = match_group(rows, 20, 24, "111")
    assert not m.ok and m.reason_code == "zero_population"


def test_match_partial_zero_still_ok_when_total_positive():
    rows = [pr(20, 22, "111", 0), pr(23, 24, "111", 200)]
    m = match_group(rows, 20, 24, "111")
    assert m.ok and m.population == 200


def test_match_age_100_truncated_group():
    # 100 岁单年龄组（标签 100-104 截断为 [100,100]）
    m = match_group([pr(100, 100, "001", 7)], 100, 100, "001")
    assert m.ok and m.population == 7
    # 人口行含 101 岁口径本身不允许登记，但 99-100 对 100 组属于跨组
    m2 = match_group([pr(99, 100, "001", 7)], 100, 100, "001")
    assert not m2.ok and m2.reason_code == "population_spans_groups"


def test_match_2d_partial_area_insufficient():
    # 二维各覆盖一半：面积只到 1/4，覆盖不足而非跨组（行都包含于组内）
    rows = [pr(20, 22, "1110", 10)]
    m = match_group(rows, 20, 24, "111")
    assert not m.ok and m.reason_code == "coverage_insufficient"
