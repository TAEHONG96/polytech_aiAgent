# 합성 데이터센터 운영 코퍼스

코퍼스에는 승인 SOP 13개, 실습 티켓 템플릿 45개와 발행 시나리오 9개 버전이 있다. 시나리오마다 이전 Bot, 현재 교육생, 다음 Bot의 연속 12시간 교대와 티켓 5건을 제공한다. 앞선 근무의 진행·대기 티켓 2건을 인수하고, 이미 해결된 사례 1건을 참고하며, 현재 교대에서 반복 장애와 정보 부족 사례를 확인한다.

시나리오마다 반복 장애 티켓(`ticket-04`)의 동일 호스트에 승인된 과거 장애 1건이 있다. 교육생은 이전 조치 결과를 읽고, 그 자료를 확인한 뒤 수행한 가상 조치에 연결할 수 있다.

| 시나리오 키 | 실습 주제 |
| --- | --- |
| `synthetic-ops-boot` | POST 정지, 부팅 장치 미인식, 복구 항목 재발 |
| `synthetic-ops-memory` | 메모리 ECC, 프로세스 메모리 압박, 슬롯 재발 |
| `synthetic-ops-storage` | 대기열 지연, 이중 경로 축소, 복제 경보 |
| `synthetic-ops-network` | 링크 플랩, CRC 증가, 구간별 패킷 손실 |
| `synthetic-ops-power` | 전원 이중화 상실, 분기 확인, 센서 누락 |
| `synthetic-ops-bmc` | BMC 타임아웃, 콘솔 고립, 관리망 손실 |
| `synthetic-ops-major` | P1 지휘, 공통 시간선, 서비스별 복구 확인 |
| `synthetic-ops-recovery` | 복구 후 간헐 오류, 관찰 창, 다음 교대 검증 |

학습 자료는 [코퍼스 정의](../tests/corpus_data.py)에, 기대 원인·근거·다음 판단·처리 결과는 [검증 데이터](../tests/corpus_expectations.py)에 저장한다. 평가 데이터의 행은 시나리오 키와 `ticket-01`부터 `ticket-05`까지의 순서에 대응한다. 실행 이미지에는 검증 파일을 포함하지 않는다. 기존 실습 평가 엔진은 교육생이 수행한 조치와 인계를 평가하고, 코퍼스 기대 결과는 QA 검증용이다.

발행된 1.0 회차에는 [코퍼스 가상 조치](../app/simulation/corpus_actions.py)가 적용된다. 조회·재측정은 반복할 수 있다. 네트워크·스토리지 경로 전환은 우회 경로 확인 후 한 번만 실행한다. 전환 후 재검증을 완료하려면 네트워크의 손실률이나 스토리지의 응답 시간·대기열을 다시 측정한다. 이 조치에서 가상 호스트 상태가 갱신된다. 공통 시간선과 잔여 위험 기록은 교육생이 작성한 내용을 조치 이력에 저장한다. 자료가 부족하면 전환을 보류하고 확인한 근거와 다음 조치를 인계할 수 있다.

### 네트워크 실습 v1.1

실습 시작 목록에서 `synthetic-ops-network v1.1`을 선택한다. 두 인수 티켓은 같은 [네트워크 대응 SOP](../tests/corpus_network.py)를 참조한다. SOP에는 양단의 10분 포트 카운터 비교, 가상 우회 조건, 표본 미수집 시 요청 절차, 전환 후 손실률 복구 기준과 잔여 위험 인계 기준이 있다.

- `업링크 반복 플랩`에서는 양단 비교와 우회 시험을 수행한 뒤 가상 전환할 수 있다. 동일 대상의 손실률을 재측정해 `4% → 0.2%`를 확인하고, 원래 링크의 CRC 증가와 이중화 점검을 인계한다.
- `특정 구간 패킷 손실`에서는 반대편 표본과 우회 시험 결과를 확인할 수 없어 전환이 거부된다. 양단 비교로 누락을 확인하고 가상으로 반대편 표본을 요청한 뒤 인계한다.
- 새 규칙은 전환 후 재측정과 자료 부족에 따른 보류를 구분한다. 두 인수 티켓의 인계 평가에는 발신 초안에 교육생이 작성한 티켓별 다음 조치가 필요하다. 합성 측정 시간창과 결과는 조치 직후 기록에 표시된다.

