"""표 캡션 — 표 청크에 '무슨 조건의 값인지'를 붙인다.

이 규칙이 없으면 R05 제3조의 지급표 세 개(상해/질병/재가입)가 값만 다른 채 구분 불가로
색인된다. 잘못 붙은 캡션은 **조건을 거짓으로 말하므로**, 정밀도를 길이로 보수적으로 지킨다.
"""
from v1.utils.chunker_adaptive import _split_segments, _table_caption


class Test캡션_판정:
    def test_조건_라벨을_캡션으로_본다(self):
        assert _table_caption("- 질병을 원인으로 보존치료를 받은 경우") == "질병을 원인으로 보존치료를 받은 경우"

    def test_HTML_엔티티를_푼다(self):
        """파이프라인이 <최초계약의 경우> 를 &lt;…&gt; 로 저장한다 — 그대로 붙이면
        조건 라벨이 엔티티 문자열로 임베딩돼 신호가 죽는다."""
        assert _table_caption("&lt;최초계약의 경우&gt;") == "<최초계약의 경우>"

    def test_표_머리도_캡션이다(self):
        assert _table_caption("구분 적립기간 지급이자") == "구분 적립기간 지급이자"

    def test_문장은_캡션이_아니다(self):
        assert _table_caption("회사는 다음과 같이 보험금을 지급합니다.") is None
        assert _table_caption("아래 표에 따라 지급함") is None

    def test_30자를_넘으면_버린다(self):
        """실측 46~60자 후보 3건이 전부 본문 조각이었다 — 9건을 포기하고 오염을 0으로 둔다."""
        assert _table_caption("2 제1항에서 정하지 않은 용어의 뜻은 무배당 실손의료비보장보험 보통약관을 따른다") is None

    def test_표_행과_헤딩은_캡션이_아니다(self):
        assert _table_caption("|구분|지급금액|") is None
        assert _table_caption("## 제3조(보험금의 지급사유)") is None

    def test_빈_줄은_None(self):
        assert _table_caption("") is None
        assert _table_caption("   ") is None


class Test세그먼트_분리:
    def test_긴_본문_끝의_라벨이_표에_붙고_본문도_남는다(self):
        body = ("회사는 다음과 같이 보험금을 지급합니다.\n"
                "- 상해를 원인으로 보존치료를 받은 경우\n"
                "|구분|지급금액|\n|---|---|\n|레진|50%|")
        segs = _split_segments(body)
        assert [s.chunk_type for s in segs] == ["text", "table"]
        assert segs[1].caption == "상해를 원인으로 보존치료를 받은 경우"
        assert "상해를 원인으로" in segs[0].content, "본문에서 지우지 않는다(손실 방지)"

    def test_라벨만_있는_텍스트는_별도_청크로_안_나간다(self):
        """R05 #5679 가 20자짜리 고아 청크였다 — 내용은 표에 실려 살아 있다."""
        body = "- 질병을 원인으로 보존치료를 받은 경우\n|구분|지급금액|\n|---|---|\n|레진|25%|"
        segs = _split_segments(body)
        assert [s.chunk_type for s in segs] == ["table"]
        assert segs[0].caption == "질병을 원인으로 보존치료를 받은 경우"

    def test_문장으로_끝나면_캡션이_안_붙는다(self):
        body = "회사는 아래와 같이 지급합니다.\n|구분|금액|\n|---|---|\n|A|1%|"
        segs = _split_segments(body)
        assert segs[-1].caption is None
        assert [s.chunk_type for s in segs] == ["text", "table"]

    def test_표가_연달아_나오면_캡션이_새지_않는다(self):
        body = ("- 상해를 원인으로 받은 경우\n|구분|금액|\n|---|---|\n|A|1%|\n"
                "\n|구분|금액|\n|---|---|\n|B|2%|")
        segs = _split_segments(body)
        tables = [s for s in segs if s.chunk_type == "table"]
        assert tables[0].caption == "상해를 원인으로 받은 경우"
        assert tables[1].caption is None, "앞 표의 캡션이 뒤 표로 새면 조건을 거짓으로 말한다"
