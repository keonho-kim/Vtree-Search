# Python LLM 주입 가이드

## 관련 문서

- [프로젝트 개요](../../README.md)
- [Python 개요](./README.md)
- [Python 모듈 레퍼런스](./module_reference.md)
- [아키텍처 청사진](../arch/blueprint.md)

## 1. 목표

Vtree Search는 LLM 연동을 HTTP가 아닌 **Python 객체 주입 방식**으로 처리한다.

- 검색: 후보 keep/drop 판정
- 적재: 표/이미지 주석 생성

## 2. 라이브러리 직접 주입 방식

애플리케이션은 LangChain 채팅 모델 구현체를 직접 생성해 주입한다.

```python
from langchain_google_genai import ChatGoogleGenerativeAI
from vtree_search import VTreeSearchEngine, VtreeIngestor

llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0)
search_engine = VTreeSearchEngine(config=search_config, llm=llm)
ingestor = VtreeIngestor(config=ingestion_config, llm=llm)
```

현재 문서/드라이버 기준 제공자는 Gemini(`ChatGoogleGenerativeAI`)다.

## 3. 드라이버 기본 방식

현재 드라이버 스크립트는 Gemini 기준으로 고정되어 있다.

- 검색: `ChatGoogleGenerativeAI(model=os.environ["SEARCH_LLM_MODEL"], temperature=0.0)`
- 적재: `ChatGoogleGenerativeAI(model=os.environ["INGESTION_LLM_MODEL"], temperature=0.0)`
- 공통 키: `GOOGLE_API_KEY`

## 4. 내부 처리 규칙

- 검색: `ainvoke` 배치 호출 후 JSON 배열(`node_id`, `keep`, `reason`)만 허용
- 적재: `ainvoke` 호출 후 `[TBL]...[/TBL]`, `[IMG]...[/IMG]` 형식 강제

## 5. 프롬프트 위치

- `src_py/vtree_search/ingestion/prompts/table_prompt.py`
  - `TABLE_PROMPT`
- `src_py/vtree_search/ingestion/prompts/image_prompt.py`
  - `IMAGE_PROMPT`

## 6. 실패 정책

- 형식 위반 응답(잘못된 JSON, 누락 node_id, 잘못된 태그 형식)은 즉시 실패한다.
- 자동 보정/fallback은 제공하지 않는다.
