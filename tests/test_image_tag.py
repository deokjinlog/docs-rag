"""마크다운 이미지 태그 정규식 잠금 — 이 패턴이 어긋나면 OCR 태스크가 통째로 no-op 이 된다.

**실제로 그랬다.** 패턴이 세 모듈에 복제돼 있었고 서로 달라졌다. ODL 이 뱉는 실제 표기는
`![](<경로.png>)`(빈 alt · 꺾쇠로 감싼 경로)인데 OCR 태스크는 `![image N](경로.png)` 를
기대해 **매칭 0건** → 매 문서가 "이미지 없음"으로 조기 return → paddle 은 호출된 적이 없고
코퍼스 image 청크가 0개였다. 청커의 넓은 스크러버가 잔해만 치워서 색인엔 증상이 안 보였다.

그래서 잠그는 것은 "정규식이 잘 도나"가 아니라 **"실제 ODL 표기를 잡나"** 다.
"""
import pytest

from src.v1.utils.preprocess import IMAGE_TAG_ANY_RE, IMAGE_TAG_RE, is_noise_line

ODL_ACTUAL = "![](<라이나_소득보장수술특약_images/imageFile1.png>)"


def test_odl_actual_notation_is_captured():
    """이 한 줄이 회귀의 핵심 — ODL 실제 출력."""
    m = IMAGE_TAG_RE.search(ODL_ACTUAL)
    assert m, "ODL 실제 표기를 못 잡으면 OCR 태스크가 no-op 이 된다"
    assert m.group(1) == "라이나_소득보장수술특약_images/imageFile1.png"


@pytest.mark.parametrize("text,path", [
    ("![image 3](docs_images/imageFile3.png)", "docs_images/imageFile3.png"),   # 구형 표기
    ("![](<a_images/x.PNG>)", "a_images/x.PNG"),                                # 대문자 확장자
    ("![캡션](a_images/y.jpg)", "a_images/y.jpg"),                              # 한글 alt
    ("![](< a_images/z.jpeg >)", "a_images/z.jpeg"),                            # 공백 허용
])
def test_notation_variants(text, path):
    m = IMAGE_TAG_RE.search(text)
    assert m and m.group(1) == path


@pytest.mark.parametrize("text", [
    "![](<a_images/x.svg>)",       # 확장자 화이트리스트 밖 — OCR 대상 아님
    "[링크](a.png)",               # 이미지가 아니라 링크
    "제5조 보험금의 지급사유",
])
def test_non_targets_are_not_captured(text):
    assert not IMAGE_TAG_RE.search(text)


def test_scrubber_is_broader_than_capturer():
    """지우는 쪽은 넓어야 한다 — 확장자 없는 태그도 텍스트 청크에 남으면 안 된다."""
    odd = "![](<a_images/no-extension>)"
    assert not IMAGE_TAG_RE.search(odd)       # OCR 대상은 아니고
    assert IMAGE_TAG_ANY_RE.search(odd)       # 지우기는 한다


def test_image_line_is_noise():
    assert is_noise_line(ODL_ACTUAL)
    assert is_noise_line("![image 1](a_images/b.png)")
    assert not is_noise_line("제5조 【보험금의 지급사유】")
