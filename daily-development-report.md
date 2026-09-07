# Daily Development Report

## 2026-09-07

- 사용자 요청으로 `results/linkedin-post.md`를 삭제했다. 보고서의 해당 파일 링크와 산출물 흐름을 정리하고, PRD를 게시글 전문은 채팅에만 제공하는 방식으로 변경했다. 게시글은 baseline·optimized와 세 집계 쿼리의 결과를 구분해 다시 작성한다.

- GitHub에서 보고서 이미지가 깨지는 원인인 Mac 절대 경로를 수정했다. `benchmark-report.md`의 이미지 4개와 문서·원시 결과 링크 17개를 보고서 기준 상대 경로로 변경하고, PRD에 저장소 문서의 링크 규칙을 추가했다.
- 검증: 내부 링크 21개 모두 Git에 포함된 파일로 연결되며 이미지 4개의 PNG 형식을 확인했다. 링크 주소를 제외한 보고서 내용은 동일하고 `git diff --check`를 통과했다.

- LinkedIn 게시글의 문체를 실험 경험을 공유하는 자연스러운 글로 수정했다. DuckDB에 관심이 생긴 이유를 도입에 담고, 5개 불렛·핵심 수치·실험 범위를 유지하면서 보고서식 표현을 줄였다.

- 보고서를 DB 선택 판단 → baseline·optimized 비교 → DB별 추천 워크로드 순서로 전면 재작성했다. DuckDB의 분석 전반 경쟁력, ClickHouse의 집계 성능, PostgreSQL·SQLite의 indexed lookup 강점을 구분했다.
- 최적화의 평균 변화뿐 아니라 SD·p95·저장 공간·구축 비용 범위를 함께 설명하고, 프로덕션 추천은 공식 문서에 근거한 판단으로 표시했다. 실제 SQL·검증·중단·파일럿 제외 근거는 유지했다.
- 원시 데이터로 모든 수치 표를 재계산했다. SQLite optimized의 medium p50은 69,766.308 ms, large p50은 393,562.730 ms로 기존 표를 정정했다. 나머지 표도 동일한 소수 셋째 자리 반올림을 적용했다.
- `results/linkedin-post.md`에 도입과 결론, 정확히 5개 핵심 불렛을 포함한 게시글 전문을 작성했다. 외부 게시 작업은 수행하지 않았다.
- 차트 제목의 이전 절 번호를 제거하고 캐시 조건을 ‘검증 후 반복 측정’으로 명확히 했다. 시각화 코드의 긴 문자열은 표시 내용을 유지하며 줄을 나눴다.
- 검증: 8개 validation과 930개 완료 측정의 checksum·행 수 일치, 31개 통계 조합과 15개 프로파일 쌍의 표 수치 일치, 로컬 링크 21개·차트 4개·SQL 블록 4개·게시글 불렛 5개 확인. Ruff 검사·포맷 검사 통과, 네 차트의 제목·축·범례·오차막대·미완료 표시를 렌더링으로 확인했다.
- 보고 흐름: 검증된 원시 결과 → 통계·비교·차트 → 관측과 추천 구분 → 전체 보고서와 LinkedIn 게시글. PRD의 산출물·목차·캐시 설명과 조인 관계를 함께 갱신했다.

- 6.1절에 baseline·optimized 직접 비교 차트와 평균 ± 표본 SD·속도 배수 표를 추가했다. 네 엔진의 15개 완료 쿼리 쌍을 비교하고 SQLite join은 미완료로 표시했다.
- 검증: 원시 데이터·summary 통계 대조, 비교표 16행(유효 15쌍)과 이미지 링크 확인, Ruff 통과, 차트 렌더링의 축·범례·SD 하한 표시 확인.
- 비교 흐름: 프로파일별 원시 측정 검증 → 평균·SD 계산 → 동일 엔진·쿼리끼리 결합 → 속도 배수 계산 → 비교 막대와 표 생성.

