"""人口表校验与可见分组的精确拼配逻辑。

人口行描述一个矩形覆盖范围：年龄闭区间 [age_lo, age_hi]（源数据口径 0~100 岁）
乘以邮编前缀对应的五位邮编集合（空前缀 = 全部邮区）。

报告分组同样是一个矩形（年龄段 × 邮区前缀）。只有当若干**完整**人口行
（不允许拆行估算）恰好拼满分组矩形、且总人数大于零时，才返回人口数与
每万人报告病例数；否则按下列原因标明：

- missing_population_table 缺人口表：当前病例版本尚未登记人口表；
- coverage_insufficient   覆盖不足：人口行无法恰好拼满该组范围；
- population_spans_groups 人口口径跨组：存在跨越本组边界的人口行；
- zero_population         总人数为零：拼满该组的人口行总人数为零。
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_AGE = 100   # 源数据年龄口径：0~100 岁闭区间
ZIP_LEN = 5     # 邮编固定五位


@dataclass(frozen=True)
class PopRow:
    age_lo: int
    age_hi: int
    zip_prefix: str       # "" 表示全部邮区，否则为 1~5 位数字前缀
    population: int


@dataclass(frozen=True)
class GroupMatch:
    ok: bool
    population: int | None
    reason_code: str | None
    reason: str | None


REASONS: dict[str, str] = {
    "missing_population_table": "缺人口表：当前病例版本尚未登记人口表",
    "coverage_insufficient": "覆盖不足：人口行无法恰好拼满该组的年龄与邮区范围",
    "population_spans_groups": "人口口径跨组：存在跨越本组边界的人口行，不能拆行估算",
    "zero_population": "总人数为零：拼成该组的人口行总人数为零",
}


def _fail(code: str) -> GroupMatch:
    return GroupMatch(False, None, code, REASONS[code])


def zip_set_size(prefix: str) -> int:
    """该前缀覆盖的五位邮编数量：空前缀覆盖全部 10^5 个。"""
    return 10 ** (ZIP_LEN - len(prefix))


def row_area(row: PopRow) -> int:
    """人口行覆盖的（年龄 × 五位邮编）单位格数量。"""
    return (row.age_hi - row.age_lo + 1) * zip_set_size(row.zip_prefix)


def age_intersects(a_lo: int, a_hi: int, b_lo: int, b_hi: int) -> bool:
    """两个闭区间是否相交。"""
    return a_lo <= b_hi and b_lo <= a_hi


def zip_intersects(p1: str, p2: str) -> bool:
    """两个邮编前缀对应的邮编集合是否相交。

    空前缀是一切前缀的前缀（覆盖全部邮区），因此“一个前缀是另一个的前缀”
    即为相交的充要条件。
    """
    return p1.startswith(p2) or p2.startswith(p1)


def zip_contained(inner: str, outer: str) -> bool:
    """inner 的邮编集合是否包含于 outer 的邮编集合（outer 为空时恒真）。"""
    return inner.startswith(outer)


def find_overlap(rows: list[PopRow]) -> tuple[int, int] | None:
    """返回第一对覆盖范围重叠的行下标（0 基）；无重叠返回 None。

    二维矩形重叠 = 年龄闭区间相交 且 邮编集合相交。
    """
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if (age_intersects(a.age_lo, a.age_hi, b.age_lo, b.age_hi)
                    and zip_intersects(a.zip_prefix, b.zip_prefix)):
                return i, j
    return None


def match_group(rows: list[PopRow],
                age_lo: int, age_hi: int, zip_prefix: str) -> GroupMatch:
    """判断人口行能否恰好拼满一个可见分组。

    分组矩形：年龄 [age_lo, age_hi]（已按 0~100 源数据口径截断）×
    邮编前缀 zip_prefix（"" = 全部邮区）。
    """
    intersecting = [
        r for r in rows
        if age_intersects(r.age_lo, r.age_hi, age_lo, age_hi)
        and zip_intersects(r.zip_prefix, zip_prefix)
    ]
    if not intersecting:
        return _fail("coverage_insufficient")

    # 任何与本组相交的行都必须完整落在组内，否则其口径跨越了组边界，
    # 而人口行不允许拆开估算。表内各行互不重叠，因此跨行的存在也使得
    # 其余行不可能恰好拼满本组。
    for r in intersecting:
        age_inside = r.age_lo >= age_lo and r.age_hi <= age_hi
        zip_inside = zip_contained(r.zip_prefix, zip_prefix)
        if not (age_inside and zip_inside):
            return _fail("population_spans_groups")

    # 相交行互不重叠且全部包含于分组矩形：面积之和等于分组面积
    # 当且仅当它们恰好拼满整个分组（无缺口、无越界）。
    target_area = (age_hi - age_lo + 1) * zip_set_size(zip_prefix)
    covered_area = sum(row_area(r) for r in intersecting)
    if covered_area != target_area:
        return _fail("coverage_insufficient")

    total = sum(r.population for r in intersecting)
    if total <= 0:
        return _fail("zero_population")
    return GroupMatch(True, total, None, None)
