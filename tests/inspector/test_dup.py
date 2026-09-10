"""중복 그룹 계산 단위 테스트 — 화면이 무엇을 '같다'고 부르는지 못박는다."""
from v1.inspector.dup import duplicate_groups, norm, shingles

BODY = (
    "회사는 피보험자가 보험기간 중 질병 또는 재해로 인하여 그 치료를 직접적인 목적으로 하여 "
    "1일이상 계속하여 중환자실에 입원하였을 때에는 보험수익자에게 중환자실 입원급여금을 "
    "지급합니다. 다만 1회 입원당 10일을 한도로 합니다."
)


def test_표_파이프가_달라도_같은_내용이면_묶인다():
    a = "|담보명|지급금액| |---|---| |중환자실 입원급여금|가입금액의 1%|"
    b = "| 담보명 | 지급금액 |\n|---|---|\n| 중환자실 입원급여금 | 가입금액의 1% |"
    groups, _ = duplicate_groups([(1, a), (2, b)])
    assert groups.get(1) == groups.get(2) != None


def test_요약이_본문에_통째로_들어가면_묶인다_길이차가_커도():
    """containment 를 쓰는 이유 — Jaccard 였다면 길이 차 때문에 놓친다."""
    summary = BODY[:120]
    long_body = BODY + " " + "제6조 보험금 지급에 관한 세부규정. " * 20
    groups, score = duplicate_groups([(1, summary), (2, long_body)])
    assert groups.get(1) == groups.get(2)
    assert score[groups[1]] >= 0.6


def test_다른_내용은_안_묶인다():
    other = "이 특약은 계약자의 청약과 회사의 승낙으로 이루어집니다. 청약철회는 15일 이내."
    groups, _ = duplicate_groups([(1, BODY), (2, other)])
    assert groups == {}


def test_상투구만_공유하면_안_묶인다():
    """면책 조 첫 문장은 특약마다 같다. 그것만으로 묶으면 화면이 온통 중복이 된다."""
    boiler = "회사는 다음 중 어느 한 가지의 경우에 의하여 보험금 지급사유가 발생한 때에는 보험금을 지급하지 않습니다."
    a = boiler + " 1. 피보험자가 고의로 자신을 해친 경우. " + "크라운치료에 한하여 적용합니다. " * 6
    b = boiler + " 1. 피보험자가 고의로 자신을 해친 경우. " + "영구치보철치료에 한하여 적용합니다. " * 6
    groups, _ = duplicate_groups([(1, a), (2, b)])
    assert groups == {}, "상투구 공유만으로 묶이면 안 된다"


def test_세_개가_한_군으로_이어진다():
    v = [BODY, BODY.replace("10일", "10 일"), BODY + " 추가 문장."]
    groups, _ = duplicate_groups(list(enumerate(v, 1)))
    assert len(set(groups.values())) == 1 and len(groups) == 3


def test_빈_내용은_무시된다():
    groups, _ = duplicate_groups([(1, ""), (2, None), (3, BODY)])
    assert groups == {}


def test_norm_은_공백과_표구분자만_지운다():
    assert norm("가 나|다") == "가나다"
    assert "%" in norm("50%")           # 숫자 의미는 살린다


def test_shingle_은_짧은_글도_하나는_만든다():
    assert shingles("짧다") == {"짧다"}