- 보고서 5·6·8절에 성능 차트와 저장 크기 비교 차트를 추가했다.
- 사용자 피드백에 따라 5·6절을 평균 응답 시간 세로 막대 차트로 변경했다. 낮을수록 빠름, 쿼리별 순위, 최속 엔진 강조를 추가하고 p50/p95는 표로 유지했다.
- 5·6절 평균 막대에 ±1 표본 표준편차 오차막대와 수치를 추가했다. 30회 원시 측정의 SD를 계산하고 완주 summary의 stddev_ms와 대조한다. 로그 축 아래로 내려가는 하한은 ▽로 구분한다.
- 시각화 구조: 원시 측정 → 평균·표본 SD 계산 및 summary 대조 → 쿼리별 평균 순위 → 평균 막대 + SD 오차막대 4개 패널 → 5·6절 PNG.
- 원시 측정의 반복 수·iteration·checksum 및 완주 summary의 평균·p50·p95를 검증한 뒤 차트를 생성한다.
- SQLite optimized의 3개 완료 쿼리는 원시 측정값을 사용하고, join은 중단으로 표시한다.
- PostgreSQL optimized 저장 필드 간 약 0.001 GiB 차이가 있어 차트는 total_bytes / 2^30으로 계산했다. 두 값 모두 소수 둘째 자리에서 212.76 GiB다.
- PostgreSQL optimized 저장 크기를 현재 10억 행 storage.json의 212.76 GiB로 보완했다.
- 재생성: `uv run scripts/visualize_results.py`. PNG는 `results/charts/`에 저장한다.
- 흐름: 결과 JSON/JSONL → 행 수·반복·checksum 검증 → 통계 계산·summary 대조 → PNG 생성 → Markdown 보고서 표시.


## 2026-09-06

### 10억 행 최종 실험 정리

- 기준선 프로파일의 ClickHouse·DuckDB·PostgreSQL·SQLite는 모두 10억 행 적재·검증·쿼리별 30회 측정을 완료했다.
- 최적화 프로파일의 ClickHouse·DuckDB·PostgreSQL도 10억 행 검증과 쿼리별 30회 측정을 완료했다.
- 이전 생성식 오류를 수정한 DuckDB optimized 결과는 네 쿼리 checksum이 기준선과 일치해 유효 처리했다.
- 최적화 SQLite는 `small`, `medium`, `large`를 각 30회 완료했다.
- 최적화 SQLite `join`은 첫 실행이 6시간 13분 동안 결과를 반환하지 않아 중단했다. 중단 시점의 CPU 사용률은 약 100%였고, 프로세스가 멈춘 것이 아니라 장시간 실행 중인 상태였다.
- 중단 직전 SQLite optimized volume 사용량은 약 107.40GiB였으며, 컨테이너와 named volume을 삭제했다.
- 최적화 SQLite의 10억 행 `summary.json`·`storage.json`·manifest는 생성되지 않았고, 기존 동일 파일은 10,000행 파일럿 결과로 남아 있다. 10억 행 최적화 SQLite `measurements.jsonl`에는 `small`, `medium`, `large` 각 30회만 보존했다.
- 모든 엔진·프로파일의 1회 검증 checksum은 일치했다. 최적화 SQLite `join`은 결과 미반환으로 checksum이 없다.
- 최종 결과 보고서를 `results/benchmark-report.md`에 갱신했다.

## 2026-09-03

### 완료

- ClickHouse, DuckDB, SQLite, PostgreSQL 비교 목표와 실험 조건을 PRD로 정리했다.
- 단일 DB 순차 실행, 1회 결과 검증 후 100회 warm-cache 측정 정책을 정의했다.
- 20개 컬럼의 결정론적 데이터 모델과 4개 쿼리 범주를 정의했다.
- Docker Compose와 Python 기반 오케스트레이터/실행기 구조를 구현했다.
- 결과 checksum, 원시 측정값, 평균·최소·최대·표준편차·p50·p95 저장을 구현했다.

### 검증 상태

- Docker CLI 및 Docker Compose CLI: 설치 확인
- Docker Desktop 기동 후 실제 컨테이너 파일럿 완료
- 10,000행 파일럿: 4개 엔진 모두 적재·검증·측정 성공
- 파일럿 100회 반복: 4개 엔진 모두 성공
- 4개 쿼리의 엔진 간 checksum 일치 확인
- 호스트 여유 공간: 약 322GiB
- 용량 기준 정정: 10억 행 1개 DB 기준 약 372.5GiB, 5억 행 1개 DB 기준 약 186.3GiB
- 5억 행의 4개 volume 보존 기준 합계는 약 745.1GiB이며, 현재 여유 공간 약 322GiB로는 자동 차단
- 요구사항 변경: 최종 목표를 5억 행으로 조정
- 요구사항 추가: 100만 행 `account_dim` dimension 테이블과 4번째 join 쿼리 추가

### 파일럿 100회 결과

단위는 ms이며, fact 10,000행과 dimension 1,000,000행의 warm-cache 기준이다.

