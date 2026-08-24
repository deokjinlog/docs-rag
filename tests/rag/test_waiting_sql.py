"""SQL 경로 waiting_sql 순수 로직 — 면책기간·감액("언제부터 온전히 받나?").

계약: (1) 의도 게이트가 '면책기간·언제부터·감액'은 잡고 순수 '얼마'는 안 잡음, (2) 질의의 담보명
으로 특약 행을 짚음(가장 구체적 매칭), (3) 면책·감액 둘 다 없으면 RAG 신호(precision-first).
"""

from src.v1.rag.waiting_sql import is_waiting_query, pick_subcontract, format_waiting


def test_waiting_gate_fires():
    for q in ["암진단비 면책기간 얼마야?", "언제부터 온전히 받아?", "감액 있어?", "가입 후 며칠 지나야 보장돼?"]:
        assert is_waiting_query(q), f"waiting 질의 미감지: {q}"


def test_waiting_gate_excludes_plain_amount():
    assert not is_waiting_query("암진단비 얼마 받아요?")


def test_pick_subcontract_most_specific():
    rows = [
        {"product_name": "1. 암진단비(유사암제외)(간편가입)", "waiting_period_days": 90,
         "reduction_period": "1년이내", "reduction_rate_pct": 50},
        {"product_name": "2. 질병입원일당(1일이상)(간편가입)", "waiting_period_days": None,
         "reduction_period": "1년이내", "reduction_rate_pct": 50},
    ]
    hit = pick_subcontract(rows, "골든라이프 암진단비 면책기간 얼마?")
    assert hit["_coverage"] == "암진단비" and hit["waiting_period_days"] == 90


def test_pick_subcontract_none_when_no_coverage():
    rows = [{"product_name": "1. 암진단비(간편가입)", "waiting_period_days": 90,
             "reduction_period": None, "reduction_rate_pct": None}]
    assert pick_subcontract(rows, "면책기간 언제부터야?") is None   # 담보 미지목 → RAG


def test_format_waiting_composes_and_rag_signal():
    out = format_waiting({"_coverage": "암진단비", "waiting_period_days": 90,
                          "reduction_period": "1년이내", "reduction_rate_pct": 50})
    assert "면책기간 90일" in out and "1년이내 50% 감액" in out
    assert "→RAG" in format_waiting(None)
    # 면책·감액 둘 다 없으면 RAG
    assert "→RAG" in format_waiting({"_coverage": "x", "waiting_period_days": None,
                                     "reduction_period": None, "reduction_rate_pct": None})
