# 새 수집 21개 세션 · 레거시 비교 1회 실행 기록 (2026-09-17)

## 0. 사후 수동 기준 평가 (2026-09-17, 추정 후 제공 · 재실행 없음)

추정이 끝난 뒤 사용자가 수동 호흡 횟수를 제공했다. 시간순(오래된 것부터) 원문은
`0 24 30 27 33 32 37 36 30 33 34 32 34 28 30 33 30 28 23`이다. 19개 값은 첫 빈 방 1개와 재실 18개에
대응하고, 마지막 빈 방 2개(완전 1·중단 1)는 **횟수가 없어 미제공으로 남겼다.** 각 값은 **명목 120초 전체 구간의
사용자 보고 총 횟수**로 해석했다. 이 해석은 작업 지시에서 추정한 것이며, 시작·끝 동기화와 세는 불확실성은
확인되지 않았으므로 정밀한 동기화 기준이 아니다. 목표 bpm은 재실 세션에 한해 횟수/2다. 경로별로 잘린
분석 길이는 분모로 쓰지 않았다. 빈 방의 0은 **부재 기준**이며 0bpm 목표가 아니다. 레거시 기준 라벨은 쓰지 않았다.

- 기준: `references/manual_breath_counts_20260916.csv`, 원문·해석 기록은 `.provenance.json`
- 평가: `outputs/legacy_run_20260917/posthoc_manual_20260917_122011/`(`posthoc_rows.csv`, `posthoc_summary.json`, `report.md`)
- 스크립트: `scripts/posthoc_manual_eval.py`(표준 라이브러리만 사용, 고정된 `report/summary.csv` 63행만 읽음)
- 고정 예측 `report/summary.csv`·`manifest.json`·`evaluation.json`의 SHA256은 평가 전후 동일하다(`summary.csv` = `bedaaa2e…9ab9`). 추정·임계값·선택은 바꾸지 않았다.

재실 18개 전체. 진단은 보류를 포함한 모든 진단 피크이고, 채택은 채택된 후보만이다.
"≤1칸"은 각 행 자신의 실제 분해능 이하인 경우다.

| 방식 | 채택 | 진단 n / MAE / ±1.1 / ±2.2 / ≤1칸 | 채택 n / MAE / ±1.1 / ±2.2 / ≤1칸 |
| --- | --- | --- | --- |
| cir | 18/18 | 18 / 0.594 / 14 / 18 / 11 | 18 / 0.594 / 14 / 18 / 11 |
| phase | 17/18 | 18 / 2.706 / 12 / 17 / 9 (보류 52.2 포함) | 17 / 0.676 / 12 / 17 / 9 |
| amplitude | 0/18 | 18 / 2.880 / 10 / 15 / 8 | 0 / — |

거리별 결과(각 그룹 n=3, 진단 ±1.1 / ±2.2):

- **100cm** (목표 18 / 15 / 16.5): phase·cir 둘 다 16.2 / 16.8 / 16.8을 채택했다. ±1.1 안은 1/3, ±2.2 안은 3/3, MAE 1.300이다. amplitude(전부 보류)는 진단 1/3, 3/3이다.
- **200cm**: phase 2/3, 3/3 · cir 2/3, 3/3 · amplitude 2/3, 3/3이다.
- **300cm**: phase 2/3, 3/3 · cir 2/3, 3/3 · amplitude 1/3, 2/3(진단 MAE 5.18)이다.
- **500cm** (목표 15 / 14 / 11.5): cir 채택값 15.0 / 14.4 / 12.3의 오차는 0.0 / 0.4 / 0.8로 **3/3 모두 ±1.1 안**이다. phase는 채택 2개(14.4, 12.3)가 모두 ±1.1 안이고, 보류된 1개의 진단값 52.2는 오차 37.2다. amplitude 진단은 1/3, 1/3이다.
- **30cm**: 세 방식 모두 3/3, 3/3이다. **60cm**: cir 3/3, 3/3 · phase 2/3, 3/3 · amplitude 2/3, 3/3이다.
- **빈 방(완전 2)**: phase·cir이 **2/2 모두 후보를 채택**했고 amplitude는 0/2다. 기준 역할은 `…_c80cd924`가 부재(0), `…_aca6e871`이 미제공이다.
- **빈 방(운영자 중단 1)**: 세 방식 모두 보류했고 횟수는 미제공이다.

