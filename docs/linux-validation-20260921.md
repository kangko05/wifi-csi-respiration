# Linux 환경 검증 및 기존 데이터 재현 — 2026-09-21

## 범위와 목적

환경 이전 후 기존 C 진폭·위상 계산이 재현되는지 확인했다. 사용자가 승인한 1~3단계(환경 준비, C 대조 테스트, 21개 데이터 재실행)만 수행했다. 미완성 Python 전처리와 관련된 기존 실패 14건은 실행·수정 범위에서 제외했다. 임계값 튜닝, CIR, 보드 통합, 포트 접근, 커밋·푸시는 하지 않았다.

## 환경과 실행

- Python 3.14.4; gcc (Ubuntu 15.2.0-16ubuntu1) 15.2.0; cmake version 4.2.3.
- `python/.venv`를 새로 생성하고 `python/requirements.txt`의 고정 버전을 설치했다. `pip check` 통과.
- 최초 설치는 sandbox 네트워크 DNS 오류로 실패했다. 네트워크 권한 승인 후 같은 명령으로 설치 완료.
- `python/.venv/bin/python -B -m unittest discover -s tests -v`: **15개 통과, 실패·오류·skip 0**. C 진폭/위상 수치 대조, 합성 전체 경로, 필터·피크 계수, 설정 배치, 마스크·공백·wrap, 실행기 입력 보존을 검사했다.

재실행 명령(저장소 루트 기준; 재실행 시 새 출력 경로 사용):

```bash
python/.venv/bin/python -B python/scripts/run_c_amplitude.py --method amplitude --output outputs/linux-validation/20260921T023240_594453Z/amplitude
python/.venv/bin/python -B python/scripts/run_c_amplitude.py --method phase --output outputs/linux-validation/20260921T023240_594453Z/phase
python/.venv/bin/python -B python/scripts/compare_c_phase.py outputs/linux-validation/20260921T023240_594453Z/phase --amplitude-run outputs/linux-validation/20260921T023240_594453Z/amplitude
```

## 결과

- 진폭: 21개 처리, 오류 0, 채택 0. `outputs/c-amplitude/20260918T064726_104124Z`의 기존 필드와 비교하여 입력 정보, 모든 열의 수치·판정·사유가 정확히 일치했다. 호스트별 원본 절대 경로는 비교에서 제외했다.
- 위상: 21개 처리, 오류 0. `outputs/c-phase/20260918T073801_465366Z`와 bpm·선택 열·피크 수·판정이 정확히 일치했다. sharpness 최대 절대 차이 3.98e-12; 비교 허용 오차 1e-6.
- 보존된 Python 위상 결과와 21/21 일치. bpm 최대 차이 2.14e-14, sharpness 최대 차이 4.30e-12 미만.
- 100cm 위상 결과: 16.2 / 16.8 / 16.8 bpm으로 기존과 동일.
- 재실 18개 중 위상 17개 채택; 채택 MAE 0.67647 bpm. 완전 빈 방 2개도 모두 채택하는 기존 한계가 그대로 재현됐다. 중단 빈 방 1개는 보류.
- 이는 기존 결과의 재현 확인이며 새 독립 정확도 평가가 아니다. 수동 기준은 추정 후 평가에만 사용했으며 동기화가 미확인이다.

## 보존과 산출물

원본 84개와 레거시 소스 44개가 이전 `sources_before.json`과 일치했다. 이번 실행 전후에 원본·vendor·C 소스 총 138개 파일의 SHA-256도 불변이었다. 기존 사용자 변경(`.clangd`, `.gitignore`, `src/phase.c`)은 보존했다. 알고리즘·테스트 코드 변경은 없었다.

결과 폴더: [`outputs/linux-validation/20260921T023240_594453Z`](../outputs/linux-validation/20260921T023240_594453Z). `tests-initial.txt`, `dependencies.txt`, `environment.json`, 실행 로그, 두 경로의 세션 JSON·summary.csv·manifest.json, `amplitude-comparison.json`, `phase-comparison.json`, `phase/comparison.json`, `preservation.json`에 근거를 남겼다.

다음 단계는 메모리·처리 시간 확인과 온디바이스 처리 구조 또는 빈 방 오검출 개선의 우선순위 결정이다. 이번에는 수행하지 않았다.
