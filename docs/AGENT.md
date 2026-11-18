# Gemini 장애 분석 에이전트

3~4단계 구현 기준 · 2026-11-18 (KST)

## 실행 구조

`POST /api/v1/agent/analyze`는 질문과 선택적 티켓 UUID를 받아 분석을 등록하고 `202` 및 `Location`을 반환한다. 인증 세션과 CSRF를 적용하며 같은 사용자가 진행 중인 분석을 또 요청하면 `409 AGENT_BUSY`와 해당 실행 ID를 반환한다. `GET /api/v1/agent/runs/{id}`에서 요청자 본인의 진행 상태, 결과, 도구 기록과 사용량을 조회한다.

기존 worker에서 색인과 분석을 별도 스레드로 처리한다. DB 리스로 실행을 확보하고 요청자 활성 상태를 다시 검사한다. Gemini 네트워크 호출 중에는 DB 트랜잭션을 열어두지 않는다. 중단된 작업은 리스 만료 뒤 실패로 표시하며, 사용자가 다시 요청할 수 있다.

`AGENT_PROVIDER=gemini`는 같은 worker의 `GEMINI_API_KEY`를 사용한다. 모델 기본값은 `GEMINI_GENERATION_MODEL=gemini-3.8-flash`이며 실제 스모크에서 접근을 확인했다. 로컬 `fake` 모드는 키 없이 같은 실행 경로를 검증한다. [Gemini 모델](https://aistudio.google.com/docs/models/gemini-3.8-flash) · [Google Gen AI SDK](https://github.com/googleapis/python-genai)

## 선택 가능한 도구

| 도구 | 인자 및 범위 |
| --- | --- |
| `search_similar_tickets` | `query` 최대 300자, `limit` 1–5. 비보관 해결·종료 사례를 조회하고 연결한 현재 티켓은 제외한다. |
| `search_sop` | 같은 검색 인자로 SOP 청크, 점수, 쪽 번호·제목과 원본 링크를 조회한다. |
| `get_ticket_details` | UUID를 받는다. 사용자 요청에 연결했거나 현재 실행의 허용된 조회에서 확인한 티켓을 읽는다. |
| `get_ticket_history` | 같은 UUID 정책을 적용한다. 현재 구현의 `ticket_events`에서 최근 8개 상태 및 수정 이벤트를 읽는다. |
| `get_open_tickets` | `filters`의 `status`, `service`, `limit`으로 비보관 미해결 티켓을 최대 5개 조회한다. |

기존 단일 조직 운영자 권한으로 접근을 검사한다. 모델에는 허용 목록의 함수 선언만 전달한다. 임의 함수, 등록되지 않은 인자 및 범위를 벗어난 티켓은 실행 전에 거부한다. 각 도구의 출력은 `untrusted_data`에 넣고, 문서 내부의 권한 주장과 지시를 따르지 않도록 시스템 지침을 설정한다.

## Function Calling과 출처 검증

1. Gemini가 사용할 도구를 선택한다. 서버는 SDK 자동 실행을 끄고 함수 인자와 접근 범위를 검증한 뒤 조회한다.
2. 서버는 모델 응답의 원본 parts와 thought signature를 메모리에 유지하고 호출 ID에 맞는 응답을 보낸다. Gemini는 그 결과를 바탕으로 다른 도구, 새 검색어 또는 조사 종료를 선택한다.
3. 별도 요청에서 수집한 근거를 제공하고 구조화 JSON을 받는다. 서버는 스키마와 출처 키를 검증한다. 확인 사실은 직접 티켓 기록을 인용한다. 가설과 진단 단계에 근거가 없으면 `basis_status=insufficient`로 표시한다. 인용할 자료가 없으면 한계를 추가한다.
4. 결과에는 사용자 질문, 확인된 사실, 가설, 읽기 전용 진단 절차, 확인 질문, 한계와 출처가 들어간다. 근거에는 출처 UUID, 청크 번호, 페이지·제목·티켓 버전, 원본 링크가 포함된다. 검증에 실패한 출력은 남은 예산으로 한 번 수정 요청할 수 있다.

실행은 접수부터 최대 120초, 작업 시작 뒤 최대 90초, 개별 모델 요청 최대 25초, 도구 실행 최대 15초다. 최종 응답에 26초를 확보한다. 조사 라운드 3회, 도구 8회, 모델 요청 8회가 상한이다. 같은 도구와 같은 입력은 반복 실행을 건너뛰고, 검색 유형별로 검색어를 바꾼 재시도 한 번을 허용한다. 같은 질의의 임베딩은 실행 내에서 재사용한다. 일시적 모델 오류는 남은 예산과 재시도 지연을 고려해 요청당 한 번 재시도한다. [Function Calling](https://aistudio.google.com/docs/function-calling) · [생각 응답 서명](https://aistudio.google.com/docs/thought-signatures) · [구조화 출력](https://aistudio.google.com/docs/structured-output)

도구 기록은 순번, 허용된 이름 또는 `unknown`, 상태, 소요 시간, 질의 길이·제한값, 오류 코드와 근거 키를 저장한다. API 키, 세션 쿠키, 도구 인자 원문과 모델 서명은 이력에 기록하지 않는다. 응답은 모델 요청·임베딩 횟수와 공급자 제공 토큰 수를 포함한다.

## 실제 RAG 검증

격리된 Compose DB에만 7개 가상 SOP·7개 해결 티켓을 새로 색인했다. `gemini-embedding-2` 응답 및 PostgreSQL의 `vector(768)` 저장 차원과 프로필을 확인했다. 기존 합성 벡터와 실제 벡터는 서로 다른 DB·프로필에서 검증했다.

| 평가 | 관련 질의 | 무관 질의 | 결과 |
| --- | --- | --- | --- |
| 보정 및 홀드아웃 | 12개 | 10개 | 출처 Recall@5 24/24. 점수 기준 0.62 적용 시 기대 출처 24/24 유지, 무관 질의 10/10 제외 |
| 별도 도전 세트 | 4개 | 5개 | 기대 출처 8/8 유지, 무관 질의 5/5 제외 |

초기 중간값 후보 0.68908은 홀드아웃의 부팅 표현 변형에서 근거 2개를 제외했다. 보정 세트의 무관 질의 최대 점수 0.605379에 0.01 여유를 더해 소수 둘째 자리로 올림한 0.62를 현재 Gemini 프로필에만 적용했다. 별도 평가의 무관 질의 최대 점수는 0.592834였다. 점수는 `1 - cosine_distance`이며 표본은 가상 사례로 한정된다. 평가에는 문서 배치 14회, 질의 임베딩 31회를 사용했다.

검색은 기존 `Embedder.query()`, `search_index()`, `calibrated_min_score()`를 API와 에이전트가 공유한다.

## 실제 Function Calling 기록

격리 환경에서 가상 부팅 티켓을 연결한 요청이 `succeeded`로 종료됐다. 실행 순서는 `get_ticket_details → get_ticket_history → search_sop → search_similar_tickets → search_sop`였다. 마지막 SOP 조회에서는 검색어를 바꿨다. 완료 응답과 브라우저 개발자 패널에서 도구 5건, 인용 출처 5개, 모델 요청 6회, 임베딩 요청 3회, 공급자 제공 모델 토큰 13,476개를 확인했다.

첫 검증에서는 외부 모델의 응답 지연으로 `AI_TIMEOUT`이 발생했다. 최종 출력 시간을 확보한 재시도에서 위 티켓 연결 작업이 성공했다. 증상만 제공한 실제 요청에서는 다단계 검색까지 확인했으며 공급자의 응답 오류로 결과 생성이 끝나지 않았다. 이후 반복 상한을 조정한 실환경 재검증에서 증상 요청과 무관 질의는 공급자 `429`를 받아 각각 두 번 요청 뒤 `AI_RATE_LIMIT`으로 기록됐다. 두 경로의 완료 동작은 모의 통합 테스트에서 확인했다.

## 4단계 인수인계 연동

인수인계 분석은 장애 분석과 같은 DB 큐, Gemini 어댑터, 시간·호출 예산, 검색 구현을 사용한다. 분석을 요청한 시점에 지정한 수집 리비전에서 티켓과 작업을 읽는다. 모형에 넘기는 텍스트는 길이를 제한하고 비밀값을 마스킹한다. 자동 함수 실행은 비활성화한다.

| 조회 도구 | 읽는 자료 |
| --- | --- |
| `get_shift_tickets(shift_id)` | 해당 보고서에 선택된 티켓 목록 |
| `get_ticket_details(ticket_id)` | 고정 티켓의 상태·우선순위·증상·영향·완료 기록 |
| `get_ticket_history(ticket_id)` | 고정 이력의 최근 변경과 완료 기록 |
| `get_pending_actions(ticket_id)` | 고정 작업, 다음 조치, 재확인 시각, 주의사항 |
| `identify_handover_gaps(ticket_id)` | 서버 규칙에 따른 확인 질문 |
| `search_sop(query, limit)`, `search_similar_tickets(query, limit)` | 교대 리뷰에 참고할 SOP와 유사 해결 이력 |

도구 호출을 마치면 Gemini가 별도 구조화 JSON에서 `highlights`, `completed_work`, `followups`, `warnings`, `needs_review`, `references`, `possible_related_groups`별 근거 키를 선택한다. 서버는 키와 자료 유형을 검증하고 저장된 문구와 원본 링크를 붙인다. 완료 작업은 티켓 해결 요약 또는 완료 작업 기록을 인용한다. 유사 티켓 묶음은 연관 가능성의 확인 항목으로 표시한다. 운영자의 편집 본문과 답변은 별도로 저장한다.

실행 한도는 장애 분석과 같다. 최대 24개 티켓, 티켓별 최대 18개 근거와 최근 5개 변경 이력을 모델에 제공한다. 범위를 줄이면 화면에 표시하며 스냅샷에 전체 기록이 남는다. 빈 스냅샷의 모델 요청은 0회다. 가상 실행 예시는 `get_shift_tickets → get_ticket_details → get_ticket_history → get_pending_actions → identify_handover_gaps → JSON 초안`이다.

실제 호출은 기존 worker 환경의 키를 사용한다. 테스트는 코드에 내장된 합성 티켓 한 건만 보내며 모델명, 도구 순서, 호출 수와 응답 토큰을 출력한다. [실제 호출과 검증 기록](PROGRESS.md)

```sh
docker compose -p polytech-handover-smoke run --build --rm --no-deps worker python -m app.handover.smoke
```