| DB | small 평균 | medium 평균 | large 평균 | join 평균 |
|---|---:|---:|---:|---:|
| ClickHouse | 1.198 | 2.504 | 2.426 | 6.045 |
| DuckDB | 0.122 | 0.491 | 1.137 | 1.187 |
| SQLite | 0.146 | 0.374 | 2.142 | 109.507 |
| PostgreSQL | 0.306 | 0.466 | 1.741 | 93.808 |

파일럿 결과 전체는 `results/pilot-100-summary.md`에 기록했다. 이 수치는 5억 행 성능을 예측하는 결과가 아니다.

### 다음 작업

1. 조인 포함 파일럿 결과를 기준선으로 사용한다.
2. 충분한 저장공간 확보 후 5억 행/100회 본 실험을 실행한다.

### 10억 행 재실험 준비

- 요구사항 변경: fact 테이블을 10억 행으로 확대
- 반복 횟수: ClickHouse·DuckDB 100회, PostgreSQL·SQLite 10회
- volume 삭제 직전 실제 저장 크기를 `results/<engine>/storage.json`에 기록하도록 오케스트레이터를 보완했다.
- 10억 행 ClickHouse 1차 시도는 join checksum 불일치로 중단했다. 병렬 `Float64` 합산의 반복별 미세 차이를 제거하기 위해 금액·할인율 집계를 정수 센트 기반으로 변경한 뒤 재실행한다.

### 10억 행 실험 진행 상태

- ClickHouse: 10억 행 적재·검증·쿼리별 30회 측정 완료. 실제 저장 크기 66.97GiB(로그 포함), volume 삭제 완료.
- DuckDB: 10억 행 적재·검증·쿼리별 30회 측정 완료. 실제 저장 크기 9.96GiB, volume 삭제 완료.
- PostgreSQL: 10억 행 적재·검증 완료 후 쿼리별 30회 측정 진행 중. 적재 후 DB 크기는 약 178GB이며, `small` 1회가 약 1분 45초 소요되는 상태다.
- SQLite: PostgreSQL 완료 후 10억 행·쿼리별 30회로 실행 예정.

### 실험 2: optimized 프로파일

- ClickHouse optimized: 10억 행 적재·검증·쿼리별 30회 측정 완료. 실제 저장 크기 73.26GiB, volume 삭제 완료.
- SQLite optimized 파일럿: 10,000행·쿼리별 1회 적재·검증·측정 성공.
- DuckDB optimized의 3개 ART 인덱스 생성은 약 33분 후 24.6GiB 메모리 한도로 OOM 종료됐다. 해당 volume은 삭제됐다.
- DuckDB optimized의 전역 정렬은 임시공간 200GB 한도에서 약 186GiB 사용 후 중단했고, 호스트 여유 공간 보호를 위해 재시도하지 않았다.
- DuckDB optimized 전략을 event_day 그룹별 직접 순서 적재로 변경해 `(event_day, id)` 물리 레이아웃과 zone map을 활용한다. DuckDB 대형 ART 인덱스는 생성하지 않는다.
- 변경된 DuckDB optimized 파일럿 재검증 성공. 다음 단계는 DuckDB 본 실험, 이어서 PostgreSQL·SQLite optimized 실행이다.
- PostgreSQL optimized: 10억 행 적재·4개 B-tree 인덱스 생성·1회 결과 검증 완료. volume은 약 213GiB이며, 쿼리별 30회 측정 중이다.
- PostgreSQL optimized 측정은 NUMERIC 센트 집계의 안정성을 유지하는 대신 medium 반복 1회가 약 2분 수준으로 진행 중이다. 최종 summary 확정 전에는 중간값을 사용하지 않는다.
- 교차 엔진 checksum 점검에서 DuckDB optimized의 직접 순서 적재 생성식 오류를 발견했다. `id` 표현식 괄호가 없어 category/region/event 관련 값과 집계 행 수가 기준선과 달랐으며, 해당 30회 측정값은 무효 처리한다.
- 오류 수정: DuckDB 생성식의 복합 `id` 표현식을 괄호로 감싸고 schema version을 v3으로 올렸다. 수정된 DuckDB optimized 본실험을 재실행해야 한다.
- 현재까지의 유효 결과와 미완료 항목은 `results/benchmark-report.md`에 기술 리포트 형식으로 정리했다.

### 5억 행 본 실험 진행 상태

