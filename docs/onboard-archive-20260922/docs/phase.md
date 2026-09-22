# C 위상 경로와 기존 데이터 비교

2026-09-18. 범위는 위상 C 구현과 기존 21개 세션 비교까지다. 신뢰도 임계값은 변경하지 않았다.

## 호출과 실행

```c
#include "csi_resp/phase.h"
csi_phase_result_t result;
int status = csi_phase_process(frames, n_frames, &result);
/* status == 0: 계산 성공. result.accepted는 별도의 주기 후보 판정.
 * result.bpm은 보류 시에도 진단값이며 호흡 검출 확정을 뜻하지 않는다. */
```

저장소 루트에서 실행한다. Python은 원본 base64와 메타데이터를 C 실행 파일에 전달한다.

```powershell
python/.venv/Scripts/python.exe python/scripts/run_c_amplitude.py --method phase
# 특정 세션만:
python/.venv/Scripts/python.exe python/scripts/run_c_amplitude.py --method phase --session 20260916T060200_034260_9dd9fd80
```

이름은 기존 실행 스크립트를 유지했다. `--method` 생략 시 기존 진폭 경로다.
C 실행 인자는 `build/csi_resp_main.exe --stdin --phase`이고 입력 형식은 기존 `--stdin`과 같다.
`outputs/c-phase/<실행 시각>/`에 세션 JSON, summary.csv, 소스/실행 파일 해시와 설정 manifest를 만든다.

## 처리 순서

1. 117개 원본 열 중 57~59번 null을 제외한 114개를 사용한다. 첫 word 무효 마스크를 포함해 모든 사용 열이 유효한 패킷만 유지한다. 원래 시각과 uint32 wrap을 보존하고, 가장자리 포함 최대 공백 100ms 초과·길이 20초 미만은 거부한다.
2. 패킷별 RMS로 복소 CSI의 크기를 정규화한다. 인접 열 위상으로 초기 시간 오차를 구하고, 전체 윈도우의 정적 평균을 기준으로 가중 최소제곱(LoS WLS) 위상 보정을 수행한다.
3. 유지한 타임스탬프 간격의 중앙값으로 fs를 계산한다. 실수·허수를 각각 선형 보간한 뒤 열별 시간 평균을 뺀다.
4. 복소 CSI를 0.05~1Hz, 0.005Hz 간격으로 평가하여 열별 coherent power를 합한다. 주파수 방향 61칸 중앙값으로 나누고 가장 강한 주파수를 후보로 쓴다.
5. 별도로 양·음 주파수 대역 에너지가 큰 5개 열을 골라 최대 분산 방향으로 실수 투영한다. 부호를 맞춰 합산하고 Hampel(11칸, 3 MAD) 및 4차 Butterworth 양방향 필터를 적용한다. 후보의 0.7~1.5배 대역으로 한 번 더 필터한 파형에서 피크를 센다.
6. sharpness > 1.55, 두 추정치 차이 < 3 bpm이면 채택한다. 대역 끝의 주파수 후보는 기존 Python처럼 피크 계수 값으로 대체할 수 있다. ACF는 이 경로에 없다.

`phase.c`가 이 흐름을 조립하고, Butterworth와 Hampel 필터는 `filters.c`에 있다. 임의 주파수 계산은 double 정밀도 Bluestein 변환을 이용하며 내부 FFT는 `csi_utils.c`의 공통 계산부를 호출한다. 진폭의 float 배열과 위상의 double 복소 배열은 저장 형식만 구분하고 FFT 알고리즘을 공유한다. 0.005Hz는 탐색 격자 간격이며 실제 관측 길이로 정해지는 주파수 분해능과 다르다.

기본 설정에서 117개 열을 -58..58, 간격 312.5kHz로 해석하는 것은 **기존 HT40 배치 가정**이다. null 평균 크기가 사용 열 평균의 5% 이하인지 검사하지만 물리 배치를 증명하지 않는다. 기존 PC 실행 명령은 이 기본 프로필을 사용한다.

## 배치와 탐색 범위 설정 (2026-09-19)

`csi_phase_process_with_config(frames, count, &config, &result)`로 배치와 탐색 설정을 전달할 수 있다. 기존 `csi_phase_process()`는 기본 설정을 쓰는 호환 진입점이다.

```c
csi_phase_config_t config = csi_phase_default_config();
config.min_hz = 0.1;
config.max_hz = 0.6;
config.frequency_step_hz = 0.01;

csi_phase_result_t result;
int status = csi_phase_process_with_config(frames, count, &config, &result);
```