해석: 이번 묶음에서는 cir이 500cm를 포함한 재실 세션의 주기와 잘 맞았다는 **관측**이다.
이것은 케이스 교체의 인과 효과를 보여 주지 않는다. 짝지어진 전/후 비교가 없고, 검증된 부재 게이트나
일반적인 5m 정확도를 보여 주지도 않는다. 빈 방 2개에서 채택이 계속 나왔고, 기준은 동기화되지 않았으며,
같은 데이터에서 본 사후 1회 결과이기 때문이다. phase와 cir은 전처리를 공유하므로 둘의 일치는 독립 증거가 아니다.

아래 1–7절은 **실행 시점(기준 없음)** 기록이다. "정답이 없다", "채점하지 않았다"는 서술은 이 0절로 대체됐다.

사용자 요청으로 **새로 수집한 21개 세션 전체**를 레거시(`../wifi-csi-proto`)의 기존
`compare_phase_cir` 경로에 **한 번** 통과시켜 현재 시스템이 무엇을 보고하는지 확인했다.
케이스 교체 이후의 관측 점검이며 **정확도 평가나 탐색·튜닝이 아니다.**

- 실행 전 고정한 사양·중단 기준: [`outputs/legacy_run_20260917/run_spec.json`](../outputs/legacy_run_20260917/run_spec.json)
- 레거시 HTML 보고서: `outputs/legacy_run_20260917/report/index.html`
- 그룹별 요약: `outputs/legacy_run_20260917/grouped_summary.csv` · `.json`
- 원본 대비 해시 스냅샷: `sources_before.json` / `sources_after.json`
- 실행·테스트 로그: `outputs/legacy_run_20260917/logs/`

레거시 저장소는 **읽기 전용**으로만 사용했다. 파일을 고치지도, 복사하지도, 그 `.venv`에
설치하지도 않았다. 실행 전후 해시 검증에서 `data/` 84개 파일과 레거시 소스 44개 파일이
**모두 동일**했고 레거시 git HEAD도 `397813b9…` 그대로다.

## 1. 실행 명령

> 2026-09-17 이후 경로 변경: 이 Python 프로젝트(`scripts/`, `src/`, `tests/`, `docs/`, `outputs/`, `references/`, `.venv`)는 저장소의 `python/` 폴더로 옮겨졌다. 원본 `data/`는 저장소 루트에 남았고, 레거시는 `python/`에서 보면 `../../wifi-csi-proto`다. 1.1과 본문의 명령·경로는 **저장소 루트에서 실행한 당시 기록**이므로 그대로 둔다. 1.2는 `python/` 폴더 기준으로 적었다. 옮기면서 스크립트의 루트·`DATA_ROOT`·레거시 경로 계산 줄을 고쳤기 때문에, 현재 스크립트의 SHA256은 `report/manifest.json`에 기록된 실행 당시 값과 다르다. 실행 기록 JSON에 남은 절대 경로 중 `outputs`를 가리키는 것은 이동 전 경로다.

### 1.1 실제로 실행한 명령 (2026-09-17, 기록용)

저장소 루트, PowerShell. `run_spec.json`은 이 명령들보다 먼저 작성했고, `outputs\legacy_run_20260917\logs`
디렉터리도 먼저 만들었다. 아래 출력 디렉터리는 **이미 채워져 있으므로 스크립트가 재사용을 거부한다.**
다시 실행할 때는 1.2를 따른다.

```powershell
$o = "outputs\legacy_run_20260917"
.\.venv\Scripts\python.exe -m pip install numpy        # 설치된 버전: numpy 2.5.3
.\.venv\Scripts\python.exe -B scripts\verify_sources.py --write "$o\sources_before.json"
.\.venv\Scripts\python.exe -B scripts\adapt_to_legacy.py data --output $o
$env:MPLCONFIGDIR = "$PWD\$o\mplconfig"; $env:PYTHONDONTWRITEBYTECODE = "1"
..\wifi-csi-proto\.venv\Scripts\python.exe -B scripts\run_legacy_compare.py "$o\derived" --output "$o\report"
.\.venv\Scripts\python.exe -B scripts\verify_sources.py --check "$o\sources_before.json"
.\.venv\Scripts\python.exe -B scripts\summarize_by_conditions.py --run $o
.\.venv\Scripts\python.exe -m unittest discover -s tests -v   # 이 실행에는 -B를 붙이지 않았다
```

각 출력은 `logs/01`~`07`에 있다(07은 문서 작성 후 한 번 더 한 `--check`).

