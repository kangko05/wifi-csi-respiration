# RX 진폭 분석 실시간화 — 원인 분석과 구현 지시 (2026-09-21)

> **2026-09-22 최신 결정: 온보드 작업 보류, Python 복귀.**
> 현재 인수인계는 [온보드 보류 및 Python 복귀 기록](onboard-pause-python-handoff-20260922.md)을 따른다.
> 아래 내용은 이전 계획·진행 이력이며 추가 온보드 작업을 자동 재개하지 않는다.

> 대상: 구현 담당 에이전트(Codex 등). 루트 `AGENTS.md`와 `python/AGENTS.md`를 먼저 읽는다.
> 선행 기록: [RX 진폭 연결](rx-amplitude-20260921.md), [최적화 실험](rx-amplitude-optimization-20260921.md).

## 1. 목표와 현황

- 목표: 60초 윈도우 진폭 분석 결과를 **1~3초마다** 갱신한다. 연속 수신 중 큐 드롭이 없어야 한다.
- 현황: 일반 모드(float FFT/FIR, oversampling 4)에서 1회 분석에 **38.3초**가 걸린다. 목표보다 13~38배 느리다.
  근거: `outputs/amplitude-optimization-20260921/normal-uart.log`.
- 분석하는 동안 큐가 가득 찬다(`queued=200`). 현재 펌웨어는 부팅당 1회만 분석하는 시험 파이프라인이다(`csi-rx/main/csi_process.c`).

실측 단계별 시간(5,991 레코드 × 117 bin, 240 MHz, PSRAM Quad 40 MHz, `-O2`):

| 단계 | 시간 | 비중 |
|---|---:|---:|
| eligibility | 2.54 s | 7% |
| interpolate | 9.96 s | 26% |
| FIR | 6.77 s | 18% |
| detrend | 0.79 s | 2% |
| PSD (4096점 FFT) | 8.85 s | 23% |
| ACF (2048점 순·역 FFT) | 7.81 s | 20% |
| score + yield + 기타 | 1.5 s | 4% |

## 2. 원인 분석

### 2.1 FPU가 없음 (가장 큰 원인)
- ESP32-C5 툴체인은 `-march=rv32imac`로, F 확장이 없다(`$IDF_PATH/tools/cmake/toolchain-clang-esp32c5.cmake`). 코어는 싱글이다.
- 따라서 float와 double 연산이 모두 소프트웨어 에뮬레이션이다. 실측에서 역산하면 다음과 같다.
  - FIR 13.6M MAC(117×580×201): double은 MAC당 약 220 cycle, float는 약 118 cycle.
  - FFT butterfly: float 기준 약 720 cycle.
- 그래서 double을 float로 바꿔도 33%만 줄었다. **정수(고정소수점) 연산으로 바꾸는 것이 핵심이다.**
- 부수 비용:
  - `csi_abs`의 `sqrtf`.
  - raw 경로의 `amp_hann`이 bin마다 double `cos()`를 580회 다시 계산한다(`src/amplitude.c`의 `csi_amp_process_raw` PSD 단계).
  - detrend 중앙값을 구하는 `qsort`(float 비교).
  - `amp_psd`와 `amp_measure_psd`의 double 루프(bin마다 n_fft/2개).

### 2.2 PSRAM 캐시 미스
- 윈도우는 `csi_record_t`(약 536 B) 6,600개, 약 3.5 MB이며 PSRAM에 있다. L1 캐시는 32 KB, 라인은 32 B다.
- `csi_amp_process_raw`는 bin마다 레코드 전체를 다시 순회한다(eligibility, interpolate). 레코드를 하나 읽을 때마다 새 캐시라인이 필요하다.
- eligibility: 미스 약 70만 회 / 2.54 s ≈ 미스당 3.6 µs. PSRAM 40 MHz에서 캐시라인 하나를 채우는 비용과 일치한다.
- `csi_window_at`은 접근할 때마다 `% capacity` 나눗셈을 한다.
- 16 KB를 넘는 임시 배열은 `CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL=16384` 때문에 PSRAM에 할당된다.

### 2.3 매번 60초 전체를 재계산
- 1~3초 간격으로 갱신하면 보간과 FIR 결과의 95% 이상이 이전과 같다.
- 기존 `csi_amp_stream_*`는 push 평균 5.7 ms, 최대 49.7 ms로, 100 Hz 수신 간격인 10 ms를 넘는다. 원인은 PSRAM 링 버퍼의 stride 접근, 모듈러, soft double FIR이다.
- 이 API는 윈도우 이동(sliding)을 지원하지 않는다.

### 2.4 과도한 스펙트럼 크기
- 580 샘플짜리 실신호에 4096점 복소 FFT를 쓴다(허수부가 0). ACF는 별도 2048점 FFT를 2회 돈다.
- 트위들 테이블이 PSRAM의 double이며, float 경로는 매번 double을 float로 변환한다.