다른 신호 배치는 호출부에서 `config.n_bins`와 `config.bins[k]`를 채운다. 각 원본 열의 역할은 USED(분석), NULL(null 크기 검사), SKIP(제외)이며 USED 열마다 실제 주파수 오프셋 `frequency_hz`를 지정한다. USED 열은 주파수 오름차순이어야 한다. 입력 열 수와 배치 길이가 다르면 오류로 반환한다. `subcarrier_spacing_hz`는 초기 시간 오차 추정에 사용한다. 패킷 길이만으로 물리 배치를 추측하지 않는다.

`n_columns`는 USED 열 개수, `n_frequencies`는 탐색 범위/간격에서 계산한다. 끝점이 간격의 배수가 아니면 마지막 격자는 상한 아래에서 끝난다. `top_columns`도 설정이고, 결과에서 `n_selected`개만 읽는다. `CSI_MAX_SUBCARRIERS`는 프로토콜상 배열 용량이며 실제 분석 열 개수가 아니다. 주파수 격자 버퍼는 계산한 길이로 할당한다. 117열·114사용열·191주파수에 의존하는 계산 코드는 제거했다.

기본 설정의 공백/길이/신뢰도 기준은 유지했다. 13열 중 10열 사용, 3개/7개 선택, 변경된 주파수 대역·격자, null 없는 배치와 잘못된 설정을 테스트한다.

## 비교 결과

최종 실행: `outputs/c-phase/20260918T073801_465366Z/`.
`comparison.csv`에 세션별 수동 기준, C 위상·기존 Python 위상·C 진폭 진단값과 채택 여부가 있다.

- 100cm 목표 18 / 15 / 16.5 bpm에 대해 위상 16.2 / 16.8 / 16.8 bpm. 3개 모두 채택, ±1.1 안 1/3, ±2.2 안 3/3.
- 전체 21개에서 C와 보존된 Python 실행의 주파수·피크 계수·채택·선택 열이 일치한다. sharpness 최대 차이 약 6e-12.
- 재실 18개 중 위상 채택 17개. 채택 MAE 0.676 bpm, ±1.1 안 12/17, ±2.2 안 17/17.
- 보류까지 포함한 전체 진단 MAE: 위상 2.706 bpm, 진폭 PSD 2.880 bpm. 위상은 500cm 한 세션에서 52.2 bpm을 제시하고 보류했다. 진폭은 18개 모두 보류하므로 진폭 수치는 채택 결과가 아니다.
- 완전 빈 방 2개는 위상에서 둘 다 잘못 채택한다. 중단된 빈 방 1개는 보류한다. 진폭은 빈 방 3개 모두 보류한다.

수동 횟수는 추정 후 평가에만 사용했다. 명목 120초 전체 횟수이며 동기화는 미확인이다. 두 경로의 판정 규칙과 필터 가장자리 처리도 다르므로 채택률 차이를 검출 성능 우위로 단정할 수 없다.

```powershell
python/.venv/Scripts/python.exe python/scripts/compare_c_phase.py outputs/c-phase/20260918T073801_465366Z
```

비교는 보존된 `python/outputs/legacy_run_20260917/report/summary.csv`와 C 진폭 실행 `outputs/c-amplitude/20260918T064726_104124Z`를 사용한다. 레거시 어댑터가 기록한 원본 SHA256과 이번 입력을 대조한다. 덮어쓰지 않으며 한 실행 폴더에 비교 파일은 한 번 만든다.

## 검증과 남은 범위

`python/.venv/Scripts/python.exe -m unittest discover -s tests -v`: 기존 진폭·입력 테스트와 위상 테스트. 보존된 Python의 LoS 보정, 전체 합성 파이프라인, NumPy DFT, SciPy Butterworth/피크 계수, 평탄 신호·마스크·공백·wrap을 대조한다.

`python/` 기존 전체 테스트 85개도 실행했다. 이번 변경과 무관한 `test_preprocessing_to_csidata.py`의 기존 실패 14건(길이·비유한 gain 등 입력 검증)이 남아 있다.

현재는 전체 120초 윈도우를 PC 메모리에 보관하는 검증용 C 구현이다. 프레임과 복소 중간 배열은 수십 MB를 사용하므로 ESP32-C5에 그대로 올릴 수 있는 메모리 구조가 아니다. 스트리밍/메모리 축소, CIR, 보드 통합, 신뢰도 개선은 이번 범위에 포함하지 않았다. 필터는 SciPy 기본 odd padding 27샘플을 유지하며 가장자리를 잘라내지 않는다.

기준 소스는 수정하지 않은 `python/vendor/wifi-csi-proto/src/csi_pipeline/phase_cir.py` 및 `_vendor/wifi_csi_backup/{preprocess,features,estimate}`다. 원본 버전/해시는 해당 vendor의 `VENDOR.json`에 기록돼 있다.