## 전용 Compose에서 적재 및 검증

아래 명령은 운영과 분리된 `polytech-qa-synthetic-corpus` 프로젝트, `polytech_training_corpus` DB 및 전용 DB·SOP 볼륨을 사용한다. 브라우저가 필요하면 Ubuntu에서 `http://127.0.0.1:18087/login`으로 접속해 교육 계정을 만든다.

```bash
unset DOCKER_HOST DOCKER_CONTEXT COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES
export APP_PORT=18087 APP_ORIGIN=http://127.0.0.1:18087 AGENT_PROVIDER=fake GEMINI_API_KEY=
corpus() {
  docker --context default compose --env-file .env.example -p polytech-qa-synthetic-corpus \
    -f compose.yaml -f compose.override.yaml -f compose.corpus.yaml "$@"
}
corpus up --build --wait --wait-timeout 240 nginx worker
corpus build test
corpus run --rm --no-deps -T test python -m tests.qa_seed_corpus
corpus run --rm --no-deps -T test python -m tests.qa_seed_corpus --apply --verify
corpus run --rm --no-deps -T test python -m pytest -q tests/test_corpus_actions.py
```

첫 스크립트 실행은 쓰기 없이 개수와 참조를 확인한다. `--apply`는 Fake provider, 루프백 HTTP 출처, 전용 DB와 격리 설정을 검사한 후 적재한다. 문서는 이름·해시·원본 바이트를 비교해 재사용하고, 발행 시나리오는 버전과 콘텐츠 지문을 비교한다. 기존 볼륨에서 다시 실행하면 v1.1과 관련 SOP를 추가하고 v1.0의 식별자·콘텐츠 지문과 기존 회차를 보존한다. 충돌한 데이터를 발견하면 멈춘다. `--verify`는 전용 QA 교육 계정 하나에서 시나리오별 실행 9건을 생성하거나 재사용해 인수·티켓·SOP·발신 인계를 검사한다. Worker 색인, SOP와 과거 장애 검색, AI 도구가 반환하는 근거도 확인한다. 네트워크 SOP는 “반대편 측정값 없이 경로를 전환해도 되나?” 질의에서 전환 보류 절이 검색되는지 확인한다.

실습 종료 후 `corpus stop`으로 전용 서비스만 정지한다. 같은 명령으로 다음에 기동하면 데이터를 다시 사용할 수 있다.

### 키로 채팅의 Gemini 확인

기본 코퍼스는 Fake 공급자를 사용하며 키로 패널에 **모의 응답**을 표시한다. 실제 호출은 Gemini 접근 권한이 있는 별도의 합성 코퍼스에서 확인한다. Fake로 합성 문서 적재를 완료한 뒤 같은 스택의 마지막 Compose 파일로 `compose.corpus-gemini.yaml`을 추가한다. `GEMINI_API_KEY`를 실행 환경에서 제공하면 Compose는 키를 워커에만 전달한다.

```bash
read -rsp 'Gemini API key ' GEMINI_API_KEY
printf '\n'
export GEMINI_API_KEY
corpus -f compose.corpus-gemini.yaml up -d --build --wait nginx worker
corpus -f compose.corpus-gemini.yaml exec -T worker python -m app.knowledge.demo --reindex
```

`GEMINI_API_KEY`는 위처럼 실행 환경에서 제공하거나 Git에서 제외된 `.env`에 설정해 `--env-file .env`로 읽을 수 있다. 환경 파일을 사용한다면 적재 시점과 같은 DB 계정·비밀번호를 유지하고, 셸에 빈 `GEMINI_API_KEY`가 남아 있으면 먼저 해제한다. 모델은 워커의 `GEMINI_GENERATION_MODEL`(기본 `gemini-3.8-flash`)과 `GEMINI_EMBEDDING_MODEL`(기본 `gemini-embedding-2`) 설정을 따른다. Gemini 오버레이를 사용하는데 키가 비어 있으면 Compose가 설정 단계에서 종료한다. 인증·모델·호출 오류는 채팅의 오류 항목으로 표시한다. Fake와 Gemini의 임베딩 검색 프로필은 다르므로 전환한 공급자로 색인을 다시 생성한다.

