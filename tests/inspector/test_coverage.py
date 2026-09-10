"""누락 vs 재배열 판정 — 이 구분을 틀리면 화면이 정상 문서를 파손으로 고발한다."""
from v1.inspector.coverage import classify_line, norm, page_report

LINE = "⑦ 직장 또는 항문질환 중 「국민건강보험법」 상 요양급여에 해당하지 않는 부분"


def test_그대로_있으면_kept():
    assert classify_line(LINE, norm("앞말 " + LINE + " 뒷말"))[0] == "kept"


def test_줄바꿈만_달라도_kept_이다():
    """공백을 지우고 비교하므로 단순 재줄바꿈은 애초에 손실이 아니다."""
    wrapped = "⑦ 직장 또는 항문질환 중 「국민건강보험법」\n상 요양급여에 해당하지 않는 부분"
    assert classify_line(LINE, norm(wrapped))[0] == "kept"


def test_표_파이프가_끼어도_kept_이다():
    """ODL 은 표를 `|셀|셀|` 로 뱉는다. 파이프를 안 지우면 정상 표가 통째로 누락이 된다."""
    as_table = "| ⑦ 직장 또는 항문질환 중 「국민건강보험법」 | 상 요양급여에 해당하지 않는 부분 |"
    assert classify_line(LINE, norm(as_table))[0] == "kept"


def test_조각이_흩어져_있으면_split_이고_missing_이_아니다():
    """R10 에서 실제로 난 일 — 글자는 전부 색인에 있는데 한 덩어리가 아니다.
    창(window)으로 셌다면 갈린 자리를 걸치는 창이 없어 missing 으로 오판한다."""
    scattered = "해당하지 않는 부분 …사이에 낀 다른 문장… ⑦ 직장 또는 항문질환 중 「국민건강보험법」 상 요양급여에"
    st, gaps = classify_line(LINE, norm(scattered))
    assert st == "split" and gaps == []


def test_진짜_빠지면_missing_이고_없는_조각을_돌려준다():
    st, gaps = classify_line(LINE, norm("⑦ 직장 또는 항문질환 중"))
    assert st == "missing" and gaps


def test_짧은_조판_부스러기는_판정하지_않는다():
    for junk in ("89", "도", "성", "별"):
        assert classify_line(junk, "")[0] == "skip"


def test_페이지_전체가_빠지면_도달률이_0():
    r = page_report(LINE + "\n" + LINE.replace("직장", "항문"), [])
    assert r["missing"] == 2 and r["reached"] == 0.0


def test_도달률은_split_을_손실로_세지_않는다():
    scattered = "해당하지 않는 부분 …사이에 낀 다른 문장… ⑦ 직장 또는 항문질환 중 「국민건강보험법」 상 요양급여에"
    r = page_report(LINE, [scattered])
    assert r["reached"] == 1.0 and r["split"] == 1


def test_한_구간이라도_못_덮으면_그_글자를_돌려준다():
    """구멍의 내용을 보여주는 게 핵심 — 사람이 '이게 빠져도 되나'를 판단할 재료다."""
    hay = LINE.replace("「국민건강보험법」 상 요양급여에 해당하지", "")
    st, gaps = classify_line(LINE, norm(hay))
    assert st == "missing" and any("요양급여" in g for g in gaps)


def test_HTML_이스케이프를_풀지_않으면_표_라벨이_통째로_누락이_된다():
    """ODL 은 `<최초계약의 경우>` 를 `&lt;…&gt;` 로 저장한다(R05 실측).
    안 풀면 '최초계약 표가 사라졌다'는 잘못된 경보가 나간다."""
    assert classify_line("<최초계약의 경우>", norm("&lt;최초계약의 경우&gt;"))[0] == "kept"


def test_코너괄호와_직선따옴표를_같게_본다():
    """PDF 는 「국민건강보험법」, 색인은 "국민건강보험법" (실측 R10). 안 접으면
    법령을 인용한 모든 줄이 누락으로 찍혀 '면책 항목이 사라졌다'는 오경보가 된다."""
    pdf = "⑦ 직장 또는 항문질환 중 「국민건강보험법」 상 요양급여에 해당하지 않는 부분"
    idx = '⑦ 직장 또는 항문질환 중 "국민건강보험법"상 요양급여에 해당하지 않는 부분'
    assert classify_line(pdf, norm(idx))[0] == "kept"