### 1.2 새로 재현할 때 (`python/` 폴더에서, 새 디렉터리 사용, 기존 출력 덮어쓰기 금지)

```powershell
$o = "outputs\legacy_run_$(Get-Date -Format yyyyMMdd_HHmmss)"
New-Item -ItemType Directory "$o\logs" | Out-Null   # 이미 있으면 실패해야 정상
# run_spec.json을 먼저 $o에 작성한다
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # 처음 한 번
.\.venv\Scripts\python.exe -B scripts\verify_sources.py --write "$o\sources_before.json"
.\.venv\Scripts\python.exe -B scripts\adapt_to_legacy.py ..\data --output $o
New-Item -ItemType Directory "$o\mplconfig" | Out-Null
$env:MPLCONFIGDIR = "$PWD\$o\mplconfig"; $env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python.exe -B scripts\run_legacy_compare.py "$o\derived" --output "$o\report"
.\.venv\Scripts\python.exe -B scripts\verify_sources.py --check "$o\sources_before.json"
.\.venv\Scripts\python.exe -B scripts\summarize_by_conditions.py --run $o
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

`adapt_to_legacy.py`는 `$o\derived`가 이미 있으면, `run_legacy_compare.py`는 `$o\report`가
비어 있지 않으면 중단한다.

1.2는 2026-09-17 이후 구성 기준이다. 레거시 코드는 `vendor/wifi-csi-proto/`의 바이트 동일 복사본(커밋 `397813b9…`)에서 가져오고, scipy·matplotlib·threadpoolctl도 이 프로젝트 `.venv`에 같은 버전으로 설치한다. 그래서 옆 폴더 체크아웃이나 레거시 `.venv`가 필요 없다. 이 구성에서 `verify_sources.py --check outputs/legacy_run_20260917/sources_before.json`은 통과했다(레거시 44개 파일 해시 일치). 클론과 같은 파일 구성으로 세션 `…_aca6e871`을 다시 돌렸을 때도 기존 결과와 같았다(amplitude 4.248 보류, phase·cir 15.60 채택). 이때 파이썬 버전은 3.14로 당시 3.13과 달랐다.

수치 라이브러리는 레거시 `.venv`(python 3.13.15, numpy 2.5.3 · scipy 1.18.1 ·
matplotlib 3.11.2 · threadpoolctl 3.6.0)를 **그대로 재사용**했고 거기에 설치한 것은 없다.
어댑터·테스트용으로 현재 프로젝트 `.venv`에만 `numpy==2.5.3`을 추가했다.
`-B`와 `MPLCONFIGDIR` 재지정으로 레거시 트리에 캐시를 쓰지 않았다.

## 2. 래퍼가 필요했던 이유 (기록된 유일한 차이)

`../wifi-csi-proto/scripts/compare_phase_cir.py:159-163`은 `--output`이 **레거시 저장소의
`outputs/` 안**이 아니면 거부한다. 레거시는 읽기 전용이므로 스크립트를 직접 호출해 이
프로젝트로 출력할 수 없었다. 그래서 `scripts/run_legacy_compare.py`가 그 `main()`의 진행
순서를 그대로 재현하되 **출력 위치 검사만** 이 프로젝트의 `outputs/` 기준으로 바꾼다.

추정·판정·보고·해시 검증은 전부 레거시의 **변경되지 않은** 코드다: `load_session`,
`correct_gain`, `assess_quality`, `estimate_periodicity`, `column_evidence`,
`run_phase_cir`, `blank_row`, `evaluate_rows`, `totals`, `plot_session`, `write_report`,
`write_csv`, `save_json`, `fingerprints`. 임계값도 전부 레거시 기본값이다.
진폭 채택은 `estimate_periodicity`가 정한다(gain `none`, 0.05–0.8Hz, 집중도 0.35, ACF 0.3,
PSD/ACF 일치, 길이·주기 수·대역 경계 검사). phase/cir 채택은 sharpness>1.55, 자체 차이<3bpm이다.
`EvidenceConfig`의 국소 SNR(기준 3)은 이 실행에서 `amplitude_local_snr`로 **계산·보고만** 하며
채택 게이트가 아니다.
이 차이와 레거시 커밋·소스 44개의 SHA256은 `report/manifest.json`의 `wrapper` 블록에 남겼다.

실행 후(2026-09-17) 래퍼 docstring의 위 임계값 설명을 **주석만** 고쳤다. `report/manifest.json`에는
실행 당시 래퍼 SHA256 `0a4fa4f765cd0cffba1a928c66720ba392ca955f9e75270e9d867790efcbeaca`가 그대로
남아 있으며 현재 파일 해시는 이와 다르다.

## 3. 입력 변환 (무손실 어댑터)

`serial.bin` + `lines.csv` + `session.json` → 파생 `csi_raw.npy` + `meta.csv` + `session.json`.
원본은 열기만 했고 `data/` 아래에는 아무것도 쓰지 않았다.

- 읽기 전에 `serial.bin`의 SHA256을 원본 매니페스트 값과 대조한다.
- 페이로드는 기록된 **바이트 오프셋에서 다시 읽어** base64 `validate=True`로 디코드한다.
  디코드 길이가 선언 `len`(234)과 수집기의 `decoded_length` **양쪽**과 같아야 통과한다.
- 바이트 순서 그대로의 **signed int8**: 짝수 바이트 허수, 홀수 바이트 실수 → 117 bin.
- **중복 제거·보간·재표본화·시간 압축·공백 메움 없음.** 반복 `seq`도 그대로 둔다.
- 파싱 실패 행은 제외하고 `excluded_rows.csv`에 원본 line index·바이트 오프셋·호스트
  시각·오류 문자열과 함께 남긴다. **유효한 0으로 바꾸지 않는다.**
- `rowmap.csv`가 패킷마다 원본 line index, 바이트 오프셋, 호스트 시각, **원래 장치
  `local_timestamp`** 를 보존한다.

변환 결과(21개 전부 성공): 보존 패킷 합계 **249,436**, 제외 행 합계 **35**.
제외 35행의 내역은 세션당 1~2행이며 끝의 미완성 행 `trailing_partial` 20건,
첫 부분 수신 행 `other` 12건, 아래 malformed 첫 행 3건이다.

| 세션 | 제외 사유 |
| --- | --- |
| `…_e2d24064` (60cm) | 첫 행 `field_count:14` |
| `…_fcb222ce` (200cm) | 첫 행 `field_count:14` |
| `…_812d036c` (500cm) | 첫 행 `field_count:29` |

반복 `seq` 2쌍(`…_489fce0f` seq 393034, `…_168e3beb` seq 630180)은 시각·내용이 다르므로
**둘 다 보존**했고 파생 `session.json`의 `seq_repeats`에 기록했다.

### 3.1 장치 시각 정책 — 1개 세션에서 uint32 wrap

레거시 리더는 `local_timestamp`가 **엄격히 증가하는 uint32**일 것을 요구하고 추측 복구를
하지 않는다. 21개 중 20개는 이미 그 조건을 만족해 **원본 값 그대로** 옮겼다.

`20260916T060200_034260_9dd9fd80` (100cm) 하나에서 32비트 마이크로초 카운터가
**한 번 wrap**한다: 정렬 패킷 167에서 `4294962111 → 4815`, 함의 간격은
(2³² − 4294962111) + 4815 = 5185 + 4815 = **10000µs**로 주변 100Hz 간격과 일치한다
(어댑터 `timestamp_policy.wraps[0].implied_interval_us` = 10000, 출력 시각 1660004 → 1670004로 재확인). 사용자가 요청한 "전체 데이터 1회 처리"를 위해 이 세션에만
명시적으로 기록된 정책 `unwrap_uint32_rebased`를 적용했다: wrap마다 2³²를 더해 펴고,
uint32 범위에 다시 들어가도록 첫 패킷 기준으로 평행 이동한다. **연속 차이는 하나도
바뀌지 않으며** 원래 장치 값은 `rowmap.csv`에 그대로 남는다. 정책·wrap 위치·뺀 값은
파생 `session.json`의 `timestamp_policy`와 `adapter_sidecar.json`에 있다.

**한계로 명시한다:** 이것은 축약 카운터의 wrap을 복원한 것이지 장치 시계의 정확성을
검증한 것이 아니다. 단일 깨끗한 wrap이 아닌 역전·중복·리셋은 복구하지 않고 예외로
중단하도록 했고, 테스트로 그 거부를 확인했다.

### 3.2 중단된 수집의 취급

`20260916T063413_795063_e446ed62` (빈 방)은 **사용자가 중단을 확인**한 수집이다.
원본 매니페스트는 여전히 `complete`이고 **원본은 수정하지 않았다.** 운영자 진술은 파생
`session.json`의 `annotations` 블록에만 `operator_interrupted: true`로 기록했다.

- 실제 수신 CSI: 9406 패킷, 장치 시간 **94.050207초**, 마지막 CSI 호스트 시각 94.033377초.
- 그 뒤 **122.758536초** 동안 CSI 행이 없고 216.791913초에 미완성 바이트 1개로 끝난다.
- 따라서 **216.8초를 유효한 CSI 길이로 쓰지 않았고 뒷부분을 채우지 않았다.**
  실제로 세 경로 모두 94.05초(진폭은 FIR 경계 제외 후 91.90초)만 분석했다.
- 실제 분해능도 달라진다(`report/summary.csv`의 경로별 값): 이 수집은 진폭 0.6522bpm/칸,
  phase·cir 0.6379bpm/칸. 완전 수집 20개는 4.1의 표를 본다.
- 보고에서 완전 수집 빈 방 2개와 **분리**해 제시한다.

## 4. 결과

전 세션 공통: 채널 36, rx_format 2, `first_word` 0, `dropped` 0, 네이티브 100.00Hz,
117 bin 중 null 3개(버퍼 57·58·59) 확인, 레거시 HT40 layout 검사 통과.
그룹 라벨은 **운영자가 입력한 조건**이며 검증된 전파 기하가 아니다.

### 4.1 그룹별 채택 수 (기존 관심사인 100cm 먼저)

| 그룹 | 세션 | amplitude 채택 | phase 채택 | cir 채택 |
| --- | --- | --- | --- | --- |
| **100cm** | 3 | **0 / 3** | 3 / 3 | 3 / 3 |
| 200cm | 3 | 0 / 3 | 3 / 3 | 3 / 3 |
| 300cm | 3 | 0 / 3 | 3 / 3 | 3 / 3 |
| 500cm | 3 | 0 / 3 | 2 / 3 | 3 / 3 |
| 30cm | 3 | 0 / 3 | 3 / 3 | 3 / 3 |
| 60cm | 3 | 0 / 3 | 3 / 3 | 3 / 3 |
| 빈 방(완전 수집) | 2 | 0 / 2 | **2 / 2** | **2 / 2** |
| 빈 방(운영자 중단) | 1 | 0 / 1 | 0 / 1 | 0 / 1 |

진단 피크는 63행 전부 출력됐다. 즉 "채택 0"은 값이 없다는 뜻이 아니라 **기존 임계값을
넘지 못해 보류**됐다는 뜻이다.

경로별 실제 분석 길이와 실제 1칸(Fs/N), `report/summary.csv`의 `analysis_duration_s`·`resolution_bpm`:

| 경로 | 완전 수집 20개 길이(초) | 완전 수집 20개 1칸(bpm) | 중단 수집 길이(초) | 중단 수집 1칸(bpm) |
| --- | --- | --- | --- | --- |
| amplitude (FIR 경계 제외) | 117.80–118.00 | 0.5080–0.5089 | 91.90 | 0.6522 |
| phase | 119.97–120.01 | 0.4999–0.5001 | 94.05 | 0.6379 |
| cir | 119.97–120.01 | 0.4999–0.5001 | 94.05 | 0.6379 |

### 4.2 100cm (기존 관심 거리) — 세션별

| 세션 | 방식 | 상태 | 진단 bpm | 채택 bpm | 근거 | 보류 이유 |
| --- | --- | --- | --- | --- | --- | --- |
| `…_9dd9fd80` | amplitude | 보류 | 19.70 | — | 집중도 0.098, ACF 0.321, 국소SNR 42.2(진단값) | `diffuse_spectrum`,`psd_acf_disagreement` |
| | phase | 채택 | 16.20 | 16.20 | sharpness 6.91, 자체 차이 1.54 | — |
| | cir | 채택 | 16.20 | 16.20 | sharpness 12.16, 자체 차이 1.54 | — |
| `…_0bd26482` | amplitude | 보류 | 16.48 | — | 집중도 0.181, ACF 0.429, 국소SNR 4.6 | `diffuse_spectrum`,`psd_acf_disagreement` |
| | phase | 채택 | 16.80 | 16.80 | sharpness 11.91, 자체 차이 1.22 | — |
| | cir | 채택 | 16.80 | 16.80 | sharpness 21.17, 자체 차이 1.20 | — |
| `…_4c4d38b6` | amplitude | 보류 | 16.70 | — | 집중도 0.178, ACF 0.382, 국소SNR 73.6 | `diffuse_spectrum`,`psd_acf_disagreement` |
| | phase | 채택 | 16.80 | 16.80 | sharpness 14.79, 자체 차이 0.06 | — |
| | cir | 채택 | 16.80 | 16.80 | sharpness 29.46, 자체 차이 0.10 | — |

분석 길이·실제 1칸: amplitude 118.00/118.00/117.90초·0.5080/0.5080/0.5085bpm,
phase·cir 120.01/120.01/120.00초·0.4999/0.4999/0.5000bpm (세션 순서대로). 사용 패킷 12001–12002.
표의 국소SNR은 보고용 진단값이며 채택 판정에 쓰이지 않는다.
`…_9dd9fd80`은 위 3.1의 wrap 정책이 적용된 세션이다.

### 4.3 200 / 300 / 500cm

| 세션 | amplitude(진단/집중도) | phase(진단·sharpness) | cir(진단·sharpness) |
| --- | --- | --- | --- |
| 200 `…_8a5360c8` | 18.46 / 0.166 보류 | 18.60 채택 · 7.25 | 18.60 채택 · 16.66 |
| 200 `…_2352edd0` | 16.19 / 0.133 보류 | 15.90 채택 · 3.47 | 16.20 채택 · 11.07 |
| 200 `…_fcb222ce` | 17.07 / 0.060 보류 | 17.10 채택 · 5.26 | 17.10 채택 · 9.81 |
| 300 `…_53ead452` | 14.28 / 0.084 보류 | 14.10 채택 · 3.47 | 14.10 채택 · 8.26 |
| 300 `…_fdb6204f` | 16.77 / 0.084 보류 | 16.80 채택 · 5.63 | 16.80 채택 · 17.07 |
| 300 `…_cec1170d` | 3.00 / 0.040 보류 (`search_band_edge`) | 15.90 채택 · 2.28 | 15.90 채택 · 5.85 |
| 500 `…_017a033d` | 15.75 / 0.038 보류 | **52.20 보류** · 1.49, 자체 차이 9.45 | 15.00 채택 · 5.23 |
| 500 `…_812d036c` | 38.89 / 0.031 보류 | 14.40 채택 · 1.57 | 14.40 채택 · 4.10 |
| 500 `…_168e3beb` | 14.21 / 0.032 보류 | 12.30 채택 · 3.25 | 12.30 채택 · 18.97 |

`…_017a033d`의 phase 보류 사유는 `insufficient_backup_sharpness`(1.49 ≤ 1.55)와
`spectral_peak_count_disagreement`(9.45 ≥ 3bpm)다. `…_812d036c`의 phase는 sharpness
1.569로 임계 1.55를 **아슬아슬하게** 넘어 채택됐다. 거리 라벨이 커질수록 진폭 집중도와
backup band SNR이 대체로 낮아지지만, 이는 **관측된 경향**이며 거리 효과의 증명이 아니다.

### 4.4 30 / 60cm

| 세션 | amplitude(진단/집중도) | phase(진단·sharpness) | cir(진단·sharpness) |
| --- | --- | --- | --- |
| 30 `…_a9836fd9` | 12.16 / 0.359 보류 | 12.00 채택 · 19.16 | 12.00 채택 · 36.02 |
| 30 `…_489fce0f` | 14.72 / 0.195 보류 | 14.70 채택 · 40.55 | 14.70 채택 · 65.73 |
| 30 `…_89412768` | 13.48 / 0.254 보류 | 13.50 채택 · 8.47 | 13.50 채택 · 10.16 |
| 60 `…_e2d24064` | 17.80 / 0.090 보류 | 17.70 채택 · 3.24 | 16.80 채택 · 4.15 |
| 60 `…_5f38716a` | 15.60 / 0.317 보류 | 15.90 채택 · 21.55 | 15.90 채택 · 27.19 |
| 60 `…_08098c26` | 17.80 / 0.128 보류 | 18.00 채택 · 2.35 | 18.00 채택 · 3.02 |

`…_a9836fd9`는 집중도 0.359로 0.35를 넘었지만 `psd_acf_disagreement`로 보류됐다.
**낮은 채택 수를 이유로 임계값을 내리지 않았다.**

### 4.5 빈 방 — 0bpm 정답이 아니라 오검출 관측

| 세션 | 방식 | 상태 | 진단 bpm | 채택 bpm | sharpness / 자체 차이 |
| --- | --- | --- | --- | --- | --- |
| `…_c80cd924` (완전) | amplitude | 보류 | 41.31 | — | 집중도 0.023 |
| | phase | **채택** | 4.20 | 4.20 | 1.562 / 0.33 |
| | cir | **채택** | 39.90 | 39.90 | 2.170 / 1.40 |
| `…_aca6e871` (완전) | amplitude | 보류 | 4.25 | — | 집중도 0.045 |
| | phase | **채택** | 15.60 | 15.60 | 1.594 / 2.88 |
| | cir | **채택** | 15.60 | 15.60 | 4.186 / 0.95 |
| `…_e446ed62` (운영자 중단, 94.05초) | amplitude | 보류 | 7.03 | — | 집중도 0.058 |
| | phase | 보류 | 23.40 | — | 1.320 / 0.83 → `insufficient_backup_sharpness` |
| | cir | 보류 | 23.40 | — | 1.477 / 5.19 → `insufficient_backup_sharpness`,`spectral_peak_count_disagreement` |

**이 표가 이번 실행에서 가장 중요한 결과다.** 재실 인원 0인 완전 수집 2개 모두에서
phase와 cir이 후보를 채택했고, 그중 `…_aca6e871`의 15.60bpm은 사람 호흡 범위 한가운데
값이다. 즉 **다른 거리군의 "채택"을 호흡 검출의 증거로 읽을 수 없다.** 이 행들은 0bpm
회귀 정답이 아니라 **빈 방 후보/오검출 관측**으로만 센다.

중단 수집은 세 경로 모두 보류했지만, 이것도 "빈 방을 옳게 거부했다"는 근거가 아니다.
길이가 94.05초로 짧아 분해능·sharpness 조건이 달라졌을 뿐일 수 있다.

## 5. 해석 한계 (명시)

1. **[실행 시점 기록 · 0절 사후 평가로 대체됨] 정답이 없다.** 이 21개에 대한 수동 호흡 횟수 기록은 존재하지 않고, 만들지도 않았다.
   레거시의 기존 기준 라벨은 다른 데이터의 것이므로 불러오지 않았다. 따라서 어떤
   추정치도 채점되지 않았고, 레거시 보고서의 `counted`/`far` 표는 **구조상 비어 있다**
   (레거시의 디렉터리명 정규식이 이 세션 ID와 맞지 않아 `distance_group`은 전부
   `unknown`, `reference_group`은 전부 `unavailable`이다). 거리 그룹핑은 이 문서와
   `grouped_summary.*`에서 운영자 입력값으로 따로 만든 것이다.
2. **정확도나 도달 거리 개선을 주장하지 않는다.** 케이스 교체는 사용자가 확인했다.
   체감 범위가 늘었다는 것은 사용자 보고이며, 케이스 재질과 교체 후 안테나 배치·간격은
   미확인이다. 짝지어진 전/후 측정과 수동 기준이 없으므로 이번 실행은 **인과나 정확도
   근거가 아니다.**
3. **진폭 채택 0/21**은 실패로도 성공으로도 읽지 않는다. 기존 채택 기준(집중도 0.35,
   ACF 0.3, PSD/ACF 일치 등)을 그대로 유지한 결과이며, 진단 피크는 63행 전부 출력됐다.
   국소 SNR 3은 진단 보고용이며 채택 기준이 아니다.
4. phase와 cir은 백업의 RMS 정규화·LoS WLS 위상 보정을 **공유**하므로 서로 독립된 증거가
   아니다. 두 값이 같다는 사실이 호흡의 독립 검증이 아니다.
5. cir 게이트(sharpness>1.55, 자체 차이<3bpm)는 **CIR 전용으로 검증되지 않은 임시 규칙**
   이고, CIR 표현은 레거시의 114열 압축 IFFT다. tap을 물리 거리나 특정 반사 경로로
   해석하지 않는다.
6. 고정 ±1.1/±2.2bpm와 **실제 분해능**은 다르다. 완전 수집은 amplitude 0.5080–0.5089,
   phase·cir 0.4999–0.5001bpm/칸이고, 중단 수집은 amplitude 0.6522, phase·cir 0.6379bpm/칸이다(4.1).
7. 시간 지지가 경로마다 다르다. 진폭은 FIR 경계를 제외하고, phase/cir은 레거시 IIR의
   패딩을 유지한 전체 기록이다. 각 세션의 실제 시작/끝은 `report/summary.csv`에 있다.
8. 한 세션에 uint32 wrap 복구 정책을 적용했다(3.1). 차이는 보존되지만 verbatim은 아니다.
9. 이 실행은 **탐색용 1회 관측**이다. 같은 데이터에서 설정을 반복 선택하지 않았고,
   할 계획도 이 범위에 없다.

## 6. 산출물 위치

```
outputs/legacy_run_20260917/
  run_spec.json              실행 전 고정한 사양·임계값·중단 기준
  sources_before.json        data/ 84개 + 레거시 소스 44개 SHA256 (실행 전)
  sources_after.json         같은 목록 (실행 후, 전부 일치)
  adapter_manifest.json      21개 세션 변환 기록·어댑터 코드 해시
  derived/<session_id>/      csi_raw.npy, meta.csv, session.json (레거시 입력)
                             rowmap.csv, excluded_rows.csv, adapter_sidecar.json (출처)
  report/index.html          레거시 비교 HTML 보고서 (열람용)
  report/summary.csv         63행(21세션 x 3경로) 전체 결과
  report/manifest.json       레거시 설정·버전·코드/입력 해시·래퍼 차이 기록
  report/evaluation.json     레거시 코호트 집계 (기준이 없어 비어 있음)
  report/<session_id>.png    경로별 파형·스펙트럼
  report/<session_id>.npz    원본 좌표·마스크·파형·스펙트럼 배열
  grouped_summary.csv/.json  운영자 조건 기준 거리/빈 방 재그룹
  logs/                      01~07 실행·테스트·최종 해시 확인 로그