### 2.5 태스크 구조
- 수집과 분석이 한 태스크(`csi_process_task`)에서 돈다. 분석 중에는 큐가 비워지지 않는다.

## 3. 예상 효과 (추정, 보드 실측으로 확정 필요)

| 항목 | 현재 | 적용 후 예상 | 근거 |
|---|---:|---:|---|
| eligibility+보간+FIR | 19.3 s | 분석 시간에서 0 | 수신 시 정수 처리, CPU 약 1% |
| PSD+ACF | 16.7 s | 0.45~0.9 s | butterfly 약 430만 개 × Q15 25~40 cycle |
| detrend/score/기타 | 2.3 s | 0.05~0.15 s | 정수화, Parseval |
| 축소 버퍼 읽기 | — | 0.02~0.25 s | bin 연속 배치면 약 15 ms |
| **합계** | **38.3 s** | **약 0.6~1.3 s** | 중앙값 약 1 s |

- 낙관 시나리오 약 0.5 s: Q15로 충분하고 oversampling을 줄이는 경우.
- 비관 시나리오 2~3 s: Q31이 필요하거나 float 경로가 남는 경우.
- A 단계만 적용하면 약 19 s로 목표에 못 미친다. **목표 달성은 B(고정소수점)에 달려 있다.**
- 분석에 약 1 s가 걸리면 1 s 간격 갱신은 CPU가 거의 포화된다. 기본 갱신 간격은 2~3 s로 두고, 1 s가 필요하면 bin 라운드로빈을 쓴다.

## 4. 구현 지시

권장 순서: **B0 → A → B → C**. 단계마다 호스트 테스트를 통과시키고 결과를 이 문서의 하단 기록에 남긴다.

### B0. 고정소수점 FFT 선행 검증 (불확실성 최소화)
- `src/csi_utils.c`와 `include/csi_resp/csi_utils.h`에 정수 radix-2 FFT를 추가한다(Q15 int16 입출력, 단계별 스케일링, int16 트위들 테이블). 기존 `csi_fft*`와 `csi_fft_float_planned`는 수정하지 않는다.
- 호스트에서 NumPy와 비교해 오차를 기록한다. 녹화 21개의 처음 60초로 채택 여부, 최고 점수 bin, PSD/ACF bpm 차이를 `tests/benchmark_amplitude_experiments.py` 방식으로 비교한다.
- Q15가 부족하면 Q31(`mulh`)로 바꾸고 그 사실을 기록한다.
- 벤치마크 모드(`CONFIG_CSI_RX_AMP_BENCHMARK`)에 정수 FFT 변형을 추가해 보드에서 PSD/ACF 시간을 측정한다. 플래시는 하드웨어 준비를 확인한 뒤에만 한다.

### A. 증분(sliding) 전처리
- 레코드가 도착할 때 다음을 처리한다.
  1. bin별 정수 진폭: `re²+im²`는 정수이고, 정수 sqrt 또는 LUT를 쓴다.
  2. 100 Hz 격자 선형 보간.
  3. `down`번째 격자마다 FIR 출력(폴리페이즈 디시메이션)을 계산해 10 Hz 축소 링 버퍼에 넣는다.
- FIR 입력 링은 내부 RAM에 두고, 모듈러 없이 이중 기록 선형 배치로 만든다. 계수는 Q15, 누산은 int32/int64다.
- 축소 버퍼는 **bin별로 연속된 배치**로 둔다(bin × 시간, int16 약 136 KB, PSRAM).
- eligibility(마스크 개수, 최대 gap, 공통 시작·끝)는 실행 카운터로 유지하고, 윈도우가 이동하면 오래된 행의 기여를 제거한다.
- 재사용: `csi_amp_stream_*`의 보간, 중복 타임스탬프, 32비트 wrap 처리, `firwin_coeff`(`src/filters.c`), `csi_amp_raw_source_t.prepared` 입력 경로.
- 합격 기준:
  - 호스트에서 증분 결과와 일괄 `csi_amp_process_raw` 전처리 결과의 차이를 문서화한다. 정수화 때문에 byte 동일일 필요는 없고, 허용 오차를 명시한다.
  - 윈도우 이동, gap, 중복, wrap, `first_word_invalid`를 테스트한다.
  - 보드에서 per-record 최대 처리 시간이 10 ms보다 훨씬 작아야 한다(목표 < 1 ms).

### B. 스펙트럼 분석 고정소수점화
- B0의 정수 FFT를 사용한다. 실신호 2개를 복소 1회 FFT로 묶는다(Hann 적용 신호와 비적용 신호 → PSD와 ACF 스펙트럼). ACF 역FFT도 2개 bin씩 묶는다.
- Hann 테이블은 1회만 계산한다. 중앙값은 선택 알고리즘으로 구한다. PSD total은 Parseval로 계산한다. score와 peak 탐색은 정수 또는 최소한의 float만 쓴다.
- 작업 버퍼와 트위들은 `MALLOC_CAP_INTERNAL`로 명시 할당한다.
- 기존 double 경로(`csi_amp_process`, 옵션 없는 raw API)는 **호스트 기준값으로 그대로 유지한다.**
- oversampling 4는 유지한다. 바꾸려면 별도 실험으로 기록한다.