색인이 완료되면 PowerShell에서 실제 Gemini에 대한 A~E 브라우저 스모크를 실행할 수 있다. 별도 합성 코퍼스의 실제 포트를 `QA_BASE_URL`로 지정한다. 스모크는 합성 교육생을 생성해 티켓 요약, 티켓 전환, 승인 SOP 링크, 일반 개념, 후속 질문을 확인한다.

```powershell
$env:QA_BASE_URL = 'http://127.0.0.1:18089'
$env:QA_GEMINI_SMOKE = '1'
Set-Location frontend
npm run test:e2e -- companion-gemini.spec.ts
```

키로 실행 제안과 승인 흐름도 기존 합성 코퍼스에서 검증할 수 있다. Gemini 환경에서는 `QA_ACTION_SMOKE=gemini`, Fake 환경에서는 `QA_ACTION_SMOKE=mock`을 설정하고 해당 환경의 루프백 주소를 `QA_BASE_URL`에 넣는다. `frontend`에서 `npm run test:e2e -- companion-actions.spec.ts`를 실행한다. 이 검증은 기존 승인 SOP와 검색 색인을 사용한다.

다단계 워크플로는 같은 환경에서 `QA_WORKFLOW_SMOKE=gemini` 또는 `QA_WORKFLOW_SMOKE=mock`을 설정하고 `npm run test:e2e -- companion-workflows.spec.ts`로 검증한다. 실습 티켓 분석, SOP 선택과 승인, 페이지 이동 후 별도의 포커스 승인, 분석 후 SOP 열기, 일반 채팅을 확인한다. Fake 모드는 취소와 컨텍스트 변경 후 재개도 검사한다.

`npm run test:e2e -- companion-workflow-continuity.spec.ts`는 같은 설정으로 SOP 비교만 하는 요청, 비교 후 선택한 문서 열기, 인계 이동 후 설명 및 별도 강조 승인을 검증한다. Fake 환경에서는 수동 화면 변경, 새로고침 후 상태 복원, 강조 대상이 사라졌을 때의 부분 결과를 추가로 검사한다.

## Cloudflare Tunnel에서 공개 실습 가입

[공개 터널 설정](PUBLIC_DEMO.md)을 따라 실습 전용 환경 파일 `.env.corpus-public`을 만든다. 기존 코퍼스 볼륨을 재사용하려면 처음 적재할 때 사용한 DB 계정·비밀번호를 같은 파일에 설정한다. `APP_ORIGIN`에는 공개 HTTPS 주소를 넣고 `CORPUS_NGINX_TARGET=training-tunnel`, `APP_PUBLIC_TRAINING_REGISTRATION=1`을 지정한다. 앞 절의 `corpus stop`으로 루프백 구성을 정지한 다음 같은 Compose 프로젝트를 아래 조합으로 시작한다.

```bash
unset APP_ORIGIN APP_PUBLIC_TRAINING_REGISTRATION CORPUS_NGINX_TARGET
docker --context default compose --env-file .env.corpus-public -p polytech-qa-synthetic-corpus \
  -f compose.yaml -f compose.tunnel.yaml -f compose.corpus.yaml up -d --build --wait
```

공개 가입은 `APP_ENV=simulation`, DB `polytech_training_corpus`, HTTPS 출처에서 활성화된다. 가입을 닫으려면 환경 파일의 `APP_PUBLIC_TRAINING_REGISTRATION`을 `0`으로 바꾸고 동일한 Compose 조합으로 `up -d --no-deps api`를 실행한다.

공개 가입을 열면 비밀번호와 확인 입력이 일치해야 제출할 수 있다. 전용 환경의 새 실습 목록에서는 기본 `dco-basic` 시나리오를 숨기며 기존 회차는 실습 기록에서 조회할 수 있다.