```

## 7. 테스트

현재 프로젝트 전체 `unittest discover -s tests`: **62개 통과, 실패 0**
(기존 41개 + 어댑터 계약 21개). 로그: `outputs/legacy_run_20260917/logs/06_tests.log`.

새 테스트(`tests/test_legacy_adapter.py`)는 실제 수집기(`csi_collect.recorder`)를 가짜
시리얼로 돌려 만든 진짜 수집 산출물을 입력으로 쓴다. 다루는 계약:

- signed int8 I/Q 왕복(-128·127·-1 포함)과 짝수=허수/홀수=실수 바이트 순서
- `rowmap.csv`의 원본 line index·바이트 오프셋·호스트 시각으로 페이로드 재현
- `meta.csv` 행과 패킷 1:1 정렬, 와이어 문자열 원문 유지, `n_records`/`n_kept` 일치
- 엄격 증가 장치 시각은 verbatim, 불규칙 공백은 압축되지 않음
- uint32 wrap 복구·평행 이동이 모든 차이를 보존하고 정책이 기록됨
- wrap이 아닌 역전·중복 시각은 **거부**됨
- 반복 `seq` 보존, malformed 첫 행 제외·기록 및 **0으로 채우지 않음**
- 첫 부분 수신 행·끝 미완성 행 제외·기록
- 선언 길이 불일치·예상 밖 `rx_format` 제외
- `serial.bin` 변조 시 거부
- 변환 후 원본 디렉터리 해시 불변, 파생 위치가 원본 안이면 거부
- 중단 주석이 **파생 JSON에만** 들어가고 원본 매니페스트는 `complete` 그대로
- 레거시 `load_session`이 파생 세션을 읽어 같은 값을 돌려줌(통합 2건, 레거시 없으면 skip)

## 8. 실행 후 문서·출처 정정 (2026-09-17, 재실행 없음)

Codex 검토에 따라 문서와 출처 기록만 고쳤다. 원본 데이터, 파생 입력, 63개 수치 결과는 바꾸지 않았다.

- `run_spec.json`: `recorded_at_utc`의 자리표시값 `2026-09-17T00:00:00+00:00`을 `null`로 바꿨다. 실제 작성 시각은 기록되지 않았다. 처리 전에 작성됐다는 사실의 근거로 정정 전 파일 mtime(03:00:35Z)과 이후 단계의 기록 시각을 적고, `provenance_corrections`를 추가했다.
- 경로별 분석 길이와 분해능을 `summary.csv` 값으로 고쳤다(4.1, 4.2, 3.2, 5-6).
- 국소 SNR 3은 보고용 진단값이며 채택 기준이 아니라고 명시했다(2, 5-3, 래퍼 docstring).
- 케이스 교체를 사용자가 확인한 사실로 적었다. 미확인인 것은 재질과 안테나 배치·간격이다(5-2).
- 실행한 명령과 새로 재현하는 명령을 구분했다(1).
- wrap 간격 재확인: (2³² − 4294962111) + 4815 = 10000µs이며, 어댑터 기록과 같아 수정하지 않았다.