### C. 스케줄링과 설정
- 태스크를 분리한다. 수집·전처리 태스크(높은 우선순위, 짧은 작업)와 분석 태스크(낮은 우선순위)로 나누고, 축소 버퍼를 더블 버퍼나 스냅샷으로 넘긴다.
- 부팅당 1회 분석을 주기 분석으로 바꾼다. 갱신 간격은 Kconfig 옵션이며 기본 2~3 s다. 필요하면 bin 라운드로빈 옵션을 둔다.
- `CONFIG_SPIRAM_SPEED` 40 → 80 MHz 전환은 별도 실험으로 한 요인만 바꿔 측정한다.
- 핫 루프는 `IRAM_ATTR` 배치를 검토한다.

## 5. 제약 (반드시 지킬 것)

- `data/` 원본 캡처를 덮어쓰거나 수정하지 않는다. `python/vendor/`를 수정하지 않는다.
- 수동 호흡 횟수는 추정 이후 평가에만 사용한다. bin, 설정, 후보 선택에 쓰지 않는다.
- 합성 테스트는 계산 계약만 검증한다. 호흡 정확도 주장에 쓰지 않는다.
- 포트 열기와 플래시는 하드웨어 준비를 확인한 뒤에만 한다.
- 커밋과 푸시는 명시 요청이 있을 때만 한다. 현재 `.git/info/exclude`가 `docs/`와 `tests/`를 제외하므로 파일은 디스크에만 존재한다.
- 산출물은 `outputs/<작업명>-<날짜>/`에 새로 만든다. 기존 산출물은 덮어쓰지 않는다.
- 기존 동작 변경은 Kconfig 옵션으로 선택 가능하게 두고, 기본값 변경은 실측 후 결정한다.

## 6. 검증 명령

```bash
cmake -S . -B build -G Ninja && cmake --build build
python/.venv/bin/python -m unittest discover -s tests -v
(cd python && .venv/bin/python -m unittest discover -s tests -v)   # 기존 14개 입력검증 실패는 알려진 문제
# 펌웨어: csi-rx에서 idf.py build (ESP-IDF v6.0.3 환경)
```

보드에서 확인할 항목: `amp timing` 단계별 시간, per-record 최대 처리 시간, 연속 수신 중 `queued`와 `queue_drops` 추이, 실제 갱신 주기.

## 7. 보고 형식

변경 파일, 단계별 호스트·보드 수치(이 문서 3절 표와 비교), 기준 경로 대비 차이(채택 여부, 최고 bin, bpm), 실행한 테스트와 실패 여부, 남은 한계.

## 8. 진행 기록

### 2026-09-22: B0 완료

사용자 승인 후 Q15 block-scaling FFT를 별도 경로로 구현하고 RX에서 측정했다.
[상세 결과](rx-amplitude-q15-b0-20260922.md).

- FFT 커널 약 5.5~6배 개선. 4096점 순방향 62.715 → 11.380 ms.
- 실제 같은 윈도우의 PSD+ACF 16.358 → 8.050초.
- 전체 분석 37.223 → 29.174초. 보간·FIR·유효성 검사는 그대로다.
- 녹화 21개에서 최종 채택 여부와 후보 칸은 동일했으나 일부 비선택 칸의
  피크 차이는 컸다. 기존 결과와 완전 동등하거나 실제 정확도가 입증된 것은 아니다.
- 루트 테스트 30개 통과, 정수 FFT UBSan 검사 통과.
- 위 3절의 0.6~1.3초/비관 2~3초 예상은 여전히 미검증이다.
  커널 전체 시간을 butterfly 수로 나눈 값은 약 111 cycles였으며,
  25~40 cycles 추정을 이번 구현에서 확인하지 못했다.
- 일반 RX에는 Q15를 기본 적용하지 않는다. A/B/C 구조 변경과 Q31은 미구현.
- 최신 요구사항은 분석과 외부 결과 전송을 합쳐 최대 3초다.

### 2026-09-22: B1 정수 전처리 완료

[상세 실측](rx-amplitude-fixed-preprocess-20260922.md). 외부 서버 처리 논의 후에도
사용자가 온보드 실험을 계속 승인했다. Q12 진폭, 정수 보간, Q23 FIR을 추가했다.
동일 60초 입력: float 38.057초, Q15 FFT만 29.259초, 정수 전처리만 26.866초,
결합 18.864초(반복 18.854초). 21개 녹화에서 최종 채택/선택 칸 동일,
루트 36개 테스트 및 UBSan 통과. 실제 호흡 정확도나 연속 수신 3초 주기는 미검증이다.
A의 증분 처리, B의 FFT 묶기, C의 태스크 분리/주기화는 아직 구현하지 않았다.
