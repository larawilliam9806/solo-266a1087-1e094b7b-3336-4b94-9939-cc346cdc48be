"""提交清单与人口表的请求模型和合法性校验。

非法提交（不满足以下任一约束）在入库存储前即被拒绝（HTTP 422），
因此不会影响已发布的旧版本或已登记的人口表。
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from .population import PopRow, find_overlap

MAX_BATCH = 40  # 整批提交最多 40 条


class CaseIn(BaseModel):
    case_id: str = Field(min_length=1, max_length=64, description="唯一病例号")
    age: int = Field(ge=0, le=100, description="年龄 0~100 岁")
    zip: str = Field(pattern=r"^\d{5}$", description="五位数字邮编")
    disease: str = Field(min_length=1, max_length=128, description="病种")

    @field_validator("case_id", "disease", mode="before")
    @classmethod
    def _strip(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v


class SubmissionIn(BaseModel):
    k: int = Field(ge=2, le=5, description="k-匿名参数，2~5")
    cases: list[CaseIn] = Field(min_length=1, max_length=MAX_BATCH)

    @field_validator("cases")
    @classmethod
    def _unique_case_ids(cls, v: list[CaseIn]) -> list[CaseIn]:
        ids = [c.case_id for c in v]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"病例号重复: {', '.join(dupes)}")
        return v


class PopulationRowIn(BaseModel):
    """一行人口数据：年龄闭区间 × 邮编前缀 × 非负人数。"""
    age_lo: int = Field(ge=0, le=100, description="年龄下限，0~100 岁（含）")
    age_hi: int = Field(ge=0, le=100, description="年龄上限，0~100 岁（含）")
    zip_prefix: str | None = Field(
        default=None,
        pattern=r"^\d{1,5}$",
        description="1~5 位邮编前缀；null/缺省表示全部邮区",
    )
    population: int = Field(ge=0, description="非负整数人数")

    @model_validator(mode="after")
    def _age_order(self) -> "PopulationRowIn":
        if self.age_lo > self.age_hi:
            raise ValueError(f"年龄区间非法：下限 {self.age_lo} 大于上限 {self.age_hi}")
        return self

    def to_row(self) -> PopRow:
        return PopRow(self.age_lo, self.age_hi, self.zip_prefix or "", self.population)


class PopulationIn(BaseModel):
    """为某个病例版本整表登记人口；表内各行覆盖范围不得重叠。"""
    version: int = Field(ge=1, description="人口表对应的病例提交版本号")
    rows: list[PopulationRowIn] = Field(min_length=1, description="至少一行人口数据")

    @model_validator(mode="after")
    def _no_overlap(self) -> "PopulationIn":
        parsed = [r.to_row() for r in self.rows]
        pair = find_overlap(parsed)
        if pair is not None:
            i, j = pair
            a, b = parsed[i], parsed[j]
            raise ValueError(
                f"人口行覆盖范围重叠：第 {i + 1} 行与第 {j + 1} 行"
                f"（年龄 [{a.age_lo},{a.age_hi}]/[{b.age_lo},{b.age_hi}]，"
                f"邮编前缀 {a.zip_prefix or '空'}/{b.zip_prefix or '空'}）"
            )
        return self
