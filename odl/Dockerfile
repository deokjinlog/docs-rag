FROM python:3.11-slim

# docling(하이브리드 ML 백엔드)이 OpenCV 를 통해 X11 계열 공유 라이브러리를 찾는다.
# python:3.11-slim 엔 없어서 `ImportError: libxcb.so.1` 로 백엔드가 500 을 냈다.
# 하이브리드 경로가 실제로 돌아본 적이 없어 이 의존성이 드러나지 않았던 것
# (easyocr 미설치 → 모델 런타임 다운로드 → libxcb 순으로 세 겹이 쌓여 있었다).
RUN apt-get update && apt-get install -y \
    openjdk-21-jre-headless \
    build-essential \
    libxcb1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -U "opendataloader-pdf[hybrid]" fastapi uvicorn

ENV JAVA_TOOL_OPTIONS=-Djava.util.logging.ConsoleHandler.level=WARNING

WORKDIR /app
COPY server.py .

EXPOSE 5002

# --no-ocr: docling 파이프라인이 OCR 엔진(기본 easyocr)을 초기화하는데 이미지에 없어서
# `ImportError: EasyOCR is not installed` 로 **하이브리드 경로가 통째로 죽어 있었다**
# (실측: 추출 완료 로그 모드 분포가 java 9 · java-direct 1 · docling **0**).
# extract.py 가 실패를 warning 만 남기고 java 로 폴백해서 무성 열화였다.
# 이 프로젝트는 이미지 OCR을 PaddleOCR로 따로 하므로 docling OCR은 애초에 불필요 —
# easyocr 설치(토치 중복) 대신 끄는 게 맞고, 옵션 설명도 "임베디드 텍스트가 있으면
# 중복 추출을 막는다"로 이 구성을 정확히 가리킨다.
CMD ["sh", "-c", "opendataloader-pdf-hybrid --port 5010 --device cpu --no-ocr & uvicorn server:app --host 0.0.0.0 --port 5002"]