- 실행 방식: `ClickHouse → DuckDB → PostgreSQL → SQLite` 순차 실행
- ClickHouse·DuckDB: 5억 행 fact + 100만 행 dimension 생성 → 1회 검증 → 4개 쿼리 100회 측정 → volume 삭제
- PostgreSQL·SQLite 후속 실행: 같은 데이터 생성·검증 후 쿼리별 1회 측정 → volume 삭제
- ClickHouse: 적재·검증·100회 측정 완료, volume 삭제 완료
- DuckDB: 적재·검증·100회 측정 완료, volume 삭제 완료
- SQLite: 쿼리별 1회 정책으로 재실행 중. 적재 완료 후 1회 검증 진행 중
- PostgreSQL: 쿼리별 1회 측정 완료. 최초 실행에서 join 금액 checksum 차이를 발견해 NUMERIC 합산으로 수정 후 재실행했고, 4개 checksum 모두 일치

현재 본 실험에서 완료된 warm-cache 100회 평균(ms)은 다음과 같다. 전체 결과 확정 전의 부분 결과이며, checksum은 쿼리별로 ClickHouse와 DuckDB가 일치한다.

| DB | small | medium | large | join |
|---|---:|---:|---:|---:|
| ClickHouse | 373.986 | 219.259 | 237.003 | 2,316.830 |
| DuckDB | 1.748 | 180.571 | 253.770 | 312.295 |
| PostgreSQL | 54,149.819 | 60,236.738 | 102,170.229 | 181,471.067 |

SQLite 기존 측정에서 medium 단일 반복이 약 80~115초였고 100회 수행은 비현실적으로 긴 것으로 확인됐다. 따라서 SQLite는 쿼리별 1회로 고정한다. PostgreSQL도 현재는 쿼리별 1회 결과를 기준으로 보며, 100회 반복은 추가하지 않는다.
# 2026-09-07 pandas / DuckDB 전처리 비교 추가

- 사용자 요청: 작은·중간·큰 데이터의 전처리/변환을 엔진별 30회 실행하고 평균·표준편차 등 다양한 통계 비교.
- 기존 파일·DB 결과를 보존하고 `main.py frames` 경로를 추가했다.
- 구현: 공통 Parquet 입력, 4개 작업, 파일/메모리 입력 분리, `.df()` 포함 시간,
  독립 worker 프로세스, 6개 묶음 순서 무작위화, 전체 결과 fingerprint 비교.
- 통계: 평균·표본 SD·분위수·IQR·MAD·CV·처리량, 묶음 기반 평균 구간·대응 묶음 bootstrap 배율.
- 메모리: 검증 해시의 할당을 제외하도록 별도 프로세스의 첫 연산 직후 OS 최대 RSS 수집.
- uv 프로젝트와 lockfile, 경계값/빈 입력/필터 pushdown/통계 테스트, ruff 설정 추가.
- 기본 크기는 10만·100만·1,000만 행. 호스트 48GiB RAM·논리 CPU 14개에서 수행하며
  외부 CPU 부하가 관찰되어 실행 순서별 시스템 상태를 기록한다.
- 검증: 단위 테스트 12개·ruff 검사/포맷 통과, 기존 DB CLI와 새 CLI 도움말 확인.
- 파일럿: 48개 조합 × 2회 완료 및 엔진·입력 방식 간 전체 결과 fingerprint 일치.
- 본 실험: 2026-09-07 11:28–11:33 KST, 48개 조합 × 30회 = 1,440개 시간 측정과
  조합별 별도 메모리 1회 완료. 모든 결과 fingerprint 일치, 원시 JSONL에서 평균·표본 SD·중앙값·극값·비율 독립 재계산 일치.
- 관측: 메모리 입력의 결측치·파생 컬럼은 pandas가 3개 크기 모두 우위.
  1,000만 행 Parquet 지역 집계는 DuckDB 3.36배, 조인 후 집계는 7.88배 빠름.
  100만 행 메모리 필터 작업의 작은 평균 차이는 구간이 1을 포함해 우위를 단정하지 않음.
- 환경: 시스템 전체 swap 입출력과 배경 부하 관측. 합성 수치형·warm-cache·최대 논리 610MiB 범위로 결론 제한.
- 시각화: 시간 분포 2개 및 배율 1개 PNG를 직접 확인. 배율 그래프의 눈금 겹침을 수정하고
  두 패널 축 범위를 통일. 측정 당시 렌더러 소스와 후속 렌더러 해시를 보존해 계산 변경과 구분했다.
- 결과: `results/pandas-duckdb/run-20260907T022808832856Z/report.md`, `summary.csv`,
  `measurements.csv`, `memory.csv`, `qa.json` 및 실행·입력 메타데이터.
