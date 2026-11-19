"""人口登记与人口关联报告的 API 测试。"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.models import PopulationIn, SubmissionIn
from app.store import Store


def case(cid, age, zipc, disease):
    return {"case_id": cid, "age": age, "zip": zipc, "disease": disease}


GOOD_CASES = [
    case("C1", 21, "11111", "flu"),
    case("C2", 22, "11112", "flu"),
    case("C3", 23, "11113", "cold"),
    case("C4", 24, "11114", "cold"),
]

# 恰好拼满 GOOD_CASES 发布组（20-24 × 前缀 111）的一行人口
FULL_ROW = {"age_lo": 20, "age_hi": 24, "zip_prefix": "111", "population": 2000}


@pytest.fixture()
def client(tmp_path):
    from app.main import create_app
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def submit(client, cases, k=2):
    return client.post("/api/submissions", json={"k": k, "cases": cases})


def register(client, version, rows):
    return client.post("/api/population", json={"version": version, "rows": rows})


# --------------------------------------------------------------- 基本登记与报告
def test_register_and_report_full_match(client):
    v = submit(client, GOOD_CASES).json()["version"]
    r = register(client, v, [FULL_ROW])
    assert r.status_code == 201
    body = r.json()
    assert body["case_version"] == v and body["rows"] == 1
    assert body["total_population"] == 2000
    pop_version = body["pop_version"]

    rep = client.get("/api/report")
    assert rep.status_code == 200
    data = rep.json()
    assert data["case_version"] == v          # 病例版本号
    assert data["pop_version"] == pop_version  # 人口表自身版本号
    assert len(data["groups"]) == 1
    g = data["groups"][0]
    assert g["age_band"] == "20-24" and g["zip"] == "111**"
    assert g["case_count"] == 4
    assert g["population"] == 2000
    assert g["cases_per_10000"] == 20.0       # 4 / 2000 × 10000
    assert g["reason_code"] is None


def test_rate_rounding(client):
    v = submit(client, GOOD_CASES).json()["version"]
    register(client, v, [{**FULL_ROW, "population": 3}])
    g = client.get("/api/report").json()["groups"][0]
    assert g["population"] == 3
    assert g["cases_per_10000"] == round(40000 / 3, 4)


def test_report_missing_population_table(client):
    v = submit(client, GOOD_CASES).json()["version"]
    data = client.get("/api/report").json()
    assert data["case_version"] == v
    assert data["pop_version"] is None
    g = data["groups"][0]
    assert g["population"] is None and g["cases_per_10000"] is None
    assert g["reason_code"] == "missing_population_table"
    assert g["reason"]


def test_report_404_without_publication(client):
    # 从未发布
    r = client.get("/api/report")
    assert r.status_code == 404
    assert r.json()["state"] == "empty"

    # 提交不可发布清单 -> 撤下，仍不生成报告
    scattered = [
        case("S1", 10, "11111", "flu"),
        case("S2", 50, "22222", "cold"),
        case("S3", 90, "33333", "flu"),
    ]
    submit(client, scattered)
    r = client.get("/api/report")
    assert r.status_code == 404
    assert r.json()["state"] == "withdrawn"
    assert r.json()["reason"]


# --------------------------------------------------------------- 登记合法性校验
def test_overlapping_rows_rejected_422(client):
    v = submit(client, GOOD_CASES).json()["version"]
    rows = [
        {"age_lo": 0, "age_hi": 50, "zip_prefix": None, "population": 100},
        {"age_lo": 40, "age_hi": 60, "zip_prefix": "111", "population": 200},
    ]
    r = register(client, v, rows)
    assert r.status_code == 422
    assert "重叠" in str(r.json())


def test_non_overlapping_rows_accepted(client):
    v = submit(client, GOOD_CASES).json()["version"]
    # 年龄分片相邻但不共享年龄；不同邮编前缀也不重叠
    rows = [
        {"age_lo": 20, "age_hi": 22, "zip_prefix": "111", "population": 300},
        {"age_lo": 23, "age_hi": 24, "zip_prefix": "111", "population": 200},
        {"age_lo": 20, "age_hi": 24, "zip_prefix": "112", "population": 100},
    ]
    assert register(client, v, rows).status_code == 201


@pytest.mark.parametrize("bad_row", [
    {"age_lo": -1, "age_hi": 10, "zip_prefix": "1", "population": 1},     # 年龄 <0
    {"age_lo": 0, "age_hi": 101, "zip_prefix": "1", "population": 1},     # 年龄 >100
    {"age_lo": 30, "age_hi": 20, "zip_prefix": "1", "population": 1},     # 下限>上限
    {"age_lo": 0, "age_hi": 10, "zip_prefix": "123456", "population": 1}, # 前缀超长
    {"age_lo": 0, "age_hi": 10, "zip_prefix": "12a", "population": 1},    # 前缀非数字
    {"age_lo": 0, "age_hi": 10, "zip_prefix": "", "population": 1},       # 空前缀须用 null
    {"age_lo": 0, "age_hi": 10, "zip_prefix": "1", "population": -1},     # 负人数
])
def test_invalid_row_fields_422(client, bad_row):
    v = submit(client, GOOD_CASES).json()["version"]
    assert register(client, v, [bad_row]).status_code == 422


def test_empty_population_table_422(client):
    v = submit(client, GOOD_CASES).json()["version"]
    assert register(client, v, []).status_code == 422


def test_register_unknown_version_422(client):
    r = register(client, 999, [FULL_ROW])
    assert r.status_code == 422
    assert r.json()["reason_code"] == "version_not_found"


def test_null_prefix_means_all_zips(client):
    v = submit(client, GOOD_CASES).json()["version"]
    r = register(client, v, [
        {"age_lo": 20, "age_hi": 24, "zip_prefix": None, "population": 2000}
    ])
    assert r.status_code == 201
    got = client.get(f"/api/population?version={v}").json()
    assert got["rows"][0]["zip_prefix"] is None
    # 全邮区口径对前缀 111 的组属于口径跨组
    g = client.get("/api/report").json()["groups"][0]
    assert g["reason_code"] == "population_spans_groups"


# ----------------------------------------------------- 失败保留与整表替换
def test_failed_registration_keeps_previous(client):
    v = submit(client, GOOD_CASES).json()["version"]
    assert register(client, v, [FULL_ROW]).status_code == 201
    before = client.get(f"/api/population?version={v}").json()

    # 非法登记（重叠）被拒，上次登记保持不变
    bad = [
        {"age_lo": 0, "age_hi": 60, "zip_prefix": None, "population": 1},
        {"age_lo": 50, "age_hi": 70, "zip_prefix": None, "population": 2},
    ]
    assert register(client, v, bad).status_code == 422
    after = client.get(f"/api/population?version={v}").json()
    assert after == before

    # 报告仍可基于上次登记正常生成
    g = client.get("/api/report").json()["groups"][0]
    assert g["population"] == 2000 and g["cases_per_10000"] == 20.0


def test_reregister_replaces_whole_table(client):
    v = submit(client, GOOD_CASES).json()["version"]
    first = register(client, v, [FULL_ROW]).json()["pop_version"]
    second = register(client, v, [
        {"age_lo": 20, "age_hi": 22, "zip_prefix": "111", "population": 3000},
        {"age_lo": 23, "age_hi": 24, "zip_prefix": "111", "population": 1000},
    ]).json()
    assert second["pop_version"] > first
    got = client.get(f"/api/population?version={v}").json()
    assert got["pop_version"] == second["pop_version"]
    assert len(got["rows"]) == 2
    g = client.get("/api/report").json()["groups"][0]
    assert g["population"] == 4000
    assert g["cases_per_10000"] == 10.0


# ----------------------------------------------------- 病例换版后的版本隔离
def test_population_scoped_to_case_version(client):
    v1 = submit(client, GOOD_CASES).json()["version"]
    register(client, v1, [FULL_ROW])
    assert client.get("/api/report").json()["groups"][0]["population"] == 2000

    # 病例换版：新版发布后只认新版登记的人口表
    new_cases = [
        case("D1", 40, "22221", "asthma"),
        case("D2", 41, "22222", "asthma"),
        case("D3", 42, "22223", "flu"),
        case("D4", 43, "22224", "flu"),
    ]
    v2 = submit(client, new_cases).json()["version"]
    rep = client.get("/api/report").json()
    assert rep["case_version"] == v2
    assert rep["pop_version"] is None  # v1 的人口表不会沿用到 v2
    assert rep["groups"][0]["reason_code"] == "missing_population_table"

    # 为新版登记后恢复
    register(client, v2, [
        {"age_lo": 40, "age_hi": 44, "zip_prefix": "222", "population": 800}
    ])
    rep = client.get("/api/report").json()
    assert rep["case_version"] == v2 and rep["pop_version"] is not None
    g = rep["groups"][0]
    assert g["population"] == 800 and g["case_count"] == 4
    assert g["cases_per_10000"] == 50.0

    # 旧版本的人口表仍可单独查询（审计留痕）
    old = client.get(f"/api/population?version={v1}").json()
    assert old["pop_version"] is not None and len(old["rows"]) == 1


# --------------------------------------------------------------- 报告原因码
def test_report_reason_zero_population(client):
    v = submit(client, GOOD_CASES).json()["version"]
    register(client, v, [{**FULL_ROW, "population": 0}])
    g = client.get("/api/report").json()["groups"][0]
    assert g["reason_code"] == "zero_population"
    assert g["population"] is None and g["cases_per_10000"] is None


def test_report_reason_spans_age(client):
    v = submit(client, GOOD_CASES).json()["version"]
    register(client, v, [
        {"age_lo": 19, "age_hi": 24, "zip_prefix": "111", "population": 2000}
    ])
    g = client.get("/api/report").json()["groups"][0]
    assert g["reason_code"] == "population_spans_groups"


def test_report_reason_spans_zip(client):
    v = submit(client, GOOD_CASES).json()["version"]
    register(client, v, [
        {"age_lo": 20, "age_hi": 24, "zip_prefix": "11", "population": 2000}
    ])
    g = client.get("/api/report").json()["groups"][0]
    assert g["reason_code"] == "population_spans_groups"


def test_report_reason_coverage_insufficient(client):
    v = submit(client, GOOD_CASES).json()["version"]
    # 年龄缺 24 岁这一行（行都包含在组内，只是拼不满）
    register(client, v, [
        {"age_lo": 20, "age_hi": 23, "zip_prefix": "111", "population": 2000}
    ])
    g = client.get("/api/report").json()["groups"][0]
    assert g["reason_code"] == "coverage_insufficient"


def test_report_multi_group_independent_status(client):
    cases = [
        case("A1", 21, "11111", "flu"), case("A2", 22, "11112", "cold"),
        case("A3", 23, "11113", "flu"), case("A4", 24, "11114", "cold"),
        case("B1", 31, "22221", "flu"), case("B2", 32, "22222", "cold"),
        case("B3", 33, "22223", "flu"), case("B4", 34, "22224", "cold"),
    ]
    v = submit(client, cases).json()["version"]
    register(client, v, [
        # A 组恰好拼满
        {"age_lo": 20, "age_hi": 24, "zip_prefix": "111", "population": 1000},
        # B 组只覆盖一半年龄 -> 覆盖不足
        {"age_lo": 30, "age_hi": 32, "zip_prefix": "222", "population": 500},
    ])
    groups = client.get("/api/report").json()["groups"]
    by_band = {g["age_band"]: g for g in groups}
    assert set(by_band) == {"20-24", "30-34"}
    assert by_band["20-24"]["population"] == 1000
    assert by_band["20-24"]["cases_per_10000"] == 40.0
    assert by_band["30-34"]["reason_code"] == "coverage_insufficient"
    assert by_band["30-34"]["population"] is None


def test_report_groups_match_publication(client):
    v = submit(client, GOOD_CASES).json()["version"]
    pub = client.get("/api/publication").json()
    rep = client.get("/api/report").json()
    assert rep["case_version"] == pub["version"]
    pub_keys = sorted({(r["age_band"], r["zip"]) for r in pub["rows"]})
    rep_keys = sorted((g["age_band"], g["zip"]) for g in rep["groups"])
    assert pub_keys == rep_keys
    assert sum(g["case_count"] for g in rep["groups"]) == len(pub["rows"])


# --------------------------------------------------------------- 0 与 100 岁边界
def test_age_boundaries_report(client):
    cases = [
        case("A", 0, "00123", "flu"),
        case("B", 100, "00124", "cold"),
        case("C", 0, "00125", "cold"),
        case("D", 100, "00126", "flu"),
    ]
    v = submit(client, cases).json()["version"]
    register(client, v, [
        {"age_lo": 0, "age_hi": 4, "zip_prefix": "001", "population": 100},
        {"age_lo": 100, "age_hi": 100, "zip_prefix": "001", "population": 100},
    ])
    groups = {g["age_band"]: g for g in client.get("/api/report").json()["groups"]}
    assert groups["0-4"]["population"] == 100 and groups["0-4"]["case_count"] == 2
    # 标签为 100-104，但按源数据口径截断为单龄 100
    assert groups["100-104"]["population"] == 100
    assert groups["100-104"]["cases_per_10000"] == 200.0


# --------------------------------------------------------------- 重启持久化
def test_population_persists_across_restart(tmp_path):
    from app.main import create_app
    db = str(tmp_path / "restart.db")
    with TestClient(create_app(db)) as c1:
        v = submit(c1, GOOD_CASES).json()["version"]
        register(c1, v, [FULL_ROW])
        assert c1.get("/api/report").json()["groups"][0]["population"] == 2000

    with TestClient(create_app(db)) as c2:
        rep = c2.get("/api/report").json()
        assert rep["case_version"] == v and rep["pop_version"] is not None
        assert rep["groups"][0]["population"] == 2000
        got = c2.get(f"/api/population?version={v}").json()
        assert len(got["rows"]) == 1


# --------------------------------------------------------------- 并发一致性
def test_interleaved_report_never_torn(tmp_path):
    store = Store(str(tmp_path / "conc.db"))
    feasible = SubmissionIn.model_validate({"k": 2, "cases": GOOD_CASES})
    errors = []
    stop = threading.Event()

    def writer_cases():
        while not stop.is_set():
            store.submit(feasible)

    def writer_population():
        while not stop.is_set():
            row = store.status()
            v = row["published_version"]
            if v is not None:
                payload = PopulationIn.model_validate(
                    {"version": v, "rows": [
                        {"age_lo": 20, "age_hi": 24,
                         "zip_prefix": "111", "population": 2000}
                    ]}
                )
                store.register_population(payload)

    def reader():
        while not stop.is_set():
            # 持同一把锁做快照式校验：报告与库内指针/人口登记必须同一时刻一致
            with store._lock:
                body, status = store.report()
                if status == 404:
                    if body["state"] not in ("withdrawn", "empty"):
                        errors.append(f"bad 404 body: {body}")
                    continue
                pointer = store._conn.execute(
                    "SELECT published_version FROM service_state WHERE id = 1"
                ).fetchone()[0]
                if pointer != body["case_version"]:
                    errors.append("case_version torn from publication pointer")
                if body["pop_version"] is not None:
                    reg = store._conn.execute(
                        "SELECT case_version FROM population_registrations WHERE id = ?",
                        (body["pop_version"],),
                    ).fetchone()
                    if reg is None or reg["case_version"] != body["case_version"]:
                        errors.append("pop_version not bound to current case version")
                n_rows = store._conn.execute(
                    "SELECT COUNT(*) AS c FROM publication_rows WHERE version = ?",
                    (body["case_version"],),
                ).fetchone()["c"]
                if sum(g["case_count"] for g in body["groups"]) != n_rows:
                    errors.append("group case counts torn from publication rows")
                for g in body["groups"]:
                    if g["reason_code"] is None:
                        if g["population"] is None or g["cases_per_10000"] is None:
                            errors.append(f"matched group missing metrics: {g}")
                    elif g["population"] is not None:
                        errors.append(f"failed group must not carry population: {g}")

    threads = [threading.Thread(target=writer_cases),
               threading.Thread(target=writer_population)] + \
              [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(1.0)
    stop.set()
    for t in threads:
        t.join()
    store.close()
    assert not errors
